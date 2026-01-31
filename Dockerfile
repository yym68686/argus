FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
  ca-certificates \
  socat \
  && rm -rf /var/lib/apt/lists/*

# Installs Codex CLI from npm (no local source build).
RUN npm install -g @openai/codex && npm cache clean --force

ENV CODEX_HOME=/codex-home

RUN mkdir -p /codex-home /workspace \
  && chown -R node:node /codex-home /workspace

USER node
WORKDIR /workspace

EXPOSE 7777

# Expose `codex app-server` (JSONL over stdio) as a TCP stream.
CMD ["sh","-lc","socat TCP-LISTEN:7777,reuseaddr,fork EXEC:'codex app-server',stderr"]

