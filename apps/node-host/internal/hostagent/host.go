package hostagent

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"math/rand"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/yym68686/argus/apps/node-host/internal/nodehost"
	"github.com/yym68686/argus/apps/node-host/internal/proto"
	"github.com/yym68686/argus/apps/node-host/internal/util"
	"github.com/yym68686/argus/apps/node-host/internal/version"
	"github.com/yym68686/argus/apps/node-host/internal/ws"
)

type connectFrame struct {
	Type        string   `json:"type"`
	HostID      string   `json:"hostId"`
	DisplayName string   `json:"displayName"`
	Platform    string   `json:"platform"`
	Version     string   `json:"version"`
	Caps        []string `json:"caps"`
	Commands    []string `json:"commands"`
}

type eventFrame struct {
	Type    string      `json:"type"`
	Event   string      `json:"event"`
	Payload interface{} `json:"payload,omitempty"`
}

type rawEventFrame struct {
	Type    string          `json:"type"`
	Event   string          `json:"event"`
	Payload json.RawMessage `json:"payload"`
}

type runtimeOpenPayload struct {
	SessionID        string `json:"sessionId"`
	WorkspacePath    string `json:"workspacePath"`
	WorkspaceMode    string `json:"workspaceMode"`
	CodexProfileMode string `json:"codexProfileMode"`
	Model            string `json:"model"`
	PublicBaseURL    string `json:"publicBaseUrl"`
	McpToken         string `json:"mcpToken"`
	OpenAIToken      string `json:"openaiToken"`
}

type runtimeFramePayload struct {
	SessionID string `json:"sessionId"`
	Data      string `json:"data"`
}

type runtimeControlPayload struct {
	SessionID string `json:"sessionId"`
	Reason    string `json:"reason"`
}

type runtimeOpenedPayload struct {
	SessionID     string `json:"sessionId"`
	RuntimeID     string `json:"runtimeId"`
	RuntimeName   string `json:"runtimeName"`
	WorkspacePath string `json:"workspacePath"`
	CodexHomePath string `json:"codexHomePath"`
	Pid           int    `json:"pid"`
}

type runtimeOpenFailedPayload struct {
	SessionID string `json:"sessionId"`
	Message   string `json:"message"`
}

type runtimeExitPayload struct {
	SessionID string `json:"sessionId"`
	ExitCode  int    `json:"exitCode"`
	Message   string `json:"message,omitempty"`
}

type runtimeProc struct {
	sessionID     string
	workspacePath string
	codexHomePath string
	cmd           *exec.Cmd
	stdin         io.WriteCloser
	cancel        context.CancelFunc
	done          chan struct{}
}

type Host struct {
	cfg     *Config
	invoker nodehost.Invoker

	connMu  sync.RWMutex
	conn    *ws.Conn
	connGen uint64

	pendingMu sync.Mutex
	pending   []eventFrame

	runtimesMu sync.Mutex
	runtimes   map[string]*runtimeProc
}

func New(cfg *Config, invoker nodehost.Invoker) *Host {
	return &Host{
		cfg:      cfg,
		invoker:  invoker,
		runtimes: map[string]*runtimeProc{},
	}
}

func (h *Host) LogLine(level string, message string) {
	fmt.Fprintf(os.Stderr, "%s [%s] %s\n", time.Now().UTC().Format(time.RFC3339Nano), level, message)
}

func (h *Host) SendEvent(event string, payload interface{}) {
	frame := eventFrame{Type: "event", Event: event, Payload: payload}
	h.connMu.RLock()
	c := h.conn
	h.connMu.RUnlock()
	if c == nil {
		h.queueEvent(frame)
		return
	}
	if err := h.sendJSONBestEffort(c, frame); err != nil {
		h.queueEvent(frame)
	}
}

func (h *Host) queueEvent(frame eventFrame) {
	h.pendingMu.Lock()
	defer h.pendingMu.Unlock()
	h.pending = append(h.pending, frame)
	if len(h.pending) > 2000 {
		h.pending = h.pending[len(h.pending)-2000:]
	}
}

func (h *Host) flushPending(c *ws.Conn) {
	h.pendingMu.Lock()
	defer h.pendingMu.Unlock()
	if len(h.pending) == 0 {
		return
	}
	out := h.pending
	h.pending = nil
	for i := 0; i < len(out); i++ {
		if err := h.sendJSONBestEffort(c, out[i]); err != nil {
			h.pending = append(out[i:], h.pending...)
			return
		}
	}
}

func (h *Host) sendJSONBestEffort(c *ws.Conn, v interface{}) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	return c.WriteText(b, h.cfg.WriteTimeout)
}

func hostCommands() []string {
	return []string{
		"system.run",
		"system.which",
		"process.list",
		"process.get",
		"process.logs",
		"process.write",
		"process.send_keys",
		"process.submit",
		"process.paste",
		"process.kill",
	}
}

func (h *Host) connectFrame() connectFrame {
	platform := runtime.GOOS
	if platform == "windows" {
		platform = "win32"
	}
	return connectFrame{
		Type:        "connect",
		HostID:      h.cfg.HostID,
		DisplayName: h.cfg.DisplayName,
		Platform:    platform,
		Version:     version.Current(),
		Caps:        []string{"runtime", "codex", "fs", "mcp", "system"},
		Commands:    hostCommands(),
	}
}

func (h *Host) Run(ctx context.Context) error {
	h.LogLine("INFO", fmt.Sprintf("Argus host-agent starting (version=%s)", version.Current()))
	h.LogLine("INFO", fmt.Sprintf("Connecting hostId=%s displayName=%s url=%s",
		util.SafeJSON(h.cfg.HostID), util.SafeJSON(h.cfg.DisplayName), util.SafeJSON(util.RedactWSURL(h.cfg.URL)),
	))
	h.LogLine("INFO", fmt.Sprintf("Workspace base: %s", h.cfg.WorkspaceBasePath))
	h.LogLine("INFO", fmt.Sprintf("Codex home base: %s", h.cfg.CodexHomeBasePath))
	h.LogLine("INFO", fmt.Sprintf("State dir: %s", h.cfg.StateDir))
	h.LogLine("INFO", fmt.Sprintf("App-server cmd: %s", util.SafeJSON(h.cfg.AppServerCmd)))

	rng := rand.New(rand.NewSource(time.Now().UnixNano()))
	everConnected := false
	consecutiveFailures := 0
	connectAttempt := 0

	for {
		select {
		case <-ctx.Done():
			h.stopAllRuntimes()
			return ctx.Err()
		default:
		}

		connectAttempt++
		c, _, err := ws.Dial(h.cfg.URL, ws.DialOptions{
			HandshakeTimeout: h.cfg.WSHandshakeTimeout,
			ConnectTimeout:   h.cfg.WSConnectTimeout,
		})
		if err != nil {
			consecutiveFailures++
			h.LogLine("ERROR", fmt.Sprintf("Failed to connect to host-agent ws (attempt=%d): %s", connectAttempt, err.Error()))
			delayMs := util.ComputeReconnectDelayMs(
				int(h.cfg.ReconnectDelayBase/time.Millisecond),
				int(h.cfg.ReconnectDelayMax/time.Millisecond),
				consecutiveFailures,
				h.cfg.ReconnectJitterPct,
				rng.Float64,
			)
			select {
			case <-time.After(time.Duration(delayMs) * time.Millisecond):
				continue
			case <-ctx.Done():
				h.stopAllRuntimes()
				return ctx.Err()
			}
		}

		h.connMu.Lock()
		h.connGen++
		gen := h.connGen
		h.conn = c
		h.connMu.Unlock()

		if err := h.sendJSONBestEffort(c, h.connectFrame()); err != nil {
			_ = c.Close()
			h.setDisconnected(gen)
			continue
		}
		h.flushPending(c)

		if everConnected {
			h.LogLine("INFO", "Reconnected to host-agent ws")
		} else {
			h.LogLine("INFO", "Connected to host-agent ws")
		}
		everConnected = true
		consecutiveFailures = 0

		connDone := make(chan struct{})
		heartbeatStop := make(chan struct{})
		go func() {
			defer close(heartbeatStop)
			ticker := time.NewTicker(h.cfg.HeartbeatInterval)
			defer ticker.Stop()
			for {
				select {
				case <-ticker.C:
					now := util.NowUnixMs()
					_ = h.sendJSONBestEffort(c, eventFrame{Type: "event", Event: "host-agent.heartbeat", Payload: map[string]interface{}{"hostId": h.cfg.HostID, "t": now}})
					_ = h.sendJSONBestEffort(c, eventFrame{Type: "event", Event: "node.heartbeat", Payload: map[string]interface{}{"nodeId": h.cfg.HostID, "t": now}})
					_ = h.sendJSONBestEffort(c, eventFrame{Type: "event", Event: "runtime.heartbeat", Payload: map[string]interface{}{"hostId": h.cfg.HostID, "t": now}})
				case <-connDone:
					return
				case <-ctx.Done():
					return
				}
			}
		}()

		pingStop := make(chan struct{})
		if h.cfg.PingInterval > 0 {
			go func() {
				defer close(pingStop)
				ticker := time.NewTicker(h.cfg.PingInterval)
				defer ticker.Stop()
				for {
					select {
					case <-ticker.C:
						if err := c.WritePing(nil, h.cfg.WriteTimeout); err != nil {
							_ = c.Close()
							return
						}
						if ok := c.WaitPong(h.cfg.PongTimeout); !ok {
							h.LogLine("WARN", fmt.Sprintf("Ping timeout after %dms; terminating connection", int(h.cfg.PongTimeout/time.Millisecond)))
							_ = c.Close()
							return
						}
					case <-connDone:
						return
					case <-ctx.Done():
						return
					}
				}
			}()
		} else {
			close(pingStop)
		}

		for {
			op, msg, rerr := c.ReadMessage(0)
			if rerr != nil {
				break
			}
			if op != 0x1 {
				continue
			}
			var f rawEventFrame
			if err := json.Unmarshal(msg, &f); err != nil {
				continue
			}
			if f.Type != "event" {
				continue
			}
			switch f.Event {
			case "node.invoke.request":
				var payload proto.InvokeRequestPayload
				if err := json.Unmarshal(f.Payload, &payload); err != nil {
					continue
				}
				if strings.TrimSpace(payload.ID) == "" {
					continue
				}
				go h.handleInvoke(ctx, payload)
			case "runtime.open":
				var payload runtimeOpenPayload
				if err := json.Unmarshal(f.Payload, &payload); err != nil {
					continue
				}
				go h.handleOpen(payload)
			case "runtime.frame":
				var payload runtimeFramePayload
				if err := json.Unmarshal(f.Payload, &payload); err != nil {
					continue
				}
				go h.handleFrame(payload)
			case "runtime.close", "runtime.restart":
				var payload runtimeControlPayload
				if err := json.Unmarshal(f.Payload, &payload); err != nil {
					continue
				}
				go h.handleClose(payload)
			}
		}

		close(connDone)
		<-heartbeatStop
		<-pingStop
		h.setDisconnected(gen)

		delayMs := util.ComputeReconnectDelayMs(
			int(h.cfg.ReconnectDelayBase/time.Millisecond),
			int(h.cfg.ReconnectDelayMax/time.Millisecond),
			1,
			h.cfg.ReconnectJitterPct,
			rng.Float64,
		)
		select {
		case <-time.After(time.Duration(delayMs) * time.Millisecond):
		case <-ctx.Done():
			h.stopAllRuntimes()
			return ctx.Err()
		}
	}
}

func (h *Host) setDisconnected(gen uint64) {
	h.connMu.Lock()
	defer h.connMu.Unlock()
	if h.connGen == gen {
		h.conn = nil
	}
}

func (h *Host) parseParams(payload proto.InvokeRequestPayload) interface{} {
	if payload.ParamsJSON != nil && strings.TrimSpace(*payload.ParamsJSON) != "" {
		var v interface{}
		if err := json.Unmarshal([]byte(*payload.ParamsJSON), &v); err == nil {
			return v
		}
		return nil
	}
	return payload.Params
}

func (h *Host) handleInvoke(ctx context.Context, payload proto.InvokeRequestPayload) {
	reqID := strings.TrimSpace(payload.ID)
	cmd := strings.TrimSpace(payload.Command)
	params := h.parseParams(payload)

	if h.cfg.AuditEnabled {
		h.LogLine("AUDIT", fmt.Sprintf("invoke id=%s command=%s", reqID, util.SafeJSON(cmd)))
	}

	start := time.Now()
	outcome := nodehost.InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "UNSUPPORTED", Message: "unsupported command"}}
	if h.invoker != nil {
		outcome = h.invoker.Invoke(ctx, nodehost.InvokeRequest{
			ID:        reqID,
			Command:   cmd,
			TimeoutMs: payload.TimeoutMs,
			Params:    params,
		})
	}
	elapsed := time.Since(start)
	if h.cfg.AuditEnabled {
		h.LogLine("AUDIT", fmt.Sprintf("result id=%s command=%s ok=%t ms=%d", reqID, util.SafeJSON(cmd), outcome.OK, elapsed.Milliseconds()))
	}

	var payloadJSON *string
	if outcome.Payload != nil {
		if b, err := json.Marshal(outcome.Payload); err == nil {
			s := string(b)
			payloadJSON = &s
		}
	}
	h.SendEvent("node.invoke.result", proto.InvokeResultPayload{
		ID:          reqID,
		NodeID:      h.cfg.HostID,
		OK:          outcome.OK,
		Payload:     outcome.Payload,
		PayloadJSON: payloadJSON,
		Error:       outcome.Error,
	})
}

func (h *Host) getRuntime(sessionID string) *runtimeProc {
	h.runtimesMu.Lock()
	defer h.runtimesMu.Unlock()
	return h.runtimes[sessionID]
}

func (h *Host) setRuntime(sessionID string, rp *runtimeProc) {
	h.runtimesMu.Lock()
	defer h.runtimesMu.Unlock()
	h.runtimes[sessionID] = rp
}

func (h *Host) removeRuntime(sessionID string, current *runtimeProc) {
	h.runtimesMu.Lock()
	defer h.runtimesMu.Unlock()
	existing := h.runtimes[sessionID]
	if existing == current {
		delete(h.runtimes, sessionID)
	}
}

func (h *Host) stopAllRuntimes() {
	h.runtimesMu.Lock()
	runtimes := make([]*runtimeProc, 0, len(h.runtimes))
	for _, rp := range h.runtimes {
		runtimes = append(runtimes, rp)
	}
	h.runtimesMu.Unlock()
	for _, rp := range runtimes {
		h.stopRuntime(rp)
	}
}

func (h *Host) stopRuntime(rp *runtimeProc) {
	if rp == nil {
		return
	}
	rp.cancel()
	if rp.stdin != nil {
		_ = rp.stdin.Close()
	}
	go func() {
		select {
		case <-rp.done:
			return
		case <-time.After(5 * time.Second):
			if rp.cmd != nil && rp.cmd.Process != nil {
				_ = rp.cmd.Process.Kill()
			}
		}
	}()
}

func (h *Host) resolveWorkspacePath(sessionID string, raw string) string {
	p := strings.TrimSpace(raw)
	if filepath.IsAbs(p) {
		return p
	}
	return filepath.Join(h.cfg.WorkspaceBasePath, "sess-"+sanitizePathPart(sessionID, "session"))
}

func (h *Host) resolveCodexHomePath(sessionID string) string {
	return filepath.Join(h.cfg.CodexHomeBasePath, "sess-"+sanitizePathPart(sessionID, "session"))
}

func shouldSkipCodexEntry(rel string) bool {
	base := strings.ToLower(filepath.Base(rel))
	if base == "" || base == "." {
		return false
	}
	if strings.HasPrefix(base, "sqlite") {
		return true
	}
	switch base {
	case "logs", "tmp", "cache":
		return true
	default:
		return false
	}
}

func copyFileIfMissing(src string, dst string, mode fs.FileMode) error {
	if _, err := os.Stat(dst); err == nil {
		return nil
	}
	data, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	if mode == 0 {
		mode = 0o644
	}
	return os.WriteFile(dst, data, mode)
}

func cloneCodexHomeIfMissing(src string, dst string) error {
	info, err := os.Stat(src)
	if err != nil || !info.IsDir() {
		return nil
	}
	return filepath.WalkDir(src, func(path string, d fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return nil
		}
		rel, err := filepath.Rel(src, path)
		if err != nil || rel == "." {
			return nil
		}
		if shouldSkipCodexEntry(rel) {
			if d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		target := filepath.Join(dst, rel)
		if d.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		if d.Type()&os.ModeSymlink != 0 {
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return nil
		}
		return copyFileIfMissing(path, target, info.Mode().Perm())
	})
}

func (h *Host) prepareSessionCodexHome(profileMode string, dst string) error {
	if err := os.MkdirAll(dst, 0o755); err != nil {
		return err
	}
	if strings.EqualFold(strings.TrimSpace(profileMode), "inherit_patch") {
		userHome, _ := os.UserHomeDir()
		if strings.TrimSpace(userHome) != "" {
			src := filepath.Join(userHome, ".codex")
			if err := cloneCodexHomeIfMissing(src, dst); err != nil {
				return err
			}
		}
	}
	return os.MkdirAll(filepath.Join(dst, "sqlite"), 0o755)
}

func upsertEnv(env []string, key string, value string) []string {
	prefix := key + "="
	out := make([]string, 0, len(env)+1)
	replaced := false
	for _, item := range env {
		if strings.HasPrefix(item, prefix) {
			if !replaced {
				out = append(out, prefix+value)
				replaced = true
			}
			continue
		}
		out = append(out, item)
	}
	if !replaced {
		out = append(out, prefix+value)
	}
	return out
}

func shellCommandContext(ctx context.Context, command string) *exec.Cmd {
	if runtime.GOOS == "windows" {
		return exec.CommandContext(ctx, "cmd", "/C", command)
	}
	return exec.CommandContext(ctx, "sh", "-lc", command)
}

func (h *Host) sendRuntimeOpened(sessionID string, rp *runtimeProc) {
	runtimeID := sessionID
	pid := 0
	if rp != nil && rp.cmd != nil && rp.cmd.Process != nil {
		pid = rp.cmd.Process.Pid
		runtimeID = fmt.Sprintf("%d", pid)
	}
	h.SendEvent("runtime.opened", runtimeOpenedPayload{
		SessionID:     sessionID,
		RuntimeID:     runtimeID,
		RuntimeName:   "native-" + sessionID,
		WorkspacePath: rp.workspacePath,
		CodexHomePath: rp.codexHomePath,
		Pid:           pid,
	})
}

func (h *Host) handleOpen(payload runtimeOpenPayload) {
	sessionID := strings.TrimSpace(payload.SessionID)
	if sessionID == "" {
		return
	}
	if existing := h.getRuntime(sessionID); existing != nil {
		h.sendRuntimeOpened(sessionID, existing)
		return
	}

	workspacePath := h.resolveWorkspacePath(sessionID, payload.WorkspacePath)
	codexHomePath := h.resolveCodexHomePath(sessionID)
	if err := os.MkdirAll(workspacePath, 0o755); err != nil {
		h.SendEvent("runtime.open.failed", runtimeOpenFailedPayload{SessionID: sessionID, Message: err.Error()})
		return
	}
	if err := h.prepareSessionCodexHome(payload.CodexProfileMode, codexHomePath); err != nil {
		h.SendEvent("runtime.open.failed", runtimeOpenFailedPayload{SessionID: sessionID, Message: err.Error()})
		return
	}

	runCtx, cancel := context.WithCancel(context.Background())
	cmd := shellCommandContext(runCtx, h.cfg.AppServerCmd)
	cmd.Dir = workspacePath
	cmd.Env = append([]string{}, os.Environ()...)
	cmd.Env = upsertEnv(cmd.Env, "CODEX_HOME", codexHomePath)
	cmd.Env = upsertEnv(cmd.Env, "CODEX_SQLITE_HOME", filepath.Join(codexHomePath, "sqlite"))
	if strings.TrimSpace(payload.McpToken) != "" {
		cmd.Env = upsertEnv(cmd.Env, "ARGUS_MCP_TOKEN", payload.McpToken)
	}
	if strings.TrimSpace(payload.OpenAIToken) != "" {
		cmd.Env = upsertEnv(cmd.Env, "ARGUS_OPENAI_TOKEN", payload.OpenAIToken)
	}

	stdin, err := cmd.StdinPipe()
	if err != nil {
		cancel()
		h.SendEvent("runtime.open.failed", runtimeOpenFailedPayload{SessionID: sessionID, Message: err.Error()})
		return
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		cancel()
		_ = stdin.Close()
		h.SendEvent("runtime.open.failed", runtimeOpenFailedPayload{SessionID: sessionID, Message: err.Error()})
		return
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		cancel()
		_ = stdin.Close()
		h.SendEvent("runtime.open.failed", runtimeOpenFailedPayload{SessionID: sessionID, Message: err.Error()})
		return
	}
	if err := cmd.Start(); err != nil {
		cancel()
		_ = stdin.Close()
		h.SendEvent("runtime.open.failed", runtimeOpenFailedPayload{SessionID: sessionID, Message: err.Error()})
		return
	}

	rp := &runtimeProc{
		sessionID:     sessionID,
		workspacePath: workspacePath,
		codexHomePath: codexHomePath,
		cmd:           cmd,
		stdin:         stdin,
		cancel:        cancel,
		done:          make(chan struct{}),
	}
	h.setRuntime(sessionID, rp)
	h.sendRuntimeOpened(sessionID, rp)

	go h.streamRuntimeOutput(rp, stdout)
	go h.logRuntimeStderr(rp, stderr)
	go h.waitRuntimeExit(rp)
}

func (h *Host) streamRuntimeOutput(rp *runtimeProc, reader io.Reader) {
	br := bufio.NewReaderSize(reader, 128*1024)
	for {
		line, err := br.ReadString('\n')
		if len(line) > 0 {
			line = strings.TrimRight(line, "\r\n")
			h.SendEvent("runtime.frame", runtimeFramePayload{SessionID: rp.sessionID, Data: line})
		}
		if err != nil {
			if err != io.EOF {
				h.LogLine("WARN", fmt.Sprintf("runtime stdout read failed for %s: %s", rp.sessionID, err.Error()))
			}
			return
		}
	}
}

func (h *Host) logRuntimeStderr(rp *runtimeProc, reader io.Reader) {
	scanner := bufio.NewScanner(reader)
	buf := make([]byte, 0, 64*1024)
	scanner.Buffer(buf, 8*1024*1024)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		h.LogLine("RUNTIME", fmt.Sprintf("%s: %s", rp.sessionID, line))
	}
}

func (h *Host) waitRuntimeExit(rp *runtimeProc) {
	err := rp.cmd.Wait()
	close(rp.done)
	h.removeRuntime(rp.sessionID, rp)

	exitCode := 0
	message := ""
	if rp.cmd.ProcessState != nil {
		exitCode = rp.cmd.ProcessState.ExitCode()
	}
	if err != nil {
		message = err.Error()
	}
	h.SendEvent("runtime.exit", runtimeExitPayload{
		SessionID: rp.sessionID,
		ExitCode:  exitCode,
		Message:   message,
	})
}

func (h *Host) handleFrame(payload runtimeFramePayload) {
	sessionID := strings.TrimSpace(payload.SessionID)
	if sessionID == "" {
		return
	}
	rp := h.getRuntime(sessionID)
	if rp == nil || rp.stdin == nil {
		return
	}
	data := payload.Data
	if !strings.HasSuffix(data, "\n") {
		data += "\n"
	}
	_, _ = io.WriteString(rp.stdin, data)
}

func (h *Host) handleClose(payload runtimeControlPayload) {
	sessionID := strings.TrimSpace(payload.SessionID)
	if sessionID == "" {
		return
	}
	rp := h.getRuntime(sessionID)
	if rp == nil {
		return
	}
	h.stopRuntime(rp)
}
