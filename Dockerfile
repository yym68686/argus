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

ENV APP_HOME=/agent-home

RUN mkdir -p /agent-home /workspace /app \
  && chown -R node:node /agent-home /workspace /app

COPY run_app_server.sh /app/run_app_server.sh
RUN chmod +x /app/run_app_server.sh

USER node
WORKDIR /workspace

EXPOSE 7777

# Expose an app-server (JSONL over stdio) as a TCP stream.
CMD ["sh","-lc","socat TCP-LISTEN:7777,reuseaddr,fork EXEC:'/app/run_app_server.sh',stderr"]
