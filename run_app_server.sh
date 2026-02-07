#!/usr/bin/env sh

set -eu

if [ -z "${APP_SERVER_CMD:-}" ]; then
  echo "ERROR: APP_SERVER_CMD is required (the command to start your app-server)." >&2
  exit 2
fi

APP_HOME_DIR="${APP_HOME:-/root/.argus}"
APP_WORKSPACE_DIR="${APP_WORKSPACE:-$APP_HOME_DIR/workspace}"

mkdir -p "$APP_HOME_DIR" "$APP_WORKSPACE_DIR"

TEMPLATE_DIR="/app/docs/templates"

bootstrap_workspace_file() {
  src="$1"
  dst="$2"

  if [ -e "$dst" ]; then
    return 0
  fi

  if [ -f "$src" ]; then
    # Strip YAML front matter if present (--- ... --- at top).
    awk 'NR==1{if($0=="---"){fm=1; next}} fm==1{if($0=="---"){fm=0; next} next} {print}' "$src" > "$dst"
  else
    printf '# %s\n' "$(basename "$dst")" > "$dst"
  fi
}

# Bootstrap "OpenClaw-style" workspace context files (do not overwrite if already present).
bootstrap_workspace_file "$TEMPLATE_DIR/SOUL.md" "$APP_WORKSPACE_DIR/SOUL.md"
bootstrap_workspace_file "$TEMPLATE_DIR/USER.md" "$APP_WORKSPACE_DIR/USER.md"
bootstrap_workspace_file "$TEMPLATE_DIR/AGENTS.default.md" "$APP_WORKSPACE_DIR/AGENTS.md"
bootstrap_workspace_file "$TEMPLATE_DIR/HEARTBEAT.md" "$APP_WORKSPACE_DIR/HEARTBEAT.md"

export HOME="$APP_HOME_DIR"

cd "$APP_WORKSPACE_DIR"

exec sh -lc "$APP_SERVER_CMD"
