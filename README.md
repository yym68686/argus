# Argus (Docker)

FastAPI 网关 + WebSocket `/ws`，用于把“app-server runtime”（JSONL over stdio）暴露成可被外部客户端连接的服务。

本仓库也提供一个 **可选** 的 React 前端（仅用于验证/调试后端功能），默认不随网关启动。

客户端接入/协议/重连恢复 thread 等内容统一放在 `REMOTE_CLIENT_GUIDE.md`，不要在这里重复。

## Quickstart（本机）

前置条件：

- 已安装 `docker` + `docker compose`
- 已配置 runtime 安装命令与启动命令（见 `docker-compose.yml` 里的 `ARGUS_RUNTIME_INSTALL_CMD` / `ARGUS_RUNTIME_CMD`）
- `ARGUS_HOME_HOST_PATH`（runtime 的 home 目录挂载点；用于持久化配置/凭据；默认 workspace 也会落在其子目录 `workspace/`；未设置时默认 `${HOME}/.argus`）
- 可选（高级）：`ARGUS_WORKSPACE_HOST_PATH`（把一个现成目录挂到 runtime 的 workspace；不设置则使用 `${ARGUS_HOME_HOST_PATH}/workspace`）

最小环境变量示例（按你的 runtime 实际情况填写）：

```bash
export ARGUS_RUNTIME_INSTALL_CMD="<install your agent/app-server runtime>"
export ARGUS_RUNTIME_CMD="<start your app-server command>"
# 可选：不设置则默认 `${HOME}/.argus`
export ARGUS_HOME_HOST_PATH="${HOME}/.argus"
# Optional (advanced):
export ARGUS_WORKSPACE_HOST_PATH="$(pwd)"
```

启动（只启动后端：会 build runtime 镜像并启动网关）：

```bash
# OrbStack(macOS) 常用 socket 路径；Linux 服务器一般不需要这行
export DOCKER_SOCK="$HOME/.orbstack/run/docker.sock"

docker compose up --build
```

健康检查：

```bash
open http://127.0.0.1:8080/healthz
```

可选：启动前端 UI（用于验证后端）

```bash
# 注意：Compose 的 profile 是“额外启用”，不会排除未设置 profile 的服务。
# 如果你想“后端 + web 一起启动”：
docker compose --profile web up --build

# 如果你只想启动 web（不启动 gateway / runtime-image）：
docker compose --profile web up --build web
open http://127.0.0.1:3000
```

可选：启动 Telegram Bot（v0；仅在 `turn/completed` 后回 1 条最终文本）

```bash
export TELEGRAM_BOT_TOKEN="123:abc..."
docker compose --profile tg up --build telegram-bot
```

> 如果你希望 bot 在群里“任何消息都触发回复”，需要在 BotFather 里关闭 Group Privacy。

> `telegram-bot` 不会自动拉起 `gateway`；如需一起启动：
>
> `docker compose --profile tg up --build gateway telegram-bot`

该 bot 默认复用仓库已有的连接配置：

- `HOST`（默认 `127.0.0.1`；在 Docker Compose 内若 `HOST=127.0.0.1/localhost` 会自动改用 `gateway` 以连接网关容器）
- `ARGUS_TOKEN`（可选；用于访问网关 `/ws` 与 `/sessions`）

你也可以把 bot 跑在 Docker 之外（例如跑在你的 Mac 上），只要能访问网关即可：

```bash
cd apps/telegram-bot
npm i
export TELEGRAM_BOT_TOKEN="123:abc..."
export HOST="your.gateway.host:8080"
export ARGUS_TOKEN="change-me"
node index.mjs
```

最小命令：

- `/where`：查看当前 `sessionId` / `threadId`
- `/new`：为当前 chat/topic 新开 thread

停止：

```bash
docker compose down
```

## 目录结构

- `Dockerfile`: runtime 镜像（app-server JSONL stdio → TCP `:7777`）
- `apps/api/`: FastAPI 网关（HTTP + WS `/ws`），支持 `ARGUS_PROVISION_MODE=docker` 自动创建后端容器（默认保留；用 `GET/DELETE /sessions` 管理）
- `apps/web/`: React 前端（Next.js + Tailwind v4），可选启动（`docker compose --profile web ...`）
- `apps/telegram-bot/`: Telegram Bot（v0；可选启动：`docker compose --profile tg ...`）
- `docker-compose.yml`: 本机/服务器一键启动（网关会通过挂载的 `docker.sock` 创建容器）
- `client_smoke.py`: 最小化 smoke client（用于调试后端 TCP JSONL 端口）
- `REMOTE_CLIENT_GUIDE.md`: 外部客户端接入文档（协议/示例/重连）

## Debug

语法检查：

```bash
python3 -m py_compile apps/api/app.py client_smoke.py
```

## Automation（systemEvent / followup queue / heartbeat / cron）

Gateway 内置一个最小的自动化机制（用于 cron/heartbeat 触发“后台 turn”）：

- 持久化位置：`$ARGUS_HOME_HOST_PATH/gateway/state.json`（未设置 `ARGUS_HOME_HOST_PATH` 时会落到 `/tmp` 并打印 warning）
- Workspace 初始化：首次启动会在 workspace 根目录生成 `AGENTS.md` / `SOUL.md` / `USER.md` / `HEARTBEAT.md`（来自 `docs/templates/*`；若文件已存在则不覆盖）
- Project Context：每次通过 `argus/input/enqueue`/follow-up/heartbeat 启动 turn 时，会把 workspace 里的 `AGENTS.md` / `SOUL.md` / `USER.md` 拼到 user prompt 的 `# Project Context`；heartbeat 会额外拼 `HEARTBEAT.md`

常用接口（都需要 `Authorization: Bearer $ARGUS_TOKEN`）：

- 立即触发 heartbeat：`POST /automation/heartbeat/now`
- 入队一个 systemEvent（默认进 main thread）：`POST /automation/systemEvent/enqueue`
- Cron jobs：
  - 列表：`GET /automation/cron/jobs`
  - 创建/更新：`POST /automation/cron/jobs`（`expr` 直接用 cron 表达式）
  - 删除：`DELETE /automation/cron/jobs/{jobId}`

WebSocket `/ws` 也提供同名的 JSON-RPC helper（推荐客户端用）：

- `argus/input/enqueue`：如果 thread 正在执行 turn，则排队为 “follow-up（下一个 turn）”，并在当前 turn 完成后自动批量执行
- `argus/systemEvent/enqueue` / `argus/heartbeat/request` / `argus/thread/main/ensure`

## Nodes + MCP（可选）

Argus 还支持一个“Node（设备）”通道与一个内置 MCP endpoint：

- Node Host（例如你的 Mac）通过 `WS /nodes/ws` 连接到网关并注册能力（示例实现：`system.run`）。
- 网关通过 `HTTP /mcp`（MCP Streamable HTTP）对 runtime 容器里的 Codex 暴露工具：`nodes_list` / `node_invoke`。
- 每个 runtime session 容器也会自带一个 node-host，并以 `nodeId=runtime:<sessionId>` 注册；调用时可用 `node="self"`（或在仅有一个 runtime node 在线时省略 `node`）。

### 启动 Node Host（在 Mac 上）

```bash
cd apps/node-host
npm i

node index.mjs \
  --url "ws://127.0.0.1:8080/nodes/ws?token=$ARGUS_TOKEN" \
  --node-id "mac" \
  --display-name "My Mac"
```

### Web 测试页（可选）

启动 web profile 后打开：

```bash
open http://127.0.0.1:3000/nodes
```

在页面里点击 “Refresh nodes”，然后用 `system.run` + `{"argv":["echo","hello"]}` 测试。

### 让容器内 Codex 通过 MCP 调用 Node

在宿主机的 `ARGUS_HOME_HOST_PATH` 下创建（或编辑）：

`$ARGUS_HOME_HOST_PATH/.codex/config.toml`

示例（docker compose 场景下，runtime 容器通过 service 名 `gateway` 访问网关）：

```toml
[mcp_servers.argus]
url = "http://gateway:8080/mcp"
bearer_token_env_var = "ARGUS_MCP_TOKEN" # or "ARGUS_TOKEN"
```

然后在对话里让 agent 调用 MCP tool `node_invoke` 即可（例如调用 `system.run`）。

## Frontend Dev（可选）

如果你想改 React 前端而不每次都 rebuild 网关镜像：

```bash
cd apps/web
npm i
npm run dev
```

前端默认会尝试连接 `ws://<当前主机>:8080/ws`；你也可以在页面里手动改 `WebSocket URL`。
网关默认对 HTTP 接口开启 CORS（允许任意 Origin），因此浏览器 UI 可以直接调用 `/sessions`（列出/删除容器）。

如果你希望打开页面就自动填好 WebSocket URL，可以在 `.env` 里设置 `NEXT_PUBLIC_ARGUS_WS_URL`，然后重建 web：

```bash
NEXT_PUBLIC_ARGUS_WS_URL="ws://HOST:8080/ws?token=..."
docker compose --profile web up --build web
```
