# Argus 远程客户端接入/操作文档（给同伴/另一个 Agent 用）

> 重要安全提醒：你在聊天里粘贴的 `sk-...` 看起来像“真实密钥/令牌”。这类信息一旦写进文档或代码仓库（或出现在 URL query、代理日志、浏览器历史、截图里）就很容易泄露并被滥用。
>
> - 建议立刻**更换网关 Token**（用随机字符串，不要用任何 API Key）并只用私密渠道发给同伴。
> - 本文档里用 `<ARGUS_TOKEN>` 占位；把真实 token 通过私信/密码管理器/一次性消息发给同伴即可。

## 1. 你要连的服务器信息

先在本地准备环境变量（不要把 token 写进代码仓库/截图/URL）：

```bash
export HOST="<YOUR_SERVER_HOST>"
export ARGUS_TOKEN="<ARGUS_TOKEN>"
```

- 服务器 HOST（IP 或域名）：`$HOST`
- 网关 HTTP（用于健康检查 / sessions 管理）：`http://$HOST:8080`
- 网关健康检查：`http://$HOST:8080/healthz`
  - 期望返回：`{"ok":true}`
- 网关 WebSocket（给程序接入 app-server runtime）：`ws://$HOST:8080/ws`
- 可选：Web UI（用于验证后端；需要服务器启动 web 服务）：`http://$HOST:3000`

认证方式（二选一）：

- 推荐（程序端）：HTTP Header `Authorization: Bearer <ARGUS_TOKEN>`
- 兼容（浏览器/简单客户端）：URL 参数 `?token=<ARGUS_TOKEN>`
  - 完整示例：`ws://$HOST:8080/ws?token=<ARGUS_TOKEN>`

## 2. 系统架构（你在写客户端时需要知道的）

1) 客户端连接网关的 WebSocket：`/ws`  
2) 网关把每条 WebSocket 文本消息当作“一行 JSON”（JSON-RPC 风格）转发给后端  
3) 当前部署模式通常是：**一个 WebSocket 连接 = 网关创建一个 runtime 容器**  
4) 成功连上后，网关会先发一个通知给客户端（可忽略，但建议保存）：
```json
{"method":"argus/session","params":{"id":"<SESSION_ID>","mode":"docker","attached":false}}
```
5) WebSocket 断开后：容器会被保留（你可以通过 `DELETE /sessions/<SESSION_ID>` 手动删除）  
6) 对话线程（thread）会写入 runtime 的持久化 home 目录（由 `ARGUS_HOME_HOST_PATH` 挂载）
   - 恢复“工作上下文”：`thread/resume`
   - 拉取“历史消息/回放记录”：`thread/read` + `includeTurns: true`（用于 UI/客户端刷新后回填聊天记录）

## 3. 最快验证：用浏览器 Web UI（可选）

> 如果服务器没有启动 Web UI（或你不想依赖 UI），可以跳过本节，直接按第 4 节实现你自己的客户端。

1) 打开：`http://$HOST:3000`
2) 页面上找到 `WebSocket URL` 输入框，填：

```
ws://$HOST:8080/ws?token=<ARGUS_TOKEN>
```

3) 点 `Connect`，成功后状态会显示 `connected`，并自动初始化 + 新建/恢复 thread  
4) 输入框里发消息即可（该 UI 会等待 `turn/completed` 后才允许发下一条）

如果你要“重连同一个容器”：

- 通过 `GET /sessions` 拿到 `<SESSION_ID>`
- 然后用带 `session` 的 URL 连接：
```
ws://$HOST:8080/ws?token=<ARGUS_TOKEN>&session=<SESSION_ID>
```

如果 `Connect` 一闪就断开：

- 先检查 `http://$HOST:8080/healthz` 是否正常
- 多数情况下是 token 不对（服务端会用 1008 关闭）或网络/代理阻断 WebSocket

## 4. 客户端协议要点（程序实现必读）

### 4.1 传输层格式

- 传输：WebSocket 文本帧（text frame）
- 每一帧内容：**一个 JSON 对象字符串**（无需手动加 `\n`；网关会自动补 `\n` 再转发给后端）
- 后端返回：也是一条条 JSON（网关按“行”拆开后逐条作为 WebSocket text frame 发回）

### 4.1.1 Session（容器会话）管理接口（可选）

如果网关运行在 `docker` provisioning 模式，提供两条 HTTP API 便于客户端管理容器：

- 列出当前 sessions：
```bash
curl -sS -H "Authorization: Bearer $ARGUS_TOKEN" "http://$HOST:8080/sessions"
```

- 删除某个 session（等价删除容器）：
```bash
curl -sS -X DELETE -H "Authorization: Bearer $ARGUS_TOKEN" "http://$HOST:8080/sessions/<SESSION_ID>"
```

### 4.2 初始化握手（必须）

连接 WebSocket 后，必须先做一次初始化，否则所有请求会报 `Not initialized`：

1) `initialize`（request，有 `id`）
```json
{"method":"initialize","id":0,"params":{"clientInfo":{"name":"my_client","title":"My Client","version":"0.0.1"}}}
```

2) 等待响应（response，有同一个 `id`）
```json
{"id":0,"result":{...}}
```

3) `initialized`（notification，无 `id`）
```json
{"method":"initialized","params":{}}
```

### 4.3 Thread（对话会话）与 Turn（一次提问）

你有两种方式：

- 新开对话：`thread/start`
- 恢复“工作上下文”：`thread/resume`（需要你保存之前拿到的 `thread.id`）
- 拉取“历史消息/回放记录”：`thread/read`（推荐 `includeTurns: true`，把 `thread.turns[].items[]` 里的 `userMessage/agentMessage` 渲染成聊天）

#### 新开 thread
```json
{"method":"thread/start","id":1,"params":{"cwd":"/workspace","approvalPolicy":"never","sandbox":"workspace-write"}}
```

响应示例（拿到 `thread.id`，务必保存）：
```json
{"id":1,"result":{"thread":{"id":"thr_123", "...":"..."}}}
```

#### 恢复 thread（断线重连继续进度）
```json
{"method":"thread/resume","id":2,"params":{"threadId":"thr_123"}}
```

#### 读取 thread（不恢复也可，用于回填历史消息）
```json
{"method":"thread/read","id":4,"params":{"threadId":"thr_123","includeTurns":true}}
```

#### 发起一次对话 turn（发 prompt）
```json
{"method":"turn/start","id":3,"params":{"threadId":"thr_123","input":[{"type":"text","text":"say test"}],"cwd":"/workspace","approvalPolicy":"never","sandboxPolicy":{"type":"externalSandbox","networkAccess":"enabled"}}}
```

之后你会收到大量 `notification`（`method` 字段存在、没有 `id`），典型包括：

- `item/agentMessage/delta`：流式输出的增量文本（你需要拼接显示）
- `item/completed`：某个 item 完成
- `turn/completed`：本次 turn 完成（UI/客户端应在这里“解锁下一次发送”）

### 4.4 Approvals（可能会出现的审批请求）

当模型要执行命令/改文件，服务端可能发来一个**带 `id` 的 request**，例如：

```json
{"id":100,"method":"item/commandExecution/requestApproval","params":{...}}
```

客户端必须回一个 response：

```json
{"id":100,"result":{"decision":"decline"}}
```

> 你的客户端可以先简单实现“永远拒绝”（最安全），或根据产品需求弹窗让用户选 accept/decline。

## 5. 参考实现（最小可跑样例）

### 5.1 Node.js（推荐做 MVP）

依赖：
```bash
npm i ws
```

示例 `client.js`（把 token 通过环境变量注入，不要写死）：
```js
import WebSocket from "ws";

const token = process.env.ARGUS_TOKEN;
if (!token) throw new Error("Missing ARGUS_TOKEN");

const host = process.env.HOST;
if (!host) throw new Error("Missing HOST");

const url = `ws://${host}:8080/ws?token=${encodeURIComponent(token)}`;
const ws = new WebSocket(url);

let nextId = 1;
const pending = new Map();

function send(obj) {
  ws.send(JSON.stringify(obj));
}

function rpc(method, params) {
  const id = nextId++;
  send({ method, id, params });
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

ws.on("open", async () => {
  await rpc("initialize", { clientInfo: { name: "node_client", title: "Node Client", version: "0.0.1" } });
  send({ method: "initialized", params: {} });

  const t = await rpc("thread/start", { cwd: "/workspace", approvalPolicy: "never", sandbox: "workspace-write" });
  const threadId = t.thread.id;
  console.log("threadId =", threadId);

  await rpc("turn/start", {
    threadId,
    input: [{ type: "text", text: "say test" }],
    cwd: "/workspace",
    approvalPolicy: "never",
    sandboxPolicy: { type: "externalSandbox", networkAccess: "enabled" },
  });
});

ws.on("message", (data) => {
  const msg = JSON.parse(String(data));

  // server->client request (approval etc.)
  if (msg.id !== undefined && msg.method) {
    // safest default: decline everything
    send({ id: msg.id, result: { decision: "decline" } });
    return;
  }

  // response
  if (msg.id !== undefined) {
    const p = pending.get(msg.id);
    if (!p) return;
    pending.delete(msg.id);
    if (msg.error) p.reject(new Error(msg.error.message || "RPC error"));
    else p.resolve(msg.result);
    return;
  }

  // notifications (streaming)
  if (msg.method === "item/agentMessage/delta") {
    process.stdout.write(msg.params?.delta ?? "");
  }
  if (msg.method === "turn/completed") {
    process.stdout.write("\n[turn completed]\n");
  }
});

ws.on("close", (code, reason) => console.log("closed:", code, reason.toString()));
ws.on("error", (e) => console.error("ws error:", e));
```

运行：
```bash
ARGUS_TOKEN="<ARGUS_TOKEN>" node client.js
```

### 5.2 Python（可选）

依赖：
```bash
python3 -m pip install websockets
```

实现思路同上：维护自增 `id` + pending map；按上面“初始化/线程/turn”流程发消息；拼 `item/agentMessage/delta`。

## 6. 断线重连“继续上次进度”的实现建议

客户端要做两件事：

1) **保存 threadId**：第一次 `thread/start` 的 response 里拿到 `result.thread.id` 后保存（浏览器可以用 localStorage；桌面/移动端用本地存储）
2) 重连后走：`initialize` → `initialized` → `thread/resume(threadId)` → 继续 `turn/start`

> 注意：恢复的是“对话/上下文 + workspace 文件”，不是“容器里跑着的进程”。容器销毁后，后台进程/内存态都会消失。

## 7. 常见错误速查

- `GET /healthz` 404：你连到的不是 FastAPI 网关（端口被别的服务占了或反代配置错误）
- WebSocket `1006`：网络中断/代理拦截/服务端异常中止（看网关 logs 最快）
- WebSocket `1008`：Unauthorized（token 不对/没带 token）
- 页面 Connect 后立刻断开：十有八九是 token 或网络问题；先跑通 `/healthz`
