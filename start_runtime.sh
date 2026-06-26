#!/usr/bin/env sh
set -eu

NODE_PID=""
BRIDGE_PID=""
SSHD_PID=""

cleanup() {
  if [ -n "$BRIDGE_PID" ]; then
    kill -TERM "$BRIDGE_PID" 2>/dev/null || true
  fi
  if [ -n "$NODE_PID" ]; then
    kill -TERM "$NODE_PID" 2>/dev/null || true
  fi
  if [ -n "$SSHD_PID" ]; then
    kill -TERM "$SSHD_PID" 2>/dev/null || true
  fi
  wait 2>/dev/null || true
}

trap cleanup TERM INT

if command -v sshd >/dev/null 2>&1; then
  mkdir -p /run/sshd /root/.ssh /workspace/.argus
  chmod 700 /root/.ssh 2>/dev/null || true
  ssh-keygen -A >/dev/null 2>&1 || true
  if [ -n "${FUGUE_SSH_SESSION_ENV_CONFIG:-}" ] && [ -f "$FUGUE_SSH_SESSION_ENV_CONFIG" ]; then
    if ! grep -Fqx "Include $FUGUE_SSH_SESSION_ENV_CONFIG" /etc/ssh/sshd_config 2>/dev/null; then
      printf '\nInclude %s\n' "$FUGUE_SSH_SESSION_ENV_CONFIG" >> /etc/ssh/sshd_config
    fi
  fi
  /usr/sbin/sshd -t
  /usr/sbin/sshd -D -e &
  SSHD_PID=$!
fi

if [ -n "${ARGUS_NODE_WS_URL:-}" ]; then
  /app/node-host/argus &
  NODE_PID=$!
fi

/app/app_server_tcp_bridge.py &
BRIDGE_PID=$!

wait "$BRIDGE_PID"
status=$?
cleanup
exit "$status"
