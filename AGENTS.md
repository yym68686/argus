# Argus (repo root) — Agent Notes

<INSTRUCTIONS>
## Skills
A skill is a set of local instructions to follow that is stored in a `SKILL.md` file. Below is the list of skills that can be used. Each entry includes a name, description, and file path so you can open the source for full instructions when using a specific skill.
### Available skills
- skill-creator: Guide for creating effective skills. Use when users want to create a new skill (or update an existing skill) that extends the agent's capabilities with specialized knowledge, workflows, or tool integrations.
- skill-installer: Install skills from a curated list or a GitHub repo path. Use when a user asks to list installable skills, install a curated skill, or install a skill from another repo (including private repos).
### How to use skills
- Discovery: The list above is the skills available in this session (name + description + file path). Skill bodies live on disk at the listed paths.
- Trigger rules: If the user names a skill (with `$SkillName` or plain text) OR the task clearly matches a skill's description shown above, you must use that skill for that turn. Multiple mentions mean use them all. Do not carry skills across turns unless re-mentioned.
- Missing/blocked: If a named skill isn't in the list or the path can't be read, say so briefly and continue with the best fallback.
- How to use a skill (progressive disclosure):
  1) After deciding to use a skill, open its `SKILL.md`. Read only enough to follow the workflow.
  2) If `SKILL.md` points to extra folders such as `references/`, load only the specific files needed for the request; don't bulk-load everything.
  3) If `scripts/` exist, prefer running or patching them instead of retyping large code blocks.
  4) If `assets/` or templates exist, reuse them instead of recreating from scratch.
- Coordination and sequencing:
  - If multiple skills apply, choose the minimal set that covers the request and state the order you'll use them.
  - Announce which skill(s) you're using and why (one short line). If you skip an obvious skill, say why.
- Context hygiene:
  - Keep context small: summarize long sections instead of pasting them; only load extra files when needed.
  - Avoid deep reference-chasing: prefer opening only files directly linked from `SKILL.md` unless you're blocked.
  - When variants exist (frameworks, providers, domains), pick only the relevant reference file(s) and note that choice.
- Safety and fallback: If a skill can't be applied cleanly (missing files, unclear instructions), state the issue, pick the next-best approach, and continue.
</INSTRUCTIONS>

## 项目目标

这个仓库的根目录现在是 **Argus 网关项目**（面向“app-server / agent server”一类的 JSONL over stdio 协议）：

- `Dockerfile`: app-server runtime → TCP JSONL bridge（用 `socat` 暴露 `7777`）
- `apps/api/`: FastAPI 网关（HTTP + WebSocket `/ws`）
  - `ARGUS_PROVISION_MODE=docker` 时：每个 WebSocket 连接自动创建一个 runtime 容器；断开后默认保留（由客户端调用 `GET /sessions` / `DELETE /sessions/<id>` 管理）
- `web/`: `chat.html` + 简单本地 `gateway.mjs`（仅用于本机测试）
- `client_smoke.py`: 最小化 JSON-RPC/JSONL 客户端（用于回归/调试）
- `docker-compose.yml`: 一键启动网关（并提前 build runtime 镜像）
- `REMOTE_CLIENT_GUIDE.md`: 给外部客户端接入的详细文档

## 文档职责（避免重复）

- `REMOTE_CLIENT_GUIDE.md` 的职责是：让“另一个 Agent / 同伴”拿到后，能以**最快速度**把当前暴露的外部服务（HTTP/WSS + JSON-RPC/JSONL 语义）适配到任意客户端（Web/Node/Python/移动端等）。
  - 尽量包含：端点、认证方式、最小握手/消息流程、断线重连/恢复 thread 的方式、常见错误速查。
  - 尽量避免：服务端部署细节、Docker 构建细节、与客户端适配无关的长篇背景。
- `README.md` 的职责是：说明如何**构建/运行/调试服务端**（网关与镜像），并指向 `REMOTE_CLIENT_GUIDE.md`（不要重复客户端接入说明）。

## 关键约定 / 易踩坑

- runtime 容器内默认以 `node` 用户运行（UID 通常是 `1000`）
  - 因此宿主机挂载的 `ARGUS_HOME_HOST_PATH` / `ARGUS_WORKSPACE_HOST_PATH` 需要对 UID `1000` 可读写，否则会出现 `Permission denied (os error 13)`。
- 网关的“自动创建容器”模式需要挂载宿主机 `docker.sock`
  - 这在公网环境是高危能力（等价宿主机 root 权限），必须配合认证/授权/限流/审计。
