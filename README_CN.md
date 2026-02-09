# Argus — 自托管的助手网关（Telegram 优先）

[英文](./README.md) | [中文](./README_CN.md)

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
- 发送 `/where`（查看当前 `sessionId` / `threadId`）
- 发送 `/new`（为当前 chat 新开一个 thread）
- 直接发一句话测试

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

每个 runtime session 容器会把它挂载到 `/root/.argus`，并使用 `/root/.argus/workspace` 作为工作区。

首次运行时，runtime 会在 workspace 根目录初始化这些文件（**仅在缺失时创建，不会覆盖已有文件**）：

- `AGENTS.md`（来自 `docs/templates/AGENTS.default.md`）
- `SOUL.md`
- `USER.md`
- `HEARTBEAT.md`

这些文件会组成“项目上下文”。每一轮 turn 都会注入 `AGENTS.md` / `SOUL.md` / `USER.md`；**只有 heartbeat turn 才会注入 `HEARTBEAT.md`**。

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
npm i
node index.mjs \
  --url "ws://127.0.0.1:8080/nodes/ws?token=$ARGUS_TOKEN" \
  --node-id "mac" \
  --display-name "My Mac"
```

备注：

- 每个 runtime session 容器也会内置一个 node-host，并注册为 `runtime:<sessionId>`。
- 在 runtime 内优先用 `node="self"`；如果同时在线的 runtime 不止一个，则用 `node="runtime:<sessionId>"`。

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

要替换 runtime，在 `docker compose up --build` 之前设置这两个环境变量即可。

高级项：

- `ARGUS_WORKSPACE_HOST_PATH`：把一个宿主机目录单独挂到 runtime 的 workspace（必须是绝对路径）。未设置时，workspace 默认位于 `${ARGUS_HOME_HOST_PATH}/workspace`。

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
- `apps/node-host/`: node-host（设备命令执行）
- `apps/web/`: 可选 Web UI（调试用）
- `docker-compose.yml`: 一键部署
- `REMOTE_CLIENT_GUIDE.md`: 客户端接入（协议、重连、示例）

## Debug

```bash
python3 -m py_compile apps/api/app.py client_smoke.py
```
