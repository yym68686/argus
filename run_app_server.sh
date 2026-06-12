#!/usr/bin/env sh

set -eu

if [ -z "${APP_SERVER_CMD:-}" ]; then
  echo "ERROR: APP_SERVER_CMD is required (the command to start your app-server)." >&2
  exit 2
fi

ARGUS_APP_SERVER_FIRST_LINE_FILE=""

cleanup_first_line_file() {
  if [ -n "${ARGUS_APP_SERVER_FIRST_LINE_FILE:-}" ]; then
    rm -f "$ARGUS_APP_SERVER_FIRST_LINE_FILE"
  fi
}

app_server_stdin_gate_enabled() {
  case "${ARGUS_APP_SERVER_STDIN_GATE:-1}" in
    0 | false | FALSE | no | NO | off | OFF)
      return 1
      ;;
    *)
      return 0
      ;;
  esac
}

read_first_app_server_line_with_python() {
  py_bin=""
  if command -v python3 >/dev/null 2>&1; then
    py_bin="python3"
  elif command -v python >/dev/null 2>&1; then
    py_bin="python"
  else
    return 127
  fi

  "$py_bin" -c '
import os
import re
import select
import sys
import time

raw_timeout = sys.argv[1]
out_path = sys.argv[2]
match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([smhd]?)", raw_timeout)
if not match:
    print("ERROR: invalid ARGUS_APP_SERVER_FIRST_LINE_TIMEOUT", file=sys.stderr)
    sys.exit(2)
timeout_s = float(match.group(1))
unit = match.group(2)
if unit == "m":
    timeout_s *= 60
elif unit == "h":
    timeout_s *= 3600
elif unit == "d":
    timeout_s *= 86400
fd = sys.stdin.fileno()
deadline = time.monotonic() + timeout_s
chunks = []
while True:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        sys.exit(3)
    ready, _, _ = select.select([fd], [], [], remaining)
    if not ready:
        sys.exit(3)
    data = os.read(fd, 1)
    if not data:
        break
    chunks.append(data)
    if data == b"\n":
        break
    if len(chunks) > 1024 * 1024:
        print("ERROR: first JSONL line exceeds 1MiB before app-server start", file=sys.stderr)
        sys.exit(2)
if not chunks:
    sys.exit(3)
with open(out_path, "wb") as f:
    f.write(b"".join(chunks))
' "$ARGUS_APP_SERVER_FIRST_LINE_TIMEOUT" "$ARGUS_APP_SERVER_FIRST_LINE_FILE"
}

read_first_app_server_line_with_timeout() {
  if ! command -v timeout >/dev/null 2>&1; then
    return 127
  fi
  timeout "$ARGUS_APP_SERVER_FIRST_LINE_TIMEOUT" sh -c 'IFS= read -r line || exit 3; printf "%s\n" "$line" > "$1"' sh "$ARGUS_APP_SERVER_FIRST_LINE_FILE"
}

if app_server_stdin_gate_enabled; then
  ARGUS_APP_SERVER_FIRST_LINE_TIMEOUT="${ARGUS_APP_SERVER_FIRST_LINE_TIMEOUT:-10s}"
  case "$ARGUS_APP_SERVER_FIRST_LINE_TIMEOUT" in
    "" | *[!0123456789.smhd]*)
      echo "ERROR: ARGUS_APP_SERVER_FIRST_LINE_TIMEOUT must be a timeout like 10s, 0.5s, 1m, or 1h." >&2
      exit 2
      ;;
  esac

  ARGUS_APP_SERVER_FIRST_LINE_FILE="$(mktemp /tmp/argus-app-server-first-line.XXXXXX)"
  trap cleanup_first_line_file EXIT

  if read_first_app_server_line_with_python; then
    :
  else
    gate_rc=$?
    if [ "$gate_rc" -eq 127 ]; then
      if read_first_app_server_line_with_timeout; then
        :
      else
        gate_rc=$?
        if [ "$gate_rc" -eq 127 ]; then
          echo "WARNING: python and timeout are unavailable; ARGUS_APP_SERVER_STDIN_GATE is disabled." >&2
        elif [ "$gate_rc" -eq 2 ]; then
          exit 2
        else
          exit 0
        fi
      fi
    elif [ "$gate_rc" -eq 2 ]; then
      exit 2
    else
      exit 0
    fi
  fi

  if [ -n "${ARGUS_APP_SERVER_FIRST_LINE_FILE:-}" ] && [ -s "$ARGUS_APP_SERVER_FIRST_LINE_FILE" ]; then
    if ! grep -q '^[[:space:]]*{' "$ARGUS_APP_SERVER_FIRST_LINE_FILE"; then
      exit 0
    fi
  fi
fi

APP_HOME_DIR="${APP_HOME:-/root/.argus}"
APP_WORKSPACE_DIR="${APP_WORKSPACE:-/workspace}"

mkdir -p "$APP_HOME_DIR" "$APP_WORKSPACE_DIR"

TEMPLATE_DIR="/app/docs/templates"

render_workspace_file_from_template() {
  src="$1"
  dst="$2"

  if [ -f "$src" ]; then
    # Strip YAML front matter if present (--- ... --- at top).
    awk 'NR==1{if($0=="---"){fm=1; next}} fm==1{if($0=="---"){fm=0; next} next} {print}' "$src"
  else
    printf '# %s\n' "$(basename "$dst")"
  fi
}

write_workspace_file_from_template() {
  src="$1"
  dst="$2"

  if [ -f "$dst" ] && render_workspace_file_from_template "$src" "$dst" | cmp -s - "$dst"; then
    return 0
  fi

  tmp="$(mktemp "${dst}.tmp.XXXXXX")"
  if render_workspace_file_from_template "$src" "$dst" > "$tmp"; then
    mv -f "$tmp" "$dst"
    return 0
  fi

  status=$?
  rm -f "$tmp"
  return "$status"
}

bootstrap_workspace_file() {
  src="$1"
  dst="$2"

  if [ -e "$dst" ]; then
    return 0
  fi

  write_workspace_file_from_template "$src" "$dst"
}

sync_workspace_file() {
  src="$1"
  dst="$2"

  write_workspace_file_from_template "$src" "$dst"
}

# Sync AGENTS.md from the template on every start; bootstrap the rest if missing.
sync_workspace_file "$TEMPLATE_DIR/AGENTS.default.md" "$APP_WORKSPACE_DIR/AGENTS.md"
bootstrap_workspace_file "$TEMPLATE_DIR/SOUL.md" "$APP_WORKSPACE_DIR/SOUL.md"
bootstrap_workspace_file "$TEMPLATE_DIR/USER.md" "$APP_WORKSPACE_DIR/USER.md"
bootstrap_workspace_file "$TEMPLATE_DIR/HEARTBEAT.md" "$APP_WORKSPACE_DIR/HEARTBEAT.md"

# Scope Codex state (threads/history, config, etc.) to the workspace by default.
# This avoids cross-session leakage when multiple runtimes share the same host.
CODEX_HOME_DIR="${CODEX_HOME:-$APP_WORKSPACE_DIR/.codex}"
export CODEX_HOME="$CODEX_HOME_DIR"
ARGUS_CODEX_MODEL_FILE="${ARGUS_CODEX_MODEL_FILE:-$CODEX_HOME_DIR/argus-agent-model}"

export HOME="$APP_HOME_DIR"

normalize_argus_codex_model() {
  value="$(printf '%s' "$1" | tr -d '\r\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  if [ -z "$value" ]; then
    printf '%s' "gpt-5.4"
    return 0
  fi
  if [ "${#value}" -gt 128 ]; then
    printf '%s' "gpt-5.4"
    return 0
  fi
  case "$value" in
    *[!A-Za-z0-9._:/+-]*)
      printf '%s' "gpt-5.4"
      ;;
    *)
      printf '%s' "$value"
      ;;
  esac
}

CODEX_MODEL_RAW="${ARGUS_CODEX_MODEL:-}"
if [ -f "$ARGUS_CODEX_MODEL_FILE" ]; then
  CODEX_MODEL_RAW="$(cat "$ARGUS_CODEX_MODEL_FILE")"
fi
CODEX_MODEL="$(normalize_argus_codex_model "$CODEX_MODEL_RAW")"

# Seed Codex config in $CODEX_HOME/config.toml (no secrets).
# Note: this overwrites any existing file.
if command -v codex >/dev/null 2>&1; then
  mkdir -p "$CODEX_HOME_DIR"
  printf '%s\n' "$CODEX_MODEL" > "$ARGUS_CODEX_MODEL_FILE"

  tmp_cfg="$(mktemp "$CODEX_HOME_DIR/config.toml.XXXXXX")"

  cat > "$tmp_cfg" <<EOF
# Generated by Argus runtime (no secrets).
model = "$CODEX_MODEL"
model_reasoning_effort = "high"
EOF

  # NOTE: In TOML, keys defined after a table header (e.g. [mcp_servers.argus])
  # belong to that table until another table header appears. Keep top-level keys
  # (like model_provider) above any table blocks.
  if [ -n "${ARGUS_CODEX_OPENAI_PROXY_BASE_URL:-}" ]; then
    cat >> "$tmp_cfg" <<EOF

model_provider = "OpenAI"
EOF
  fi

  cat >> "$tmp_cfg" <<EOF

[mcp_servers.argus]
url = "${ARGUS_CODEX_MCP_URL:-http://gateway:8080/mcp}"
bearer_token_env_var = "ARGUS_MCP_TOKEN"
EOF

  if [ -n "${ARGUS_CODEX_OPENAI_PROXY_BASE_URL:-}" ]; then
    cat >> "$tmp_cfg" <<EOF

[model_providers.OpenAI]
name = "OpenAI"
base_url = "${ARGUS_CODEX_OPENAI_PROXY_BASE_URL}"
wire_api = "responses"
env_key = "ARGUS_OPENAI_TOKEN"
requires_openai_auth = false
supports_websockets = false
EOF
  fi

  mv -f "$tmp_cfg" "$CODEX_HOME_DIR/config.toml"
fi

cd "$APP_WORKSPACE_DIR"

if [ -n "${ARGUS_APP_SERVER_FIRST_LINE_FILE:-}" ]; then
  {
    cat "$ARGUS_APP_SERVER_FIRST_LINE_FILE"
    cat
  } | sh -lc "$APP_SERVER_CMD"
  exit "$?"
fi

exec sh -lc "$APP_SERVER_CMD"
