package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestResolveGatewayRepoDirUsesExplicitRepo(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "apps", "api"), 0o755); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{
		filepath.Join(root, ".git"),
		filepath.Join(root, "docker-compose.yml"),
		filepath.Join(root, "apps", "api", "app.py"),
	} {
		if strings.HasSuffix(path, ".git") {
			if err := os.Mkdir(path, 0o755); err != nil {
				t.Fatal(err)
			}
			continue
		}
		if err := os.WriteFile(path, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	resolved, err := resolveGatewayRepoDir(root)
	if err != nil {
		t.Fatal(err)
	}
	if resolved != root {
		t.Fatalf("resolved = %q, want %q", resolved, root)
	}
}

func TestGatewayUpgradeShellScriptIncludesManagedRuntimeCleanup(t *testing.T) {
	script := gatewayUpgradeShellScript()
	for _, want := range []string{
		"git pull --ff-only",
		"docker compose down",
		`docker ps -aq --filter "label=io.argus.gateway=apps/api"`,
		`docker compose "${compose_args[@]}" up --build -d`,
		"/app/node-host/argus",
	} {
		if !strings.Contains(script, want) {
			t.Fatalf("script does not contain %q", want)
		}
	}
}
