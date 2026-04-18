# Argus — 自托管的助手网关（Telegram 优先）

[![Telegram 用户群](https://img.shields.io/badge/Telegram-加入用户群-2CA5E0?logo=telegram&logoColor=white)](https://t.me/uni_api)

[英文](./README.md) | [中文](./README_CN.md)

**名字由来**：Argus 取自希腊神话中的“百眼巨人”（Argus Panoptes）。传说他即使睡觉也只闭两只眼，其余的眼睛仍在值守。我希望这个项目也能在用户不在场（比如睡觉）时继续工作，成为主动式个人助理——通过 heartbeat、cron 以及连接的设备节点来完成持续的检查与行动。

Argus 是一个你可以部署在自己服务器上的“小网关”。
它让你可以在 **Telegram** 里和 AI 助手对话，并支持后台定时/心跳（heartbeat）、任务调度（cron）、以及连接设备节点（nodes）执行动作。

目前本仓库默认提供 **Codex** runtime；长期目标是支持 **任意 app-server runtime**（只要它遵循 **JSONL over stdio** 的形态）。

如果你要做自定义客户端/自定义渠道接入（WebSocket JSON-RPC、断线重连等协议细节），请看 `REMOTE_CLIENT_GUIDE.md` —— 本 README 主要讲如何运行/运维服务端。

## 快速开始（Telegram）

前置条件：

- 已安装 Docker + Docker Compose
- 已从 `@BotFather` 获取 Telegram bot token

1) 设置环境变量（最小集）：

```bash
export TELEGRAM_BOT_TOKEN="123:abc..."
# 如果你把 8080 暴露到非本机，强烈建议开启访问令牌：
export ARGUS_TOKEN="$(openssl rand -hex 16)"
# 可选：通过 Telegram Bot API `sendMessageDraft` 在私聊里显示流式草稿。
export TELEGRAM_DRAFT_STREAMING="auto"
```

2) 启动网关 + Telegram bot：

```bash
docker compose --profile tg up --build
```

3) 验证：

- 打开 Telegram → 私聊你的 bot
- 发送 `/start`
- 发送 `/menu`（打开控制面板）
- 直接发一句话测试
-（群聊/话题）在群里发送 `/menu` → 点击 **Bind This Chat** →（可选）点击 **New Thread**

备注：

- 如果你希望 bot 在群里对“所有消息”都触发回复，需要在 BotFather 里关闭 **Group Privacy**。
- 出站回复由 **gateway** 统一投递。设置 `TELEGRAM_DRAFT_STREAMING=auto` 后，私聊里会在生成过程中显示实时草稿，完成后仍发送最终消息；bot 继续负责入站消息与 “typing…” 指示。

停止：

```bash
docker compose down
```

## 环境变量

`docker compose` 会自动读取仓库根目录下的 `.env`。建议先从示例文件开始：

```bash
cp .env.example .env
```

环境变量改动的生效范围可以这样理解：

- **构建期生效**：`ARGUS_RUNTIME_INSTALL_CMD`、`NEXT_PUBLIC_ARGUS_WS_URL` 会被写进镜像或构建产物，修改后需要重新 build。
- **重启服务生效**：大多数 gateway / Telegram bot 环境变量，在重启对应容器后生效。
- **仅对新建 runtime session 生效**：注入到 runtime 容器内的变量（如 `ARGUS_RUNTIME_CMD`、资源限制、gateway 内部回连地址、按 session 派生的代理配置）只会影响新创建的 session 容器。

### Gateway 核心、鉴权与持久化

| 变量 | 是否必需 | 默认值 | 作用 / 注意事项 |
| --- | --- | --- | --- |
| `ARGUS_TOKEN` | 推荐设置；如果要暴露到 localhost 之外，几乎等同必需 | 未设置 | `/ws` 与 HTTP 管理接口共用的 Bearer token。同时也是 `ARGUS_NODE_TOKEN`、`ARGUS_MCP_TOKEN`、`ARGUS_OPENAI_TOKEN` 的回退 master secret。它**不是** OpenAI API Key。 |
| `ARGUS_NODE_TOKEN` | 可选 | 回退到 `ARGUS_TOKEN` | 用于派生 `/nodes/ws` 的 session 级 token。如果你希望 node 访问和主网关 token 分离，就单独设置它。 |
| `ARGUS_MCP_TOKEN` | 可选 | 回退到 `ARGUS_TOKEN` | gateway MCP 端点使用的独立 master secret。runtime 容器里拿到的是按 session 派生后的 token，而不是原始 secret。 |
| `ARGUS_HOME_HOST_PATH` | 在 Compose 中可选，但强烈推荐设置清楚 | Compose 下默认 `${HOME}/.argus`；直接运行 gateway 时默认未设置 | 用于存放 gateway 持久化状态和本地 workspace 镜像。在 `docker` 模式下，这些目录会 bind mount 到 runtime 容器；在 `fugue` 模式下，gateway 会把这些镜像目录同步到 Fugue 管理的持久化存储。若直接运行 gateway 且未设置它，automation state 会落到 `/tmp/argus-gateway-state.json`。设置时必须是**宿主机绝对路径**。 |
| `ARGUS_WORKSPACE_HOST_PATH` | 可选 | 未设置 | 通用 `/ws` session 的本地 workspace 镜像基础目录。未设置时，通用 session 使用 `${ARGUS_HOME_HOST_PATH}/workspaces/sess-<sessionId>`。设置时必须是**宿主机绝对路径**。 |
| `ARGUS_CORS_ORIGINS` | 可选 | `*` | 逗号分隔的 HTTP 允许来源，例如 `https://app.example.com,https://admin.example.com`。如果你要把 HTTP API 暴露给浏览器，建议收紧。 |
| `ARGUS_GC_DELETE_ORPHAN_RUNTIMES` | 可选 | `delete` | 控制 gateway 启动时是否清理未被状态文件引用的托管 runtime。`docker` 模式下清理的是 session 容器，`fugue` 模式下清理的是配置项目里的 session app。支持：`off`、`dry-run`、`delete`（也接受 `true` / `yes` / `on`）。 |
| `ARGUS_STUCK_TURN_TIMEOUT_S` | 可选 | `900` | 防止某个上游 turn 永远不返回 `turn/completed`，导致 lane 一直卡在 `busy`。设为 `0` 可关闭这层保护。 |

### Docker runtime 创建与资源控制

| 变量 | 是否必需 | 默认值 | 作用 / 注意事项 |
| --- | --- | --- | --- |
| `ARGUS_PROVISION_MODE` | 只有在你想覆盖自动探测结果时才需要设置 | 代码默认 `auto`；仓库自带 Compose 也默认 `auto` | `auto` 会按顺序自动判定：先看是否具备完整 Fugue 配置，有则用 `fugue`；否则若检测到 Docker 端点则用 `docker`；再否则回退到 `static`。完整 Fugue 配置指：base URL + token + project id + 一个 runtime 来源（`ARGUS_FUGUE_RUNTIME_IMAGE`、`ARGUS_FUGUE_RUNTIME_APP_ID`、`ARGUS_FUGUE_RUNTIME_APP_NAME` 或 `ARGUS_FUGUE_RUNTIME_COMPOSE_SERVICE`）+ 一个 gateway 回调目标（`ARGUS_GATEWAY_INTERNAL_HOST` 或 `ARGUS_FUGUE_GATEWAY_COMPOSE_SERVICE`）。`docker` 表示 `/ws` 连接会自动创建 runtime 容器；`fugue` 表示每个托管 session 都会在同一个 Fugue 项目里自动创建一个独立 app/service；`static` 表示 gateway 只代理到预先存在的 app-server。 |
| `ARGUS_RUNTIME_IMAGE` | 可选 | `argus-runtime` | gateway 创建 runtime session 容器时使用的镜像 tag。 |
| `ARGUS_DOCKER_NETWORK` | 可选 | `argus-net` | gateway 与 runtime 容器加入的 Docker 网络。 |
| `ARGUS_RUNTIME_INSTALL_CMD` | 可选 | `npm i -g @openai/codex` | runtime 镜像的**构建期**安装命令（对应 `Dockerfile` 的 `APP_SERVER_INSTALL_CMD`）。如果你要切换 bundled runtime，就改这里并重新 build。 |
| `ARGUS_RUNTIME_CMD` | Compose 下可选；在 `docker` 模式下本质上必须有 | `codex app-server` | 每个 runtime 容器内部真正执行的启动命令，`run_app_server.sh` 会通过 TCP bridge 启动它。 |
| `ARGUS_CONNECT_TIMEOUT_S` | 可选 | `30` | runtime 启动探测和 TCP 建连的超时时间；Docker API 调用也会把它当作一个粗粒度上限。 |
| `ARGUS_CONTAINER_PREFIX` | 可选 | `argus-session` | 自动创建的 docker 容器名前缀，最终名字类似 `argus-session-<sessionId>`。 |
| `ARGUS_RUNTIME_CPUS` | 可选 | 未设置 | 新建 runtime 容器的 CPU 限制，例如 `0.8`、`2`。 |
| `ARGUS_RUNTIME_MEM_LIMIT` | 可选 | 未设置 | 新建 runtime 容器的内存限制，支持 `512m`、`1g`、`2gb` 这类写法。 |
| `ARGUS_RUNTIME_MEMSWAP_LIMIT` | 可选 | 未设置 | 新建 runtime 容器的 memory+swap 上限。要求同时设置 `ARGUS_RUNTIME_MEM_LIMIT`，且必须 `>=` 它。若与内存上限相同，效果上接近“禁用 swap”。 |
| `ARGUS_RUNTIME_PIDS_LIMIT` | 可选 | 未设置 | 新建 runtime 容器的最大进程数限制，必须是正整数。 |
| `ARGUS_JSONL_LINE_LIMIT_BYTES` | 可选 | `134217728`（128 MiB） | runtime 上游单条 JSONL 消息允许的最大字节数。如果你遇到 `Separator is not found, and chunk exceed the limit`，通常该调它。 |
| `DOCKER_SOCK` | 可选 | `/var/run/docker.sock` | 挂进 gateway 容器的宿主机 Docker socket 路径。适用于 OrbStack 或自定义 Docker 安装。注意：挂载 docker.sock 基本等价宿主机 root 权限。 |

### Fugue runtime 创建

| 变量 | 是否必需 | 默认值 | 作用 / 注意事项 |
| --- | --- | --- | --- |
| `ARGUS_FUGUE_BASE_URL` | `fugue` 模式下必需 | 未设置 | Fugue API server 的 base URL，例如 `https://fugue.example.com`。 |
| `ARGUS_FUGUE_TOKEN` | `fugue` 模式下必需 | 未设置 | gateway 用来创建、查询、重启和删除 session app 的 Bearer token。 |
| `ARGUS_FUGUE_TENANT_ID` | 可选 | 回退到 `FUGUE_TENANT_ID` | Fugue API 调用使用的 tenant id。如果 gateway 使用的是可见多个 tenant 的 bootstrap/admin key，建议显式设置它；若使用 tenant-scoped API key，通常可以省略。 |
| `ARGUS_FUGUE_PROJECT_ID` | `fugue` 模式下必需 | 未设置 | 放置 gateway 管理 session app 的 Fugue project。一个逻辑 Argus session 会映射成这个项目里的一个 Fugue app。 |
| `ARGUS_FUGUE_RUNTIME_IMAGE` | 可选；当下面任一 runtime app 选择器已设置时可省略 | 未设置 | 新建 session app 时显式使用的运行时镜像引用。适合你已经自行发布 runtime 镜像的场景。 |
| `ARGUS_FUGUE_RUNTIME_APP_ID` | 可选 | 未设置 | 从一个已部署的 Fugue app id 解析 session 镜像。 |
| `ARGUS_FUGUE_RUNTIME_APP_NAME` | 可选 | 未设置 | 从 `ARGUS_FUGUE_PROJECT_ID` 内一个已部署的 Fugue app 名称解析 session 镜像。 |
| `ARGUS_FUGUE_RUNTIME_COMPOSE_SERVICE` | 可选 | 未设置 | 从 `ARGUS_FUGUE_PROJECT_ID` 内 `source.compose_service` 匹配该值的 Fugue app 解析 session 镜像。对仓库自带的 `fugue.yaml` 来说，这是最稳妥的做法，因为即使最终 app 名被 Fugue 自动加后缀，也不会失效。 |
| `ARGUS_FUGUE_RUNTIME_ID` | 可选 | `runtime_managed_shared` | 新建 session app 默认使用的 Fugue runtime target id。 |
| `ARGUS_GATEWAY_INTERNAL_HOST` | `fugue` 模式下必需；若设置了 `ARGUS_FUGUE_GATEWAY_COMPOSE_SERVICE` 可省略 | `docker` 模式下自动探测 gateway 容器名，失败后回退到 `gateway` | session app 回连 gateway（`/mcp`、`/nodes/ws`、`/openai/v1`）时使用的集群内主机名。在 Fugue 模式下，它应当能从同一集群 / 项目内的其他 app 解析到 gateway。 |
| `ARGUS_FUGUE_GATEWAY_COMPOSE_SERVICE` | 可选 | 未设置 | 不再写死最终 app 名，而是根据 Fugue project id 加 `compose_service` 别名规则计算 gateway 回调地址。仓库自带 `fugue.yaml` 应使用 `gateway`。 |
| `ARGUS_FUGUE_WORKSPACE_MOUNT_PATH` | 可选 | `/workspace` | Fugue 为每个 session app 挂载持久化存储时使用的容器内路径。 |
| `ARGUS_FUGUE_WORKSPACE_STORAGE_SIZE` | 可选 | `10Gi` | 每个 Fugue session app 申请的持久化存储大小。 |
| `ARGUS_FUGUE_WORKSPACE_STORAGE_CLASS` | 可选 | 未设置 | 需要指定 PVC storage class 时使用。 |
| `ARGUS_FUGUE_SERVICE_PORT` | 可选 | `7777` | 每个 Fugue session app 内部 app-server bridge 监听的 TCP 端口。 |
| `ARGUS_FUGUE_APP_NAME_PREFIX` | 可选 | `argus-session` | Fugue app 名称前缀，最终类似 `argus-session-<sessionId>`。 |

### 模型提供方 / OpenAI 兼容代理

| 变量 | 是否必需 | 默认值 | 作用 / 注意事项 |
| --- | --- | --- | --- |
| `OPENAI_API_KEY` | 对默认 Codex runtime 来说可选但强烈推荐 | 未设置 | 仅供内置 `gateway` 渠道使用的长期 API Key，只保存在 **gateway** 上，不会直接注入 runtime 容器。 |
| `ARGUS_OPENAI_API_KEY` | 可选 | 未设置 | `OPENAI_API_KEY` 的兼容别名。内置 `gateway` 渠道会先读 `OPENAI_API_KEY`，再回退到这个名字。 |
| `ARGUS_OPENAI_RESPONSES_UPSTREAM_URL` | 可选 | `https://api.openai.com/v1/responses` | 内置 `gateway` 渠道真正转发到的 Responses API 地址。若你想改成自己的 OpenAI-compatible 提供方、公司内网代理或自建网关，就改这里。 |
| `ARGUS_OPENAI_TOKEN` | 可选 | 回退到 `ARGUS_TOKEN` | gateway 用来派生 `/openai/v1/responses` session 级 Bearer token 的 master secret。注意：在 gateway 进程里它表示 master secret；在 runtime 容器里同名变量表示**派生后的 session token**。它只控制代理访问权限，真正走哪个上游由当前渠道决定。 |
| `ARGUS_GATEWAY_INTERNAL_HOST` | `fugue` 模式下必需；`docker` 模式下可选 | `docker` 模式下自动探测当前 gateway 容器名，失败后回退到 `gateway` | runtime 回连 gateway（`/mcp`、`/nodes/ws`、`/openai/v1`）时使用的主机名。即使用户切换渠道，runtime 里仍保持这个固定的 gateway 地址。在 Fugue 模式下，这应该是 gateway service 的集群内地址。 |

渠道行为：

- 内置 `gateway`：默认选中；使用 `OPENAI_API_KEY` + `ARGUS_OPENAI_RESPONSES_UPSTREAM_URL`。
- 内置 `0-0.pro`：固定 Base URL 为 `https://api.0-0.pro/v1`；每个用户自己在 Telegram 菜单里填 API Key。
- 自定义渠道：每个用户都可以增删改自己的 OpenAI-compatible `baseUrl` + API Key。
- 渠道列表和用户 API Key 都存放在 `${ARGUS_HOME_HOST_PATH}/gateway/state.json`；请把它当作带 secrets 的文件保护。
- “当前渠道”是**用户级全局状态**：切换一次，会影响该用户现有和未来的所有 agent / 容器。

### Web UI 与 Telegram bot

| 变量 | 是否必需 | 默认值 | 作用 / 注意事项 |
| --- | --- | --- | --- |
| `NEXT_PUBLIC_ARGUS_WS_URL` | 可选 | 未设置 | 可选 Web UI 的**构建期** WebSocket 预设地址。修改后需要重新 build `web`。常见示例：`ws://127.0.0.1:8080/ws?token=...`。如果页面本身跑在 HTTPS 下，但预设仍然是 `ws://`，前端会自动改用当前页面同源的 `wss://`，同时保留原来的路径和 query。 |
| `TELEGRAM_BOT_TOKEN` | `docker compose --profile tg ...` 时必需 | 未设置 | 从 `@BotFather` 获取的 bot token；缺失时 Telegram bot 服务会直接退出。 |
| `TELEGRAM_DRAFT_STREAMING` | 可选 | `auto` | 控制 gateway 在私聊里是否发送实时草稿。接受形式：`auto` / `on` / `true`、`force` / `always`、`off`。 |
| `HOST` | 可选辅助变量 | `127.0.0.1` | 主要给文档示例、Web 预设和 Telegram bot 自动推导 gateway 地址使用。它**不是**安全配置项，gateway 自身也不依赖它做鉴权。Docker 场景下会在需要时自动改用 `gateway`。 |
| `ARGUS_GATEWAY_WS_URL` | 可选 | 从 `HOST` 或 `NEXT_PUBLIC_ARGUS_WS_URL` 推导 | 当 `apps/telegram-bot` 不在默认 Compose 拓扑里运行时，用它显式指定 WebSocket 地址。 |
| `ARGUS_GATEWAY_HTTP_URL` | 可选 | 从 `ARGUS_GATEWAY_WS_URL` 或 `HOST` 推导 | 给 `apps/telegram-bot` 显式指定 HTTP base URL。适合 WS / HTTP 分流，或者自动推导不正确的部署。 |
| `ARGUS_CWD` | 可选 | `/workspace` | `apps/telegram-bot` 创建 / 恢复线程时默认使用的工作目录。 |
| `STATE_PATH` | 可选 | 容器内默认 `/data/state.json`；容器外默认 `./state.json` | `apps/telegram-bot` 的本地状态文件路径。只有你直接运行 bot（而不是用当前 compose volume）时才需要关心。 |
| `TELEGRAM_ADMIN_CHAT_IDS` | 预留 / 当前版本未使用 | 未设置 | `docker-compose.yml` 里保留了这个变量，但当前代码并没有读取它；现在可以安全忽略。 |

### 独立 node-host（`apps/node-host`）

如果你只使用 runtime 容器里内置的 node-host，通常**不需要**自己设置下面这些变量；gateway 会自动把连接参数注入进去。以下变量主要用于你在别的机器（例如自己的 Mac）上手动运行 `apps/node-host`。

| 变量 | 是否必需 | 默认值 | 作用 / 注意事项 |
| --- | --- | --- | --- |
| `ARGUS_NODE_WS_URL` | 必需（除非你改用 `--url`） | 未设置 | `/nodes/ws` 的 WebSocket 地址，例如 `ws://127.0.0.1:8080/nodes/ws?token=...`。 |
| `ARGUS_NODE_ID` | 可选 | 当前机器 hostname | 显示给 gateway 的逻辑 node id。 |
| `ARGUS_NODE_DISPLAY_NAME` | 可选 | 当前机器 hostname | 给人看的节点名称，会出现在节点列表和 system event 中。 |
| `ARGUS_NODE_STATE_DIR` | 可选 | 若有 `APP_HOME` 则为 `$APP_HOME/node-host/<nodeId>`，否则为 `$HOME/.argus/node-host/<nodeId>` | 本地 node-host 状态目录。 |
| `ARGUS_NODE_AUDIT` | 可选 | `true` | 是否把远程命令审计日志输出到 stderr。设为 `0` / `false` 可关闭。 |
| `ARGUS_NODE_AUDIT_MAX_BYTES` | 可选 | `4096` | 每条审计日志中保留的 stdout/stderr 预览最大字节数。 |
| `ARGUS_NODE_AUDIT_STDIN_PREVIEW_BYTES` | 可选 | `256` | 审计日志中保留的 stdin 预览最大字节数。设为 `0` 可不记录 stdin 预览。 |
| `ARGUS_NODE_HANDSHAKE_TIMEOUT_MS` | 可选 | `15000` | WebSocket 握手超时（毫秒）。 |
| `ARGUS_NODE_CONNECT_TIMEOUT_MS` | 可选 | 跟随握手超时推导，通常为 `17000` | WebSocket 整体连接超时。未设置时，如果握手超时启用，则默认为 `ARGUS_NODE_HANDSHAKE_TIMEOUT_MS + 2000`。 |
| `ARGUS_NODE_RECONNECT_DELAY_MS` | 可选 | `1000` | 重连基础退避时间（毫秒）。 |
| `ARGUS_NODE_RECONNECT_DELAY_MAX_MS` | 可选 | `30000` | 重连最大退避时间（毫秒）。 |
| `ARGUS_NODE_RECONNECT_JITTER_PCT` | 可选 | `0.2` | 重连抖动比例（`0` 到 `0.5`）。 |
| `ARGUS_NODE_PING_INTERVAL_MS` | 可选 | `30000` | ping 间隔（毫秒）；设为 `0` 可关闭 ping。 |
| `ARGUS_NODE_PONG_TIMEOUT_MS` | 可选 | `10000` | pong 超时（毫秒）。 |

### 高级：static upstream 模式

如果你**不想**让 Argus 自己创建 / 管理 runtime，可以把 gateway 运行在 `ARGUS_PROVISION_MODE=static`，然后手动指向已经存在的 app-server。

| 变量 | 是否必需 | 默认值 | 作用 / 注意事项 |
| --- | --- | --- | --- |
| `ARGUS_UPSTREAMS_JSON` | 可选高级项 | 未设置 | 用 JSON 对象描述一个或多个命名 upstream。设置后会覆盖 `ARGUS_TCP_HOST` / `ARGUS_TCP_PORT`。每个条目可包含 `host`、`port` 以及可选的 `token`。 |
| `ARGUS_TCP_HOST` | static 模式下可选 | `127.0.0.1` | 当未设置 `ARGUS_UPSTREAMS_JSON` 时，默认上游主机名。 |
| `ARGUS_TCP_PORT` | static 模式下可选 | `7777` | 当未设置 `ARGUS_UPSTREAMS_JSON` 时，默认上游端口。 |

`ARGUS_UPSTREAMS_JSON` 示例：

```json
{
  "default": { "host": "127.0.0.1", "port": 7777, "token": "change-me" },
  "staging": { "host": "10.0.0.25", "port": 7777 }
}
```

### 一般**不要手动设置**的内部变量

当 gateway 创建 runtime 容器或 Fugue app 时，会自动注入一批内部环境变量，包括 `APP_SERVER_CMD`、`APP_HOME`、`APP_WORKSPACE`、`CODEX_HOME`、`ARGUS_SESSION_ID`、`ARGUS_CODEX_MODEL`、`ARGUS_CODEX_MCP_URL`、派生后的 `ARGUS_MCP_TOKEN`、派生后的 `ARGUS_OPENAI_TOKEN`、以及 `ARGUS_NODE_WS_URL`。

这些都属于 runtime bridge 的内部实现细节。正常部署时，优先设置上面那些高层 gateway 变量，而不是自己在 `.env` 里预填这些内部变量。

## 数据目录与工作区（workspace）

默认情况下，Argus 会把数据存到运行 gateway 的机器上：

- `ARGUS_HOME_HOST_PATH`（默认：`${HOME}/.argus`）
  - 网关自动化状态：`${ARGUS_HOME_HOST_PATH}/gateway/state.json`
  - 同时保存每个用户的 API 渠道定义、当前选中的渠道，以及用户自行填写的 API Key；请按 secrets 文件对待。

runtime 在容器内始终使用 `/workspace`，但底层存储会随 provision mode 改变：

- `docker`：每个 runtime session 容器都会把**一个宿主机 workspace 目录**挂载到 `/workspace`。
- `fugue`：每个 runtime session app 都会拥有自己挂载到 `/workspace` 的持久化存储，而 gateway 会在 `ARGUS_HOME_HOST_PATH` / `ARGUS_WORKSPACE_HOST_PATH` 下保留本地镜像，并把受管文件双向同步。

- Codex 的状态（threads/历史记录、配置）默认存放在 `/workspace/.codex`（按 workspace 隔离）。
- 宿主机工作区目录：
  - Telegram 私聊 agent：`${ARGUS_HOME_HOST_PATH}/workspace-<tgid>-<name>`（例如 `${ARGUS_HOME_HOST_PATH}/workspace-182262230-main`）
  - 通用 `/ws` session：`${ARGUS_HOME_HOST_PATH}/workspaces/sess-<sessionId>`
    - 如果设置了 `ARGUS_WORKSPACE_HOST_PATH`，通用 session 会改用 `${ARGUS_WORKSPACE_HOST_PATH}/sess-<sessionId>`。

runtime 会管理这些 workspace 文件：

- `AGENTS.md`（从 `docs/templates/AGENTS.default.md` 同步；模板更新后会自动刷新到 workspace 副本）
- `SOUL.md`（缺失时创建）
- `USER.md`（缺失时创建）
- `HEARTBEAT.md`（缺失时创建）

如果你希望 agent 指令有持久修改，请编辑 `docs/templates/AGENTS.default.md`，不要改某个 workspace 里的 `AGENTS.md` 副本。

这些文件会组成“项目上下文”。每一轮 turn 都会注入 `AGENTS.md` / `SOUL.md` / `USER.md`；**只有 heartbeat turn 才会注入 `HEARTBEAT.md`**。

### Telegram 多用户 Agent（私聊隔离）

如果你使用 `apps/telegram-bot` 接入 Telegram，则网关支持按 Telegram 用户隔离 runtime 容器（agent）：

- **首次 `/start`**：网关会为该 Telegram 私聊用户创建一个专属 `main` agent（一个独立 session 容器 + 独立 workspace），并自动绑定为当前 agent。
  - 宿主机工作区目录命名：`${ARGUS_HOME_HOST_PATH}/workspace-<tgid>-main`
- **重复 `/start`**：不会重复创建；只会复用并确保绑定到自己的 `main`。
- 使用 `/menu` 打开控制面板：
  - **Switch Agent**：切换当前私聊使用的 agent（workspace/session）。
  - **Switch Model**：在 `gpt-5.2` 和 `gpt-5.4` 之间切换当前 agent 的模型。
  - **API Channels**：管理用户级渠道列表（`gateway`、`0-0.pro`、以及你自己添加的自定义渠道），并为你所有 agent / 容器切换当前渠道。
  - **Create Agent**：创建新的 agent 并切换过去（同名会报“已存在”）。
  - **Rename Agent**：重命名当前 agent（仅 owner；不包含 `main`）。
  - **Delete Agent**：删除当前 agent（仅 owner；包含 `main`）。删除 `main` 后，下次会提示创建新的 `main`。
  - **New Conversation**：重置当前 agent 的 main thread（会影响 heartbeat 与私聊路由）。
- 在群聊/话题中发送 `/menu`，使用 **Bind This Chat** 把该 chat/topic 绑定到一个 agent。
- **共享/授权**：管理员可编辑 `${ARGUS_HOME_HOST_PATH}/gateway/state.json`，在对应 `agentId` 下把目标用户的 tgid 加入 `allowedUserIds`。
  - 建议在停止 gateway 后编辑，或编辑后重启（gateway 运行中会持续写回 `state.json`，手动修改可能被覆盖）。

迁移提示：

- 启用该隔离方案不会自动删除旧 session 容器。
- 默认情况下，gateway 启动时会删除 **未被** `${ARGUS_HOME_HOST_PATH}/gateway/state.json` **引用** 的托管 runtime（可设置 `ARGUS_GC_DELETE_ORPHAN_RUNTIMES=off` 关闭，或 `dry-run` 仅预览）。
- 如果旧 session 在 automation state 中仍存在启用的 cron job，它会继续被 gateway 调度执行；要停止请禁用/删除 cron job，并在需要时删除对应 session 容器。

## 自动化（system events / heartbeat / cron）

网关内置一个最小自动化层：

- **system events**：持久化的后台事件队列（cron、node/process 事件等）
- **heartbeat**：处理 system events 与定时任务；即使没有任何 UI 在线也会运行
- **cron**：用 cron 表达式到点入队 system events，再由 heartbeat 消化

自动化状态持久化位置：

- `${ARGUS_HOME_HOST_PATH}/gateway/state.json`

## Nodes（可选）

Nodes 用于让助手在你的设备上执行命令（例如你的 Mac）。

在 Mac 上启动 node-host：

```bash
cd apps/node-host
go build -o argus ./cmd/argus
./argus \
  --url "ws://127.0.0.1:8080/nodes/ws?token=argus-node-v1.<sessionId>.<sig>" \
  --node-id "mac" \
  --display-name "My Mac"
```

备注：

- 每个 runtime session 容器也会内置一个 node-host，并注册为 `runtime:<sessionId>`。
- 在 runtime 内优先用 `node="self"`；如果同时在线的 runtime 不止一个，则用 `node="runtime:<sessionId>"`。
- Node host 是**按 session 隔离**的：
  - 如果服务端开启了认证，必须使用派生 token：`argus-node-v1.<sessionId>.<sig>`（master secret：`ARGUS_NODE_TOKEN`，否则回退 `ARGUS_TOKEN`；`sig = base64url(hmac_sha256(master, sessionId))[:32]`）。
  - 开发模式（未配置认证）下可以不带 token。
- node-host 会把连接/重连状态，以及收到的远程命令审计日志输出到 stderr。
  - 关闭审计日志：`ARGUS_NODE_AUDIT=0`（或 `./argus --audit false ...`）
  - 调整输出：`ARGUS_NODE_AUDIT_MAX_BYTES`、`ARGUS_NODE_AUDIT_STDIN_PREVIEW_BYTES`
- 交互式 CLI：先用 `system.run` + `params={"argv":[...],"pty":true,"yieldMs":0}` 启动作业，再用返回的 `jobId` 调 `process.write`、`process.send_keys`、`process.submit` 或 `process.paste`。

## 可选 Web UI

Web UI 仅用于验证/调试（Telegram 场景不依赖 UI）。

```bash
docker compose --profile web up --build
open http://127.0.0.1:3000
```

## Runtime 配置

默认 runtime（Codex）：

- 安装/构建命令：`ARGUS_RUNTIME_INSTALL_CMD`（默认安装 `@openai/codex`）
- 启动命令：`ARGUS_RUNTIME_CMD`（默认 `codex app-server`）
- 根目录 `Dockerfile` 在没有传入 `APP_SERVER_INSTALL_CMD` 时，也会默认安装 `@openai/codex`。这让仓库自带的 Fugue `runtime` 服务不依赖 Compose 专用 build args 也能直接 build。

## 部署到 Fugue

仓库现在自带 [fugue.yaml](/Users/yanyuming/Downloads/GitHub/argus/fugue.yaml)。推荐的 Fugue 拓扑是：

- `gateway`：公网入口，运行在 `ARGUS_PROVISION_MODE=fugue`
- `runtime`：非公网的模板 app，gateway 会复用它的当前镜像来创建每个 session app
- `telegram-bot`：可选配套服务；只有在你提供 `TELEGRAM_BOT_TOKEN` 时才建议保留

这份 manifest 现在已经包含 `gateway`、`runtime`、`web` 和 `telegram-bot`。对 `web` 来说，你可以不设置 `NEXT_PUBLIC_ARGUS_WS_URL`，让浏览器自动按当前页面同源推导 WebSocket 地址；也可以在需要指向特定网关时提供一个构建期预设。

由于 gateway 需要 `ARGUS_FUGUE_PROJECT_ID`，Fugue 部署不是单步导入，而是先准备 project，再部署进去：

```bash
# 1) 先创建或选定一个 project，并拿到它的 id。
fugue --tenant <tenant> project create argus
fugue --tenant <tenant> -o json project ls

# 2) 填写部署到 Fugue 的环境变量文件。
cp .env.fugue.example .env.fugue

# 3) 从本地源码或 GitHub 直接部署。
fugue --tenant <tenant> --project argus deploy . --env-file .env.fugue
# 或
fugue --tenant <tenant> --project argus deploy github yym68686/argus --env-file .env.fugue
```

说明：

- `.env.fugue` 里的 `ARGUS_FUGUE_PROJECT_ID` 必须是 `--project` 对应现有 project 的 id。
- gateway 通过 `ARGUS_FUGUE_RUNTIME_COMPOSE_SERVICE=runtime` 解析 runtime 模板镜像，不依赖最终 app 名，所以即使 Fugue 因命名冲突给 app 自动加后缀，也不会失效。
- 如果你在 Fugue 上暂时不需要 Telegram，把 [fugue.yaml](/Users/yanyuming/Downloads/GitHub/argus/fugue.yaml) 里的 `telegram-bot` 服务删掉再部署即可。

### OpenAI 代理与 API 渠道

Argus 会让 runtime 容器始终使用固定的 gateway 代理地址（`/openai/v1/responses`）。用户切换渠道是在 gateway 侧完成的，所以已有容器里的 `.codex/config.toml` **不需要**被重写。

说明：

- 内置 `gateway` 渠道默认选中，使用 `OPENAI_API_KEY` / `ARGUS_OPENAI_API_KEY` 加上 `ARGUS_OPENAI_RESPONSES_UPSTREAM_URL`。
- 内置宣传渠道 `0-0.pro` 永远指向 `https://api.0-0.pro/v1`；每个用户需要先填自己的 API Key 才能切过去。
- 用户可以在 Telegram 的 **API Channels** 菜单里增删改其他 OpenAI-compatible 渠道。
- 渠道列表、当前选中的渠道、以及用户填写的 API Key 都存放在 `${ARGUS_HOME_HOST_PATH}/gateway/state.json`。
- “当前渠道”是**用户级全局状态**：会影响该用户现有和未来的所有 agent / 容器。
- 没有归属 Telegram 用户的通用 session，仍然回退到内置 `gateway` 渠道。
- 代理要求每个 session 的派生 Bearer token（master：`ARGUS_OPENAI_TOKEN`；未设置则回退到 `ARGUS_TOKEN`）。
- runtime 会写入一个生成的 `CODEX_HOME/config.toml`（不包含 provider secrets），用于把 Codex 指向 gateway 的 MCP 和代理。
  - 默认 `CODEX_HOME`：`/workspace/.codex`（按 workspace 隔离）
  - 默认模型：`gpt-5.4`（Telegram agent 可在 `/menu` 中切换 `gpt-5.2` / `gpt-5.4`，并按 agent 持久化）
  - 生成的 provider block 会标记成 `OpenAI`，这样 Codex 可以继续使用官方压缩能力，但实际流量仍然走 Argus 的 gateway 代理地址。
- 如果你希望所有用户开箱即用，就在 gateway 上配置 `OPENAI_API_KEY`；否则用户需要先选中一个“已就绪”的个人渠道。

要替换 runtime，在 `docker compose up --build` 之前设置这两个环境变量即可。

高级项：

- `ARGUS_WORKSPACE_HOST_PATH`：可选的通用 `/ws` session workspace 基础目录（必须是绝对路径）。未设置时，通用 session 使用 `${ARGUS_HOME_HOST_PATH}/workspaces/sess-<sessionId>`。

## 资源限制（小机器推荐）

runtime session 容器是由网关动态创建的。你可以通过网关环境变量来限制新建 session 的资源：

```bash
export ARGUS_RUNTIME_CPUS="0.8"
export ARGUS_RUNTIME_MEM_LIMIT="768m"
export ARGUS_RUNTIME_MEMSWAP_LIMIT="768m" # 可选；需要配合 MEM_LIMIT
export ARGUS_RUNTIME_PIDS_LIMIT="512"     # 可选
```

只对**新建** session 生效；已存在容器需要 `docker update` 或删除后重建。

## 安全提示

- 挂载 `/var/run/docker.sock` 等价于授予 **宿主机 root** 权限；不要在无鉴权/无 ACL/无限流的情况下把网关暴露到公网。
- 尽量使用 `Authorization: Bearer ...`，不要把 token 放 URL（容易进日志/历史记录）。

## 仓库结构

- `Dockerfile`: runtime 镜像（JSONL over stdio → TCP `:7777` bridge）
- `apps/api/`: 网关（HTTP + WS `/ws`）
- `apps/telegram-bot/`: Telegram bot（入站 + typing）
- `apps/node-host/`: node-host（Go；设备命令执行）
- `apps/web/`: 可选 Web UI（调试用）
- `docker-compose.yml`: 一键部署
- `REMOTE_CLIENT_GUIDE.md`: 客户端接入（协议、重连、示例）

## Debug

```bash
python3 -m py_compile apps/api/app.py
```
