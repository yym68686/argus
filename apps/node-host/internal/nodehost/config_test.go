package nodehost

import (
	"strings"
	"testing"
)

func clearNodeConfigEnv(t *testing.T) {
	t.Helper()
	for _, name := range []string{
		"ARGUS_NODE_WS_URL",
		"ARGUS_NODE_GATEWAY_URL",
		"ARGUS_NODE_HOST",
		"ARGUS_NODE_WS_TOKEN",
		"ARGUS_NODE_ID",
		"ARGUS_NODE_DISPLAY_NAME",
		"ARGUS_NODE_STATE_DIR",
		"APP_HOME",
	} {
		t.Setenv(name, "")
	}
}

func TestParseConfigBuildsURLFromHostAndToken(t *testing.T) {
	clearNodeConfigEnv(t)

	cfg, err := ParseConfig([]string{
		"--host", "91.103.121.64:8080",
		"--token", "argus-node-v1.session.sig",
	})
	if err != nil {
		t.Fatal(err)
	}

	want := "ws://91.103.121.64:8080/nodes/ws?token=argus-node-v1.session.sig"
	if cfg.URL != want {
		t.Fatalf("URL = %q, want %q", cfg.URL, want)
	}
}

func TestParseConfigBuildsURLFromGatewayAndToken(t *testing.T) {
	clearNodeConfigEnv(t)

	cfg, err := ParseConfig([]string{
		"--gateway", "https://argus.example.com/base",
		"--token", "abc123",
	})
	if err != nil {
		t.Fatal(err)
	}

	want := "wss://argus.example.com/nodes/ws?token=abc123"
	if cfg.URL != want {
		t.Fatalf("URL = %q, want %q", cfg.URL, want)
	}
}

func TestParseConfigURLFlagStillWins(t *testing.T) {
	clearNodeConfigEnv(t)

	cfg, err := ParseConfig([]string{
		"--url", "ws://legacy.example.com/nodes/ws?token=legacy",
		"--gateway", "https://argus.example.com",
		"--token", "abc123",
	})
	if err != nil {
		t.Fatal(err)
	}

	want := "ws://legacy.example.com/nodes/ws?token=legacy"
	if cfg.URL != want {
		t.Fatalf("URL = %q, want %q", cfg.URL, want)
	}
}

func TestParseConfigGatewayRequiresToken(t *testing.T) {
	clearNodeConfigEnv(t)

	_, err := ParseConfig([]string{"--gateway", "https://argus.example.com"})
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(err.Error(), "missing --token") {
		t.Fatalf("error = %q, want missing --token", err.Error())
	}
}
