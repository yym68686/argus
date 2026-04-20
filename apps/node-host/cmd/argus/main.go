package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"strings"

	"github.com/yym68686/argus/apps/node-host/internal/hostagent"
	"github.com/yym68686/argus/apps/node-host/internal/jobstore"
	"github.com/yym68686/argus/apps/node-host/internal/nodehost"
	"github.com/yym68686/argus/apps/node-host/internal/runtimehost"
	"github.com/yym68686/argus/apps/node-host/internal/version"
)

type claimResponse struct {
	OK             bool           `json:"ok"`
	HostID         string         `json:"hostId"`
	DisplayName    string         `json:"displayName"`
	Token          string         `json:"token"`
	WSPath         string         `json:"wsPath"`
	GatewayBaseURL string         `json:"gatewayBaseUrl"`
	Binding        map[string]any `json:"binding"`
}

type selfStatusResponse struct {
	OK   bool           `json:"ok"`
	Host map[string]any `json:"host"`
}

func main() {
	if err := dispatch(os.Args[1:]); err != nil && !errors.Is(err, context.Canceled) {
		fmt.Fprintln(os.Stderr, err.Error())
		os.Exit(1)
	}
}

func dispatch(args []string) error {
	if len(args) == 0 {
		return runLegacyNode(nil)
	}
	switch args[0] {
	case "connect":
		return runConnect(args[1:])
	case "daemon":
		return runDaemon(args[1:])
	case "status":
		return runStatus(args[1:])
	case "disconnect":
		return runDisconnect(args[1:])
	case "legacy-node":
		return runLegacyNode(args[1:])
	case "legacy-runtime-host":
		return runLegacyRuntimeHost(args[1:])
	case "help", "--help", "-h":
		printUsage()
		return nil
	default:
		if strings.HasPrefix(args[0], "-") {
			return runLegacyNode(args)
		}
		printUsage()
		return fmt.Errorf("unknown subcommand: %s", args[0])
	}
}

func printUsage() {
	fmt.Fprintf(os.Stderr, `argus %s

Usage:
  argus connect --gateway <url> --enroll-token <token> [--default]
  argus daemon [--config <path>]
  argus status [--config <path>]
  argus disconnect [--config <path>]

Legacy compatibility:
  argus --url <nodes-ws-url> --node-id <id>
  argus legacy-runtime-host --url <runtime-host-ws-url> --host-id <id>
`, version.Current())
}

func defaultConfigPath() string {
	return hostagent.DefaultConfigPath()
}

func runLegacyNode(args []string) error {
	cfg, err := nodehost.ParseConfig(args)
	if err != nil {
		return err
	}

	invoker := &nodehost.CommandInvoker{}
	host := nodehost.New(cfg, invoker)

	store, err := jobstore.New(cfg.StateDir, cfg.NodeID, host.SendEvent)
	if err != nil {
		return err
	}
	invoker.Store = store

	ctx, stop := signal.NotifyContext(context.Background(), shutdownSignals()...)
	defer stop()
	return host.Run(ctx)
}

func runLegacyRuntimeHost(args []string) error {
	cfg, err := runtimehost.ParseConfig(args)
	if err != nil {
		return err
	}
	host := runtimehost.New(cfg)
	ctx, stop := signal.NotifyContext(context.Background(), shutdownSignals()...)
	defer stop()
	return host.Run(ctx)
}

func runDaemon(args []string) error {
	fs := flag.NewFlagSet("daemon", flag.ContinueOnError)
	configPath := fs.String("config", defaultConfigPath(), "host-agent config path")
	if err := fs.Parse(args); err != nil {
		return err
	}
	return runStoredHostAgent(*configPath)
}

func runConnect(args []string) error {
	fs := flag.NewFlagSet("connect", flag.ContinueOnError)
	gateway := fs.String("gateway", os.Getenv("ARGUS_GATEWAY_URL"), "gateway base URL")
	enrollToken := fs.String("enroll-token", "", "one-time enrollment token")
	configPath := fs.String("config", defaultConfigPath(), "host-agent config path")
	hostID := fs.String("host-id", "", "host id")
	displayName := fs.String("display-name", "", "display name")
	workspaceBase := fs.String("workspace-base", "", "workspace base path")
	codexHomeBase := fs.String("codex-home-base", "", "Codex home overlay base path")
	appServerCmd := fs.String("app-server-cmd", "", "app-server command")
	setDefault := fs.Bool("default", false, "set this host as the default native host binding")
	scopeType := fs.String("scope-type", "", "binding scope type: global|user|agent")
	scopeID := fs.String("scope-id", "", "binding scope id")
	claimOnly := fs.Bool("claim-only", false, "claim and save config without starting the daemon")
	if err := fs.Parse(args); err != nil {
		return err
	}

	if strings.TrimSpace(*gateway) == "" {
		return fmt.Errorf("missing --gateway")
	}
	if strings.TrimSpace(*enrollToken) == "" {
		return fmt.Errorf("missing --enroll-token")
	}
	defaultHostID, defaultDisplayName := hostagent.DefaultHostIdentity()
	if strings.TrimSpace(*hostID) == "" {
		*hostID = defaultHostID
	}
	if strings.TrimSpace(*displayName) == "" {
		*displayName = defaultDisplayName
	}
	if strings.TrimSpace(*workspaceBase) == "" {
		*workspaceBase = hostagent.DefaultWorkspaceBasePath()
	}
	if strings.TrimSpace(*codexHomeBase) == "" {
		*codexHomeBase = hostagent.DefaultCodexHomeBasePath()
	}
	if strings.TrimSpace(*appServerCmd) == "" {
		*appServerCmd = hostagent.DefaultAppServerCmd()
	}
	if strings.HasPrefix(strings.TrimSpace(*appServerCmd), "codex ") || strings.TrimSpace(*appServerCmd) == "codex" {
		if _, err := exec.LookPath("codex"); err != nil {
			return fmt.Errorf("codex is not installed or not in PATH")
		}
	}

	scopeTypeValue := strings.TrimSpace(*scopeType)
	scopeIDValue := strings.TrimSpace(*scopeID)
	if *setDefault && scopeTypeValue == "" {
		scopeTypeValue = "global"
	}
	if scopeTypeValue == "global" && scopeIDValue == "" {
		scopeIDValue = "default"
	}

	body := map[string]any{
		"enrollToken":       strings.TrimSpace(*enrollToken),
		"hostId":            strings.TrimSpace(*hostID),
		"displayName":       strings.TrimSpace(*displayName),
		"platform":          hostagent.PlatformName(),
		"version":           version.Current(),
		"setDefault":        *setDefault,
		"scopeType":         scopeTypeValue,
		"scopeId":           scopeIDValue,
		"workspaceBasePath": strings.TrimSpace(*workspaceBase),
	}
	var resp claimResponse
	if err := doJSONRequest(http.MethodPost, strings.TrimRight(strings.TrimSpace(*gateway), "/")+"/host-agent/claim", "", body, &resp); err != nil {
		return err
	}
	if !resp.OK || strings.TrimSpace(resp.Token) == "" {
		return fmt.Errorf("host-agent claim failed")
	}

	stored := &hostagent.StoredConfig{
		GatewayBaseURL:    strings.TrimRight(strings.TrimSpace(*gateway), "/"),
		HostID:            firstNonEmpty(resp.HostID, strings.TrimSpace(*hostID)),
		DisplayName:       firstNonEmpty(resp.DisplayName, strings.TrimSpace(*displayName)),
		DeviceToken:       strings.TrimSpace(resp.Token),
		WorkspaceBasePath: strings.TrimSpace(*workspaceBase),
		CodexHomeBasePath: strings.TrimSpace(*codexHomeBase),
		StateDir:          hostagent.DefaultStateDir(firstNonEmpty(resp.HostID, strings.TrimSpace(*hostID))),
		AppServerCmd:      strings.TrimSpace(*appServerCmd),
	}
	if err := hostagent.SaveStoredConfig(*configPath, stored); err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "Saved host-agent config to %s\n", *configPath)
	if *claimOnly {
		return nil
	}
	return runStoredHostAgent(*configPath)
}

func runStatus(args []string) error {
	fs := flag.NewFlagSet("status", flag.ContinueOnError)
	configPath := fs.String("config", defaultConfigPath(), "host-agent config path")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg, err := hostagent.LoadStoredConfig(*configPath)
	if err != nil {
		return err
	}
	var resp selfStatusResponse
	if err := doJSONRequest(http.MethodGet, strings.TrimRight(cfg.GatewayBaseURL, "/")+"/host-agent/self", cfg.DeviceToken, nil, &resp); err != nil {
		return err
	}
	if !resp.OK {
		return fmt.Errorf("failed to fetch host-agent status")
	}
	payload, err := json.MarshalIndent(resp.Host, "", "  ")
	if err != nil {
		return err
	}
	fmt.Printf("%s\n", string(payload))
	return nil
}

func runDisconnect(args []string) error {
	fs := flag.NewFlagSet("disconnect", flag.ContinueOnError)
	configPath := fs.String("config", defaultConfigPath(), "host-agent config path")
	keepConfig := fs.Bool("keep-config", false, "do not remove the local config after revoking")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg, err := hostagent.LoadStoredConfig(*configPath)
	if err != nil {
		return err
	}
	if err := doJSONRequest(http.MethodPost, strings.TrimRight(cfg.GatewayBaseURL, "/")+"/host-agent/revoke", cfg.DeviceToken, map[string]any{}, nil); err != nil {
		return err
	}
	if !*keepConfig {
		if err := os.Remove(*configPath); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	fmt.Fprintf(os.Stderr, "Disconnected host %s\n", cfg.HostID)
	return nil
}

func runStoredHostAgent(configPath string) error {
	stored, err := hostagent.LoadStoredConfig(configPath)
	if err != nil {
		return err
	}
	cfg, err := stored.RuntimeConfig()
	if err != nil {
		return err
	}
	invoker := &nodehost.CommandInvoker{}
	host := hostagent.New(cfg, invoker)
	store, err := jobstore.New(cfg.StateDir, cfg.HostID, host.SendEvent)
	if err != nil {
		return err
	}
	invoker.Store = store
	ctx, stop := signal.NotifyContext(context.Background(), shutdownSignals()...)
	defer stop()
	return host.Run(ctx)
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func doJSONRequest(method string, rawURL string, bearerToken string, body any, out any) error {
	var payload ioReader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return err
		}
		payload = bytes.NewReader(data)
	}
	req, err := http.NewRequest(method, rawURL, payload)
	if err != nil {
		return err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if strings.TrimSpace(bearerToken) != "" {
		req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(bearerToken))
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		var raw map[string]any
		_ = json.NewDecoder(resp.Body).Decode(&raw)
		if detail, ok := raw["detail"].(string); ok && strings.TrimSpace(detail) != "" {
			return fmt.Errorf("%s", detail)
		}
		return fmt.Errorf("unexpected status %d", resp.StatusCode)
	}
	if out == nil {
		return nil
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

type ioReader interface {
	Read(p []byte) (n int, err error)
}
