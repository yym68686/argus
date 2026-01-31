# Argus (Docker)

FastAPI 网关 + WebSocket `/ws`，用于把“app-server runtime”（JSONL over stdio）暴露成可被外部客户端连接的服务；同时提供一个内置网页聊天 UI（`/`）。

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

启动（会 build 两个镜像并启动网关）：

```bash
# OrbStack(macOS) 常用 socket 路径；Linux 服务器一般不需要这行
export DOCKER_SOCK="$HOME/.orbstack/run/docker.sock"

docker compose up --build
```

打开：

```bash
open http://127.0.0.1:8080
```

停止：

```bash
docker compose down
```

## 目录结构

- `Dockerfile`: runtime 镜像（app-server JSONL stdio → TCP `:7777`）
- `apps/api/`: FastAPI 网关（HTTP + WS `/ws`），支持 `ARGUS_PROVISION_MODE=docker` 自动创建/销毁后端容器
- `docker-compose.yml`: 本机/服务器一键启动（网关会通过挂载的 `docker.sock` 创建容器）
- `web/`: `chat.html` + 本地调试用 `gateway.mjs`
- `client_smoke.py`: 最小化 smoke client（用于调试后端 TCP JSONL 端口）
- `REMOTE_CLIENT_GUIDE.md`: 外部客户端接入文档（协议/示例/重连）

## Debug

语法检查：

```bash
python3 -m py_compile apps/api/app.py client_smoke.py
```
