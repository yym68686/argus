import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.websockets import WebSocketState


log = logging.getLogger("argus_gateway")


@dataclass(frozen=True)
class Upstream:
    host: str
    port: int
    token: Optional[str] = None


@dataclass(frozen=True)
class DockerProvisionConfig:
    image: str
    network: str
    home_host_path: Optional[str]
    workspace_host_path: Optional[str]
    home_container_path: str = "/agent-home"
    workspace_container_path: str = "/workspace"
    runtime_cmd: Optional[str] = None
    connect_timeout_s: float = 30.0
    jsonl_line_limit_bytes: int = 8 * 1024 * 1024
    container_prefix: str = "argus-session"


@dataclass
class LiveDockerSession:
    session_id: str
    container_id: str
    container_name: str
    upstream_host: str
    upstream_port: int
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    cfg: DockerProvisionConfig
    pump_task: Optional[asyncio.Task[None]]

    attach_lock: asyncio.Lock
    attached_wss: set[WebSocket]
    request_handler_ws: Optional[WebSocket]

    initialized_result: Optional[dict[str, Any]]
    handshake_done: bool
    pending_initialize_ids: set[str]
    initialize_waiters: list[tuple[WebSocket, Any]]

    pending_client_requests: dict[str, tuple[WebSocket, Any]]
    next_upstream_id: int

    pending_server_requests: dict[str, str]
    outbox: deque[str]

    turn_owners_by_thread: dict[str, WebSocket]

    closed: bool = False

    async def attach(self, ws: WebSocket) -> None:
        async with self.attach_lock:
            self.attached_wss.add(ws)
            if self.request_handler_ws is None or self.request_handler_ws.client_state != WebSocketState.CONNECTED:
                self.request_handler_ws = ws

    async def detach(self, ws: WebSocket) -> None:
        async with self.attach_lock:
            self.attached_wss.discard(ws)
            if self.request_handler_ws is ws:
                self.request_handler_ws = None
                for candidate in list(self.attached_wss):
                    if candidate.client_state == WebSocketState.CONNECTED:
                        self.request_handler_ws = candidate
                        break

            for tid, owner in list(self.turn_owners_by_thread.items()):
                if owner is ws:
                    self.turn_owners_by_thread.pop(tid, None)

            for rid, (pending_ws, _) in list(self.pending_client_requests.items()):
                if pending_ws is ws:
                    self.pending_client_requests.pop(rid, None)

            if self.initialize_waiters:
                self.initialize_waiters = [(w, did) for (w, did) in self.initialize_waiters if w is not ws]

    async def reserve_upstream_id(self, ws: WebSocket, downstream_id: Any) -> int:
        async with self.attach_lock:
            rid = self.next_upstream_id
            self.next_upstream_id += 1
            self.pending_client_requests[str(rid)] = (ws, downstream_id)
            return rid

    async def _send_ws(self, ws: WebSocket, text: str) -> bool:
        if ws.client_state != WebSocketState.CONNECTED:
            return False
        try:
            await ws.send_text(text)
            return True
        except Exception:
            return False

    async def broadcast(self, text: str, *, buffer_when_detached: bool = True) -> None:
        async with self.attach_lock:
            targets = [w for w in self.attached_wss if w.client_state == WebSocketState.CONNECTED]

        if not targets:
            if not buffer_when_detached:
                return
            self.outbox.append(text)
            while len(self.outbox) > 2000:
                self.outbox.popleft()
            return

        dead: list[WebSocket] = []
        for w in targets:
            try:
                await w.send_text(text)
            except Exception:
                dead.append(w)

        for w in dead:
            await self.detach(w)

    async def deliver_response(self, msg: dict[str, Any]) -> bool:
        rid = str(msg.get("id"))
        async with self.attach_lock:
            pending = self.pending_client_requests.pop(rid, None)
        if pending is None:
            return False
        ws, downstream_id = pending
        if ws.client_state != WebSocketState.CONNECTED:
            return True
        try:
            rewritten = dict(msg)
            rewritten["id"] = downstream_id
            await ws.send_text(json.dumps(rewritten))
            return True
        except Exception:
            await self.detach(ws)
            return True

    async def resolve_initialize_waiters(self) -> None:
        if self.initialized_result is None:
            return
        async with self.attach_lock:
            waiters = list(self.initialize_waiters)
            self.initialize_waiters.clear()
        if not waiters:
            return
        for ws, downstream_id in waiters:
            if ws.client_state != WebSocketState.CONNECTED:
                continue
            try:
                await ws.send_text(json.dumps({"id": downstream_id, "result": self.initialized_result}))
            except Exception:
                await self.detach(ws)

    async def set_turn_owner(self, *, thread_id: Optional[str], ws: WebSocket) -> None:
        if not thread_id:
            return
        async with self.attach_lock:
            self.turn_owners_by_thread[thread_id] = ws
            self.request_handler_ws = ws

    async def clear_turn_owner(self, thread_id: Optional[str]) -> None:
        if not thread_id:
            return
        async with self.attach_lock:
            self.turn_owners_by_thread.pop(thread_id, None)

    async def deliver_server_request(self, text: str, *, thread_id: Optional[str]) -> None:
        ws: Optional[WebSocket] = None
        async with self.attach_lock:
            if thread_id and thread_id in self.turn_owners_by_thread:
                candidate = self.turn_owners_by_thread.get(thread_id)
                if candidate and candidate.client_state == WebSocketState.CONNECTED:
                    ws = candidate
                    self.request_handler_ws = candidate
            if ws is None and self.request_handler_ws is not None and self.request_handler_ws.client_state == WebSocketState.CONNECTED:
                ws = self.request_handler_ws
            if ws is None:
                for candidate in list(self.attached_wss):
                    if candidate.client_state == WebSocketState.CONNECTED:
                        ws = candidate
                        self.request_handler_ws = candidate
                        break
        if ws is None:
            return
        ok = await self._send_ws(ws, text)
        if not ok:
            await self.detach(ws)

    async def flush_pending(self, ws: WebSocket) -> None:
        if ws.client_state != WebSocketState.CONNECTED:
            return
        async with self.attach_lock:
            is_handler = self.request_handler_ws is ws
            pending_requests = list(self.pending_server_requests.values()) if is_handler else []
        for raw in pending_requests:
            if not await self._send_ws(ws, raw):
                await self.detach(ws)
                return
        while self.outbox:
            raw = self.outbox.popleft()
            if not await self._send_ws(ws, raw):
                self.outbox.appendleft(raw)
                await self.detach(ws)
                break

    async def write_upstream(self, text: str) -> None:
        if not text.endswith("\n"):
            text += "\n"
        self.writer.write(text.encode("utf-8"))
        await self.writer.drain()


@dataclass
class NodeSession:
    node_id: str
    conn_id: str
    ws: WebSocket
    display_name: Optional[str] = None
    platform: Optional[str] = None
    version: Optional[str] = None
    caps: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    connected_at_ms: int = 0
    last_seen_ms: int = 0


@dataclass
class NodeInvokeResult:
    ok: bool
    payload: Optional[Any] = None
    payload_json: Optional[str] = None
    error: Optional[dict[str, Any]] = None


class NodeRegistry:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._nodes_by_id: dict[str, NodeSession] = {}
        self._nodes_by_conn: dict[str, str] = {}
        self._pending: dict[str, tuple[str, asyncio.Future[NodeInvokeResult]]] = {}

    async def register(self, session: NodeSession) -> None:
        async with self._lock:
            existing = self._nodes_by_id.get(session.node_id)
            if existing is not None:
                self._nodes_by_conn.pop(existing.conn_id, None)
                self._nodes_by_id.pop(existing.node_id, None)
                for rid, (pending_node_id, fut) in list(self._pending.items()):
                    if pending_node_id != session.node_id:
                        continue
                    if not fut.done():
                        fut.set_result(
                            NodeInvokeResult(
                                ok=False,
                                error={"code": "NODE_REPLACED", "message": "node replaced by a new connection"},
                            )
                        )
                    self._pending.pop(rid, None)
                try:
                    await existing.ws.close(code=1012, reason="replaced")
                except Exception:
                    pass
            self._nodes_by_id[session.node_id] = session
            self._nodes_by_conn[session.conn_id] = session.node_id

    async def unregister(self, conn_id: str) -> Optional[str]:
        async with self._lock:
            node_id = self._nodes_by_conn.pop(conn_id, None)
            if not node_id:
                return None
            self._nodes_by_id.pop(node_id, None)
            for rid, (pending_node_id, fut) in list(self._pending.items()):
                if pending_node_id != node_id:
                    continue
                if fut.done():
                    self._pending.pop(rid, None)
                    continue
                fut.set_result(
                    NodeInvokeResult(ok=False, error={"code": "NODE_DISCONNECTED", "message": "node disconnected"})
                )
                self._pending.pop(rid, None)
            return node_id

    async def list_connected(self) -> list[dict[str, Any]]:
        async with self._lock:
            sessions = list(self._nodes_by_id.values())
        out: list[dict[str, Any]] = []
        for s in sessions:
            out.append(
                {
                    "nodeId": s.node_id,
                    "displayName": s.display_name,
                    "platform": s.platform,
                    "version": s.version,
                    "caps": s.caps,
                    "commands": s.commands,
                    "connectedAtMs": s.connected_at_ms,
                    "lastSeenMs": s.last_seen_ms,
                }
            )
        out.sort(key=lambda x: x.get("displayName") or x.get("nodeId") or "")
        return out

    async def resolve_node_id(self, ident: str) -> Optional[str]:
        key = (ident or "").strip()
        if not key:
            return None
        async with self._lock:
            if key in self._nodes_by_id:
                return key
            matches = [
                s.node_id
                for s in self._nodes_by_id.values()
                if (s.display_name or "").strip().lower() == key.lower()
            ]
        if len(matches) == 1:
            return matches[0]
        return None

    async def touch(self, node_id: str) -> None:
        now_ms = int(time.time() * 1000)
        async with self._lock:
            s = self._nodes_by_id.get(node_id)
            if s is not None:
                s.last_seen_ms = now_ms

    async def invoke(self, *, node_id: str, command: str, params: Any = None, timeout_ms: Optional[int] = None) -> NodeInvokeResult:
        async with self._lock:
            session = self._nodes_by_id.get(node_id)
        if session is None or session.ws.client_state != WebSocketState.CONNECTED:
            return NodeInvokeResult(ok=False, error={"code": "NOT_CONNECTED", "message": "node not connected"})
        if session.commands and command not in session.commands:
            return NodeInvokeResult(ok=False, error={"code": "UNSUPPORTED", "message": f"command not supported: {command}"})

        request_id = uuid.uuid4().hex
        params_json = None
        if params is not None:
            try:
                params_json = json.dumps(params)
            except Exception:
                return NodeInvokeResult(ok=False, error={"code": "BAD_PARAMS", "message": "params must be JSON-serializable"})

        payload = {
            "id": request_id,
            "nodeId": node_id,
            "command": command,
            "paramsJSON": params_json,
            "timeoutMs": timeout_ms,
        }

        fut: asyncio.Future[NodeInvokeResult] = asyncio.get_running_loop().create_future()
        async with self._lock:
            self._pending[request_id] = (node_id, fut)
        try:
            await session.ws.send_text(json.dumps({"type": "event", "event": "node.invoke.request", "payload": payload}))
        except Exception:
            async with self._lock:
                self._pending.pop(request_id, None)
            return NodeInvokeResult(ok=False, error={"code": "UNAVAILABLE", "message": "failed to send invoke to node"})

        try:
            if timeout_ms is None:
                timeout_ms = 30_000
            return await asyncio.wait_for(fut, timeout_ms / 1000.0)
        except asyncio.TimeoutError:
            async with self._lock:
                self._pending.pop(request_id, None)
            return NodeInvokeResult(ok=False, error={"code": "TIMEOUT", "message": "node invoke timed out"})

    async def handle_invoke_result(self, payload: dict[str, Any]) -> bool:
        request_id = str(payload.get("id") or "")
        if not request_id:
            return False
        async with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return False
        _, fut = pending
        if fut.done():
            return False
        fut.set_result(
            NodeInvokeResult(
                ok=bool(payload.get("ok")),
                payload=payload.get("payload"),
                payload_json=payload.get("payloadJSON"),
                error=(payload.get("error") if isinstance(payload.get("error"), dict) else None),
            )
        )
        return True

@dataclass
class McpSessionState:
    session_id: str
    protocol_version: str
    initialized: bool
    client_info: Optional[dict[str, Any]]
    created_at_ms: int
    last_seen_ms: int

def _provision_mode() -> str:
    return os.getenv("ARGUS_PROVISION_MODE", "static").strip().lower()


def _load_upstreams() -> dict[str, Upstream]:
    raw = os.getenv("ARGUS_UPSTREAMS_JSON")
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError("Invalid ARGUS_UPSTREAMS_JSON; must be valid JSON") from e

        out: dict[str, Upstream] = {}
        for key, cfg in data.items():
            out[str(key)] = Upstream(
                host=str(cfg.get("host", "127.0.0.1")),
                port=int(cfg.get("port", 7777)),
                token=(str(cfg["token"]) if "token" in cfg and cfg["token"] is not None else None),
            )
        if not out:
            raise RuntimeError("ARGUS_UPSTREAMS_JSON is empty")
        return out

    host = os.getenv("ARGUS_TCP_HOST", "127.0.0.1")
    port = int(os.getenv("ARGUS_TCP_PORT", "7777"))
    token = os.getenv("ARGUS_TOKEN")
    return {"default": Upstream(host=host, port=port, token=token)}


UPSTREAMS = _load_upstreams()


def _extract_token(ws: WebSocket) -> Optional[str]:
    auth = ws.headers.get("authorization") or ws.headers.get("Authorization")
    if auth:
        parts = auth.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip() or None
        return auth.strip() or None
    return ws.query_params.get("token")


def _extract_token_http(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth:
        parts = auth.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip() or None
        return auth.strip() or None
    return request.query_params.get("token")


def _is_token_valid(expected: Optional[str], provided: Optional[str]) -> bool:
    if expected is None:
        return True
    return provided == expected


app = FastAPI(title="Argus gateway", version="0.1.0")

origins_raw = os.getenv("ARGUS_CORS_ORIGINS") or ""
cors_origins = [o.strip() for o in origins_raw.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["MCP-Session-Id", "MCP-Protocol-Version"],
)


@app.on_event("startup")
async def _startup():
    app.state.sessions_lock = asyncio.Lock()
    app.state.sessions: dict[str, LiveDockerSession] = {}
    app.state.node_registry = NodeRegistry()
    app.state.mcp_lock = asyncio.Lock()
    app.state.mcp_sessions: dict[str, McpSessionState] = {}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def index():
    return PlainTextResponse("Argus gateway is running. Optional web UI runs separately (see README.md).")


@app.get("/chat")
async def chat():
    return PlainTextResponse("No built-in UI. Start the optional web UI separately (see README.md).")


@app.get("/robots.txt")
async def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@app.get("/mcp")
async def mcp_get(request: Request):
    _mcp_require_token(request)
    raise HTTPException(status_code=405, detail="Streamable HTTP GET is not supported on this MCP endpoint")


@app.delete("/mcp")
async def mcp_delete(request: Request):
    _mcp_require_token(request)
    session_id = request.headers.get("mcp-session-id") or ""
    if not session_id:
        raise HTTPException(status_code=400, detail="Missing MCP-Session-Id header")
    async with app.state.mcp_lock:
        existed = session_id in app.state.mcp_sessions
        app.state.mcp_sessions.pop(session_id, None)
    if not existed:
        raise HTTPException(status_code=404, detail="Unknown MCP session")
    return {"ok": True}


@app.post("/mcp")
async def mcp_post(request: Request):
    _mcp_require_token(request)
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if isinstance(body, list):
        # JSON-RPC batch: process each message and return an array of responses.
        # Notifications produce no entry in the response array.
        responses: list[dict[str, Any]] = []
        mcp_session_id_header: Optional[str] = None
        mcp_protocol_version_header: Optional[str] = None

        for item in body:
            if not isinstance(item, dict):
                responses.append(_jsonrpc_error(request_id=None, code=-32600, message="Invalid JSON-RPC message"))
                continue
            resp = await _mcp_handle_single_message(request, item)
            if resp is None:
                continue
            resp_body, hdrs = resp
            responses.append(resp_body)
            mcp_session_id_header = mcp_session_id_header or hdrs.get("MCP-Session-Id")
            mcp_protocol_version_header = mcp_protocol_version_header or hdrs.get("MCP-Protocol-Version")

        if not responses:
            return Response(status_code=202)
        out = JSONResponse(responses, status_code=200)
        if mcp_session_id_header:
            out.headers["MCP-Session-Id"] = mcp_session_id_header
        if mcp_protocol_version_header:
            out.headers["MCP-Protocol-Version"] = mcp_protocol_version_header
        return out

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    single = await _mcp_handle_single_message(request, body)
    if single is None:
        return Response(status_code=202)
    payload, headers = single
    out = JSONResponse(payload, status_code=200)
    for k, v in headers.items():
        out.headers[k] = v
    return out


async def _mcp_handle_single_message(request: Request, body: dict[str, Any]) -> Optional[tuple[dict[str, Any], dict[str, str]]]:
    jsonrpc = body.get("jsonrpc")
    if jsonrpc is not None and jsonrpc != "2.0":
        return (_jsonrpc_error(request_id=body.get("id"), code=-32600, message="Invalid JSON-RPC version"), {})

    has_method = "method" in body
    has_id = "id" in body

    # JSON-RPC notification or response: accept (202) if session is valid, otherwise ignore.
    if not has_method and has_id:
        await _mcp_get_session(request, allow_missing=True)
        return None
    if has_method and not has_id:
        method = str(body.get("method") or "")
        if method == "notifications/initialized":
            sess = await _mcp_get_session(request, allow_missing=True)
            if sess is not None:
                sess.initialized = True
            return None
        if method.startswith("notifications/"):
            await _mcp_get_session(request, allow_missing=True)
            return None
        return None

    if not has_method or not has_id:
        return (_jsonrpc_error(request_id=body.get("id"), code=-32600, message="Invalid JSON-RPC message"), {})

    request_id = body.get("id")
    method = str(body.get("method") or "")
    params = body.get("params")

    if method == "initialize":
        if not isinstance(params, dict):
            return (_jsonrpc_error(request_id=request_id, code=-32602, message="Invalid params"), {})
        client_proto = str(params.get("protocolVersion") or "").strip() or MCP_PROTOCOL_VERSION_LATEST
        negotiated = (
            client_proto
            if client_proto in SUPPORTED_MCP_PROTOCOL_VERSIONS
            else MCP_PROTOCOL_VERSION_LATEST
        )
        client_info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else None

        session_id = uuid.uuid4().hex
        now_ms = int(time.time() * 1000)
        sess = McpSessionState(
            session_id=session_id,
            protocol_version=negotiated,
            # We don't emit server-side notifications, so we can treat initialize as sufficient.
            initialized=True,
            client_info=client_info,
            created_at_ms=now_ms,
            last_seen_ms=now_ms,
        )
        async with app.state.mcp_lock:
            app.state.mcp_sessions[session_id] = sess

        result = {
            "protocolVersion": negotiated,
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": "argus_gateway",
                "title": "Argus Gateway MCP",
                "version": "0.1.0",
            },
            "instructions": "This MCP server exposes tools for listing and invoking connected nodes.",
        }
        return (
            _jsonrpc_result(request_id=request_id, result=result),
            {"MCP-Session-Id": session_id, "MCP-Protocol-Version": negotiated},
        )

    sess = await _mcp_get_session(request, allow_missing=True)
    if sess is None:
        return (_jsonrpc_error(request_id=request_id, code=-32000, message="Not initialized"), {})

    if method == "ping":
        return (_jsonrpc_result(request_id=request_id, result={}), {"MCP-Protocol-Version": sess.protocol_version})

    if method == "tools/list":
        tools = [
            {
                "name": "nodes_list",
                "title": "Nodes List",
                "description": "List connected nodes (devices) and their advertised commands.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "node_invoke",
                "title": "Node Invoke",
                "description": "Invoke a command on a connected node (device).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "node": {"type": "string", "description": "nodeId or displayName"},
                        "command": {"type": "string", "description": "Command name to invoke (must be supported by the node)"},
                        "params": {"type": ["object", "array", "string", "number", "boolean", "null"]},
                        "timeoutMs": {"type": "number"},
                        "idempotencyKey": {"type": "string"},
                    },
                    "required": ["node", "command"],
                    "additionalProperties": False,
                },
            },
        ]
        return (
            _jsonrpc_result(request_id=request_id, result={"tools": tools}),
            {"MCP-Protocol-Version": sess.protocol_version},
        )

    if method == "tools/call":
        if not isinstance(params, dict):
            return (_jsonrpc_error(request_id=request_id, code=-32602, message="Invalid params"), {"MCP-Protocol-Version": sess.protocol_version})
        tool_name = str(params.get("name") or "")
        arguments = params.get("arguments")
        args = arguments if isinstance(arguments, dict) else {}

        if tool_name == "nodes_list":
            nodes = await app.state.node_registry.list_connected()
            return (
                _jsonrpc_result(
                    request_id=request_id,
                    result=_mcp_call_tool_result(
                        content=[{"type": "text", "text": json.dumps({"nodes": nodes}, ensure_ascii=False)}],
                        structured={"nodes": nodes},
                    ),
                ),
                {"MCP-Protocol-Version": sess.protocol_version},
            )

        if tool_name == "node_invoke":
            node_raw = str(args.get("node") or args.get("nodeId") or "").strip()
            cmd = str(args.get("command") or "").strip()
            invoke_params = args.get("params")
            timeout_ms = args.get("timeoutMs")
            if timeout_ms is not None and not isinstance(timeout_ms, (int, float)):
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(
                            content=[{"type": "text", "text": "timeoutMs must be a number (milliseconds)"}],
                            structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "timeoutMs"}},
                            is_error=True,
                        ),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            if not node_raw or not cmd:
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(
                            content=[{"type": "text", "text": "Missing required fields: node, command"}],
                            structured={"ok": False, "error": {"code": "INVALID_ARGUMENT"}},
                            is_error=True,
                        ),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            node_id = await app.state.node_registry.resolve_node_id(node_raw)
            if not node_id:
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(
                            content=[{"type": "text", "text": "Node not found (or ambiguous display name)"}],
                            structured={"ok": False, "error": {"code": "NODE_NOT_FOUND", "node": node_raw}},
                            is_error=True,
                        ),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            res: NodeInvokeResult = await app.state.node_registry.invoke(
                node_id=node_id,
                command=cmd,
                params=invoke_params,
                timeout_ms=int(timeout_ms) if isinstance(timeout_ms, (int, float)) else None,
            )
            payload = res.payload if res.payload is not None else None
            return (
                _jsonrpc_result(
                    request_id=request_id,
                    result=_mcp_call_tool_result(
                        content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                        structured={"ok": res.ok, "nodeId": node_id, "command": cmd, "payload": payload, "error": res.error},
                        is_error=not res.ok,
                    ),
                ),
                {"MCP-Protocol-Version": sess.protocol_version},
            )

        return (
            _jsonrpc_result(
                request_id=request_id,
                result=_mcp_call_tool_result(
                    content=[{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                    structured={"ok": False, "error": {"code": "UNKNOWN_TOOL", "message": tool_name}},
                    is_error=True,
                ),
            ),
            {"MCP-Protocol-Version": sess.protocol_version},
        )

    return (
        _jsonrpc_error(request_id=request_id, code=-32601, message=f"Method not found: {method}"),
        {"MCP-Protocol-Version": sess.protocol_version},
    )

    jsonrpc = body.get("jsonrpc")
    if jsonrpc is not None and jsonrpc != "2.0":
        return JSONResponse(
            _jsonrpc_error(request_id=body.get("id"), code=-32600, message="Invalid JSON-RPC version"),
            status_code=200,
        )

    has_method = "method" in body
    has_id = "id" in body

    # JSON-RPC notification or response: accept (202) if session is valid, otherwise error.
    if not has_method and has_id:
        await _mcp_get_session(request)
        return Response(status_code=202)
    if has_method and not has_id:
        method = str(body.get("method") or "")
        if method != "initialize":
            await _mcp_get_session(request)
        if method == "notifications/initialized":
            sess = await _mcp_get_session(request)
            if sess is not None:
                sess.initialized = True
            return Response(status_code=202)
        if method.startswith("notifications/"):
            return Response(status_code=202)
        return Response(status_code=202)

    if not has_method or not has_id:
        return JSONResponse(
            _jsonrpc_error(request_id=body.get("id"), code=-32600, message="Invalid JSON-RPC message"),
            status_code=200,
        )

    request_id = body.get("id")
    method = str(body.get("method") or "")
    params = body.get("params")

    if method == "initialize":
        if not isinstance(params, dict):
            return JSONResponse(
                _jsonrpc_error(request_id=request_id, code=-32602, message="Invalid params"),
                status_code=200,
            )
        client_proto = str(params.get("protocolVersion") or "").strip() or MCP_PROTOCOL_VERSION_LATEST
        negotiated = (
            client_proto
            if client_proto in SUPPORTED_MCP_PROTOCOL_VERSIONS
            else MCP_PROTOCOL_VERSION_LATEST
        )
        client_info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else None

        session_id = uuid.uuid4().hex
        now_ms = int(time.time() * 1000)
        sess = McpSessionState(
            session_id=session_id,
            protocol_version=negotiated,
            initialized=False,
            client_info=client_info,
            created_at_ms=now_ms,
            last_seen_ms=now_ms,
        )
        async with app.state.mcp_lock:
            app.state.mcp_sessions[session_id] = sess

        result = {
            "protocolVersion": negotiated,
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": "argus_gateway",
                "title": "Argus Gateway MCP",
                "version": "0.1.0",
            },
            "instructions": "This MCP server exposes tools for listing and invoking connected nodes.",
        }
        resp = JSONResponse(_jsonrpc_result(request_id=request_id, result=result), status_code=200)
        resp.headers["MCP-Session-Id"] = session_id
        return resp

    sess = await _mcp_get_session(request)
    if sess is None:
        return JSONResponse(_jsonrpc_error(request_id=request_id, code=-32000, message="Not initialized"), status_code=200)
    if not sess.initialized and method != "ping":
        return JSONResponse(_jsonrpc_error(request_id=request_id, code=-32000, message="Not initialized"), status_code=200)

    if method == "ping":
        return JSONResponse(_jsonrpc_result(request_id=request_id, result={}), status_code=200)

    if method == "tools/list":
        tools = [
            {
                "name": "nodes_list",
                "title": "Nodes List",
                "description": "List connected nodes (devices) and their advertised commands.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "node_invoke",
                "title": "Node Invoke",
                "description": "Invoke a command on a connected node (device).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "node": {"type": "string", "description": "nodeId or displayName"},
                        "command": {"type": "string", "description": "Command name to invoke (must be supported by the node)"},
                        "params": {"type": ["object", "array", "string", "number", "boolean", "null"]},
                        "timeoutMs": {"type": "number"},
                        "idempotencyKey": {"type": "string"},
                    },
                    "required": ["node", "command"],
                    "additionalProperties": False,
                },
            },
        ]
        return JSONResponse(_jsonrpc_result(request_id=request_id, result={"tools": tools}), status_code=200)

    if method == "tools/call":
        if not isinstance(params, dict):
            return JSONResponse(_jsonrpc_error(request_id=request_id, code=-32602, message="Invalid params"), status_code=200)
        tool_name = str(params.get("name") or "").strip()
        args = params.get("arguments")
        if not isinstance(args, dict):
            args = {}

        if tool_name == "nodes_list":
            nodes = await app.state.node_registry.list_connected()
            return JSONResponse(
                _jsonrpc_result(
                    request_id=request_id,
                    result=_mcp_call_tool_result(
                        content=[{"type": "text", "text": f"{len(nodes)} node(s) connected"}],
                        structured={"nodes": nodes},
                    ),
                ),
                status_code=200,
            )

        if tool_name == "node_invoke":
            node_raw = str(args.get("node") or "").strip()
            cmd = str(args.get("command") or "").strip()
            timeout_ms = args.get("timeoutMs")
            if timeout_ms is not None:
                try:
                    timeout_ms = int(timeout_ms)
                except Exception:
                    timeout_ms = None
            invoke_params = args.get("params")

            if not node_raw or not cmd:
                return JSONResponse(
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(
                            content=[{"type": "text", "text": "node_invoke requires 'node' and 'command'"}],
                            structured={"ok": False, "error": {"code": "BAD_INPUT", "message": "missing node/command"}},
                            is_error=True,
                        ),
                    ),
                    status_code=200,
                )

            node_id = await app.state.node_registry.resolve_node_id(node_raw)
            if not node_id:
                return JSONResponse(
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(
                            content=[{"type": "text", "text": "Node not found (or ambiguous display name)"}],
                            structured={"ok": False, "error": {"code": "NOT_FOUND", "message": "node not found"}},
                            is_error=True,
                        ),
                    ),
                    status_code=200,
                )

            res: NodeInvokeResult = await app.state.node_registry.invoke(
                node_id=node_id,
                command=cmd,
                params=invoke_params,
                timeout_ms=timeout_ms,
            )

            if not res.ok:
                return JSONResponse(
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(
                            content=[{"type": "text", "text": res.error.get("message") if res.error else "node invoke failed"}],
                            structured={"ok": False, "nodeId": node_id, "command": cmd, "error": res.error},
                            is_error=True,
                        ),
                    ),
                    status_code=200,
                )

            payload: Any = res.payload
            if payload is None and res.payload_json:
                try:
                    payload = json.loads(res.payload_json)
                except Exception:
                    payload = res.payload_json
            return JSONResponse(
                _jsonrpc_result(
                    request_id=request_id,
                    result=_mcp_call_tool_result(
                        content=[{"type": "text", "text": "ok"}],
                        structured={"ok": True, "nodeId": node_id, "command": cmd, "payload": payload},
                    ),
                ),
                status_code=200,
            )

        return JSONResponse(
            _jsonrpc_result(
                request_id=request_id,
                result=_mcp_call_tool_result(
                    content=[{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                    structured={"ok": False, "error": {"code": "UNKNOWN_TOOL", "message": tool_name}},
                    is_error=True,
                ),
            ),
            status_code=200,
        )

    return JSONResponse(_jsonrpc_error(request_id=request_id, code=-32601, message=f"Method not found: {method}"), status_code=200)


def _http_require_token(request: Request):
    expected = os.getenv("ARGUS_TOKEN") or None
    if expected is None:
        return
    provided = _extract_token_http(request)
    if provided != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


SUPPORTED_MCP_PROTOCOL_VERSIONS = [
    # Latest MCP spec revision in /contextify/txt/mcp.txt
    "2025-11-25",
    # Current Codex MCP client references this revision.
    "2025-06-18",
]
MCP_PROTOCOL_VERSION_LATEST = SUPPORTED_MCP_PROTOCOL_VERSIONS[0]


def _mcp_require_token(request: Request):
    expected = os.getenv("ARGUS_MCP_TOKEN") or os.getenv("ARGUS_TOKEN") or None
    if expected is None:
        return
    provided = _extract_token_http(request)
    if provided != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _jsonrpc_error(*, request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}


def _jsonrpc_result(*, request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _mcp_call_tool_result(*, content: list[dict[str, Any]], structured: Any = None, is_error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": content}
    if structured is not None:
        out["structuredContent"] = structured
    if is_error:
        out["isError"] = True
    return out


async def _mcp_get_session(request: Request, *, allow_missing: bool = False) -> Optional[McpSessionState]:
    session_id = request.headers.get("mcp-session-id") or ""
    if not session_id:
        if allow_missing:
            return None
        raise HTTPException(status_code=400, detail="Missing MCP-Session-Id header")
    async with app.state.mcp_lock:
        sess = app.state.mcp_sessions.get(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="Unknown MCP session")
    proto = request.headers.get("mcp-protocol-version") or ""
    # Some clients omit MCP-Protocol-Version on streamable HTTP requests.
    # Prefer compatibility: accept missing/mismatched values (we still track the
    # negotiated protocol version from initialize()).
    if proto and proto != sess.protocol_version:
        log.warning(
            "MCP protocol header mismatch for session %s (got %s, expected %s)",
            session_id,
            proto,
            sess.protocol_version,
        )
    sess.last_seen_ms = int(time.time() * 1000)
    return sess


def _docker_cfg() -> DockerProvisionConfig:
    image = os.getenv("ARGUS_RUNTIME_IMAGE", "argus-runtime")
    network = os.getenv("ARGUS_DOCKER_NETWORK", "argus-net")

    home_host_path = os.getenv("ARGUS_HOME_HOST_PATH") or None
    workspace_host_path = os.getenv("ARGUS_WORKSPACE_HOST_PATH") or None
    runtime_cmd = os.getenv("ARGUS_RUNTIME_CMD") or None

    if home_host_path and not os.path.isabs(home_host_path):
        raise RuntimeError("ARGUS_HOME_HOST_PATH must be an absolute host path")
    if workspace_host_path and not os.path.isabs(workspace_host_path):
        raise RuntimeError("ARGUS_WORKSPACE_HOST_PATH must be an absolute host path")

    connect_timeout_s = float(os.getenv("ARGUS_CONNECT_TIMEOUT_S", "30"))
    try:
        jsonl_line_limit_bytes = int(os.getenv("ARGUS_JSONL_LINE_LIMIT_BYTES", str(8 * 1024 * 1024)))
    except Exception:
        jsonl_line_limit_bytes = 8 * 1024 * 1024
    container_prefix = os.getenv("ARGUS_CONTAINER_PREFIX", "argus-session")

    return DockerProvisionConfig(
        image=image,
        network=network,
        home_host_path=home_host_path,
        workspace_host_path=workspace_host_path,
        runtime_cmd=runtime_cmd,
        connect_timeout_s=connect_timeout_s,
        jsonl_line_limit_bytes=jsonl_line_limit_bytes,
        container_prefix=container_prefix,
    )


def _docker_create_container_sync(cfg: DockerProvisionConfig, session_id: str):
    try:
        import docker  # type: ignore
        from docker.errors import ImageNotFound, NotFound  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("Docker provision mode requires the 'docker' Python package") from e

    client = docker.from_env()

    try:
        client.networks.get(cfg.network)
    except NotFound as e:
        raise RuntimeError(
            f"Docker network '{cfg.network}' not found. "
            "Create it (or run via docker-compose that defines it) and attach the gateway to it."
        ) from e

    name = f"{cfg.container_prefix}-{session_id}"

    env = {}
    if cfg.runtime_cmd:
        env["APP_SERVER_CMD"] = cfg.runtime_cmd
    env["APP_HOME"] = cfg.home_container_path
    # Pass gateway tokens through to the runtime so the agent can authenticate
    # against gateway-provided services (e.g. MCP at /mcp).
    #
    # NOTE: Codex rejects inline bearer tokens in config.toml for streamable_http;
    # use `bearer_token_env_var` and read from these env vars instead.
    gateway_token = os.getenv("ARGUS_TOKEN") or None
    mcp_token = os.getenv("ARGUS_MCP_TOKEN") or gateway_token
    if gateway_token:
        env["ARGUS_TOKEN"] = gateway_token
    if mcp_token:
        env["ARGUS_MCP_TOKEN"] = mcp_token
    volumes = {}
    if cfg.home_host_path:
        volumes[cfg.home_host_path] = {"bind": cfg.home_container_path, "mode": "rw"}
    if cfg.workspace_host_path:
        volumes[cfg.workspace_host_path] = {"bind": cfg.workspace_container_path, "mode": "rw"}

    labels = {
        "io.argus.gateway": "apps/api",
        "io.argus.session_id": session_id,
    }

    run_kwargs = {}
    if cfg.workspace_host_path:
        run_kwargs["working_dir"] = cfg.workspace_container_path

    try:
        container = client.containers.run(
            cfg.image,
            name=name,
            detach=True,
            network=cfg.network,
            environment=env,
            volumes=volumes,
            labels=labels,
            **run_kwargs,
        )
    except ImageNotFound as e:
        raise RuntimeError(
            f"Docker image '{cfg.image}' not found. Build it first (e.g. 'docker build -t {cfg.image} ...')."
        ) from e

    return container


def _docker_container_ip_sync(container, network: str) -> Optional[str]:
    container.reload()
    nets = container.attrs.get("NetworkSettings", {}).get("Networks", {}) or {}
    net = nets.get(network)
    if not net:
        return None
    ip = net.get("IPAddress")
    return ip or None


def _docker_remove_container_sync(container):
    try:
        container.remove(force=True)
    except Exception:
        pass


def _docker_list_argus_containers_sync():
    try:
        import docker  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("Docker provision mode requires the 'docker' Python package") from e

    client = docker.from_env()
    containers = client.containers.list(
        all=True,
        filters={"label": ["io.argus.gateway=apps/api"]},
    )

    out = []
    for c in containers:
        labels = getattr(c, "labels", None) or {}
        out.append(
            {
                "sessionId": labels.get("io.argus.session_id"),
                "containerId": c.id,
                "name": c.name,
                "status": c.status,
            }
        )

    out.sort(key=lambda x: (x.get("status") != "running", x.get("name") or ""))
    return out


def _docker_get_container_by_session_sync(session_id: str):
    try:
        import docker  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("Docker provision mode requires the 'docker' Python package") from e

    client = docker.from_env()
    containers = client.containers.list(
        all=True,
        filters={
            "label": [
                "io.argus.gateway=apps/api",
                f"io.argus.session_id={session_id}",
            ]
        },
    )
    return containers[0] if containers else None


def _docker_ensure_running_sync(container):
    container.reload()
    if container.status != "running":
        container.start()
        container.reload()
    return container


async def _docker_wait_for_ip(container, network: str, timeout_s: float) -> str:
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_ip: Optional[str] = None
    while True:
        last_ip = await asyncio.to_thread(_docker_container_ip_sync, container, network)
        if last_ip:
            return last_ip
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("Timed out waiting for container IP")
        await asyncio.sleep(0.1)


async def _wait_for_tcp(host: str, port: int, timeout_s: float):
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_err: Optional[Exception] = None
    while True:
        try:
            return await asyncio.open_connection(host, port)
        except Exception as e:  # noqa: BLE001
            last_err = e
            if asyncio.get_running_loop().time() >= deadline:
                raise last_err
            await asyncio.sleep(0.2)


async def _read_jsonl_line(
    reader: asyncio.StreamReader,
    buffer: bytearray,
    *,
    max_line_bytes: int,
    chunk_size: int = 4096,
) -> Optional[bytes]:
    while True:
        nl = buffer.find(b"\n")
        if nl != -1:
            line = bytes(buffer[:nl])
            del buffer[: nl + 1]
            return line
        if len(buffer) > max_line_bytes:
            raise ValueError(f"JSONL line exceeded {max_line_bytes} bytes without newline")
        chunk = await reader.read(chunk_size)
        if not chunk:
            if buffer:
                line = bytes(buffer)
                buffer.clear()
                return line
            return None
        buffer.extend(chunk)


@app.get("/sessions")
async def list_sessions(request: Request):
    _http_require_token(request)
    if _provision_mode() != "docker":
        raise HTTPException(status_code=400, detail="Not in docker provision mode")
    try:
        sessions = await asyncio.to_thread(_docker_list_argus_containers_sync)
    except Exception as e:
        log.exception("Failed to list docker sessions")
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"sessions": sessions}


@app.get("/nodes")
async def list_nodes(request: Request):
    _http_require_token(request)
    nodes = await app.state.node_registry.list_connected()
    return {"nodes": nodes}


@app.post("/nodes/invoke")
async def invoke_node(request: Request):
    _http_require_token(request)
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    node_raw = str(body.get("node") or body.get("nodeId") or "").strip()
    command = str(body.get("command") or "").strip()
    params = body.get("params")
    timeout_ms = body.get("timeoutMs")

    if not node_raw:
        raise HTTPException(status_code=400, detail="Missing 'node' (or 'nodeId')")
    if not command:
        raise HTTPException(status_code=400, detail="Missing 'command'")
    if timeout_ms is not None and not isinstance(timeout_ms, int):
        raise HTTPException(status_code=400, detail="'timeoutMs' must be an integer (milliseconds)")

    node_id = await app.state.node_registry.resolve_node_id(node_raw)
    if not node_id:
        raise HTTPException(status_code=404, detail="Node not found (or ambiguous display name)")

    res: NodeInvokeResult = await app.state.node_registry.invoke(
        node_id=node_id,
        command=command,
        params=params,
        timeout_ms=timeout_ms,
    )
    return {"ok": res.ok, "nodeId": node_id, "command": command, "payload": res.payload, "payloadJSON": res.payload_json, "error": res.error}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    _http_require_token(request)
    if _provision_mode() != "docker":
        raise HTTPException(status_code=400, detail="Not in docker provision mode")
    try:
        container = await asyncio.to_thread(_docker_get_container_by_session_sync, session_id)
        if container is None:
            raise HTTPException(status_code=404, detail="Session not found")
        async with app.state.sessions_lock:
            live = app.state.sessions.pop(session_id, None)
        if live is not None:
            try:
                live.closed = True
                try:
                    live.writer.close()
                except Exception:
                    pass
                try:
                    live.pump_task.cancel()
                except Exception:
                    pass
            except Exception:
                pass
        await asyncio.to_thread(_docker_remove_container_sync, container)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Failed to delete docker session %s", session_id)
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"ok": True, "sessionId": session_id}


@app.websocket("/nodes/ws")
async def ws_nodes(ws: WebSocket):
    provided = _extract_token(ws)
    expected = os.getenv("ARGUS_NODE_TOKEN") or os.getenv("ARGUS_TOKEN") or None
    if expected and provided != expected:
        await ws.accept()
        await ws.close(code=1008, reason="Unauthorized")
        return

    await ws.accept()
    conn_id = uuid.uuid4().hex[:12]

    try:
        raw = await ws.receive_text()
    except WebSocketDisconnect:
        return

    try:
        msg = json.loads(raw)
    except Exception:
        msg = None

    if not isinstance(msg, dict) or msg.get("type") != "connect":
        await ws.close(code=1008, reason="First message must be {type:'connect', ...}")
        return

    node_id = str(msg.get("nodeId") or "").strip()
    if not node_id:
        await ws.close(code=1008, reason="Missing nodeId")
        return

    display_name = (str(msg.get("displayName")) if msg.get("displayName") is not None else None) or None
    platform = (str(msg.get("platform")) if msg.get("platform") is not None else None) or None
    version = (str(msg.get("version")) if msg.get("version") is not None else None) or None
    caps = [str(x) for x in (msg.get("caps") or [])] if isinstance(msg.get("caps"), list) else []
    commands = [str(x) for x in (msg.get("commands") or [])] if isinstance(msg.get("commands"), list) else []

    now_ms = int(time.time() * 1000)
    session = NodeSession(
        node_id=node_id,
        conn_id=conn_id,
        ws=ws,
        display_name=display_name,
        platform=platform,
        version=version,
        caps=caps,
        commands=commands,
        connected_at_ms=now_ms,
        last_seen_ms=now_ms,
    )
    await app.state.node_registry.register(session)

    try:
        await ws.send_text(json.dumps({"type": "event", "event": "node.connected", "payload": {"nodeId": node_id}}))
    except Exception:
        pass

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                msg = None
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "event" and msg.get("event") == "node.invoke.result" and isinstance(msg.get("payload"), dict):
                payload = msg["payload"]
                if str(payload.get("nodeId") or "") != node_id:
                    continue
                await app.state.node_registry.touch(node_id)
                await app.state.node_registry.handle_invoke_result(payload)
                continue
            if msg.get("type") == "event" and msg.get("event") == "node.heartbeat":
                await app.state.node_registry.touch(node_id)
                continue
    except WebSocketDisconnect:
        pass
    finally:
        await app.state.node_registry.unregister(conn_id)


@app.websocket("/ws")
async def ws_proxy(ws: WebSocket):
    provided = _extract_token(ws)

    if _provision_mode() == "docker":
        expected = os.getenv("ARGUS_TOKEN")
        if expected and provided != expected:
            await ws.accept()
            await ws.close(code=1008, reason="Unauthorized")
            return

        await ws.accept()

        try:
            cfg = _docker_cfg()
        except Exception:
            log.exception("Invalid docker provisioning configuration")
            await ws.close(code=1011, reason="Server misconfigured")
            return
        if not cfg.runtime_cmd:
            await ws.close(code=1011, reason="Server misconfigured: ARGUS_RUNTIME_CMD is not set")
            return
        requested_session = (ws.query_params.get("session") or "").strip() or None
        session_id = requested_session or uuid.uuid4().hex[:12]
        created = False

        async with app.state.sessions_lock:
            live = app.state.sessions.get(session_id)

        if live is None or live.closed:
            try:
                if requested_session:
                    container = await asyncio.to_thread(_docker_get_container_by_session_sync, session_id)
                    if container is None:
                        await ws.close(code=1008, reason="Unknown session")
                        return
                    container = await asyncio.to_thread(_docker_ensure_running_sync, container)
                else:
                    created = True
                    container = await asyncio.to_thread(_docker_create_container_sync, cfg, session_id)

                host = await _docker_wait_for_ip(container, cfg.network, cfg.connect_timeout_s)
                reader, writer = await _wait_for_tcp(host, 7777, cfg.connect_timeout_s)
            except Exception:
                await ws.close(code=1011, reason="Failed to provision upstream container")
                return

            live = LiveDockerSession(
                session_id=session_id,
                container_id=container.id,
                container_name=container.name,
                upstream_host=host,
                upstream_port=7777,
                reader=reader,
                writer=writer,
                cfg=cfg,
                pump_task=None,
                attach_lock=asyncio.Lock(),
                attached_wss=set(),
                request_handler_ws=None,
                initialized_result=None,
                handshake_done=False,
                pending_initialize_ids=set(),
                initialize_waiters=[],
                pending_client_requests={},
                next_upstream_id=1_000_000_000,
                pending_server_requests={},
                outbox=deque(),
                turn_owners_by_thread={},
            )

            async def pump() -> None:
                buf = bytearray()
                try:
                    while True:
                        raw = await _read_jsonl_line(live.reader, buf, max_line_bytes=cfg.jsonl_line_limit_bytes)
                        if raw is None:
                            break
                        text = raw.decode("utf-8", errors="replace").rstrip("\r")

                        msg: Any = None
                        try:
                            msg = json.loads(text)
                        except Exception:
                            msg = None

                        if isinstance(msg, dict):
                            if "id" in msg and "method" in msg:
                                rid = str(msg.get("id"))
                                if rid not in live.pending_server_requests:
                                    live.pending_server_requests[rid] = text
                                params = msg.get("params")
                                thread_id: Optional[str] = None
                                if isinstance(params, dict):
                                    tid = params.get("threadId")
                                    if isinstance(tid, str) and tid.strip():
                                        thread_id = tid.strip()
                                await live.deliver_server_request(text, thread_id=thread_id)
                                continue

                            if "id" in msg and "method" not in msg:
                                rid = str(msg.get("id"))
                                should_resolve = False
                                async with live.attach_lock:
                                    if rid in live.pending_initialize_ids:
                                        live.pending_initialize_ids.discard(rid)
                                        if isinstance(msg.get("result"), dict):
                                            live.initialized_result = msg["result"]
                                            should_resolve = True
                                if should_resolve:
                                    await live.resolve_initialize_waiters()
                                await live.deliver_response(msg)
                                continue

                            if msg.get("method") == "turn/completed":
                                params = msg.get("params")
                                if isinstance(params, dict):
                                    tid = params.get("threadId")
                                    if isinstance(tid, str) and tid.strip():
                                        await live.clear_turn_owner(tid.strip())

                        await live.broadcast(text)
                except Exception:
                    log.exception("Upstream pump failed for session %s", session_id)
                finally:
                    live.closed = True
                    try:
                        live.writer.close()
                    except Exception:
                        pass
                    async with app.state.sessions_lock:
                        current = app.state.sessions.get(session_id)
                        if current is live:
                            app.state.sessions.pop(session_id, None)

            live.pump_task = asyncio.create_task(pump())

            async with app.state.sessions_lock:
                app.state.sessions[session_id] = live

            log.info("Provisioned session %s -> %s:%s", session_id, host, 7777)

        await live.attach(ws)

        try:
            try:
                await ws.send_text(
                    json.dumps(
                        {
                            "method": "argus/session",
                            "params": {
                                "id": session_id,
                                "mode": "docker",
                                "attached": bool(requested_session),
                                "created": created,
                            },
                        }
                    )
                )
            except Exception:
                pass

            await live.flush_pending(ws)

            while True:
                try:
                    text = await ws.receive_text()
                except WebSocketDisconnect:
                    break

                msg: Any = None
                try:
                    msg = json.loads(text)
                except Exception:
                    msg = None

                if isinstance(msg, dict) and msg.get("method") == "initialize":
                    req_id = msg.get("id")
                    if req_id is None:
                        await live.write_upstream(text)
                        continue
                    cached_result: Optional[dict[str, Any]] = None
                    forward: Optional[str] = None
                    async with live.attach_lock:
                        if live.initialized_result is not None:
                            cached_result = live.initialized_result
                        elif live.pending_initialize_ids:
                            live.initialize_waiters.append((ws, req_id))
                        else:
                            upstream_id = live.next_upstream_id
                            live.next_upstream_id += 1
                            live.pending_client_requests[str(upstream_id)] = (ws, req_id)
                            live.pending_initialize_ids.add(str(upstream_id))
                            rewritten = dict(msg)
                            rewritten["id"] = upstream_id
                            forward = json.dumps(rewritten)
                    if cached_result is not None:
                        try:
                            await ws.send_text(json.dumps({"id": req_id, "result": cached_result}))
                        except Exception:
                            pass
                        continue
                    if forward is None:
                        continue
                    await live.write_upstream(forward)
                    continue

                if isinstance(msg, dict) and msg.get("method") == "initialized":
                    if live.handshake_done:
                        continue
                    await live.write_upstream(text)
                    live.handshake_done = True
                    continue

                if isinstance(msg, dict) and "id" in msg and "method" not in msg:
                    rid = str(msg.get("id"))
                    if rid in live.pending_server_requests:
                        live.pending_server_requests.pop(rid, None)
                    await live.write_upstream(text)
                    continue

                if isinstance(msg, dict) and "id" in msg and "method" in msg:
                    downstream_id = msg.get("id")
                    upstream_id = await live.reserve_upstream_id(ws, downstream_id)
                    rewritten = dict(msg)
                    rewritten["id"] = upstream_id
                    if rewritten.get("method") == "turn/start":
                        params = rewritten.get("params")
                        if isinstance(params, dict):
                            tid = params.get("threadId")
                            if isinstance(tid, str) and tid.strip():
                                await live.set_turn_owner(thread_id=tid.strip(), ws=ws)
                    await live.write_upstream(json.dumps(rewritten))
                    continue

                await live.write_upstream(text)
        finally:
            await live.detach(ws)
            log.info("Session %s detached (codex process retained)", session_id)

        return

    upstream_id = ws.query_params.get("id")
    if upstream_id:
        upstream = UPSTREAMS.get(upstream_id)
        if not upstream:
            await ws.accept()
            await ws.close(code=1008, reason="Unknown upstream id")
            return
    else:
        if len(UPSTREAMS) == 1:
            upstream = next(iter(UPSTREAMS.values()))
        else:
            matches = [u for u in UPSTREAMS.values() if _is_token_valid(u.token, provided)]
            if len(matches) != 1:
                await ws.accept()
                await ws.close(code=1008, reason="Unauthorized")
                return
            upstream = matches[0]

    if not _is_token_valid(upstream.token, provided):
        await ws.accept()
        await ws.close(code=1008, reason="Unauthorized")
        return

    await ws.accept()

    try:
        reader, writer = await asyncio.open_connection(upstream.host, upstream.port)
    except Exception:
        await ws.close(code=1011, reason="Failed to connect to upstream")
        return

    async def ws_to_tcp():
        try:
            while True:
                text = await ws.receive_text()
                if not text.endswith("\n"):
                    text += "\n"
                writer.write(text.encode("utf-8"))
                await writer.drain()
        except WebSocketDisconnect:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def tcp_to_ws():
        try:
            line_limit = int(os.getenv("ARGUS_JSONL_LINE_LIMIT_BYTES", str(8 * 1024 * 1024)))
        except Exception:
            line_limit = 8 * 1024 * 1024
        buf = bytearray()
        try:
            while True:
                raw = await _read_jsonl_line(reader, buf, max_line_bytes=line_limit)
                if raw is None:
                    break
                await ws.send_text(raw.decode("utf-8", errors="replace").rstrip("\r"))
        finally:
            if ws.client_state == WebSocketState.CONNECTED:
                try:
                    await ws.close()
                except Exception:
                    pass

    a = asyncio.create_task(ws_to_tcp())
    b = asyncio.create_task(tcp_to_ws())

    done, pending = await asyncio.wait({a, b}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        try:
            task.result()
        except Exception:
            pass
