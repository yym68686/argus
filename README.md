# Argus — Self-hosted Assistant Gateway (Telegram-first)

[![Telegram user group](https://img.shields.io/badge/Telegram-Join%20the%20community-2CA5E0?logo=telegram&logoColor=white)](https://t.me/argus_hub)

[English](./README.md) | [Chinese](./README_CN.md)

**Why “Argus”?** Argus is named after the hundred‑eyed giant (Argus Panoptes) from Greek mythology. The legend says that even when he slept, he only closed two eyes and the rest stayed awake. The goal of this project is similar: a proactive personal assistant that can keep working when you’re not around (e.g. asleep), via heartbeats, cron jobs, and connected devices.

Argus is a small gateway you run on your own server.
It lets you talk to an AI assistant in **Telegram**, run background check-ins (heartbeat), schedule tasks (cron), and connect devices (nodes) that can execute actions.

Today this repo ships with a **Codex** runtime by default. The long-term goal is to support **any app-server runtime** that speaks **JSONL over stdio**.

If you’re building your own clients / channels (WebSocket JSON-RPC, reconnection, etc.), read `REMOTE_CLIENT_GUIDE.md` — this README focuses on running and operating the server.

## Quickstart (Telegram)

Prereqs:

- Docker + Docker Compose
- A Telegram bot token from `@BotFather`

1) Set env vars (minimal):

```bash
export TELEGRAM_BOT_TOKEN="123:abc..."
# Recommended if you expose 8080 beyond localhost:
export ARGUS_TOKEN="$(openssl rand -hex 16)"
# Optional: stream private-chat drafts via Telegram Bot API `sendMessageDraft`.
export TELEGRAM_DRAFT_STREAMING="auto"
```

2) Start gateway + Telegram bot:

```bash
docker compose --profile tg up --build
```

3) Verify:

- Open Telegram → DM your bot
- Run `/start`
- Run `/menu` (open the control panel)
- Send a message
- (Groups/topics) Run `/menu` → **Bind This Chat** → (optional) **New Thread**

Notes:

- If you want the bot to respond to all group messages, disable **Group Privacy** in BotFather.
- Outbound replies are delivered by the **gateway**. With `TELEGRAM_DRAFT_STREAMING=auto`, private chats can show a live draft during generation before the final message is sent; the bot still handles inbound messages + typing indicators.

Stop:

```bash
docker compose down
```

## Environment variables

`docker compose` automatically reads `.env` from the repo root. Start from the example file:

```bash
cp .env.example .env
```

How changes take effect:

- **Build-time**: `ARGUS_RUNTIME_INSTALL_CMD` and `NEXT_PUBLIC_ARGUS_WS_URL` are baked into images/build output; rebuild after changing them.
- **Gateway restart**: most gateway / Telegram bot variables take effect after restarting the relevant container.
- **New runtime sessions only**: variables injected into spawned runtime containers (`ARGUS_RUNTIME_CMD`, resource limits, gateway-internal host, per-session proxy wiring) apply only to newly created sessions.

### Core gateway, auth, and persistence

| Variable | Required? | Default | What it does / notes |
| --- | --- | --- | --- |
| `ARGUS_TOKEN` | Recommended; effectively required if you expose the gateway beyond localhost | unset | Shared bearer token for `/ws` and the HTTP management endpoints. Also acts as the fallback master secret for `ARGUS_NODE_TOKEN`, `ARGUS_MCP_TOKEN`, and `ARGUS_OPENAI_TOKEN`. This is **not** an OpenAI API key. |
| `ARGUS_NODE_TOKEN` | Optional | falls back to `ARGUS_TOKEN` | Separate master secret for derived `/nodes/ws` session tokens. Set this if you want node access scoped separately from the main gateway token. |
| `ARGUS_MCP_TOKEN` | Optional | falls back to `ARGUS_TOKEN` | Separate master secret for the gateway MCP endpoint. Runtime containers receive per-session derived tokens instead of the raw secret. |
| `ARGUS_HOME_HOST_PATH` | Optional in Compose, but strongly recommended | Compose: `${HOME}/.argus`; direct gateway run: unset | Host directory used for persistent gateway state and per-user workspaces. If you run the gateway directly without this set, automation state falls back to `/tmp/argus-gateway-state.json`. Must be an **absolute host path** when set. |
| `ARGUS_WORKSPACE_HOST_PATH` | Optional | unset | Base directory for generic `/ws` sessions. When unset, generic sessions use `${ARGUS_HOME_HOST_PATH}/workspaces/sess-<sessionId>`. Must be an **absolute host path** when set. |
| `ARGUS_CORS_ORIGINS` | Optional | `*` | Comma-separated list of allowed HTTP origins, for example `https://app.example.com,https://admin.example.com`. Tighten this when exposing the HTTP APIs to browsers. |
| `ARGUS_GC_DELETE_ORPHAN_RUNTIMES` | Optional | `delete` | Controls startup garbage collection for docker runtime containers not referenced by gateway state. Supported values: `off`, `dry-run`, `delete` (also `true` / `yes` / `on`). |
| `ARGUS_STUCK_TURN_TIMEOUT_S` | Optional | `900` | Safety timeout for lanes stuck in `busy` state because an upstream turn never completed. Set `0` to disable the reset logic. |

### Docker runtime provisioning

| Variable | Required? | Default | What it does / notes |
| --- | --- | --- | --- |
| `ARGUS_PROVISION_MODE` | Only if you run the gateway outside the provided Compose setup | Compose sets `docker`; code default is `static` | `docker` means each `/ws` session can auto-create its own runtime container. `static` means the gateway proxies to pre-existing upstream app-servers instead. |
| `ARGUS_RUNTIME_IMAGE` | Optional | `argus-runtime` | Docker image tag used for spawned runtime session containers. |
| `ARGUS_DOCKER_NETWORK` | Optional | `argus-net` | Docker network that both the gateway and runtime containers join. |
| `ARGUS_RUNTIME_INSTALL_CMD` | Optional | `npm i -g @openai/codex` | **Build-time** install command for the runtime image (`Dockerfile` build arg `APP_SERVER_INSTALL_CMD`). Change this when swapping the bundled runtime; rebuild afterward. |
| `ARGUS_RUNTIME_CMD` | Optional in Compose, conceptually required in `docker` mode | `codex app-server` | Command executed inside each spawned runtime container. This is what `run_app_server.sh` launches through the TCP bridge. |
| `ARGUS_CONNECT_TIMEOUT_S` | Optional | `30` | Timeout for runtime bootstrap and TCP connect checks. Also reused as a coarse upper bound for Docker API calls. |
| `ARGUS_CONTAINER_PREFIX` | Optional | `argus-session` | Prefix for auto-created docker container names. Resulting names look like `argus-session-<sessionId>`. |
| `ARGUS_RUNTIME_CPUS` | Optional | unset | CPU limit for newly created runtime containers. Use decimal CPU counts such as `0.8` or `2`. |
| `ARGUS_RUNTIME_MEM_LIMIT` | Optional | unset | Memory limit for newly created runtime containers. Supports values like `512m`, `1g`, `2gb`. |
| `ARGUS_RUNTIME_MEMSWAP_LIMIT` | Optional | unset | Memory+swap limit for newly created runtime containers. Requires `ARGUS_RUNTIME_MEM_LIMIT`, and must be `>=` that value. Set it equal to mem limit to effectively disable swap. |
| `ARGUS_RUNTIME_PIDS_LIMIT` | Optional | unset | Process count limit for newly created runtime containers. Must be a positive integer. |
| `ARGUS_JSONL_LINE_LIMIT_BYTES` | Optional | `134217728` (128 MiB) | Max JSONL line size accepted from upstream runtimes. Increase this if you hit `Separator is not found, and chunk exceed the limit` on large tool outputs. |
| `DOCKER_SOCK` | Optional | `/var/run/docker.sock` | Host path mounted into the gateway container as the Docker socket. Useful on OrbStack or custom Docker setups. Mounting the socket is root-equivalent on the host. |

### Model provider / OpenAI-compatible proxy

| Variable | Required? | Default | What it does / notes |
| --- | --- | --- | --- |
| `OPENAI_API_KEY` | Optional, but recommended for the default Codex runtime | unset | Long-lived API key for the built-in `gateway` channel only. The key stays on the **gateway** and is not injected directly into runtime containers. |
| `ARGUS_OPENAI_API_KEY` | Optional | unset | Compatibility alias for `OPENAI_API_KEY`. The built-in `gateway` channel checks `OPENAI_API_KEY` first, then this alias. |
| `ARGUS_OPENAI_RESPONSES_UPSTREAM_URL` | Optional | `https://api.openai.com/v1/responses` | Upstream Responses API URL used by the built-in `gateway` channel. Point this at any OpenAI-compatible `/v1/responses` endpoint if you want the shared gateway channel to target your own provider or company proxy. |
| `ARGUS_OPENAI_TOKEN` | Optional | falls back to `ARGUS_TOKEN` | Master secret used by the gateway to derive per-session bearer tokens for `/openai/v1/responses`. On the gateway this is the master secret; inside each runtime container the gateway injects a **derived session token** with the same env name. This controls access to the proxy; the actual upstream is chosen from the selected channel. |
| `ARGUS_GATEWAY_INTERNAL_HOST` | Optional | auto-detect current gateway container name, then fall back to `gateway` | Hostname that spawned runtime containers should use when calling back into the gateway (`/mcp`, `/nodes/ws`, `/openai/v1`). Runtime containers keep this fixed gateway URL even when users switch channels. Set this only if Docker DNS / service naming differs from the default assumptions. |

Channel behavior:

- Built-in `gateway`: selected by default; uses `OPENAI_API_KEY` + `ARGUS_OPENAI_RESPONSES_UPSTREAM_URL`.
- Built-in `0-0.pro`: fixed base URL `https://api.0-0.pro/v1`; each user supplies their own API key from the Telegram menu.
- Custom channels: each user can add/delete/rename their own OpenAI-compatible `baseUrl` + API key entries.
- The model switch menu fetches the current channel's model list from `<baseUrl>/models` (OpenAI-compatible shape). If that request fails, Argus falls back to the agent's current model so the session still stays usable.
- The channel list and user API keys are stored in `${ARGUS_HOME_HOST_PATH}/gateway/state.json`. Protect this file like any other secret store.
- Switching the current channel is **user-global**: one switch affects that user's existing and future agents/containers.

### Web UI and Telegram bot

| Variable | Required? | Default | What it does / notes |
| --- | --- | --- | --- |
| `NEXT_PUBLIC_ARGUS_WS_URL` | Optional | unset | **Build-time** preset WebSocket URL for the optional web UI. Rebuild `web` after changing it. Common example: `ws://127.0.0.1:8080/ws?token=...`. |
| `TELEGRAM_BOT_TOKEN` | Required for `docker compose --profile tg ...` | unset | Telegram bot token from `@BotFather`. The Telegram bot service exits immediately if this is missing. |
| `TELEGRAM_DRAFT_STREAMING` | Optional | `auto` | Controls gateway-side private-chat draft streaming. Accepted forms: `auto` / `on` / `true`, `force` / `always`, and `off`. |
| `HOST` | Optional helper variable | `127.0.0.1` | Convenience host used by docs, examples, and Telegram bot URL derivation. It is **not** a security boundary and is not required by the gateway itself. In Docker, `gateway` is used automatically when appropriate. |
| `ARGUS_GATEWAY_WS_URL` | Optional | derived from `HOST` or `NEXT_PUBLIC_ARGUS_WS_URL` | Explicit WebSocket URL for `apps/telegram-bot` when it is not running in the default Compose topology. |
| `ARGUS_GATEWAY_HTTP_URL` | Optional | derived from `ARGUS_GATEWAY_WS_URL` or `HOST` | Explicit HTTP base URL for `apps/telegram-bot`. Useful when WS and HTTP are routed differently or when URL derivation is wrong for your deployment. |
| `ARGUS_CWD` | Optional | `/workspace` | Default working directory passed by `apps/telegram-bot` when creating/resuming threads. |
| `STATE_PATH` | Optional | In container: `/data/state.json`; outside container: `./state.json` | Persistent state path for `apps/telegram-bot`. Only relevant if you run the bot directly rather than through the provided volume setup. |
| `TELEGRAM_ADMIN_CHAT_IDS` | Reserved / currently unused in this repo | unset | Present in `docker-compose.yml`, but the current codebase does not read it yet. Safe to leave unset for now. |

### Standalone node-host (`apps/node-host`)

You normally do **not** need these when using the built-in runtime node-host; the gateway injects that wiring automatically. These are for running `apps/node-host` manually on another machine (for example your Mac).

| Variable | Required? | Default | What it does / notes |
| --- | --- | --- | --- |
| `ARGUS_NODE_WS_URL` | Yes, unless you pass `--url` | unset | WebSocket URL for `/nodes/ws`. Example: `ws://127.0.0.1:8080/nodes/ws?token=...`. |
| `ARGUS_NODE_ID` | Optional | machine hostname | Logical node id shown to the gateway. |
| `ARGUS_NODE_DISPLAY_NAME` | Optional | machine hostname | Human-friendly label shown in node listings / events. |
| `ARGUS_NODE_STATE_DIR` | Optional | `$APP_HOME/node-host/<nodeId>` if `APP_HOME` exists, else `$HOME/.argus/node-host/<nodeId>` | Directory for local node-host state. |
| `ARGUS_NODE_AUDIT` | Optional | `true` | Enables stderr audit logging for remote commands. Set `0` / `false` to disable. |
| `ARGUS_NODE_AUDIT_MAX_BYTES` | Optional | `4096` | Max stdout/stderr bytes included in each audit log preview. |
| `ARGUS_NODE_AUDIT_STDIN_PREVIEW_BYTES` | Optional | `256` | Max stdin bytes included in audit previews. Set `0` to suppress stdin previews. |
| `ARGUS_NODE_HANDSHAKE_TIMEOUT_MS` | Optional | `15000` | WebSocket handshake timeout in milliseconds. |
| `ARGUS_NODE_CONNECT_TIMEOUT_MS` | Optional | derived from handshake timeout, usually `17000` | Overall WebSocket connect timeout. If unset, it defaults to `ARGUS_NODE_HANDSHAKE_TIMEOUT_MS + 2000` when handshake timeout is enabled. |
| `ARGUS_NODE_RECONNECT_DELAY_MS` | Optional | `1000` | Base reconnect delay in milliseconds. |
| `ARGUS_NODE_RECONNECT_DELAY_MAX_MS` | Optional | `30000` | Max reconnect backoff in milliseconds. |
| `ARGUS_NODE_RECONNECT_JITTER_PCT` | Optional | `0.2` | Reconnect jitter fraction (`0` to `0.5`). |
| `ARGUS_NODE_PING_INTERVAL_MS` | Optional | `30000` | Ping interval in milliseconds. Set `0` to disable pings. |
| `ARGUS_NODE_PONG_TIMEOUT_MS` | Optional | `10000` | Pong timeout in milliseconds. |

### Advanced: static upstream mode

If you do **not** want Argus to auto-create Docker runtime containers, you can run the gateway in `ARGUS_PROVISION_MODE=static` and point it at pre-existing app-servers.

| Variable | Required? | Default | What it does / notes |
| --- | --- | --- | --- |
| `ARGUS_UPSTREAMS_JSON` | Optional advanced override | unset | JSON object describing one or more named upstreams. When set, it replaces `ARGUS_TCP_HOST` / `ARGUS_TCP_PORT`. Each entry can include `host`, `port`, and optional `token`. |
| `ARGUS_TCP_HOST` | Optional in static mode | `127.0.0.1` | Default upstream host when `ARGUS_UPSTREAMS_JSON` is unset. |
| `ARGUS_TCP_PORT` | Optional in static mode | `7777` | Default upstream port when `ARGUS_UPSTREAMS_JSON` is unset. |

Example `ARGUS_UPSTREAMS_JSON`:

```json
{
  "default": { "host": "127.0.0.1", "port": 7777, "token": "change-me" },
  "staging": { "host": "10.0.0.25", "port": 7777 }
}
```

### Variables you normally should **not** set manually

When the gateway provisions a runtime container, it injects several internal env vars automatically, including `APP_SERVER_CMD`, `APP_HOME`, `APP_WORKSPACE`, `CODEX_HOME`, `ARGUS_SESSION_ID`, `ARGUS_CODEX_MODEL`, `ARGUS_CODEX_MCP_URL`, derived `ARGUS_MCP_TOKEN`, derived `ARGUS_OPENAI_TOKEN`, and `ARGUS_NODE_WS_URL`.

These are implementation details of the runtime bridge; in normal deployments you should set the higher-level gateway vars above instead of pre-filling these manually.

## Data & workspace

By default Argus persists data on the Docker host at:

- `ARGUS_HOME_HOST_PATH` (default: `${HOME}/.argus`)
  - Gateway automation state: `${ARGUS_HOME_HOST_PATH}/gateway/state.json`
  - Also stores per-user API channel definitions, the selected current channel, and user-supplied API keys. Treat it as a secret-bearing file.

Each runtime session container mounts a **single host workspace directory** at `/workspace`.

- Codex state (threads/history, config) is stored under `/workspace/.codex` by default.
- Host workspace directories:
  - Telegram DM agents: `${ARGUS_HOME_HOST_PATH}/workspace-<tgid>-<name>` (e.g. `${ARGUS_HOME_HOST_PATH}/workspace-182262230-main`)
  - Generic `/ws` sessions: `${ARGUS_HOME_HOST_PATH}/workspaces/sess-<sessionId>`
    - If `ARGUS_WORKSPACE_HOST_PATH` is set, generic sessions use `${ARGUS_WORKSPACE_HOST_PATH}/sess-<sessionId>` instead.

The runtime manages these workspace files:

- `AGENTS.md` (synced from `docs/templates/AGENTS.default.md`; template updates automatically refresh the workspace copy)
- `SOUL.md` (created if missing)
- `USER.md` (created if missing)
- `HEARTBEAT.md` (created if missing)

If you want a durable change to the managed agent instructions, edit `docs/templates/AGENTS.default.md` instead of a per-workspace `AGENTS.md` copy.

These files are used to build the assistant’s “project context”. Every turn injects `AGENTS.md` / `SOUL.md` / `USER.md`; **`HEARTBEAT.md` is injected only for heartbeat turns**.

### Telegram multi-user agents (DM isolation)

When using `apps/telegram-bot`, the gateway can isolate runtime containers (agents) per Telegram private user:

- First `/start` in a private chat bootstraps a dedicated per-user `main` agent (one session container + one workspace).
  - Host workspace directory: `${ARGUS_HOME_HOST_PATH}/workspace-<tgid>-main`
- Subsequent `/start` reuses the existing `main`.
- Telegram uploads are staged into the active workspace under `/workspace/inbox/telegram/<chatKey>/...`.
  - If the upload includes a caption, that caption starts the turn immediately.
  - If the upload has no caption, the file is still saved immediately and will be attached to the next text message from that chat.
- Use `/menu` to open the control panel:
  - **Switch Agent**: switch the current DM agent (workspace/session).
  - **Switch Model**: switch the current agent to any model exposed by the currently selected API channel's `/models` endpoint.
  - **API Channels**: manage the per-user channel list (`gateway`, `0-0.pro`, and your own custom channels) and switch the current channel for all of your agents/containers.
  - **Create Agent**: create a new agent (workspace/session) and switch to it.
  - **Rename Agent**: rename the current agent (owner-only; non-`main`).
  - **Delete Agent**: delete the current agent (owner-only; includes `main`). If you delete `main`, you’ll be prompted to create a new `main`.
  - **New Conversation**: reset the main thread for the current agent (affects heartbeat + DM routing).
- In groups/topics, run `/menu` in the chat/topic and use **Bind This Chat** to route that chat/topic to an agent.
- Admin sharing: edit `${ARGUS_HOME_HOST_PATH}/gateway/state.json` and add the target user tgid into `allowedUserIds` for the desired `agentId`.
  - Recommended: edit with the gateway stopped, or restart after edits (the gateway continuously writes back `state.json`).

Migration notes:

- Enabling this does not delete existing session containers.
- By default, the gateway deletes orphan docker runtime containers on startup (set `ARGUS_GC_DELETE_ORPHAN_RUNTIMES=off` to disable, or `dry-run` to preview).
- If older sessions still have enabled cron jobs in automation state, the gateway will keep scheduling them until you disable/delete those jobs and remove the sessions.

## Automation (system events, heartbeat, cron)

Argus has a minimal automation layer in the gateway:

- **system events**: a persistent queue of background events (cron, node/process events, etc.)
- **heartbeat**: processes system events and scheduled tasks; runs even when no UI is online
- **cron**: cron expressions enqueue system events; heartbeat picks them up

Automation state is persisted under:

- `${ARGUS_HOME_HOST_PATH}/gateway/state.json`

## Nodes (optional)

Nodes let the assistant execute commands on your devices (e.g. your Mac).

Start a node-host on your Mac:

```bash
cd apps/node-host
go build -o argus ./cmd/argus
./argus \
  --url "ws://127.0.0.1:8080/nodes/ws?token=argus-node-v1.<sessionId>.<sig>" \
  --node-id "mac" \
  --display-name "My Mac"
```

Notes:

- Each runtime session container also runs a built-in node-host and registers as `runtime:<sessionId>`.
- Inside the runtime, prefer `node="self"`; if multiple runtimes are online, use `node="runtime:<sessionId>"`.
- Node hosts are **session-scoped**:
  - When auth is configured, you must use a derived token: `argus-node-v1.<sessionId>.<sig>` (master secret: `ARGUS_NODE_TOKEN`, fallback: `ARGUS_TOKEN`; `sig = base64url(hmac_sha256(master, sessionId))[:32]`).
  - In dev mode with no auth configured, the node token can be omitted.
- node-host prints connection/reconnect status and an audit log of received remote commands to stderr.
  - Disable audit logs: `ARGUS_NODE_AUDIT=0` (or `./argus --audit false ...`)
  - Tune output: `ARGUS_NODE_AUDIT_MAX_BYTES`, `ARGUS_NODE_AUDIT_STDIN_PREVIEW_BYTES`
- Interactive CLI flow: start with `system.run` using `params={"argv":[...],"pty":true,"yieldMs":0}`, then continue with `process.write`, `process.send_keys`, `process.submit`, or `process.paste` using the returned `jobId`.

## Optional web UI

The web UI is for verification/debugging (not required for Telegram).

```bash
docker compose --profile web up --build
open http://127.0.0.1:3000
```

## Runtime configuration

Default runtime (Codex):

- Build/install command: `ARGUS_RUNTIME_INSTALL_CMD` (defaults to installing `@openai/codex`)
- Start command: `ARGUS_RUNTIME_CMD` (defaults to `codex app-server`)
- The base runtime image includes a few common CLI tools by default, including `curl`, `git`, `rg`, and `strings`.

### OpenAI proxy & API channels

Argus keeps runtime containers on a fixed gateway proxy URL (`/openai/v1/responses`). Users switch channels on the gateway side, so existing containers do **not** need their `.codex/config.toml` rewritten.

Notes:

- The built-in `gateway` channel is selected by default and uses `OPENAI_API_KEY` / `ARGUS_OPENAI_API_KEY` plus `ARGUS_OPENAI_RESPONSES_UPSTREAM_URL`.
- The built-in promo channel `0-0.pro` always points to `https://api.0-0.pro/v1`; each user provides their own API key before switching to it.
- Users can add/delete/rename extra OpenAI-compatible channels from the Telegram **API Channels** menu.
- The channel list, selected current channel, and user-supplied API keys are stored in `${ARGUS_HOME_HOST_PATH}/gateway/state.json`.
- Switching the current channel is **user-global**: it affects that user's existing and future agents/containers.
- Sessions without an owning Telegram user still fall back to the built-in `gateway` channel.
- The proxy requires a per-session derived bearer token (master: `ARGUS_OPENAI_TOKEN`, fallback: `ARGUS_TOKEN`).
- The runtime writes a generated `CODEX_HOME/config.toml` (no provider secrets) to point Codex at the gateway MCP server and proxy.
  - Default `CODEX_HOME`: `/workspace/.codex` (workspace-scoped)
  - Default model: `gpt-5.4` (Telegram agents can switch to any valid model id returned by the selected channel, and the selection is persisted per agent).
- If you want the default `gateway` channel to work out of the box for every user, set `OPENAI_API_KEY` on the gateway. Otherwise users must select a ready personal channel first.

To swap runtimes, set these before `docker compose up --build`.

Advanced:

- `ARGUS_WORKSPACE_HOST_PATH`: optional base directory for auto-created `/ws` session workspaces (absolute path). When unset, generic sessions use `${ARGUS_HOME_HOST_PATH}/workspaces/sess-<sessionId>`.

## Resource limits (small servers)

The gateway spawns runtime session containers dynamically. Set these on the gateway to limit newly created sessions:

```bash
export ARGUS_RUNTIME_CPUS="0.8"
export ARGUS_RUNTIME_MEM_LIMIT="768m"
export ARGUS_RUNTIME_MEMSWAP_LIMIT="768m" # optional; requires MEM_LIMIT
export ARGUS_RUNTIME_PIDS_LIMIT="512"     # optional
```

They apply to **new** sessions. Existing containers need `docker update` or recreation.

## Security notes

- Mounting `/var/run/docker.sock` grants **host-root equivalent** power. Do not expose the gateway publicly without auth, ACLs, and rate limiting.
- Prefer `Authorization: Bearer ...` over putting tokens in URLs (URLs leak into logs/history).

## Repo layout

- `Dockerfile`: runtime image (JSONL over stdio → TCP `:7777` bridge)
- `apps/api/`: gateway (HTTP + WS `/ws`)
- `apps/telegram-bot/`: Telegram bot (inbound + typing)
- `apps/node-host/`: node-host (Go; device command runner)
- `apps/web/`: optional web UI (debug)
- `docker-compose.yml`: one-command local/server deployment
- `REMOTE_CLIENT_GUIDE.md`: client integration (protocol, reconnection, examples)

## Debug

```bash
python3 -m py_compile apps/api/app.py
```
