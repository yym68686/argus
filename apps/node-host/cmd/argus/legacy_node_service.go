package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"time"

	"github.com/yym68686/argus/apps/node-host/internal/nodehost"
	"github.com/yym68686/argus/apps/node-host/internal/util"
)

type legacyNodeStoredConfig struct {
	Version                int     `json:"version"`
	URL                    string  `json:"url"`
	NodeID                 string  `json:"nodeId"`
	DisplayName            string  `json:"displayName"`
	StateDir               string  `json:"stateDir"`
	AuditEnabled           bool    `json:"auditEnabled"`
	AuditMaxBytes          int     `json:"auditMaxBytes"`
	AuditStdinPreviewBytes int     `json:"auditStdinPreviewBytes"`
	WSHandshakeTimeoutMs   int     `json:"wsHandshakeTimeoutMs"`
	WSConnectTimeoutMs     int     `json:"wsConnectTimeoutMs"`
	ReconnectDelayBaseMs   int     `json:"reconnectDelayBaseMs"`
	ReconnectDelayMaxMs    int     `json:"reconnectDelayMaxMs"`
	ReconnectJitterPct     float64 `json:"reconnectJitterPct"`
	HeartbeatIntervalMs    int     `json:"heartbeatIntervalMs"`
	PingIntervalMs         int     `json:"pingIntervalMs"`
	PongTimeoutMs          int     `json:"pongTimeoutMs"`
	WriteTimeoutMs         int     `json:"writeTimeoutMs"`
	PID                    int     `json:"pid,omitempty"`
	LogPath                string  `json:"logPath"`
	StartedAt              string  `json:"startedAt,omitempty"`
}

func runLegacyNodeSubcommand(args []string) error {
	if len(args) == 0 {
		fmt.Fprintf(os.Stderr, "usage: argus node connect|disconnect|reconnect|logs\n")
		return nil
	}
	switch args[0] {
	case "connect":
		return runLegacyNodeBackground(args[1:])
	case "disconnect":
		return runLegacyNodeDisconnect(args[1:])
	case "reconnect":
		return runLegacyNodeReconnect(args[1:])
	case "logs", "log":
		return runLegacyNodeLogs(args[1:])
	case "foreground":
		return runLegacyNode(args[1:])
	default:
		return fmt.Errorf("unknown node subcommand: %s", args[0])
	}
}

func runLegacyNodeBackground(args []string) error {
	nodeArgs, foreground, err := splitLegacyNodeControlArgs(args)
	if err != nil {
		return err
	}
	if foreground {
		return runLegacyNode(nodeArgs)
	}
	cfg, err := nodehost.ParseConfig(nodeArgs)
	if err != nil {
		return err
	}
	configPath := legacyNodeDefaultConfigPath()
	stored := legacyNodeStoredFromConfig(cfg, legacyNodeDefaultLogPath())

	if existing, err := loadLegacyNodeStoredConfig(configPath); err == nil && existing != nil && existing.PID > 0 && legacyNodeProcessRunning(existing.PID) {
		if existing.URL == stored.URL && existing.NodeID == stored.NodeID {
			fmt.Fprintf(os.Stderr, "Argus node is already running in the background (pid=%d).\n", existing.PID)
			fmt.Fprintf(os.Stderr, "Logs: argus logs\n")
			fmt.Fprintf(os.Stderr, "Disconnect: argus disconnect\n")
			return nil
		}
		_ = stopLegacyNodeProcess(existing.PID)
	}

	if err := saveLegacyNodeStoredConfig(configPath, stored); err != nil {
		return err
	}
	pid, err := startLegacyNodeWorker(configPath, stored.LogPath)
	if err != nil {
		return err
	}
	stored.PID = pid
	stored.StartedAt = time.Now().UTC().Format(time.RFC3339)
	if err := saveLegacyNodeStoredConfig(configPath, stored); err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "Argus node started in the background (pid=%d).\n", pid)
	fmt.Fprintf(os.Stderr, "Logs: argus logs\n")
	fmt.Fprintf(os.Stderr, "Reconnect: argus reconnect\n")
	fmt.Fprintf(os.Stderr, "Disconnect: argus disconnect\n")
	return nil
}

func runLegacyNodeWorker(args []string) error {
	fs := flag.NewFlagSet("legacy-node-run", flag.ContinueOnError)
	configPath := fs.String("config", legacyNodeDefaultConfigPath(), "legacy node config path")
	if err := fs.Parse(args); err != nil {
		return err
	}
	stored, err := loadLegacyNodeStoredConfig(*configPath)
	if err != nil {
		return err
	}
	return runLegacyNodeConfig(stored.RuntimeConfig())
}

func runLegacyNodeDisconnect(args []string) error {
	fs := flag.NewFlagSet("disconnect", flag.ContinueOnError)
	configPath := fs.String("config", legacyNodeDefaultConfigPath(), "legacy node config path")
	forget := fs.Bool("forget", false, "remove the saved legacy node config after disconnecting")
	fs.Bool("keep-config", false, "accepted for compatibility; legacy node config is kept unless --forget is set")
	if err := fs.Parse(args); err != nil {
		return err
	}
	stored, err := loadLegacyNodeStoredConfig(*configPath)
	if err != nil {
		return err
	}
	if stored.PID > 0 && legacyNodeProcessRunning(stored.PID) {
		if err := stopLegacyNodeProcess(stored.PID); err != nil {
			return err
		}
		fmt.Fprintf(os.Stderr, "Disconnected Argus node (pid=%d).\n", stored.PID)
	} else {
		fmt.Fprintf(os.Stderr, "Argus node is not running.\n")
	}
	stored.PID = 0
	if *forget {
		return os.Remove(*configPath)
	}
	return saveLegacyNodeStoredConfig(*configPath, stored)
}

func runLegacyNodeReconnect(args []string) error {
	fs := flag.NewFlagSet("reconnect", flag.ContinueOnError)
	configPath := fs.String("config", legacyNodeDefaultConfigPath(), "legacy node config path")
	if err := fs.Parse(args); err != nil {
		return err
	}
	stored, err := loadLegacyNodeStoredConfig(*configPath)
	if err != nil {
		return err
	}
	if stored.PID > 0 && legacyNodeProcessRunning(stored.PID) {
		_ = stopLegacyNodeProcess(stored.PID)
	}
	pid, err := startLegacyNodeWorker(*configPath, stored.LogPath)
	if err != nil {
		return err
	}
	stored.PID = pid
	stored.StartedAt = time.Now().UTC().Format(time.RFC3339)
	if err := saveLegacyNodeStoredConfig(*configPath, stored); err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "Reconnected Argus node in the background (pid=%d).\n", pid)
	fmt.Fprintf(os.Stderr, "Logs: argus logs\n")
	return nil
}

func runLegacyNodeLogs(args []string) error {
	fs := flag.NewFlagSet("logs", flag.ContinueOnError)
	configPath := fs.String("config", legacyNodeDefaultConfigPath(), "legacy node config path")
	tailLines := fs.Int("tail", 120, "number of lines to print before following")
	follow := fs.Bool("follow", true, "continue following logs until interrupted")
	if err := fs.Parse(args); err != nil {
		return err
	}
	stored, err := loadLegacyNodeStoredConfig(*configPath)
	if err != nil {
		return err
	}
	return tailLegacyNodeLog(stored.LogPath, *tailLines, *follow)
}

func splitLegacyNodeControlArgs(args []string) ([]string, bool, error) {
	out := make([]string, 0, len(args))
	foreground := false
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--foreground":
			foreground = true
		case "--detach":
			foreground = false
		case "--detach=false", "--no-detach":
			foreground = true
		default:
			out = append(out, args[i])
		}
	}
	return out, foreground, nil
}

func legacyNodeConfigExists() bool {
	return legacyNodeConfigExistsAt(legacyNodeDefaultConfigPath())
}

func legacyNodeConfigExistsForArgs(args []string) bool {
	configPath := legacyNodeDefaultConfigPath()
	fs := flag.NewFlagSet("legacy-node-exists", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	fs.StringVar(&configPath, "config", configPath, "legacy node config path")
	_ = fs.Parse(args)
	return legacyNodeConfigExistsAt(configPath)
}

func legacyNodeProcessRunningForArgs(args []string) bool {
	configPath := legacyNodeDefaultConfigPath()
	fs := flag.NewFlagSet("legacy-node-running", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	fs.StringVar(&configPath, "config", configPath, "legacy node config path")
	_ = fs.Parse(args)
	stored, err := loadLegacyNodeStoredConfig(configPath)
	return err == nil && stored.PID > 0 && legacyNodeProcessRunning(stored.PID)
}

func legacyNodeConfigExistsAt(configPath string) bool {
	_, err := loadLegacyNodeStoredConfig(configPath)
	return err == nil
}

func legacyNodeDefaultConfigPath() string {
	return filepath.Join(legacyNodeBaseDir(), "legacy-node.json")
}

func legacyNodeDefaultLogPath() string {
	return filepath.Join(legacyNodeBaseDir(), "legacy-node.log")
}

func legacyNodeBaseDir() string {
	home, _ := os.UserHomeDir()
	if strings.TrimSpace(home) == "" {
		home = "."
	}
	return filepath.Join(home, ".argus", "node-host")
}

func legacyNodeStoredFromConfig(cfg *nodehost.Config, logPath string) *legacyNodeStoredConfig {
	return &legacyNodeStoredConfig{
		Version:                1,
		URL:                    cfg.URL,
		NodeID:                 cfg.NodeID,
		DisplayName:            cfg.DisplayName,
		StateDir:               cfg.StateDir,
		AuditEnabled:           cfg.AuditEnabled,
		AuditMaxBytes:          cfg.AuditMaxBytes,
		AuditStdinPreviewBytes: cfg.AuditStdinPreviewBytes,
		WSHandshakeTimeoutMs:   durationMs(cfg.WSHandshakeTimeout),
		WSConnectTimeoutMs:     durationMs(cfg.WSConnectTimeout),
		ReconnectDelayBaseMs:   durationMs(cfg.ReconnectDelayBase),
		ReconnectDelayMaxMs:    durationMs(cfg.ReconnectDelayMax),
		ReconnectJitterPct:     cfg.ReconnectJitterPct,
		HeartbeatIntervalMs:    durationMs(cfg.HeartbeatInterval),
		PingIntervalMs:         durationMs(cfg.PingInterval),
		PongTimeoutMs:          durationMs(cfg.PongTimeout),
		WriteTimeoutMs:         durationMs(cfg.WriteTimeout),
		LogPath:                logPath,
	}
}

func (c *legacyNodeStoredConfig) RuntimeConfig() *nodehost.Config {
	return &nodehost.Config{
		URL:                    c.URL,
		NodeID:                 firstNonEmpty(c.NodeID, "node"),
		DisplayName:            firstNonEmpty(c.DisplayName, c.NodeID, "node"),
		StateDir:               firstNonEmpty(c.StateDir, filepath.Join(legacyNodeBaseDir(), nodehost.SafeBasename(c.NodeID))),
		AuditEnabled:           c.AuditEnabled,
		AuditMaxBytes:          intDefault(c.AuditMaxBytes, 4096),
		AuditStdinPreviewBytes: c.AuditStdinPreviewBytes,
		WSHandshakeTimeout:     durationFromMsAllowZero(c.WSHandshakeTimeoutMs, 15*time.Second),
		WSConnectTimeout:       durationFromMs(c.WSConnectTimeoutMs, 20*time.Second),
		ReconnectDelayBase:     durationFromMs(c.ReconnectDelayBaseMs, time.Second),
		ReconnectDelayMax:      durationFromMs(c.ReconnectDelayMaxMs, 30*time.Second),
		ReconnectJitterPct:     util.ClampFloat64(c.ReconnectJitterPct, 0, 0.5),
		HeartbeatInterval:      durationFromMs(c.HeartbeatIntervalMs, 15*time.Second),
		PingInterval:           durationFromMsAllowZero(c.PingIntervalMs, 30*time.Second),
		PongTimeout:            durationFromMs(c.PongTimeoutMs, 10*time.Second),
		WriteTimeout:           durationFromMs(c.WriteTimeoutMs, 10*time.Second),
	}
}

func loadLegacyNodeStoredConfig(path string) (*legacyNodeStoredConfig, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var cfg legacyNodeStoredConfig
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return nil, err
	}
	if strings.TrimSpace(cfg.URL) == "" {
		return nil, fmt.Errorf("legacy node config is missing url")
	}
	if strings.TrimSpace(cfg.LogPath) == "" {
		cfg.LogPath = legacyNodeDefaultLogPath()
	}
	return &cfg, nil
}

func saveLegacyNodeStoredConfig(path string, cfg *legacyNodeStoredConfig) error {
	if cfg == nil {
		return fmt.Errorf("missing legacy node config")
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	if strings.TrimSpace(cfg.LogPath) != "" {
		if err := os.MkdirAll(filepath.Dir(cfg.LogPath), 0o755); err != nil {
			return err
		}
	}
	payload, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(payload, '\n'), 0o600)
}

func startLegacyNodeWorker(configPath string, logPath string) (int, error) {
	exe, err := os.Executable()
	if err != nil {
		return 0, err
	}
	if err := os.MkdirAll(filepath.Dir(logPath), 0o755); err != nil {
		return 0, err
	}
	logFile, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return 0, err
	}
	defer logFile.Close()
	devNull, err := os.Open(os.DevNull)
	if err != nil {
		return 0, err
	}
	defer devNull.Close()

	cmd := exec.Command(exe, "legacy-node-run", "--config", configPath)
	cmd.Stdin = devNull
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	cmd.SysProcAttr = detachedSysProcAttr()
	if err := cmd.Start(); err != nil {
		return 0, err
	}
	return cmd.Process.Pid, nil
}

func tailLegacyNodeLog(path string, tailLines int, follow bool) error {
	if tailLines < 0 {
		tailLines = 0
	}
	data, err := os.ReadFile(path)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if len(data) > 0 && tailLines > 0 {
		fmt.Print(lastLogLines(string(data), tailLines))
	}
	if !follow {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDONLY, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := f.Seek(0, io.SeekEnd); err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), shutdownSignals()...)
	defer stop()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		if _, err := io.Copy(os.Stdout, f); err != nil {
			return err
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}

func lastLogLines(raw string, n int) string {
	if n <= 0 || raw == "" {
		return ""
	}
	lines := strings.SplitAfter(raw, "\n")
	if len(lines) > 0 && lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "")
}

func durationMs(d time.Duration) int {
	return int(d / time.Millisecond)
}

func durationFromMs(ms int, fallback time.Duration) time.Duration {
	if ms <= 0 {
		return fallback
	}
	return time.Duration(ms) * time.Millisecond
}

func durationFromMsAllowZero(ms int, fallback time.Duration) time.Duration {
	if ms < 0 {
		return fallback
	}
	return time.Duration(ms) * time.Millisecond
}

func intDefault(value int, fallback int) int {
	if value == 0 {
		return fallback
	}
	return value
}
