package runtimehost

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/yym68686/argus/apps/node-host/internal/util"
)

type Config struct {
	URL         string
	HostID      string
	DisplayName string

	WorkspaceBasePath string
	CodexHomeBasePath string
	AppServerCmd      string

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

type maybeInt struct {
	set bool
	val int
}

func (mi *maybeInt) Set(s string) error {
	v, err := strconv.Atoi(strings.TrimSpace(s))
	if err != nil {
		return err
	}
	mi.val = v
	mi.set = true
	return nil
}

func (mi *maybeInt) String() string {
	if mi == nil || !mi.set {
		return ""
	}
	return strconv.Itoa(mi.val)
}

type maybeFloat struct {
	set bool
	val float64
}

func (mf *maybeFloat) Set(s string) error {
	v, err := strconv.ParseFloat(strings.TrimSpace(s), 64)
	if err != nil {
		return err
	}
	mf.val = v
	mf.set = true
	return nil
}

func (mf *maybeFloat) String() string {
	if mf == nil || !mf.set {
		return ""
	}
	return strconv.FormatFloat(mf.val, 'f', -1, 64)
}

func ParseConfig(argv []string) (*Config, error) {
	fs := flag.NewFlagSet("runtime-host", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)

	var (
		flagURL         = fs.String("url", "", "runtime-host ws URL (or ARGUS_RUNTIME_HOST_WS_URL)")
		flagHostID      = fs.String("host-id", "", "runtime host id (or ARGUS_RUNTIME_HOST_ID)")
		flagDisplayName = fs.String("display-name", "", "display name (or ARGUS_RUNTIME_HOST_DISPLAY_NAME)")
		flagWorkspace   = fs.String("workspace-base", "", "workspace base dir (or ARGUS_RUNTIME_HOST_WORKSPACE_BASE_PATH)")
		flagCodexHome   = fs.String("codex-home-base", "", "session Codex home base dir (or ARGUS_RUNTIME_HOST_CODEX_HOME_BASE_PATH)")
		flagAppServer   = fs.String("app-server-cmd", "", "codex app-server command (or ARGUS_RUNTIME_HOST_APP_SERVER_CMD)")

		flagHandshakeTimeoutMs maybeInt
		flagConnectTimeoutMs   maybeInt
		flagReconnectDelayMs   maybeInt
		flagReconnectDelayMax  maybeInt
		flagReconnectJitterPct maybeFloat
		flagHeartbeatMs        maybeInt
		flagPingIntervalMs     maybeInt
		flagPongTimeoutMs      maybeInt
	)

	fs.Var(&flagHandshakeTimeoutMs, "handshake-timeout-ms", "websocket handshake timeout ms")
	fs.Var(&flagConnectTimeoutMs, "connect-timeout-ms", "websocket connect timeout ms")
	fs.Var(&flagReconnectDelayMs, "reconnect-delay-ms", "reconnect base delay ms")
	fs.Var(&flagReconnectDelayMax, "reconnect-delay-max-ms", "reconnect max delay ms")
	fs.Var(&flagReconnectJitterPct, "reconnect-jitter-pct", "reconnect jitter fraction (0..0.5)")
	fs.Var(&flagHeartbeatMs, "heartbeat-interval-ms", "runtime host heartbeat interval ms")
	fs.Var(&flagPingIntervalMs, "ping-interval-ms", "websocket ping interval ms")
	fs.Var(&flagPongTimeoutMs, "pong-timeout-ms", "websocket pong timeout ms")

	if err := fs.Parse(argv); err != nil {
		return nil, err
	}

	urlStr := strings.TrimSpace(*flagURL)
	if urlStr == "" {
		urlStr = strings.TrimSpace(os.Getenv("ARGUS_RUNTIME_HOST_WS_URL"))
	}
	if urlStr == "" {
		return nil, fmt.Errorf("missing --url (or ARGUS_RUNTIME_HOST_WS_URL)")
	}

	hostname, _ := os.Hostname()
	hostID := strings.TrimSpace(*flagHostID)
	if hostID == "" {
		hostID = strings.TrimSpace(os.Getenv("ARGUS_RUNTIME_HOST_ID"))
	}
	if hostID == "" {
		hostID = hostname
	}
	displayName := strings.TrimSpace(*flagDisplayName)
	if displayName == "" {
		displayName = strings.TrimSpace(os.Getenv("ARGUS_RUNTIME_HOST_DISPLAY_NAME"))
	}
	if displayName == "" {
		displayName = hostname
	}

	userHome, _ := os.UserHomeDir()
	if strings.TrimSpace(userHome) == "" {
		userHome = "."
	}

	workspaceBase := strings.TrimSpace(*flagWorkspace)
	if workspaceBase == "" {
		workspaceBase = strings.TrimSpace(os.Getenv("ARGUS_RUNTIME_HOST_WORKSPACE_BASE_PATH"))
	}
	if workspaceBase == "" {
		workspaceBase = filepath.Join(userHome, ".argus", "native-workspaces")
	}

	codexHomeBase := strings.TrimSpace(*flagCodexHome)
	if codexHomeBase == "" {
		codexHomeBase = strings.TrimSpace(os.Getenv("ARGUS_RUNTIME_HOST_CODEX_HOME_BASE_PATH"))
	}
	if codexHomeBase == "" {
		codexHomeBase = filepath.Join(userHome, ".argus", "native-codex")
	}

	appServerCmd := strings.TrimSpace(*flagAppServer)
	if appServerCmd == "" {
		appServerCmd = strings.TrimSpace(os.Getenv("ARGUS_RUNTIME_HOST_APP_SERVER_CMD"))
	}
	if appServerCmd == "" {
		appServerCmd = "codex app-server --listen stdio://"
	}

	handshakeTimeoutMs := intFromMaybeIntOrEnv(&flagHandshakeTimeoutMs, "ARGUS_RUNTIME_HOST_HANDSHAKE_TIMEOUT_MS", 15000)
	handshakeTimeoutMs = util.ClampInt(handshakeTimeoutMs, 1000, 120000)
	connectTimeoutMs := intFromMaybeIntOrEnv(&flagConnectTimeoutMs, "ARGUS_RUNTIME_HOST_CONNECT_TIMEOUT_MS", 20000)
	connectTimeoutMs = util.ClampInt(connectTimeoutMs, handshakeTimeoutMs, 300000)
	reconnectDelayMs := intFromMaybeIntOrEnv(&flagReconnectDelayMs, "ARGUS_RUNTIME_HOST_RECONNECT_DELAY_MS", 1000)
	reconnectDelayMs = util.ClampInt(reconnectDelayMs, 250, 60000)
	reconnectDelayMaxMs := intFromMaybeIntOrEnv(&flagReconnectDelayMax, "ARGUS_RUNTIME_HOST_RECONNECT_DELAY_MAX_MS", 30000)
	reconnectDelayMaxMs = util.ClampInt(reconnectDelayMaxMs, reconnectDelayMs, 600000)
	reconnectJitterPct := floatFromMaybeFloatOrEnv(&flagReconnectJitterPct, "ARGUS_RUNTIME_HOST_RECONNECT_JITTER_PCT", 0.2)
	reconnectJitterPct = util.ClampFloat64(reconnectJitterPct, 0, 0.5)
	heartbeatIntervalMs := intFromMaybeIntOrEnv(&flagHeartbeatMs, "ARGUS_RUNTIME_HOST_HEARTBEAT_INTERVAL_MS", 15000)
	heartbeatIntervalMs = util.ClampInt(heartbeatIntervalMs, 1000, 600000)
	pingIntervalMs := intFromMaybeIntOrEnv(&flagPingIntervalMs, "ARGUS_RUNTIME_HOST_PING_INTERVAL_MS", 30000)
	pingIntervalMs = util.ClampInt(pingIntervalMs, 0, 600000)
	pongTimeoutMs := intFromMaybeIntOrEnv(&flagPongTimeoutMs, "ARGUS_RUNTIME_HOST_PONG_TIMEOUT_MS", 10000)
	pongTimeoutMs = util.ClampInt(pongTimeoutMs, 1000, 600000)

	return &Config{
		URL:         urlStr,
		HostID:      hostID,
		DisplayName: displayName,

		WorkspaceBasePath: workspaceBase,
		CodexHomeBasePath: codexHomeBase,
		AppServerCmd:      appServerCmd,

		WSHandshakeTimeout: time.Duration(handshakeTimeoutMs) * time.Millisecond,
		WSConnectTimeout:   time.Duration(connectTimeoutMs) * time.Millisecond,
		ReconnectDelayBase: time.Duration(reconnectDelayMs) * time.Millisecond,
		ReconnectDelayMax:  time.Duration(reconnectDelayMaxMs) * time.Millisecond,
		ReconnectJitterPct: reconnectJitterPct,
		HeartbeatInterval:  time.Duration(heartbeatIntervalMs) * time.Millisecond,
		PingInterval:       time.Duration(pingIntervalMs) * time.Millisecond,
		PongTimeout:        time.Duration(pongTimeoutMs) * time.Millisecond,
		WriteTimeout:       10 * time.Second,
	}, nil
}

func intFromMaybeIntOrEnv(flagVal *maybeInt, envName string, def int) int {
	if flagVal != nil && flagVal.set {
		return flagVal.val
	}
	raw := strings.TrimSpace(os.Getenv(envName))
	if raw == "" {
		return def
	}
	v, err := strconv.Atoi(raw)
	if err != nil {
		return def
	}
	return v
}

func floatFromMaybeFloatOrEnv(flagVal *maybeFloat, envName string, def float64) float64 {
	if flagVal != nil && flagVal.set {
		return flagVal.val
	}
	raw := strings.TrimSpace(os.Getenv(envName))
	if raw == "" {
		return def
	}
	v, err := strconv.ParseFloat(raw, 64)
	if err != nil {
		return def
	}
	return v
}
