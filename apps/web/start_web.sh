#!/usr/bin/env sh
set -eu

if command -v sshd >/dev/null 2>&1; then
  mkdir -p /run/sshd /root/.ssh
  chmod 700 /root/.ssh 2>/dev/null || true
  ssh-keygen -A >/dev/null 2>&1 || true
  if [ -n "${FUGUE_SSH_SESSION_ENV_CONFIG:-}" ] && [ -f "$FUGUE_SSH_SESSION_ENV_CONFIG" ]; then
    if ! grep -Fqx "Include $FUGUE_SSH_SESSION_ENV_CONFIG" /etc/ssh/sshd_config 2>/dev/null; then
      printf '\nInclude %s\n' "$FUGUE_SSH_SESSION_ENV_CONFIG" >> /etc/ssh/sshd_config
    fi
  fi
  sshd -t
  /usr/sbin/sshd -D -e &
fi

exec /docker-entrypoint.sh nginx -g 'daemon off;'
