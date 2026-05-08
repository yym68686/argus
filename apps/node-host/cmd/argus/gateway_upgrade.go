package main

import (
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/yym68686/argus/apps/node-host/internal/hostagent"
)

func runGateway(args []string) error {
	if len(args) == 0 {
		printGatewayUsage()
		return fmt.Errorf("missing gateway subcommand")
	}
	switch args[0] {
	case "upgrade", "update":
		return runGatewayUpgrade(args[1:])
	case "help", "--help", "-h":
		printGatewayUsage()
		return nil
	default:
		printGatewayUsage()
		return fmt.Errorf("unknown gateway subcommand: %s", args[0])
	}
}

func printGatewayUsage() {
	fmt.Fprintf(os.Stderr, `Usage:
  argus gateway upgrade [--dir <repo>] [--profile tg]

Updates a local Argus gateway checkout and rebuilt Docker services:
  git pull --ff-only
  docker compose down
  docker rm -f <managed runtime containers>
  docker compose --profile tg up --build -d
`)
}

func runGatewayUpgrade(args []string) error {
	fs := flagSet("gateway upgrade")
	dirFlag := fs.String("dir", os.Getenv("ARGUS_GATEWAY_DIR"), "Argus gateway repo directory")
	profiles := fs.String("profile", defaultGatewayProfiles(), "comma/space-separated docker compose profiles; empty disables profiles")
	sourceBashrc := fs.Bool("source-bashrc", true, "source ~/.bashrc before running docker compose")
	removeRuntimeContainers := fs.Bool("remove-runtime-containers", true, "remove managed runtime containers after compose down")
	installCLI := fs.Bool("install-cli", true, "refresh the local argus CLI from the rebuilt runtime image")
	runtimeImage := fs.String("runtime-image", "argus-runtime", "runtime image containing /app/node-host/argus")
	if err := fs.Parse(args); err != nil {
		return err
	}

	repoDir, err := resolveGatewayRepoDir(*dirFlag)
	if err != nil {
		return err
	}
	currentBinary := ""
	if path, err := hostagent.CurrentBinaryPath(); err == nil {
		currentBinary = path
	}

	if _, err := exec.LookPath("bash"); err != nil {
		return fmt.Errorf("bash is required for gateway upgrade")
	}
	cmd := exec.Command("bash", "-lc", gatewayUpgradeShellScript())
	cmd.Env = append(os.Environ(),
		"ARGUS_GATEWAY_UPGRADE_DIR="+repoDir,
		"ARGUS_GATEWAY_UPGRADE_PROFILES="+strings.TrimSpace(*profiles),
		"ARGUS_GATEWAY_UPGRADE_SOURCE_BASHRC="+boolEnv(*sourceBashrc),
		"ARGUS_GATEWAY_UPGRADE_REMOVE_RUNTIME="+boolEnv(*removeRuntimeContainers),
		"ARGUS_GATEWAY_UPGRADE_INSTALL_CLI="+boolEnv(*installCLI),
		"ARGUS_GATEWAY_UPGRADE_RUNTIME_IMAGE="+strings.TrimSpace(*runtimeImage),
		"ARGUS_GATEWAY_UPGRADE_CURRENT_BINARY="+currentBinary,
	)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Stdin = os.Stdin
	return cmd.Run()
}

func flagSet(name string) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	return fs
}

func defaultGatewayProfiles() string {
	if raw := strings.TrimSpace(os.Getenv("ARGUS_GATEWAY_COMPOSE_PROFILES")); raw != "" {
		return raw
	}
	if raw := strings.TrimSpace(os.Getenv("COMPOSE_PROFILES")); raw != "" {
		return raw
	}
	return "tg"
}

func resolveGatewayRepoDir(explicit string) (string, error) {
	candidates := []string{}
	if strings.TrimSpace(explicit) != "" {
		candidates = append(candidates, explicit)
	} else {
		if cwd, err := os.Getwd(); err == nil {
			candidates = append(candidates, cwd)
		}
		if home, err := os.UserHomeDir(); err == nil && strings.TrimSpace(home) != "" {
			candidates = append(candidates, filepath.Join(home, "argus"))
		}
		candidates = append(candidates, "/root/argus", "/opt/argus", "/srv/argus")
	}
	seen := map[string]bool{}
	for _, candidate := range candidates {
		candidate = strings.TrimSpace(candidate)
		if candidate == "" {
			continue
		}
		abs, err := filepath.Abs(candidate)
		if err != nil {
			abs = candidate
		}
		if seen[abs] {
			continue
		}
		seen[abs] = true
		if isGatewayRepoDir(abs) {
			return abs, nil
		}
	}
	if strings.TrimSpace(explicit) != "" {
		return "", fmt.Errorf("not an Argus gateway repo: %s", explicit)
	}
	return "", fmt.Errorf("unable to find Argus gateway repo; pass --dir or set ARGUS_GATEWAY_DIR")
}

func isGatewayRepoDir(dir string) bool {
	for _, rel := range []string{"docker-compose.yml", "apps/api/app.py", ".git"} {
		if _, err := os.Stat(filepath.Join(dir, rel)); err != nil {
			return false
		}
	}
	return true
}

func boolEnv(v bool) string {
	if v {
		return "1"
	}
	return "0"
}

func gatewayUpgradeShellScript() string {
	return `
set -euo pipefail

if [ "${ARGUS_GATEWAY_UPGRADE_SOURCE_BASHRC:-1}" = "1" ] && [ -f "${HOME}/.bashrc" ]; then
  set +u
  # shellcheck disable=SC1090
  . "${HOME}/.bashrc"
  set -u
fi

cd "$ARGUS_GATEWAY_UPGRADE_DIR"
echo "Updating Argus gateway in $(pwd)"
git pull --ff-only

docker compose down

if [ "${ARGUS_GATEWAY_UPGRADE_REMOVE_RUNTIME:-1}" = "1" ]; then
  ids="$(docker ps -aq --filter "label=io.argus.gateway=apps/api" || true)"
  if [ -n "$ids" ]; then
    docker rm -f $ids
  fi
fi

compose_args=()
profiles="${ARGUS_GATEWAY_UPGRADE_PROFILES:-}"
profiles="${profiles//,/ }"
for profile in $profiles; do
  if [ -n "$profile" ]; then
    compose_args+=(--profile "$profile")
  fi
done

docker compose "${compose_args[@]}" up --build -d

if [ "${ARGUS_GATEWAY_UPGRADE_INSTALL_CLI:-1}" = "1" ]; then
  runtime_image="${ARGUS_GATEWAY_UPGRADE_RUNTIME_IMAGE:-argus-runtime}"
  tmp="$(mktemp)"
  cleanup() { rm -f "$tmp"; }
  trap cleanup EXIT
  docker run --rm --entrypoint cat "$runtime_image" /app/node-host/argus > "$tmp"
  chmod +x "$tmp"
  target="${ARGUS_GATEWAY_UPGRADE_CURRENT_BINARY:-}"
  if [ -z "$target" ]; then
    target="$(command -v argus || true)"
  fi
  if [ -n "$target" ]; then
    target_dir="$(dirname "$target")"
    if [ -w "$target_dir" ] && { [ ! -e "$target" ] || [ -w "$target" ]; }; then
      install -m 0755 "$tmp" "$target"
    elif command -v sudo >/dev/null 2>&1; then
      sudo install -m 0755 "$tmp" "$target"
    else
      echo "Unable to update local argus CLI at $target; sudo is not available." >&2
      exit 1
    fi
    echo "Updated local argus CLI: $target"
  else
    echo "argus CLI was not found in PATH; skipped local CLI refresh." >&2
  fi
fi

docker compose "${compose_args[@]}" ps
`
}
