package main

import (
	"testing"
	"time"

	"github.com/yym68686/argus/apps/node-host/internal/nodehost"
)

func TestLegacyNodeStoredConfigRoundTripPreservesNodeSettings(t *testing.T) {
	cfg := &nodehost.Config{
		URL:                    "ws://example.com/nodes/ws?token=secret",
		NodeID:                 "mac",
		DisplayName:            "Mac",
		StateDir:               "/tmp/argus-node",
		AuditEnabled:           false,
		AuditMaxBytes:          1234,
		AuditStdinPreviewBytes: 0,
		WSHandshakeTimeout:     0,
		WSConnectTimeout:       21 * time.Second,
		ReconnectDelayBase:     2 * time.Second,
		ReconnectDelayMax:      40 * time.Second,
		ReconnectJitterPct:     0.3,
		HeartbeatInterval:      16 * time.Second,
		PingInterval:           0,
		PongTimeout:            11 * time.Second,
		WriteTimeout:           12 * time.Second,
	}

	stored := legacyNodeStoredFromConfig(cfg, "/tmp/argus-node.log")
	got := stored.RuntimeConfig()

	if got.URL != cfg.URL || got.NodeID != cfg.NodeID || got.DisplayName != cfg.DisplayName || got.StateDir != cfg.StateDir {
		t.Fatalf("basic config did not round-trip: %#v", got)
	}
	if got.AuditEnabled != cfg.AuditEnabled || got.AuditMaxBytes != cfg.AuditMaxBytes || got.AuditStdinPreviewBytes != cfg.AuditStdinPreviewBytes {
		t.Fatalf("audit config did not round-trip: %#v", got)
	}
	if got.WSHandshakeTimeout != 0 || got.PingInterval != 0 {
		t.Fatalf("zero-valued timeout settings were not preserved: handshake=%s ping=%s", got.WSHandshakeTimeout, got.PingInterval)
	}
	if got.WSConnectTimeout != cfg.WSConnectTimeout || got.ReconnectDelayBase != cfg.ReconnectDelayBase || got.ReconnectDelayMax != cfg.ReconnectDelayMax {
		t.Fatalf("duration config did not round-trip: %#v", got)
	}
}

func TestSplitLegacyNodeControlArgsSupportsForeground(t *testing.T) {
	args, foreground, err := splitLegacyNodeControlArgs([]string{"--foreground", "--host", "127.0.0.1:8080", "--token", "abc"})
	if err != nil {
		t.Fatal(err)
	}
	if !foreground {
		t.Fatal("foreground = false, want true")
	}
	if want := []string{"--host", "127.0.0.1:8080", "--token", "abc"}; len(args) != len(want) {
		t.Fatalf("args = %#v, want %#v", args, want)
	} else {
		for i := range want {
			if args[i] != want[i] {
				t.Fatalf("args = %#v, want %#v", args, want)
			}
		}
	}
}

func TestLastLogLines(t *testing.T) {
	got := lastLogLines("a\nb\nc\n", 2)
	if got != "b\nc\n" {
		t.Fatalf("lastLogLines = %q", got)
	}
}
