package nodehost

import (
	"flag"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/yym68686/argus/apps/node-host/internal/util"
)

type Config struct {
	URL         string
	NodeID      string
	DisplayName string

	StateDir string

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
	fs := flag.NewFlagSet("argus", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)

	var (
		flagURL         = fs.String("url", "", "nodes/ws URL (or ARGUS_NODE_WS_URL)")
		flagNodeID      = fs.String("node-id", "", "node id (or ARGUS_NODE_ID)")
		flagDisplayName = fs.String("display-name", "", "display name (or ARGUS_NODE_DISPLAY_NAME)")

		flagAudit = fs.String("audit", "", "enable audit logs (true/false); default true (or ARGUS_NODE_AUDIT)")

		flagHandshakeTimeoutMs maybeInt
		flagConnectTimeoutMs   maybeInt

		flagReconnectDelayMs    maybeInt
		flagReconnectDelayMaxMs maybeInt
		flagReconnectJitterPct  maybeFloat

		flagPingIntervalMs maybeInt
		flagPongTimeoutMs  maybeInt
	)

	fs.Var(&flagHandshakeTimeoutMs, "handshake-timeout-ms", "websocket handshake timeout ms (or ARGUS_NODE_HANDSHAKE_TIMEOUT_MS)")
	fs.Var(&flagConnectTimeoutMs, "connect-timeout-ms", "websocket connect timeout ms (or ARGUS_NODE_CONNECT_TIMEOUT_MS)")

	fs.Var(&flagReconnectDelayMs, "reconnect-delay-ms", "reconnect base delay ms (or ARGUS_NODE_RECONNECT_DELAY_MS)")
	fs.Var(&flagReconnectDelayMaxMs, "reconnect-delay-max-ms", "reconnect max delay ms (or ARGUS_NODE_RECONNECT_DELAY_MAX_MS)")
	fs.Var(&flagReconnectJitterPct, "reconnect-jitter-pct", "reconnect jitter fraction (0..0.5) (or ARGUS_NODE_RECONNECT_JITTER_PCT)")

	fs.Var(&flagPingIntervalMs, "ping-interval-ms", "ws ping interval ms (0 disables) (or ARGUS_NODE_PING_INTERVAL_MS)")
	fs.Var(&flagPongTimeoutMs, "pong-timeout-ms", "ws pong timeout ms (or ARGUS_NODE_PONG_TIMEOUT_MS)")

	if err := fs.Parse(argv); err != nil {
		return nil, err
	}

	urlStr := strings.TrimSpace(*flagURL)
	if urlStr == "" {
		urlStr = strings.TrimSpace(os.Getenv("ARGUS_NODE_WS_URL"))
	}
	if urlStr == "" {
		return nil, fmt.Errorf("missing --url (or ARGUS_NODE_WS_URL). Example: ws://127.0.0.1:8080/nodes/ws?token=...")
	}

	nodeID := strings.TrimSpace(*flagNodeID)
	if nodeID == "" {
		nodeID = strings.TrimSpace(os.Getenv("ARGUS_NODE_ID"))
	}
	displayName := strings.TrimSpace(*flagDisplayName)
	if displayName == "" {
		displayName = strings.TrimSpace(os.Getenv("ARGUS_NODE_DISPLAY_NAME"))
	}

	hostname, _ := os.Hostname()
	if nodeID == "" {
		nodeID = hostname
	}
	if displayName == "" {
		displayName = hostname
	}

	stateDir := strings.TrimSpace(os.Getenv("ARGUS_NODE_STATE_DIR"))
	if stateDir == "" {
		appHome := strings.TrimSpace(os.Getenv("APP_HOME"))
		if appHome != "" {
			stateDir = JoinPath(appHome, "node-host", SafeBasename(nodeID))
		} else {
			home, _ := os.UserHomeDir()
			if strings.TrimSpace(home) == "" {
				home = "."
			}
			stateDir = JoinPath(home, ".argus", "node-host", SafeBasename(nodeID))
		}
	}

	auditEnabled := true
	if s := strings.TrimSpace(*flagAudit); s != "" {
		auditEnabled = util.ParseBool(s, true)
	} else {
		auditEnabled = util.ParseBool(os.Getenv("ARGUS_NODE_AUDIT"), true)
	}

	auditMaxBytes := parseClampedEnvInt("ARGUS_NODE_AUDIT_MAX_BYTES", 4096, 256, 1024*1024)
	auditStdinPreviewBytes := parseClampedEnvInt("ARGUS_NODE_AUDIT_STDIN_PREVIEW_BYTES", 256, 0, 1024*1024)

	handshakeTimeoutMs := intFromMaybeIntOrEnv(&flagHandshakeTimeoutMs, "ARGUS_NODE_HANDSHAKE_TIMEOUT_MS", 15000)
	handshakeTimeoutMs = util.ClampInt(handshakeTimeoutMs, 0, 120000)

	connectTimeoutMsDefault := 20000
	if handshakeTimeoutMs > 0 {
		connectTimeoutMsDefault = handshakeTimeoutMs + 2000
	}
	connectTimeoutMs := intFromMaybeIntOrEnv(&flagConnectTimeoutMs, "ARGUS_NODE_CONNECT_TIMEOUT_MS", connectTimeoutMsDefault)
	connectTimeoutMs = util.ClampInt(connectTimeoutMs, 1000, 300000)
	if handshakeTimeoutMs > 0 && connectTimeoutMs < handshakeTimeoutMs {
		connectTimeoutMs = handshakeTimeoutMs
	}

	reconnectDelayBaseMs := intFromMaybeIntOrEnv(&flagReconnectDelayMs, "ARGUS_NODE_RECONNECT_DELAY_MS", 1000)
	reconnectDelayBaseMs = util.ClampInt(reconnectDelayBaseMs, 250, 60000)
	reconnectDelayMaxMs := intFromMaybeIntOrEnv(&flagReconnectDelayMaxMs, "ARGUS_NODE_RECONNECT_DELAY_MAX_MS", 30000)
	reconnectDelayMaxMs = util.ClampInt(reconnectDelayMaxMs, reconnectDelayBaseMs, 600000)
	reconnectJitterPct := floatFromMaybeFloatOrEnv(&flagReconnectJitterPct, "ARGUS_NODE_RECONNECT_JITTER_PCT", 0.2)
	reconnectJitterPct = util.ClampFloat64(reconnectJitterPct, 0, 0.5)

	pingIntervalMs := intFromMaybeIntOrEnv(&flagPingIntervalMs, "ARGUS_NODE_PING_INTERVAL_MS", 30000)
	pingIntervalMs = util.ClampInt(pingIntervalMs, 0, 600000)
	pongTimeoutMs := intFromMaybeIntOrEnv(&flagPongTimeoutMs, "ARGUS_NODE_PONG_TIMEOUT_MS", 10000)
	pongTimeoutMs = util.ClampInt(pongTimeoutMs, 1000, 600000)

	return &Config{
		URL:         urlStr,
		NodeID:      nodeID,
		DisplayName: displayName,
		StateDir:    stateDir,

		AuditEnabled:           auditEnabled,
		AuditMaxBytes:          auditMaxBytes,
		AuditStdinPreviewBytes: auditStdinPreviewBytes,

		WSHandshakeTimeout: time.Duration(handshakeTimeoutMs) * time.Millisecond,
		WSConnectTimeout:   time.Duration(connectTimeoutMs) * time.Millisecond,

		ReconnectDelayBase: time.Duration(reconnectDelayBaseMs) * time.Millisecond,
		ReconnectDelayMax:  time.Duration(reconnectDelayMaxMs) * time.Millisecond,
		ReconnectJitterPct: reconnectJitterPct,

		HeartbeatInterval: 15 * time.Second,
		PingInterval:      time.Duration(pingIntervalMs) * time.Millisecond,
		PongTimeout:       time.Duration(pongTimeoutMs) * time.Millisecond,

		WriteTimeout: 10 * time.Second,
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

func parseClampedEnvInt(envName string, def int, min int, max int) int {
	raw := strings.TrimSpace(os.Getenv(envName))
	if raw == "" {
		return def
	}
	v, err := strconv.Atoi(raw)
	if err != nil {
		return def
	}
	return util.ClampInt(v, min, max)
}
