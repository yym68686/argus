FROM golang:1.26-alpine AS node-host-builder

WORKDIR /src/apps/node-host
COPY apps/node-host/ ./
ARG ARGUS_BUILD_HOST_AGENT_DIST=0
RUN set -eu; \
    mkdir -p /out/host-agent-dist; \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags "-s -w" -o /out/argus ./cmd/argus; \
    cp /out/argus /out/host-agent-dist/argus-linux-amd64; \
    if [ "${ARGUS_BUILD_HOST_AGENT_DIST}" = "1" ]; then \
      CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags "-s -w" -o /out/host-agent-dist/argus-linux-arm64 ./cmd/argus; \
      CGO_ENABLED=0 GOOS=darwin GOARCH=amd64 go build -trimpath -ldflags "-s -w" -o /out/host-agent-dist/argus-darwin-amd64 ./cmd/argus; \
      CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 go build -trimpath -ldflags "-s -w" -o /out/host-agent-dist/argus-darwin-arm64 ./cmd/argus; \
      CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -trimpath -ldflags "-s -w" -o /out/host-agent-dist/argus-windows-amd64.exe ./cmd/argus; \
      CGO_ENABLED=0 GOOS=windows GOARCH=arm64 go build -trimpath -ldflags "-s -w" -o /out/host-agent-dist/argus-windows-arm64.exe ./cmd/argus; \
    fi

FROM node:22-trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
  binutils \
  bubblewrap \
  ca-certificates \
  curl \
  git \
  openssh-server \
  procps \
  python-is-python3 \
  python3 \
  ripgrep \
  socat \
  && rm -rf /var/lib/apt/lists/*

ARG APP_SERVER_INSTALL_CMD
RUN install_cmd="${APP_SERVER_INSTALL_CMD:-npm i -g @openai/codex}" \
    && sh -lc "$install_cmd"

ENV HOME=/workspace \
    APP_HOME=/workspace/.argus \
    APP_WORKSPACE=/workspace

RUN mkdir -p /root/.ssh /workspace/.argus /app /run/sshd \
  && chmod 700 /root/.ssh \
  && sed -i 's#^root:\([^:]*:[^:]*:[^:]*:[^:]*:\)[^:]*:\(.*\)$#root:\1/workspace:\2#' /etc/passwd \
  && printf '%s\n' \
    'Port 22' \
    'ListenAddress 0.0.0.0' \
    'PasswordAuthentication no' \
    'KbdInteractiveAuthentication no' \
    'ChallengeResponseAuthentication no' \
    'PermitRootLogin prohibit-password' \
    'PubkeyAuthentication yes' \
    'AuthorizedKeysFile /root/.ssh/authorized_keys' \
    'AllowUsers root' \
    'X11Forwarding no' \
    'AllowTcpForwarding no' \
    'PrintMotd no' \
    'UsePAM no' \
    'PidFile /run/sshd.pid' \
    > /etc/ssh/sshd_config

COPY VERSION /app/VERSION
COPY docs/templates /app/docs/templates

COPY app_server_tcp_bridge.py /app/app_server_tcp_bridge.py
COPY run_app_server.sh /app/run_app_server.sh
COPY start_runtime.sh /app/start_runtime.sh
RUN chmod +x /app/app_server_tcp_bridge.py /app/run_app_server.sh /app/start_runtime.sh

RUN mkdir -p /app/node-host /app/host-agent-dist
COPY --from=node-host-builder /out/argus /app/node-host/argus
COPY --from=node-host-builder /out/host-agent-dist/ /app/host-agent-dist/

WORKDIR /workspace

EXPOSE 7777
EXPOSE 22

# Expose an app-server (JSONL over stdio) as a TCP stream.
# Also starts a long-lived node-host daemon (if configured) for background job execution.
CMD ["/app/start_runtime.sh"]
