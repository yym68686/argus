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
- Outbound replies are delivered by the **gateway** (one final message per turn). The bot handles inbound messages + typing indicators.

Stop:

```bash
docker compose down
```

## Data & workspace

By default Argus persists data on the Docker host at:

- `ARGUS_HOME_HOST_PATH` (default: `${HOME}/.argus`)
  - Gateway automation state: `${ARGUS_HOME_HOST_PATH}/gateway/state.json`

Each runtime session container mounts a **single host workspace directory** at `/root/.argus/workspace`.

- Codex state (threads/history, config) is stored under `/root/.argus/workspace/.codex` by default.
- Host workspace directories:
  - Telegram DM agents: `${ARGUS_HOME_HOST_PATH}/workspace-<tgid>-<name>` (e.g. `${ARGUS_HOME_HOST_PATH}/workspace-182262230-main`)
  - Generic `/ws` sessions: `${ARGUS_HOME_HOST_PATH}/workspaces/sess-<sessionId>`
    - If `ARGUS_WORKSPACE_HOST_PATH` is set, generic sessions use `${ARGUS_WORKSPACE_HOST_PATH}/sess-<sessionId>` instead.

On first run, the runtime bootstraps these workspace files **if missing** (it will not overwrite existing files):

- `AGENTS.md` (from `docs/templates/AGENTS.default.md`)
- `SOUL.md`
- `USER.md`
- `HEARTBEAT.md`

These files are used to build the assistant’s “project context”. Every turn injects `AGENTS.md` / `SOUL.md` / `USER.md`; **`HEARTBEAT.md` is injected only for heartbeat turns**.

### Telegram multi-user agents (DM isolation)

When using `apps/telegram-bot`, the gateway can isolate runtime containers (agents) per Telegram private user:

- First `/start` in a private chat bootstraps a dedicated per-user `main` agent (one session container + one workspace).
  - Host workspace directory: `${ARGUS_HOME_HOST_PATH}/workspace-<tgid>-main`
- Subsequent `/start` reuses the existing `main`.
- Use `/menu` to open the control panel:
  - **Switch Agent**: switch the current DM agent (workspace/session).
  - **Create Agent**: create a new agent (workspace/session) and switch to it.
  - **Rename Agent**: rename the current agent (owner-only; non-`main`).
  - **Delete Agent**: delete the current agent (owner-only; includes `main`). If you delete `main`, you’ll be prompted to create a new `main`.
  - **New Main Thread**: reset the main thread for the current agent (affects heartbeat + DM routing).
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

### OpenAI credentials (recommended)

To avoid putting your long-lived OpenAI API key inside each runtime container, set `OPENAI_API_KEY` on the **gateway**.
When `OPENAI_API_KEY` is set, the gateway exposes a narrow `/openai/v1/responses` proxy and each runtime is configured to use it automatically.

Notes:

- The key is **not** passed into runtime containers.
- The runtime writes a generated `CODEX_HOME/config.toml` (no secrets) to point Codex at the gateway MCP server and optional OpenAI proxy.
  - Default `CODEX_HOME`: `/root/.argus/workspace/.codex` (workspace-scoped)
- The proxy requires a per-session derived bearer token (master: `ARGUS_OPENAI_TOKEN`, fallback: `ARGUS_TOKEN`).
- Optional: override the upstream URL with `ARGUS_OPENAI_RESPONSES_UPSTREAM_URL` (default: `https://api.openai.com/v1/responses`).

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
