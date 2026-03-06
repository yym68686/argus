# Argus — 自托管的助手网关（Telegram 优先）

[![Telegram 用户群](https://img.shields.io/badge/Telegram-加入用户群-2CA5E0?logo=telegram&logoColor=white)](https://t.me/argus_hub)

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
- 出站回复由 **gateway** 统一投递（每轮 turn 只发 1 条最终文本）；bot 负责入站消息与 “typing…” 指示。

停止：

```bash
docker compose down
```

## 数据目录与工作区（workspace）

默认情况下，Argus 会把数据存到 Docker 宿主机上：

- `ARGUS_HOME_HOST_PATH`（默认：`${HOME}/.argus`）
  - 网关自动化状态：`${ARGUS_HOME_HOST_PATH}/gateway/state.json`

每个 runtime session 容器只会把**一个宿主机 workspace 目录**挂载到 `/workspace`。

- Codex 的状态（threads/历史记录、配置）默认存放在 `/workspace/.codex`（按 workspace 隔离）。
- 宿主机工作区目录：
  - Telegram 私聊 agent：`${ARGUS_HOME_HOST_PATH}/workspace-<tgid>-<name>`（例如 `${ARGUS_HOME_HOST_PATH}/workspace-182262230-main`）
  - 通用 `/ws` session：`${ARGUS_HOME_HOST_PATH}/workspaces/sess-<sessionId>`
    - 如果设置了 `ARGUS_WORKSPACE_HOST_PATH`，通用 session 会改用 `${ARGUS_WORKSPACE_HOST_PATH}/sess-<sessionId>`。

首次运行时，runtime 会在 workspace 根目录初始化这些文件（**仅在缺失时创建，不会覆盖已有文件**）：

- `AGENTS.md`（来自 `docs/templates/AGENTS.default.md`）
- `SOUL.md`
- `USER.md`
- `HEARTBEAT.md`

这些文件会组成“项目上下文”。每一轮 turn 都会注入 `AGENTS.md` / `SOUL.md` / `USER.md`；**只有 heartbeat turn 才会注入 `HEARTBEAT.md`**。

### Telegram 多用户 Agent（私聊隔离）

如果你使用 `apps/telegram-bot` 接入 Telegram，则网关支持按 Telegram 用户隔离 runtime 容器（agent）：

- **首次 `/start`**：网关会为该 Telegram 私聊用户创建一个专属 `main` agent（一个独立 session 容器 + 独立 workspace），并自动绑定为当前 agent。
  - 宿主机工作区目录命名：`${ARGUS_HOME_HOST_PATH}/workspace-<tgid>-main`
- **重复 `/start`**：不会重复创建；只会复用并确保绑定到自己的 `main`。
- 使用 `/menu` 打开控制面板：
  - **Switch Agent**：切换当前私聊使用的 agent（workspace/session）。
  - **Switch Model**：在 `gpt-5.2` 和 `gpt-5.4` 之间切换当前 agent 的模型。
  - **Create Agent**：创建新的 agent 并切换过去（同名会报“已存在”）。
  - **Rename Agent**：重命名当前 agent（仅 owner；不包含 `main`）。
  - **Delete Agent**：删除当前 agent（仅 owner；包含 `main`）。删除 `main` 后，下次会提示创建新的 `main`。
  - **New Conversation**：重置当前 agent 的 main thread（会影响 heartbeat 与私聊路由）。
- 在群聊/话题中发送 `/menu`，使用 **Bind This Chat** 把该 chat/topic 绑定到一个 agent。
- **共享/授权**：管理员可编辑 `${ARGUS_HOME_HOST_PATH}/gateway/state.json`，在对应 `agentId` 下把目标用户的 tgid 加入 `allowedUserIds`。
  - 建议在停止 gateway 后编辑，或编辑后重启（gateway 运行中会持续写回 `state.json`，手动修改可能被覆盖）。

迁移提示：

- 启用该隔离方案不会自动删除旧 session 容器。
- 默认情况下，gateway 启动时会删除 **未被** `${ARGUS_HOME_HOST_PATH}/gateway/state.json` **引用** 的 docker runtime 容器（可设置 `ARGUS_GC_DELETE_ORPHAN_RUNTIMES=off` 关闭，或 `dry-run` 仅预览）。
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

### OpenAI 凭据（推荐）

为了避免把长期有效的 OpenAI API Key 放进每个 runtime 容器里，建议把 `OPENAI_API_KEY` 只配置在 **gateway** 上。
当 `OPENAI_API_KEY` 存在时，gateway 会暴露一个范围很窄的 `/openai/v1/responses` 代理，并自动把 runtime 配置为走这个代理。

说明：

- Key **不会**被传入 runtime 容器。
- runtime 会写入一个生成的 `CODEX_HOME/config.toml`（不包含任何 secrets），用于把 Codex 指向 gateway 的 MCP 以及可选的 OpenAI 代理。
  - 默认 `CODEX_HOME`：`/workspace/.codex`（按 workspace 隔离）
  - 默认模型：`gpt-5.4`（Telegram agent 可在 `/menu` 中切换 `gpt-5.2` / `gpt-5.4`，并按 agent 持久化）
- 代理要求每个 session 的派生 Bearer token（master：`ARGUS_OPENAI_TOKEN`；未设置则回退到 `ARGUS_TOKEN`）。
- 可选：用 `ARGUS_OPENAI_RESPONSES_UPSTREAM_URL` 覆盖上游地址（默认：`https://api.openai.com/v1/responses`）。

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
