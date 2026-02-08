FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
  ca-certificates \
  socat \
  && rm -rf /var/lib/apt/lists/*

ARG APP_SERVER_INSTALL_CMD
RUN if [ -z "${APP_SERVER_INSTALL_CMD:-}" ]; then \
      echo "ERROR: APP_SERVER_INSTALL_CMD is required (e.g. install your agent CLI/runtime)." >&2; \
      exit 2; \
    fi \
    && sh -lc "$APP_SERVER_INSTALL_CMD"

ENV APP_HOME=/root/.argus

RUN mkdir -p /root/.argus/workspace /app \
  && if [ -e /workspace ]; then rm -rf /workspace; fi \
  && ln -s /root/.argus/workspace /workspace

COPY docs/templates /app/docs/templates

COPY run_app_server.sh /app/run_app_server.sh
RUN chmod +x /app/run_app_server.sh

COPY apps/node-host/package.json /app/node-host/package.json
COPY apps/node-host/package-lock.json /app/node-host/package-lock.json
RUN cd /app/node-host && npm ci --omit=dev
COPY apps/node-host/index.mjs /app/node-host/index.mjs

WORKDIR /root/.argus/workspace

EXPOSE 7777

# Expose an app-server (JSONL over stdio) as a TCP stream.
# Also starts a long-lived node-host daemon (if configured) for background job execution.
CMD ["sh","-lc","set -eu; NODE_PID=\"\"; if [ -n \"${ARGUS_NODE_WS_URL:-}\" ]; then node /app/node-host/index.mjs & NODE_PID=$!; fi; socat TCP-LISTEN:7777,reuseaddr,fork EXEC:'/app/run_app_server.sh',stderr & SOCAT_PID=$!; trap 'kill -TERM $SOCAT_PID 2>/dev/null || true; if [ -n \"$NODE_PID\" ]; then kill -TERM $NODE_PID 2>/dev/null || true; fi; wait' TERM INT; wait $SOCAT_PID; if [ -n \"$NODE_PID\" ]; then kill -TERM $NODE_PID 2>/dev/null || true; wait $NODE_PID 2>/dev/null || true; fi"]
