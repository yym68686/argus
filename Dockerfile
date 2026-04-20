FROM golang:1.26-trixie AS node-host-builder

WORKDIR /src/apps/node-host
COPY apps/node-host/ ./
RUN set -eu; \
    mkdir -p /out/host-agent-dist; \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags "-s -w" -o /out/argus ./cmd/argus; \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags "-s -w" -o /out/host-agent-dist/argus-linux-amd64 ./cmd/argus; \
    CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags "-s -w" -o /out/host-agent-dist/argus-linux-arm64 ./cmd/argus; \
    CGO_ENABLED=0 GOOS=darwin GOARCH=amd64 go build -trimpath -ldflags "-s -w" -o /out/host-agent-dist/argus-darwin-amd64 ./cmd/argus; \
    CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 go build -trimpath -ldflags "-s -w" -o /out/host-agent-dist/argus-darwin-arm64 ./cmd/argus; \
    CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -trimpath -ldflags "-s -w" -o /out/host-agent-dist/argus-windows-amd64.exe ./cmd/argus; \
    CGO_ENABLED=0 GOOS=windows GOARCH=arm64 go build -trimpath -ldflags "-s -w" -o /out/host-agent-dist/argus-windows-arm64.exe ./cmd/argus

FROM node:22-trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
  binutils \
  bubblewrap \
  ca-certificates \
  curl \
  git \
  procps \
  python-is-python3 \
  python3 \
  ripgrep \
  socat \
  && rm -rf /var/lib/apt/lists/*

ARG APP_SERVER_INSTALL_CMD
RUN install_cmd="${APP_SERVER_INSTALL_CMD:-npm i -g @openai/codex}" \
    && sh -lc "$install_cmd"

ENV APP_HOME=/root/.argus \
    APP_WORKSPACE=/workspace

RUN mkdir -p /root/.argus /workspace /app

COPY VERSION /app/VERSION
COPY docs/templates /app/docs/templates

COPY run_app_server.sh /app/run_app_server.sh
RUN chmod +x /app/run_app_server.sh

RUN mkdir -p /app/node-host /app/host-agent-dist
COPY --from=node-host-builder /out/argus /app/node-host/argus
COPY --from=node-host-builder /out/host-agent-dist/ /app/host-agent-dist/

WORKDIR /workspace

EXPOSE 7777

# Expose an app-server (JSONL over stdio) as a TCP stream.
# Also starts a long-lived node-host daemon (if configured) for background job execution.
CMD ["sh","-lc","set -eu; NODE_PID=\"\"; if [ -n \"${ARGUS_NODE_WS_URL:-}\" ]; then /app/node-host/argus & NODE_PID=$!; fi; socat TCP-LISTEN:7777,reuseaddr,fork EXEC:'/app/run_app_server.sh',stderr & SOCAT_PID=$!; trap 'kill -TERM $SOCAT_PID 2>/dev/null || true; if [ -n \"$NODE_PID\" ]; then kill -TERM $NODE_PID 2>/dev/null || true; fi; wait' TERM INT; wait $SOCAT_PID; if [ -n \"$NODE_PID\" ]; then kill -TERM $NODE_PID 2>/dev/null || true; wait $NODE_PID 2>/dev/null || true; fi"]
