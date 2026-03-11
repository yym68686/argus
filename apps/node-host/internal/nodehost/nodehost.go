package nodehost

import (
	"context"
	"encoding/json"
	"fmt"
	"math/rand"
	"os"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/yym68686/argus/apps/node-host/internal/proto"
	"github.com/yym68686/argus/apps/node-host/internal/util"
	"github.com/yym68686/argus/apps/node-host/internal/version"
	"github.com/yym68686/argus/apps/node-host/internal/ws"
)

type Invoker interface {
	Invoke(ctx context.Context, req InvokeRequest) InvokeOutcome
}

type InvokeRequest struct {
	ID        string
	Command   string
	TimeoutMs *int
	Params    interface{}
}

type InvokeOutcome struct {
	OK      bool
	Payload interface{}
	Error   *proto.InvokeError
}

type Host struct {
	cfg     *Config
	invoker Invoker

	connMu  sync.RWMutex
	conn    *ws.Conn
	connGen uint64

	pendingMu sync.Mutex
	pending   []proto.EventFrame
}

func New(cfg *Config, invoker Invoker) *Host {
	return &Host{
		cfg:     cfg,
		invoker: invoker,
	}
}

func (h *Host) LogLine(level string, message string) {
	fmt.Fprintf(os.Stderr, "%s [%s] %s\n", time.Now().UTC().Format(time.RFC3339Nano), level, message)
}

func (h *Host) SendEvent(event string, payload interface{}) {
	frame := proto.EventFrame{Type: "event", Event: event, Payload: payload}
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

func (h *Host) queueEvent(frame proto.EventFrame) {
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

func (h *Host) connectFrame() proto.ConnectFrame {
	caps := []string{"system"}
	commands := []string{
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
	platform := runtime.GOOS
	if platform == "windows" {
		platform = "win32"
	}
	return proto.ConnectFrame{
		Type:        "connect",
		NodeID:      h.cfg.NodeID,
		DisplayName: h.cfg.DisplayName,
		Platform:    platform,
		Version:     version.Current(),
		Caps:        caps,
		Commands:    commands,
	}
}

type rawEventFrame struct {
	Type    string          `json:"type"`
	Event   string          `json:"event"`
	Payload json.RawMessage `json:"payload"`
}

func (h *Host) Run(ctx context.Context) error {
	h.LogLine("INFO", fmt.Sprintf("Argus node-host starting (version=%s)", version.Current()))
	h.LogLine("INFO", fmt.Sprintf("Node state dir: %s", h.cfg.StateDir))
	h.LogLine("INFO", fmt.Sprintf("Connecting nodeId=%s displayName=%s url=%s",
		util.SafeJSON(h.cfg.NodeID), util.SafeJSON(h.cfg.DisplayName), util.SafeJSON(util.RedactWSURL(h.cfg.URL)),
	))
	if h.cfg.AuditEnabled {
		h.LogLine("INFO", fmt.Sprintf("Audit logging enabled (maxBytes=%d stdinPreviewBytes=%d)", h.cfg.AuditMaxBytes, h.cfg.AuditStdinPreviewBytes))
	}
	h.LogLine("INFO", fmt.Sprintf(
		"WS settings: handshakeTimeoutMs=%d connectTimeoutMs=%d reconnectBaseMs=%d reconnectMaxMs=%d pingIntervalMs=%d pongTimeoutMs=%d",
		int(h.cfg.WSHandshakeTimeout/time.Millisecond),
		int(h.cfg.WSConnectTimeout/time.Millisecond),
		int(h.cfg.ReconnectDelayBase/time.Millisecond),
		int(h.cfg.ReconnectDelayMax/time.Millisecond),
		int(h.cfg.PingInterval/time.Millisecond),
		int(h.cfg.PongTimeout/time.Millisecond),
	))

	rng := rand.New(rand.NewSource(time.Now().UnixNano()))

	everConnected := false
	consecutiveConnectFailures := 0
	connectAttempt := 0

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}

		connectAttempt++
		h.LogLine("INFO", fmt.Sprintf("Connecting to nodes/ws (attempt=%d)", connectAttempt))
		c, _, err := ws.Dial(h.cfg.URL, ws.DialOptions{
			HandshakeTimeout: h.cfg.WSHandshakeTimeout,
			ConnectTimeout:   h.cfg.WSConnectTimeout,
		})
		if err != nil {
			consecutiveConnectFailures++
			h.LogLine("ERROR", fmt.Sprintf("Failed to connect to nodes/ws (attempt=%d): %s", connectAttempt, err.Error()))
			delayMs := util.ComputeReconnectDelayMs(
				int(h.cfg.ReconnectDelayBase/time.Millisecond),
				int(h.cfg.ReconnectDelayMax/time.Millisecond),
				consecutiveConnectFailures,
				h.cfg.ReconnectJitterPct,
				rng.Float64,
			)
			h.LogLine("INFO", fmt.Sprintf("Retrying in %dms", delayMs))
			select {
			case <-time.After(time.Duration(delayMs) * time.Millisecond):
				continue
			case <-ctx.Done():
				return ctx.Err()
			}
		}

		h.connMu.Lock()
		h.connGen++
		gen := h.connGen
		h.connMu.Unlock()

		if consecutiveConnectFailures > 0 {
			if everConnected {
				h.LogLine("INFO", fmt.Sprintf("Reconnected to nodes/ws after %d failed attempt(s)", consecutiveConnectFailures))
			} else {
				h.LogLine("INFO", fmt.Sprintf("Connected to nodes/ws after %d failed attempt(s)", consecutiveConnectFailures))
			}
		} else {
			if everConnected {
				h.LogLine("INFO", "Reconnected to nodes/ws")
			} else {
				h.LogLine("INFO", "Connected to nodes/ws")
			}
		}
		everConnected = true
		consecutiveConnectFailures = 0

		if err := h.sendJSONBestEffort(c, h.connectFrame()); err != nil {
			h.LogLine("WARN", fmt.Sprintf("Failed to send connect frame: %s", err.Error()))
			_ = c.Close()
			h.setDisconnected(gen)
			continue
		}

		h.connMu.Lock()
		if h.connGen == gen {
			h.conn = c
		}
		h.connMu.Unlock()

		h.flushPending(c)

		connDone := make(chan struct{})

		heartbeatStop := make(chan struct{})
		go func() {
			defer close(heartbeatStop)
			ticker := time.NewTicker(h.cfg.HeartbeatInterval)
			defer ticker.Stop()
			for {
				select {
				case <-ticker.C:
					_ = h.sendJSONBestEffort(c, proto.EventFrame{
						Type:  "event",
						Event: "node.heartbeat",
						Payload: map[string]interface{}{
							"t": util.NowUnixMs(),
						},
					})
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

		closedKind := "close"
		var closeDetails string
		for {
			op, msg, rerr := c.ReadMessage(0)
			if rerr != nil {
				if ce, ok := rerr.(*ws.CloseError); ok {
					closedKind = "close"
					if ce.Reason != "" {
						closeDetails = fmt.Sprintf("code=%d reason=%s", ce.Code, util.SafeJSON(ce.Reason))
					} else {
						closeDetails = fmt.Sprintf("code=%d", ce.Code)
					}
				} else {
					closedKind = "error"
					closeDetails = rerr.Error()
				}
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
			if f.Event != "node.invoke.request" {
				continue
			}
			var payload proto.InvokeRequestPayload
			if err := json.Unmarshal(f.Payload, &payload); err != nil {
				continue
			}
			if strings.TrimSpace(payload.ID) == "" {
				continue
			}
			go h.handleInvoke(ctx, payload)
		}

		close(connDone)
		<-heartbeatStop
		<-pingStop

		h.setDisconnected(gen)

		if closedKind == "close" {
			h.LogLine("WARN", fmt.Sprintf("Disconnected from nodes/ws (%s)", closeDetails))
		} else {
			h.LogLine("WARN", fmt.Sprintf("WebSocket error: %s", closeDetails))
		}

		delayMs := util.ComputeReconnectDelayMs(
			int(h.cfg.ReconnectDelayBase/time.Millisecond),
			int(h.cfg.ReconnectDelayMax/time.Millisecond),
			1,
			h.cfg.ReconnectJitterPct,
			rng.Float64,
		)
		h.LogLine("INFO", fmt.Sprintf("Reconnecting in %dms", delayMs))
		select {
		case <-time.After(time.Duration(delayMs) * time.Millisecond):
		case <-ctx.Done():
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
		summary := h.summarizeInvokeForAudit(cmd, params, payload.TimeoutMs)
		h.LogLine("AUDIT", fmt.Sprintf("invoke id=%s %s", reqID, summary))
	}

	start := time.Now()
	outcome := InvokeOutcome{OK: false, Error: &proto.InvokeError{Code: "UNSUPPORTED", Message: "unsupported command"}}
	if h.invoker != nil {
		outcome = h.invoker.Invoke(ctx, InvokeRequest{
			ID:        reqID,
			Command:   cmd,
			TimeoutMs: payload.TimeoutMs,
			Params:    params,
		})
	}
	elapsed := time.Since(start)

	if h.cfg.AuditEnabled {
		if !outcome.OK || cmd == "system.run" {
			parts := []string{
				fmt.Sprintf("ok=%t", outcome.OK),
				fmt.Sprintf("ms=%d", elapsed.Milliseconds()),
			}
			jobID := extractJobID(outcome.Payload)
			if jobID != "" {
				parts = append(parts, fmt.Sprintf("jobId=%s", util.SafeJSON(jobID)))
			}
			if payload.TimeoutMs != nil {
				parts = append(parts, fmt.Sprintf("timeoutMs=%d", *payload.TimeoutMs))
			}
			if outcome.Error != nil && strings.TrimSpace(outcome.Error.Code) != "" {
				parts = append(parts, fmt.Sprintf("errorCode=%s", util.SafeJSON(outcome.Error.Code)))
			}
			if outcome.Error != nil && strings.TrimSpace(outcome.Error.Message) != "" {
				parts = append(parts, fmt.Sprintf("error=%s", util.SafeJSON(util.TruncateUTF8Bytes(outcome.Error.Message, h.cfg.AuditMaxBytes))))
			}
			h.LogLine("AUDIT", fmt.Sprintf("result id=%s command=%s %s", reqID, cmd, strings.Join(parts, " ")))
		}
	}

	var payloadJSON *string
	if outcome.Payload != nil {
		if b, err := json.Marshal(outcome.Payload); err == nil {
			s := string(b)
			payloadJSON = &s
		}
	}

	reply := proto.EventFrame{
		Type:  "event",
		Event: "node.invoke.result",
		Payload: proto.InvokeResultPayload{
			ID:          reqID,
			NodeID:      h.cfg.NodeID,
			OK:          outcome.OK,
			Payload:     outcome.Payload,
			PayloadJSON: payloadJSON,
			Error:       outcome.Error,
		},
	}

	h.connMu.RLock()
	c := h.conn
	h.connMu.RUnlock()
	if c == nil {
		return
	}
	_ = h.sendJSONBestEffort(c, reply)
}

func extractJobID(payload interface{}) string {
	m, ok := payload.(map[string]interface{})
	if !ok || m == nil {
		return ""
	}
	v, ok := m["jobId"]
	if !ok {
		return ""
	}
	s, _ := v.(string)
	return strings.TrimSpace(s)
}

func (h *Host) summarizeInvokeForAudit(command string, params interface{}, timeoutMs *int) string {
	cmd := strings.TrimSpace(command)
	p := params

	if cmd == "system.run" {
		pm, _ := p.(map[string]interface{})
		argv := coerceStringArray(pm["argv"])
		cwd := strings.TrimSpace(asString(pm["cwd"]))
		notifyOnExitPresent := mapHasKey(pm, "notifyOnExit")
		notifyOnExit := false
		notifyOnExitVal := ""
		if notifyOnExitPresent {
			notifyOnExit = asBool(pm["notifyOnExit"])
			notifyOnExitVal = fmt.Sprintf("notifyOnExit=%t", notifyOnExit)
		}
		envKeys := envKeysFrom(pm["env"])

		argvPreview := ""
		if argv != nil {
			argvPreview = util.TruncateUTF8Bytes(util.SafeJSON(argv), h.cfg.AuditMaxBytes)
		}

		parts := []string{
			fmt.Sprintf("command=%s", cmd),
		}
		if argvPreview != "" {
			parts = append(parts, fmt.Sprintf("argv=%s", argvPreview))
		} else if argv != nil {
			parts = append(parts, fmt.Sprintf("argv=%d args", len(argv)))
		} else {
			parts = append(parts, "argv=null")
		}
		if cwd != "" {
			parts = append(parts, fmt.Sprintf("cwd=%s", util.SafeJSON(cwd)))
		}
		if timeoutMs != nil {
			parts = append(parts, fmt.Sprintf("timeoutMs=%d", *timeoutMs))
		}
		if cmdTimeoutMs, ok := asNumber(pm["timeoutMs"]); ok {
			parts = append(parts, fmt.Sprintf("cmdTimeoutMs=%d", int(cmdTimeoutMs)))
		}
		if yieldMs, ok := asNumber(pm["yieldMs"]); ok {
			parts = append(parts, fmt.Sprintf("yieldMs=%d", int(yieldMs)))
		}
		if mapHasKey(pm, "pty") {
			parts = append(parts, fmt.Sprintf("pty=%t", asBool(pm["pty"])))
		}
		if notifyOnExitVal != "" {
			parts = append(parts, notifyOnExitVal)
		}
		if envKeys != nil {
			preview := envKeys
			if len(preview) > 50 {
				preview = preview[:50]
			}
			parts = append(parts, fmt.Sprintf("envKeys=%s", util.TruncateUTF8Bytes(util.SafeJSON(preview), h.cfg.AuditMaxBytes)))
		}
		parts = append(parts, h.auditTextParts("stdin", asString(pm["stdinText"]))...)
		return strings.Join(parts, " ")
	}

	if cmd == "system.which" {
		pm, _ := p.(map[string]interface{})
		bin := strings.TrimSpace(asString(pm["bin"]))
		return fmt.Sprintf("command=%s bin=%s", cmd, util.SafeJSON(bin))
	}

	if cmd == "process.get" || cmd == "process.kill" || cmd == "process.logs" || cmd == "process.submit" {
		pm, _ := p.(map[string]interface{})
		jobID := strings.TrimSpace(asString(pm["jobId"]))
		extra := ""
		if cmd == "process.logs" {
			if tb, ok := asNumber(pm["tailBytes"]); ok {
				extra = fmt.Sprintf(" tailBytes=%d", int(tb))
			}
		}
		return fmt.Sprintf("command=%s jobId=%s%s", cmd, util.SafeJSON(jobID), extra)
	}

	if cmd == "process.write" {
		pm, _ := p.(map[string]interface{})
		parts := []string{
			fmt.Sprintf("command=%s", cmd),
			fmt.Sprintf("jobId=%s", util.SafeJSON(strings.TrimSpace(asString(pm["jobId"])))),
		}
		parts = append(parts, h.auditTextParts("data", asString(pm["data"]))...)
		return strings.Join(parts, " ")
	}

	if cmd == "process.paste" {
		pm, _ := p.(map[string]interface{})
		parts := []string{
			fmt.Sprintf("command=%s", cmd),
			fmt.Sprintf("jobId=%s", util.SafeJSON(strings.TrimSpace(asString(pm["jobId"])))),
		}
		if mapHasKey(pm, "bracketed") {
			parts = append(parts, fmt.Sprintf("bracketed=%t", asBool(pm["bracketed"])))
		}
		parts = append(parts, h.auditTextParts("text", asString(pm["text"]))...)
		return strings.Join(parts, " ")
	}

	if cmd == "process.send_keys" {
		pm, _ := p.(map[string]interface{})
		parts := []string{
			fmt.Sprintf("command=%s", cmd),
			fmt.Sprintf("jobId=%s", util.SafeJSON(strings.TrimSpace(asString(pm["jobId"])))),
		}
		if keys := coerceStringArray(pm["keys"]); len(keys) > 0 {
			parts = append(parts, fmt.Sprintf("keys=%s", util.TruncateUTF8Bytes(util.SafeJSON(keys), h.cfg.AuditMaxBytes)))
		}
		parts = append(parts, h.auditTextParts("literal", asString(pm["literal"]))...)
		return strings.Join(parts, " ")
	}

	if cmd == "process.list" {
		return fmt.Sprintf("command=%s", cmd)
	}

	raw := util.SafeJSON(map[string]interface{}{
		"command":   cmd,
		"params":    p,
		"timeoutMs": timeoutMs,
	})
	if raw == "" {
		return util.TruncateUTF8Bytes(cmd, h.cfg.AuditMaxBytes)
	}
	return util.TruncateUTF8Bytes(raw, h.cfg.AuditMaxBytes)
}

func (h *Host) auditTextParts(prefix string, text string) []string {
	if text == "" {
		return nil
	}
	parts := []string{fmt.Sprintf("%sBytes=%d", prefix, len([]byte(text)))}
	if h.cfg.AuditStdinPreviewBytes > 0 {
		preview := util.TruncateUTF8Bytes(text, h.cfg.AuditStdinPreviewBytes)
		if preview != "" {
			parts = append(parts, fmt.Sprintf("%sPreview=%s", prefix, util.TruncateUTF8Bytes(util.SafeJSON(preview), h.cfg.AuditMaxBytes)))
		}
	}
	return parts
}

func coerceStringArray(v interface{}) []string {
	arr, ok := v.([]interface{})
	if !ok {
		return nil
	}
	out := make([]string, 0, len(arr))
	for _, it := range arr {
		s, ok := it.(string)
		if !ok {
			return nil
		}
		out = append(out, s)
	}
	return out
}

func asString(v interface{}) string {
	s, _ := v.(string)
	return s
}

func asBool(v interface{}) bool {
	b, _ := v.(bool)
	return b
}

func asNumber(v interface{}) (float64, bool) {
	switch n := v.(type) {
	case float64:
		return n, true
	case int:
		return float64(n), true
	case int64:
		return float64(n), true
	default:
		return 0, false
	}
}

func mapHasKey(m map[string]interface{}, key string) bool {
	if m == nil {
		return false
	}
	_, ok := m[key]
	return ok
}

func envKeysFrom(v interface{}) []string {
	m, ok := v.(map[string]interface{})
	if !ok || m == nil {
		return nil
	}
	keys := make([]string, 0, len(m))
	for k, vv := range m {
		if strings.TrimSpace(k) == "" {
			continue
		}
		if _, ok := vv.(string); !ok {
			continue
		}
		keys = append(keys, k)
	}
	if len(keys) == 0 {
		return nil
	}
	sort.Strings(keys)
	return keys
}
