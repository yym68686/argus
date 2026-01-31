#!/usr/bin/env sh

set -eu

if [ -z "${APP_SERVER_CMD:-}" ]; then
  echo "ERROR: APP_SERVER_CMD is required (the command to start your app-server)." >&2
  exit 2
fi

APP_HOME_DIR="${APP_HOME:-/agent-home}"
mkdir -p "$APP_HOME_DIR" /workspace

export HOME="$APP_HOME_DIR"

exec sh -lc "$APP_SERVER_CMD"

