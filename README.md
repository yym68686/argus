# Argus (Docker)

FastAPI 网关 + WebSocket `/ws`，用于把“app-server runtime”（JSONL over stdio）暴露成可被外部客户端连接的服务。

本仓库也提供一个 **可选** 的 React 前端（仅用于验证/调试后端功能），默认不随网关启动。

客户端接入/协议/重连恢复 thread 等内容统一放在 `REMOTE_CLIENT_GUIDE.md`，不要在这里重复。

## Quickstart（本机）

前置条件：

- 已安装 `docker` + `docker compose`
- 已配置 runtime 安装命令与启动命令（见 `docker-compose.yml` 里的 `ARGUS_RUNTIME_INSTALL_CMD` / `ARGUS_RUNTIME_CMD`）
- 已准备好 `ARGUS_HOME_HOST_PATH`（runtime 的 home 目录挂载点；用于持久化配置/凭据）

最小环境变量示例（按你的 runtime 实际情况填写）：

```bash
export ARGUS_RUNTIME_INSTALL_CMD="<install your agent/app-server runtime>"
export ARGUS_RUNTIME_CMD="<start your app-server command>"
export ARGUS_HOME_HOST_PATH="$(pwd)/.argus-home"
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

停止：

```bash
docker compose down
```

## 目录结构

- `Dockerfile`: runtime 镜像（app-server JSONL stdio → TCP `:7777`）
- `apps/api/`: FastAPI 网关（HTTP + WS `/ws`），支持 `ARGUS_PROVISION_MODE=docker` 自动创建后端容器（默认保留；用 `GET/DELETE /sessions` 管理）
- `apps/web/`: React 前端（Next.js + Tailwind v4），可选启动（`docker compose --profile web ...`）
- `docker-compose.yml`: 本机/服务器一键启动（网关会通过挂载的 `docker.sock` 创建容器）
- `client_smoke.py`: 最小化 smoke client（用于调试后端 TCP JSONL 端口）
- `REMOTE_CLIENT_GUIDE.md`: 外部客户端接入文档（协议/示例/重连）

## Debug

语法检查：

```bash
python3 -m py_compile apps/api/app.py client_smoke.py
```

## Nodes + MCP（可选）

Argus 还支持一个“Node（设备）”通道与一个内置 MCP endpoint：

- Node Host（例如你的 Mac）通过 `WS /nodes/ws` 连接到网关并注册能力（示例实现：`system.run`）。
- 网关通过 `HTTP /mcp`（MCP Streamable HTTP）对 runtime 容器里的 Codex 暴露工具：`nodes_list` / `node_invoke`。

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
bearer_token = "change-me"
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
