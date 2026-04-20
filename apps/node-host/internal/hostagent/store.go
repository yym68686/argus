package hostagent

import (
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

type StoredConfig struct {
	GatewayBaseURL    string `json:"gatewayBaseUrl"`
	HostID            string `json:"hostId"`
	DisplayName       string `json:"displayName"`
	DeviceToken       string `json:"deviceToken"`
	WorkspaceBasePath string `json:"workspaceBasePath"`
	CodexHomeBasePath string `json:"codexHomeBasePath"`
	StateDir          string `json:"stateDir"`
	AppServerCmd      string `json:"appServerCmd"`
}

type Config struct {
	URL         string
	HostID      string
	DisplayName string

	StateDir          string
	WorkspaceBasePath string
	CodexHomeBasePath string
	AppServerCmd      string

	AuditEnabled           bool
	AuditMaxBytes          int
	AuditStdinPreviewBytes int

	WSHandshakeTimeout time.Duration
	WSConnectTimeout   time.Duration

	ReconnectDelayBase time.Duration
	ReconnectDelayMax  time.Duration
	ReconnectJitterPct float64

	HeartbeatInterval time.Duration
	PingInterval      time.Duration
	PongTimeout       time.Duration

	WriteTimeout time.Duration
}

func defaultArgusBaseDir() string {
	home, _ := os.UserHomeDir()
	if strings.TrimSpace(home) == "" {
		home = "."
	}
	return filepath.Join(home, ".argus")
}

func DefaultConfigPath() string {
	return filepath.Join(defaultArgusBaseDir(), "host-agent.json")
}

func defaultHostname() string {
	hostname, _ := os.Hostname()
	if strings.TrimSpace(hostname) == "" {
		return "host"
	}
	return hostname
}

func sanitizePathPart(raw string, fallback string) string {
	s := strings.TrimSpace(raw)
	if s == "" {
		s = fallback
	}
	var b strings.Builder
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z':
			b.WriteRune(r)
		case r >= 'A' && r <= 'Z':
			b.WriteRune(r)
		case r >= '0' && r <= '9':
			b.WriteRune(r)
		case r == '_' || r == '.' || r == '-':
			b.WriteRune(r)
		default:
			b.WriteByte('_')
		}
		if b.Len() >= 120 {
			break
		}
	}
	out := b.String()
	if out == "" {
		return fallback
	}
	return out
}

func DefaultWorkspaceBasePath() string {
	return filepath.Join(defaultArgusBaseDir(), "native-workspaces")
}

func DefaultCodexHomeBasePath() string {
	return filepath.Join(defaultArgusBaseDir(), "native-codex")
}

func DefaultStateDir(hostID string) string {
	return filepath.Join(defaultArgusBaseDir(), "node-host", sanitizePathPart(hostID, "host"))
}

func DefaultAppServerCmd() string {
	return "codex app-server --listen stdio://"
}

func DefaultHostIdentity() (string, string) {
	hostname := defaultHostname()
	return hostname, hostname
}

func LoadStoredConfig(path string) (*StoredConfig, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var cfg StoredConfig
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return nil, err
	}
	return cfg.Resolve()
}

func SaveStoredConfig(path string, cfg *StoredConfig) error {
	if cfg == nil {
		return fmt.Errorf("missing config")
	}
	resolved, err := cfg.Resolve()
	if err != nil {
		return err
	}
	payload, err := json.MarshalIndent(resolved, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return os.WriteFile(path, append(payload, '\n'), 0o600)
}

func (c *StoredConfig) Resolve() (*StoredConfig, error) {
	if c == nil {
		return nil, fmt.Errorf("missing config")
	}
	hostID := strings.TrimSpace(c.HostID)
	displayName := strings.TrimSpace(c.DisplayName)
	if hostID == "" || displayName == "" {
		defaultHostID, defaultDisplayName := DefaultHostIdentity()
		if hostID == "" {
			hostID = defaultHostID
		}
		if displayName == "" {
			displayName = defaultDisplayName
		}
	}
	gatewayBase := strings.TrimRight(strings.TrimSpace(c.GatewayBaseURL), "/")
	if gatewayBase == "" {
		return nil, fmt.Errorf("missing gatewayBaseUrl")
	}
	deviceToken := strings.TrimSpace(c.DeviceToken)
	if deviceToken == "" {
		return nil, fmt.Errorf("missing deviceToken")
	}
	workspaceBase := strings.TrimSpace(c.WorkspaceBasePath)
	if workspaceBase == "" {
		workspaceBase = DefaultWorkspaceBasePath()
	}
	codexHomeBase := strings.TrimSpace(c.CodexHomeBasePath)
	if codexHomeBase == "" {
		codexHomeBase = DefaultCodexHomeBasePath()
	}
	stateDir := strings.TrimSpace(c.StateDir)
	if stateDir == "" {
		stateDir = DefaultStateDir(hostID)
	}
	appServerCmd := strings.TrimSpace(c.AppServerCmd)
	if appServerCmd == "" {
		appServerCmd = DefaultAppServerCmd()
	}
	return &StoredConfig{
		GatewayBaseURL:    gatewayBase,
		HostID:            hostID,
		DisplayName:       displayName,
		DeviceToken:       deviceToken,
		WorkspaceBasePath: workspaceBase,
		CodexHomeBasePath: codexHomeBase,
		StateDir:          stateDir,
		AppServerCmd:      appServerCmd,
	}, nil
}

func (c *StoredConfig) WSURL() (string, error) {
	resolved, err := c.Resolve()
	if err != nil {
		return "", err
	}
	return resolved.GatewayBaseURL + "/host-agent/ws?token=" + url.QueryEscape(resolved.DeviceToken), nil
}

func (c *StoredConfig) RuntimeConfig() (*Config, error) {
	resolved, err := c.Resolve()
	if err != nil {
		return nil, err
	}
	wsURL, err := resolved.WSURL()
	if err != nil {
		return nil, err
	}
	return &Config{
		URL:                    wsURL,
		HostID:                 resolved.HostID,
		DisplayName:            resolved.DisplayName,
		StateDir:               resolved.StateDir,
		WorkspaceBasePath:      resolved.WorkspaceBasePath,
		CodexHomeBasePath:      resolved.CodexHomeBasePath,
		AppServerCmd:           resolved.AppServerCmd,
		AuditEnabled:           true,
		AuditMaxBytes:          4096,
		AuditStdinPreviewBytes: 256,
		WSHandshakeTimeout:     15 * time.Second,
		WSConnectTimeout:       20 * time.Second,
		ReconnectDelayBase:     1 * time.Second,
		ReconnectDelayMax:      30 * time.Second,
		ReconnectJitterPct:     0.2,
		HeartbeatInterval:      15 * time.Second,
		PingInterval:           30 * time.Second,
		PongTimeout:            10 * time.Second,
		WriteTimeout:           10 * time.Second,
	}, nil
}

func PlatformName() string {
	if runtime.GOOS == "windows" {
		return "win32"
	}
	return runtime.GOOS
}
