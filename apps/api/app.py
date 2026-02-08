import asyncio
import json
import logging
import os
import re
import time
import uuid
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.websockets import WebSocketState

from croniter import croniter


log = logging.getLogger("argus_gateway")

RUNTIME_LAYOUT = "root_argus_v1"


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
    home_container_path: str = "/root/.argus"
    workspace_container_path: str = "/root/.argus/workspace"
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
    pending_internal_requests: dict[str, asyncio.Future[dict[str, Any]]]
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

    async def reserve_internal_id(self) -> tuple[int, asyncio.Future[dict[str, Any]]]:
        async with self.attach_lock:
            rid = self.next_upstream_id
            self.next_upstream_id += 1
            fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self.pending_internal_requests[str(rid)] = fut
            return rid, fut

    async def resolve_internal_response(self, msg: dict[str, Any]) -> bool:
        rid = str(msg.get("id"))
        async with self.attach_lock:
            fut = self.pending_internal_requests.pop(rid, None)
        if fut is None:
            return False
        if not fut.done():
            fut.set_result(msg)
        return True

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


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ms_to_dt_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


HEARTBEAT_FILENAME = "HEARTBEAT.md"
HEARTBEAT_TASKS_INTERVAL_MS = 30 * 60 * 1000
HEARTBEAT_TOKEN = "HEARTBEAT_OK"
HEARTBEAT_ACK_MAX_CHARS = 300

TELEGRAM_MAX_MESSAGE_CHARS = 4000

AGENTS_FILENAME = "AGENTS.md"
AGENTS_TEMPLATE_FILENAME = "AGENTS.default.md"
SOUL_FILENAME = "SOUL.md"
USER_FILENAME = "USER.md"

PROJECT_CONTEXT_FILENAMES: list[str] = [AGENTS_FILENAME, SOUL_FILENAME, USER_FILENAME]
WORKSPACE_BOOTSTRAP_TEMPLATES: list[tuple[str, str]] = [
    (AGENTS_TEMPLATE_FILENAME, AGENTS_FILENAME),
    (SOUL_FILENAME, SOUL_FILENAME),
    (USER_FILENAME, USER_FILENAME),
    (HEARTBEAT_FILENAME, HEARTBEAT_FILENAME),
]

def _strip_heartbeat_token_at_edges(raw: str, token: str) -> tuple[str, bool]:
    text = str(raw or "").strip()
    if not text or token not in text:
        return text, False
    did_strip = False
    changed = True
    while changed:
        changed = False
        next_text = text.strip()
        if next_text.startswith(token):
            text = next_text[len(token) :].lstrip()
            did_strip = True
            changed = True
            continue
        if next_text.endswith(token):
            text = next_text[: max(0, len(next_text) - len(token))].rstrip()
            did_strip = True
            changed = True
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed, did_strip


def _strip_heartbeat_token(raw: Optional[str]) -> dict[str, Any]:
    # OpenClaw-style: treat HEARTBEAT_OK at start/end as an ack token and suppress short replies.
    if not isinstance(raw, str):
        return {"shouldSkip": True, "text": "", "didStrip": False}
    trimmed = raw.strip()
    if not trimmed:
        return {"shouldSkip": True, "text": "", "didStrip": False}

    token = HEARTBEAT_TOKEN
    if token not in trimmed:
        return {"shouldSkip": False, "text": trimmed, "didStrip": False}

    def _strip_markup_edges(text: str) -> str:
        # Drop HTML tags, normalize nbsp, remove markdown-ish wrappers at the edges.
        out = re.sub(r"<[^>]*>", " ", text)
        out = re.sub(r"&nbsp;", " ", out, flags=re.IGNORECASE)
        out = re.sub(r"^[*`~_]+", "", out)
        out = re.sub(r"[*`~_]+$", "", out)
        return out

    normalized = _strip_markup_edges(trimmed)

    stripped_original, did_original = _strip_heartbeat_token_at_edges(trimmed, token)
    stripped_norm, did_norm = _strip_heartbeat_token_at_edges(normalized, token)
    picked_text = stripped_original if (did_original and stripped_original) else stripped_norm
    did_strip = bool((did_original and stripped_original) or did_norm)
    if not did_strip:
        return {"shouldSkip": False, "text": trimmed, "didStrip": False}
    if not picked_text:
        return {"shouldSkip": True, "text": "", "didStrip": True}
    if len(picked_text) <= HEARTBEAT_ACK_MAX_CHARS:
        return {"shouldSkip": True, "text": "", "didStrip": True}
    return {"shouldSkip": False, "text": picked_text, "didStrip": True}


def _telegram_truncate(text: str) -> str:
    raw = str(text or "")
    if len(raw) <= TELEGRAM_MAX_MESSAGE_CHARS:
        return raw
    suffix = "\n…(truncated)"
    return raw[: max(0, TELEGRAM_MAX_MESSAGE_CHARS - len(suffix))] + suffix


def _telegram_escape_html(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _telegram_escape_html_attr(text: str) -> str:
    return _telegram_escape_html(text).replace('"', "&quot;")


def _markdown_to_telegram_html(markdown: str) -> str:
    # Best-effort, safe conversion: subset of Markdown -> Telegram HTML.
    inp = str(markdown or "")
    if not inp.strip():
        return ""
    normalized = inp.replace("\r\n", "\n").replace("\r", "\n")

    def render_code_block(raw: str) -> str:
        safe = _telegram_escape_html(str(raw or "").rstrip("\n"))
        with_nl = safe if safe.endswith("\n") else safe + "\n"
        return f"<pre><code>{with_nl}</code></pre>"

    def render_inline_spans(raw: str) -> str:
        if not isinstance(raw, str) or not raw:
            return ""
        text = raw
        placeholders: dict[str, str] = {}
        idx = 0

        def store(html: str) -> str:
            nonlocal idx
            key = f"\x00{idx}\x00"
            idx += 1
            placeholders[key] = html
            return key

        # Inline code: `code`
        text = re.sub(r"`([^`\n]+)`", lambda m: store(f"<code>{_telegram_escape_html(m.group(1))}</code>"), text)

        # Links: [label](url)
        def _link(m) -> str:
            label = m.group(1)
            href = m.group(2)
            safe_label = _telegram_escape_html(label)
            href_trim = str(href or "").strip().strip('"').strip("'")
            if not href_trim:
                return safe_label
            safe_href = _telegram_escape_html_attr(href_trim)
            return store(f'<a href="{safe_href}">{safe_label}</a>')

        text = re.sub(r"\[([^\]\n]+)\]\(([^)\n]+)\)", _link, text)

        # Escape everything else to prevent accidental HTML.
        text = _telegram_escape_html(text)

        # Basic emphasis (best-effort).
        text = re.sub(r"\*\*([^*\n]+?)\*\*", r"<b>\1</b>", text)
        text = re.sub(r"__([^_\n]+?)__", r"<b>\1</b>", text)
        text = re.sub(r"~~([^~\n]+?)~~", r"<s>\1</s>", text)
        text = re.sub(r"(^|[^\w*])\*([^*\n]+?)\*(?!\*)", r"\1<i>\2</i>", text)
        text = re.sub(r"(^|[^\w_])_([^_\n]+?)_(?!_)", r"\1<i>\2</i>", text)

        for key, html in placeholders.items():
            text = text.replace(key, html)
        return text

    def render_inline_block(block: str) -> str:
        lines = str(block or "").split("\n")
        out_lines: list[str] = []
        for line in lines:
            if not (line or "").strip():
                out_lines.append("")
                continue
            m = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
            if m:
                out_lines.append(f"<b>{render_inline_spans(m.group(1))}</b>")
                continue
            m = re.match(r"^\s{0,3}>\s?(.*)$", line)
            if m:
                out_lines.append(f"│ {render_inline_spans(m.group(1))}")
                continue
            m = re.match(r"^\s{0,3}[-*+]\s+(.+?)\s*$", line)
            if m:
                out_lines.append(f"• {render_inline_spans(m.group(1))}")
                continue
            out_lines.append(render_inline_spans(line))
        return "\n".join(out_lines)

    out = ""
    i = 0
    while i < len(normalized):
        start = normalized.find("```", i)
        if start == -1:
            out += render_inline_block(normalized[i:])
            break

        out += render_inline_block(normalized[i:start])
        end = normalized.find("```", start + 3)
        if end == -1:
            out += render_inline_block(normalized[start:])
            break

        code_start = start + 3
        if code_start < len(normalized) and normalized[code_start] == "\n":
            code_start += 1
        else:
            nl = normalized.find("\n", code_start)
            if nl != -1 and nl < end:
                code_start = nl + 1
        code = normalized[code_start:end]

        if out and not out.endswith("\n"):
            out += "\n"
        out += render_code_block(code)

        i = end + 3
        if i < len(normalized) and normalized[i] == "\n":
            out += "\n"
            i += 1

    return str(out or "").strip()


def _telegram_target_from_chat_key(chat_key: str) -> Optional[dict[str, Any]]:
    if not isinstance(chat_key, str) or not chat_key.strip():
        return None
    raw = chat_key.strip()
    chat_id_raw, topic_raw = (raw.split(":", 1) + [""])[:2]
    try:
        chat_id = int(chat_id_raw)
    except Exception:
        return None
    if chat_id == 0:
        return None
    out: dict[str, Any] = {"chat_id": chat_id}
    if topic_raw.strip():
        try:
            topic_id = int(topic_raw.strip())
        except Exception:
            topic_id = None
        # Telegram rejects sendMessage with message_thread_id=1 ("thread not found").
        if isinstance(topic_id, int) and topic_id != 1:
            out["message_thread_id"] = topic_id
    return out


TELEGRAM_HTML_PARSE_ERR_RE = re.compile(r"can't parse entities|parse entities|find end of the entity", re.IGNORECASE)
TELEGRAM_MESSAGE_TOO_LONG_RE = re.compile(r"message is too long", re.IGNORECASE)


def _telegram_api_call_sync(token: str, method: str, params: dict[str, Any]) -> Any:
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
    if not isinstance(method, str) or not method.strip():
        raise RuntimeError("Missing Telegram method")
    if not isinstance(params, dict):
        raise RuntimeError("Telegram params must be an object")

    url = f"https://api.telegram.org/bot{token.strip()}/{method.strip()}"
    body = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        raise RuntimeError(f"Telegram {method} failed: HTTP {e.code} {raw}".strip()) from e
    except Exception as e:
        raise RuntimeError(f"Telegram {method} failed: {e}") from e

    try:
        data = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"Telegram {method} failed: bad JSON response") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"Telegram {method} failed: bad response")
    if not data.get("ok"):
        desc = data.get("description") or "unknown error"
        raise RuntimeError(f"Telegram {method} failed: {desc}")
    return data.get("result")


async def _telegram_send_message(
    *,
    token: str,
    target: dict[str, Any],
    text: str,
    parse_mode: Optional[str] = None,
) -> Any:
    params = dict(target)
    params["text"] = text
    if parse_mode:
        params["parse_mode"] = parse_mode
        params["disable_web_page_preview"] = True
    return await asyncio.to_thread(_telegram_api_call_sync, token, "sendMessage", params)


def _strip_yaml_front_matter(raw: str) -> str:
    text = raw.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return raw
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1 :]).lstrip("\n")
    return raw


def _is_heartbeat_content_effectively_empty(content: Optional[str]) -> bool:
    # Mirror OpenClaw's semantics: treat whitespace/headers/comments/empty checklist as empty.
    if content is None or not isinstance(content, str):
        return True
    text = _strip_yaml_front_matter(content)
    for line in text.splitlines():
        trimmed = line.strip()
        if not trimmed:
            continue
        # Skip markdown headers like "# Title" / "## ..." (requires space or EOL after #).
        if re.match(r"^#+(\s|$)", trimmed):
            continue
        # Skip empty markdown list items like "- [ ]" / "- " / "* [x]" with no text.
        if re.match(r"^[-*+]\s*(\[[\sXx]?\]\s*)?$", trimmed):
            continue
        # Skip one-line HTML comments.
        if trimmed.startswith("<!--") and trimmed.endswith("-->"):
            continue
        return False
    return True


def _resolve_repo_root() -> Path:
    # apps/api/app.py -> repo root (or /app in container image)
    return Path(__file__).resolve().parents[2]


def _load_template_text(template_filename: str) -> Optional[str]:
    if not isinstance(template_filename, str) or not template_filename.strip():
        return None
    name = template_filename.strip()
    if "/" in name or "\\" in name:
        return None
    template_path = _resolve_repo_root() / "docs" / "templates" / name
    try:
        return template_path.read_text(encoding="utf-8")
    except Exception:
        return None


def _load_heartbeat_template_text() -> str:
    return _load_template_text(HEARTBEAT_FILENAME) or "# HEARTBEAT.md\n\n"


@dataclass
class PersistedSystemEvent:
    event_id: str
    kind: str
    text: str
    created_at_ms: int
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "eventId": self.event_id,
            "kind": self.kind,
            "text": self.text,
            "createdAtMs": self.created_at_ms,
        }
        if self.meta:
            out["meta"] = self.meta
        return out

    @staticmethod
    def from_json(obj: Any) -> Optional["PersistedSystemEvent"]:
        if not isinstance(obj, dict):
            return None
        event_id = str(obj.get("eventId") or "").strip()
        kind = str(obj.get("kind") or "").strip()
        text = str(obj.get("text") or "")
        created_at_ms = obj.get("createdAtMs")
        if not event_id or not kind or created_at_ms is None:
            return None
        try:
            created_at_ms_int = int(created_at_ms)
        except Exception:
            return None
        meta = obj.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        return PersistedSystemEvent(
            event_id=event_id,
            kind=kind,
            text=text,
            created_at_ms=created_at_ms_int,
            meta=meta,
        )


@dataclass
class PersistedCronJob:
    job_id: str
    expr: str
    text: str
    enabled: bool = True
    last_run_at_ms: Optional[int] = None
    next_run_at_ms: Optional[int] = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "jobId": self.job_id,
            "expr": self.expr,
            "text": self.text,
            "enabled": bool(self.enabled),
        }
        if self.last_run_at_ms is not None:
            out["lastRunAtMs"] = self.last_run_at_ms
        if self.next_run_at_ms is not None:
            out["nextRunAtMs"] = self.next_run_at_ms
        return out

    @staticmethod
    def from_json(obj: Any) -> Optional["PersistedCronJob"]:
        if not isinstance(obj, dict):
            return None
        job_id = str(obj.get("jobId") or "").strip()
        expr = str(obj.get("expr") or "").strip()
        text = str(obj.get("text") or "")
        if not job_id or not expr:
            return None
        enabled = bool(obj.get("enabled", True))

        last_run_at_ms = obj.get("lastRunAtMs")
        if last_run_at_ms is not None:
            try:
                last_run_at_ms = int(last_run_at_ms)
            except Exception:
                last_run_at_ms = None

        next_run_at_ms = obj.get("nextRunAtMs")
        if next_run_at_ms is not None:
            try:
                next_run_at_ms = int(next_run_at_ms)
            except Exception:
                next_run_at_ms = None

        return PersistedCronJob(
            job_id=job_id,
            expr=expr,
            text=text,
            enabled=enabled,
            last_run_at_ms=last_run_at_ms,
            next_run_at_ms=next_run_at_ms,
        )

@dataclass
class PersistedLastActiveTarget:
    channel: str
    chat_key: str
    at_ms: int

    def to_json(self) -> dict[str, Any]:
        return {"channel": self.channel, "chatKey": self.chat_key, "atMs": self.at_ms}

    @staticmethod
    def from_json(obj: Any) -> Optional["PersistedLastActiveTarget"]:
        if not isinstance(obj, dict):
            return None
        channel = str(obj.get("channel") or "").strip()
        chat_key = str(obj.get("chatKey") or "").strip()
        at_ms = obj.get("atMs")
        if not channel or not chat_key or at_ms is None:
            return None
        try:
            at_ms_int = int(at_ms)
        except Exception:
            return None
        if at_ms_int <= 0:
            return None
        return PersistedLastActiveTarget(channel=channel, chat_key=chat_key, at_ms=at_ms_int)


@dataclass
class PersistedSessionAutomation:
    main_thread_id: Optional[str] = None
    cron_jobs: list[PersistedCronJob] = field(default_factory=list)
    system_event_queues: dict[str, list[PersistedSystemEvent]] = field(default_factory=dict)
    last_active_by_thread: dict[str, PersistedLastActiveTarget] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        queues: dict[str, Any] = {}
        for tid, events in self.system_event_queues.items():
            if not tid:
                continue
            queues[tid] = [e.to_json() for e in events]
        last_active: dict[str, Any] = {}
        for tid, tgt in self.last_active_by_thread.items():
            if not tid:
                continue
            if not isinstance(tgt, PersistedLastActiveTarget):
                continue
            last_active[tid] = tgt.to_json()
        return {
            "mainThreadId": self.main_thread_id,
            "cronJobs": [j.to_json() for j in self.cron_jobs],
            "systemEventQueues": queues,
            "lastActiveByThread": last_active,
        }

    @staticmethod
    def from_json(obj: Any) -> "PersistedSessionAutomation":
        if not isinstance(obj, dict):
            return PersistedSessionAutomation()
        main_thread_id = obj.get("mainThreadId")
        if not isinstance(main_thread_id, str) or not main_thread_id.strip():
            main_thread_id = None
        cron_jobs_raw = obj.get("cronJobs")
        cron_jobs: list[PersistedCronJob] = []
        if isinstance(cron_jobs_raw, list):
            for j in cron_jobs_raw:
                pj = PersistedCronJob.from_json(j)
                if pj is not None:
                    cron_jobs.append(pj)
        queues_raw = obj.get("systemEventQueues")
        queues: dict[str, list[PersistedSystemEvent]] = {}
        if isinstance(queues_raw, dict):
            for tid, raw_events in queues_raw.items():
                if not isinstance(tid, str) or not tid.strip():
                    continue
                if not isinstance(raw_events, list):
                    continue
                out_events: list[PersistedSystemEvent] = []
                for ev in raw_events:
                    pe = PersistedSystemEvent.from_json(ev)
                    if pe is not None:
                        out_events.append(pe)
                if out_events:
                    queues[tid.strip()] = out_events
        last_active_raw = obj.get("lastActiveByThread")
        last_active_by_thread: dict[str, PersistedLastActiveTarget] = {}
        if isinstance(last_active_raw, dict):
            for tid, raw_tgt in last_active_raw.items():
                if not isinstance(tid, str) or not tid.strip():
                    continue
                tgt = PersistedLastActiveTarget.from_json(raw_tgt)
                if tgt is not None:
                    last_active_by_thread[tid.strip()] = tgt
        return PersistedSessionAutomation(
            main_thread_id=main_thread_id.strip() if isinstance(main_thread_id, str) else None,
            cron_jobs=cron_jobs,
            system_event_queues=queues,
            last_active_by_thread=last_active_by_thread,
        )


@dataclass
class PersistedGatewayAutomationState:
    version: int = 1
    default_session_id: Optional[str] = None
    sessions: dict[str, PersistedSessionAutomation] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": int(self.version),
            "defaultSessionId": self.default_session_id,
            "sessions": {sid: sess.to_json() for sid, sess in self.sessions.items()},
        }

    @staticmethod
    def from_json(obj: Any) -> "PersistedGatewayAutomationState":
        if not isinstance(obj, dict):
            return PersistedGatewayAutomationState()
        version = obj.get("version")
        try:
            version_int = int(version) if version is not None else 1
        except Exception:
            version_int = 1
        default_session_id = obj.get("defaultSessionId")
        if not isinstance(default_session_id, str) or not default_session_id.strip():
            default_session_id = None
        sessions_raw = obj.get("sessions")
        sessions: dict[str, PersistedSessionAutomation] = {}
        if isinstance(sessions_raw, dict):
            for sid, v in sessions_raw.items():
                if not isinstance(sid, str) or not sid.strip():
                    continue
                sessions[sid.strip()] = PersistedSessionAutomation.from_json(v)
        return PersistedGatewayAutomationState(
            version=version_int,
            default_session_id=default_session_id.strip() if isinstance(default_session_id, str) else None,
            sessions=sessions,
        )


class AutomationStateStore:
    def __init__(self, state_path: Path) -> None:
        self._path = state_path
        self._lock = asyncio.Lock()
        self._state = PersistedGatewayAutomationState()

    @property
    def state(self) -> PersistedGatewayAutomationState:
        return self._state

    async def load(self) -> PersistedGatewayAutomationState:
        async with self._lock:
            try:
                raw = await asyncio.to_thread(self._path.read_text, "utf-8")
                parsed = json.loads(raw)
                self._state = PersistedGatewayAutomationState.from_json(parsed)
            except FileNotFoundError:
                self._state = PersistedGatewayAutomationState()
            except Exception:
                log.exception("Failed to load automation state from %s", str(self._path))
                self._state = PersistedGatewayAutomationState()
            return self._state

    async def save(self) -> None:
        async with self._lock:
            data = self._state.to_json()
            try:
                await asyncio.to_thread(_atomic_write_json, self._path, data)
            except Exception:
                log.exception("Failed to save automation state to %s", str(self._path))

    async def update(self, fn) -> PersistedGatewayAutomationState:
        async with self._lock:
            fn(self._state)
            data = self._state.to_json()
            try:
                await asyncio.to_thread(_atomic_write_json, self._path, data)
            except Exception:
                log.exception("Failed to save automation state to %s", str(self._path))
            return self._state


@dataclass
class ThreadLane:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    busy: bool = False
    active_turn_id: Optional[str] = None
    followups: deque[str] = field(default_factory=deque)


class AutomationManager:
    def __init__(
        self,
        *,
        state_store: AutomationStateStore,
        home_host_path: Optional[str],
        workspace_host_path: Optional[str],
    ) -> None:
        self._store = state_store
        self._home_host_path = home_host_path
        self._workspace_host_path = workspace_host_path

        self._lanes: dict[tuple[str, str], ThreadLane] = {}
        self._cron_wakeup = asyncio.Event()
        self._heartbeat_wakeup = asyncio.Event()
        self._last_heartbeat_run_at_ms: dict[tuple[str, str], int] = {}

        self._tasks: list[asyncio.Task[None]] = []
        self._bootstrap_lock = asyncio.Lock()
        self._session_backoff_until_ms: dict[str, int] = {}
        self._session_backoff_failures: dict[str, int] = {}
        self._turn_text_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def start(self) -> None:
        await self._store.load()
        await self._ensure_workspace_files()
        await self._prune_persisted_sessions()
        self._tasks.append(asyncio.create_task(self._cron_loop()))
        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))

    async def _prune_persisted_sessions(self) -> None:
        # Keep persisted automation state in sync with existing docker runtime containers.
        # Users often delete old session containers manually; without pruning, stale sessions can
        # confuse UIs/bots that expect a single "current" session.
        if _provision_mode() != "docker":
            return
        try:
            sessions = await asyncio.to_thread(_docker_list_argus_containers_sync)
        except Exception as e:
            log.warning("Failed to list docker sessions for pruning: %s", str(e))
            return

        live_session_ids: list[str] = []
        for s in sessions:
            if not isinstance(s, dict):
                continue
            sid = s.get("sessionId")
            layout = s.get("runtimeLayout")
            if isinstance(sid, str) and sid.strip() and layout == RUNTIME_LAYOUT:
                live_session_ids.append(sid.strip())
        live_set = set(live_session_ids)

        st = self._store.state
        removed = [sid for sid in st.sessions.keys() if sid not in live_set]
        default_missing = bool(st.default_session_id and st.default_session_id not in live_set)
        if not removed and not default_missing:
            return

        next_default = None
        if live_session_ids:
            # `_docker_list_argus_containers_sync` is already sorted (running first).
            next_default = live_session_ids[0]

        def _prune(st2: PersistedGatewayAutomationState) -> None:
            for sid in removed:
                st2.sessions.pop(sid, None)
            if st2.default_session_id and st2.default_session_id not in live_set:
                st2.default_session_id = next_default

        await self._store.update(_prune)
        log.info(
            "Pruned automation state sessions: removed=%d default_missing=%s kept=%d",
            len(removed),
            "yes" if default_missing else "no",
            len(live_set),
        )

    def _workspace_root(self) -> Optional[Path]:
        if self._workspace_host_path:
            return Path(self._workspace_host_path)
        if self._home_host_path:
            return Path(self._home_host_path) / "workspace"
        return None

    async def _ensure_workspace_files(self) -> None:
        root = self._workspace_root()
        if root is None:
            return

        def _ensure() -> None:
            root.mkdir(parents=True, exist_ok=True)
            legacy_home = Path(self._home_host_path) if self._home_host_path else None
            for template_name, target_name in WORKSPACE_BOOTSTRAP_TEMPLATES:
                target_path = root / target_name
                if target_path.exists():
                    continue
                # Migration: if legacy HEARTBEAT.md exists in home, keep it.
                if legacy_home is not None and target_name == HEARTBEAT_FILENAME:
                    legacy_heartbeat = legacy_home / HEARTBEAT_FILENAME
                    if legacy_heartbeat.exists():
                        try:
                            _atomic_write_text(target_path, legacy_heartbeat.read_text(encoding="utf-8"))
                            continue
                        except Exception:
                            pass

                template_raw = _load_template_text(template_name) or f"# {target_name}\n"
                template_clean = _strip_yaml_front_matter(template_raw).strip()
                if template_clean:
                    template_clean += "\n"
                _atomic_write_text(target_path, template_clean or f"# {target_name}\n")

        try:
            await asyncio.to_thread(_ensure)
        except Exception:
            log.exception("Failed to initialize workspace templates under %s", str(root))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except Exception:
                pass

    def lane(self, session_id: str, thread_id: str) -> ThreadLane:
        key = (session_id, thread_id)
        lane = self._lanes.get(key)
        if lane is None:
            lane = ThreadLane()
            self._lanes[key] = lane
        return lane

    def _is_session_backed_off(self, session_id: str, now_ms: Optional[int] = None) -> bool:
        if not session_id:
            return False
        if now_ms is None:
            now_ms = _now_ms()
        until = self._session_backoff_until_ms.get(session_id)
        return until is not None and now_ms < until

    def _clear_session_backoff(self, session_id: str) -> None:
        self._session_backoff_until_ms.pop(session_id, None)
        self._session_backoff_failures.pop(session_id, None)

    def _note_session_failure(self, session_id: str, *, what: str, err: Exception) -> None:
        if not session_id:
            return
        failures = self._session_backoff_failures.get(session_id, 0) + 1
        self._session_backoff_failures[session_id] = failures
        # 5s, 10s, 20s, ... capped at 10min.
        delay_s = min(600.0, 5.0 * (2 ** min(failures - 1, 10)))
        self._session_backoff_until_ms[session_id] = _now_ms() + int(delay_s * 1000)
        log.warning("%s failed for session %s; backing off %.0fs: %s", what, session_id, delay_s, str(err))

    def on_upstream_notification(self, session_id: str, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        params = msg.get("params")
        if not isinstance(params, dict):
            params = {}

        thread_id_raw = params.get("threadId")
        thread_id = thread_id_raw.strip() if isinstance(thread_id_raw, str) and thread_id_raw.strip() else None

        turn_id: Optional[str] = None
        turn_id_raw = params.get("turnId")
        if isinstance(turn_id_raw, str) and turn_id_raw.strip():
            turn_id = turn_id_raw.strip()
        else:
            turn = params.get("turn")
            if isinstance(turn, dict):
                tid = turn.get("id")
                if isinstance(tid, str) and tid.strip():
                    turn_id = tid.strip()

        key = (session_id, thread_id, turn_id) if thread_id and turn_id else None

        if method == "item/agentMessage/delta":
            if not key:
                return
            delta = params.get("delta")
            if not isinstance(delta, str) or not delta:
                return
            entry = self._turn_text_by_key.get(key)
            if entry is None:
                entry = {"delta": "", "fullText": None}
                self._turn_text_by_key[key] = entry
            entry["delta"] = str(entry.get("delta") or "") + delta
            return

        if method == "item/completed":
            if not key:
                return
            item = params.get("item")
            if not isinstance(item, dict) or item.get("type") != "agentMessage":
                return
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                return
            entry = self._turn_text_by_key.get(key)
            if entry is None:
                entry = {"delta": "", "fullText": None}
                self._turn_text_by_key[key] = entry
            entry["fullText"] = text
            return

        if method == "turn/started":
            if thread_id:
                lane = self.lane(session_id, thread_id)
                lane.busy = True
                if turn_id:
                    lane.active_turn_id = turn_id
            return

        if method == "turn/completed":
            if thread_id:
                lane = self.lane(session_id, thread_id)
                lane.busy = False
                lane.active_turn_id = None
                asyncio.create_task(self._process_lane_after_turn(session_id, thread_id))
                asyncio.create_task(self._maybe_request_heartbeat(session_id, thread_id))

                final_text = ""
                if key:
                    entry = self._turn_text_by_key.pop(key, None)
                    if isinstance(entry, dict):
                        full = entry.get("fullText")
                        delta = entry.get("delta")
                        if isinstance(full, str) and full.strip():
                            final_text = full
                        elif isinstance(delta, str) and delta.strip():
                            final_text = delta
                if final_text.strip():
                    asyncio.create_task(self._deliver_turn_text(session_id, thread_id, final_text))
            return

    async def _deliver_turn_text(self, session_id: str, thread_id: str, text: str) -> None:
        # Gateway-owned delivery: send a single final message on turn/completed.
        if not isinstance(text, str) or not text.strip():
            return
        st = self._store.state
        sess = st.sessions.get(session_id)
        if sess is None:
            return
        tgt = sess.last_active_by_thread.get(thread_id)
        if tgt is None or not isinstance(tgt, PersistedLastActiveTarget):
            return
        channel = (tgt.channel or "").strip().lower()
        if channel not in ("telegram", "tg"):
            return

        token = os.getenv("TELEGRAM_BOT_TOKEN") or ""
        if not token.strip():
            return
        target = _telegram_target_from_chat_key(tgt.chat_key)
        if target is None:
            return

        stripped = _strip_heartbeat_token(text)
        if stripped.get("shouldSkip"):
            return
        final_text = stripped.get("text") if isinstance(stripped.get("text"), str) else text
        final_text = final_text.strip()
        if not final_text:
            return

        truncated = _telegram_truncate(final_text)
        html = _markdown_to_telegram_html(truncated)

        try:
            if html:
                try:
                    await _telegram_send_message(token=token, target=target, text=html, parse_mode="HTML")
                    return
                except Exception as e:
                    msg = str(e)
                    if TELEGRAM_HTML_PARSE_ERR_RE.search(msg) or TELEGRAM_MESSAGE_TOO_LONG_RE.search(msg):
                        # Fall back to plain text.
                        await _telegram_send_message(token=token, target=target, text=truncated, parse_mode=None)
                        return
                    raise
            await _telegram_send_message(token=token, target=target, text=truncated, parse_mode=None)
        except Exception as e:
            log.warning("Failed to deliver to telegram for %s/%s: %s", session_id, thread_id, str(e))

    async def ensure_default_ready(self) -> str:
        if _provision_mode() != "docker":
            raise RuntimeError("Automation is only supported in docker provision mode")

        async with self._bootstrap_lock:
            session_id = await self._choose_default_session_id()
            live, _ = await _ensure_live_docker_session(session_id, allow_create=True)
            await self._ensure_initialized(live)
            await self.ensure_main_thread(session_id)
            return session_id

    async def _choose_default_session_id(self) -> str:
        existing = self._store.state.default_session_id
        if isinstance(existing, str) and existing.strip():
            return existing.strip()

        sessions: list[dict[str, Any]] = []
        try:
            sessions = await asyncio.to_thread(_docker_list_argus_containers_sync)
        except Exception as e:
            # Docker might be slow/unavailable; don't spam stack traces for background bootstrap.
            log.warning("Failed to list docker sessions while choosing default session: %s", str(e))

        sid: Optional[str] = None
        for s in sessions:
            cand = s.get("sessionId")
            layout = s.get("runtimeLayout")
            if isinstance(cand, str) and cand.strip() and layout == RUNTIME_LAYOUT:
                sid = cand.strip()
                break

        if sid is None:
            sid = uuid.uuid4().hex[:12]

        await self._store.update(lambda st: setattr(st, "default_session_id", sid))
        return sid

    async def ensure_main_thread(self, session_id: str) -> str:
        def get_existing(st: PersistedGatewayAutomationState) -> Optional[str]:
            sess = st.sessions.get(session_id)
            return sess.main_thread_id if sess else None

        existing = get_existing(self._store.state)
        live, _ = await _ensure_live_docker_session(session_id, allow_create=True)
        await self._ensure_initialized(live)

        if isinstance(existing, str) and existing.strip():
            try:
                await self._rpc(live, "thread/resume", {"threadId": existing.strip()})
                return existing.strip()
            except Exception:
                log.warning("Failed to resume mainThreadId=%s; will create a new main thread", existing)

        result = await self._rpc(
            live,
            "thread/start",
            {"cwd": live.cfg.workspace_container_path, "approvalPolicy": "never", "sandbox": "danger-full-access"},
        )
        tid = None
        if isinstance(result, dict):
            thread = result.get("thread")
            if isinstance(thread, dict):
                tid_val = thread.get("id")
                if isinstance(tid_val, str) and tid_val.strip():
                    tid = tid_val.strip()
        if not tid:
            raise RuntimeError("Invalid thread/start response (missing thread.id)")

        def _save_main(st: PersistedGatewayAutomationState) -> None:
            sess = st.sessions.get(session_id)
            if sess is None:
                sess = PersistedSessionAutomation()
                st.sessions[session_id] = sess
            prev_tid = sess.main_thread_id
            sess.main_thread_id = tid
            if prev_tid and prev_tid != tid:
                # Migrate pending system events to the new main thread so they don't get stuck.
                old_q = sess.system_event_queues.pop(prev_tid, None)
                if old_q:
                    new_q = sess.system_event_queues.get(tid)
                    if new_q is None:
                        sess.system_event_queues[tid] = old_q
                    else:
                        new_q.extend(old_q)

        await self._store.update(_save_main)
        return tid

    async def enqueue_user_input(
        self,
        *,
        session_id: str,
        thread_id: Optional[str],
        target: Optional[str],
        text: str,
        source_channel: Optional[str] = None,
        source_chat_key: Optional[str] = None,
    ) -> dict[str, Any]:
        if not text.strip():
            raise ValueError("text must not be empty")

        live, _ = await _ensure_live_docker_session(session_id, allow_create=True)
        await self._ensure_initialized(live)

        if target == "main" or not (isinstance(thread_id, str) and thread_id.strip()):
            thread_id = await self.ensure_main_thread(session_id)
        else:
            thread_id = thread_id.strip()

        if isinstance(source_channel, str) and source_channel.strip() and isinstance(source_chat_key, str) and source_chat_key.strip():
            ch = source_channel.strip()
            ck = source_chat_key.strip()

            def _touch_last_active(st: PersistedGatewayAutomationState) -> None:
                sess = st.sessions.get(session_id)
                if sess is None:
                    sess = PersistedSessionAutomation()
                    st.sessions[session_id] = sess
                sess.last_active_by_thread[thread_id] = PersistedLastActiveTarget(channel=ch, chat_key=ck, at_ms=_now_ms())

            await self._store.update(_touch_last_active)

        lane = self.lane(session_id, thread_id)

        # If busy, enqueue follow-up (next turn).
        if lane.busy:
            lane.followups.append(text)
            return {
                "ok": True,
                "threadId": thread_id,
                "queued": True,
                "started": False,
                "followupDepth": len(lane.followups),
            }

        async with lane.lock:
            if lane.busy:
                lane.followups.append(text)
                return {
                    "ok": True,
                    "threadId": thread_id,
                    "queued": True,
                    "started": False,
                    "followupDepth": len(lane.followups),
                }
            lane.busy = True

            try:
                # `turn/start` fails with "thread not found" if the app-server process hasn't loaded the thread yet
                # (e.g. after reconnect/restart). Make `argus/input/enqueue` robust by resuming first.
                await self._rpc(live, "thread/resume", {"threadId": thread_id})
                assembled = await self._assemble_turn_input(session_id, thread_id, user_text=text, heartbeat=False)
                resp = await self._rpc(
                    live,
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": assembled}],
                        "cwd": live.cfg.workspace_container_path,
                        "approvalPolicy": "never",
                        "sandboxPolicy": {"type": "dangerFullAccess"},
                    },
                )
                turn_id = None
                if isinstance(resp, dict):
                    turn = resp.get("turn")
                    if isinstance(turn, dict):
                        tid_val = turn.get("id")
                        if isinstance(tid_val, str) and tid_val.strip():
                            turn_id = tid_val.strip()
                if turn_id:
                    lane.active_turn_id = turn_id
                return {"ok": True, "threadId": thread_id, "queued": False, "started": True, "turnId": turn_id}
            except Exception:
                lane.busy = False
                raise

    async def enqueue_system_event(
        self,
        *,
        session_id: str,
        thread_id: str,
        kind: str,
        text: str,
        meta: Optional[dict[str, Any]] = None,
    ) -> PersistedSystemEvent:
        ev = PersistedSystemEvent(
            event_id=uuid.uuid4().hex,
            kind=kind,
            text=text,
            created_at_ms=_now_ms(),
            meta=meta or {},
        )

        def _add(st: PersistedGatewayAutomationState) -> None:
            sess = st.sessions.get(session_id)
            if sess is None:
                sess = PersistedSessionAutomation()
                st.sessions[session_id] = sess
            q = sess.system_event_queues.get(thread_id)
            if q is None:
                q = []
                sess.system_event_queues[thread_id] = q
            q.append(ev)
            # Keep bounded.
            if len(q) > 2000:
                del q[: len(q) - 2000]

        await self._store.update(_add)
        self.request_heartbeat_now()
        return ev

    def request_heartbeat_now(self) -> None:
        self._heartbeat_wakeup.set()

    async def _maybe_request_heartbeat(self, session_id: str, thread_id: str) -> None:
        # If main thread has pending system events, wake heartbeat after any turn.
        st = self._store.state
        sess = st.sessions.get(session_id)
        if sess is None:
            return
        main_tid = sess.main_thread_id
        if not main_tid:
            return
        if thread_id != main_tid:
            return
        q = sess.system_event_queues.get(main_tid) or []
        if q:
            self.request_heartbeat_now()

    async def _process_lane_after_turn(self, session_id: str, thread_id: str) -> None:
        lane = self.lane(session_id, thread_id)
        if lane.busy:
            return
        async with lane.lock:
            if lane.busy:
                return
            if not lane.followups:
                return
            followups: list[str] = []
            while lane.followups:
                followups.append(lane.followups.popleft())
            merged = self._format_followups(followups)

            live, _ = await _ensure_live_docker_session(session_id, allow_create=True)
            await self._ensure_initialized(live)

            lane.busy = True
            try:
                await self._rpc(live, "thread/resume", {"threadId": thread_id})
                assembled = await self._assemble_turn_input(session_id, thread_id, user_text=merged, heartbeat=False)
                resp = await self._rpc(
                    live,
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": assembled}],
                        "cwd": live.cfg.workspace_container_path,
                        "approvalPolicy": "never",
                        "sandboxPolicy": {"type": "dangerFullAccess"},
                    },
                )
                turn_id = None
                if isinstance(resp, dict):
                    turn = resp.get("turn")
                    if isinstance(turn, dict):
                        tid_val = turn.get("id")
                        if isinstance(tid_val, str) and tid_val.strip():
                            turn_id = tid_val.strip()
                lane.active_turn_id = turn_id
            except Exception:
                lane.busy = False
                log.exception("Failed to start follow-up turn for %s/%s", session_id, thread_id)

    def _format_followups(self, texts: list[str]) -> str:
        lines = ["Follow-ups (batched):"]
        for i, t in enumerate(texts, start=1):
            lines.append(f"{i}) {t.strip()}")
        return "\n".join(lines).strip()

    async def _read_project_context_block(self, *, include_heartbeat: bool) -> str:
        root = self._workspace_root()
        if root is None:
            return ""

        filenames = list(PROJECT_CONTEXT_FILENAMES)
        if include_heartbeat:
            filenames.append(HEARTBEAT_FILENAME)

        def _read() -> str:
            blocks: list[str] = []
            for name in filenames:
                p = root / name
                try:
                    content = p.read_text(encoding="utf-8")
                except FileNotFoundError:
                    continue
                except Exception as e:  # noqa: BLE001
                    blocks.append(f"[{name}]\n(failed to read {name}: {e})\n[/{name}]")
                    continue

                blocks.append(f"[{name}]\n{content.strip()}\n[/{name}]")

            if not blocks:
                return ""
            return "# Project Context\n\n" + "\n\n".join(blocks)

        try:
            return await asyncio.to_thread(_read)
        except Exception:
            log.exception("Failed to read project context from workspace")
            return ""

    async def _assemble_turn_input(self, session_id: str, thread_id: str, *, user_text: str, heartbeat: bool) -> str:
        drained = await self._drain_system_events(session_id, thread_id, max_events=20)
        blocks: list[str] = []

        project_context = await self._read_project_context_block(include_heartbeat=heartbeat)
        if project_context:
            blocks.append(project_context)

        if heartbeat:
            blocks.append("HEARTBEAT: You are running a background heartbeat. Read and follow HEARTBEAT.md in # Project Context.")
        elif user_text.strip():
            blocks.append(user_text.strip())

        if drained:
            blocks.append("System events (batched):\n" + "\n".join(drained))

        if heartbeat:
            blocks.append(
                "\n".join(
                    [
                        "Heartbeat response contract:",
                        f"- If nothing needs attention right now, reply exactly: {HEARTBEAT_TOKEN}",
                        f"- If you have a user-visible update/alert, reply with the alert text only (do NOT include {HEARTBEAT_TOKEN}).",
                        "- Do NOT include meta/status text (timestamps, next schedule, 'heartbeat check', 'wrote memory', etc) unless it's part of the user-visible alert.",
                        "",
                        "Instructions:",
                        "- Process each system event in order (if any).",
                        "- If an event requires actions, perform them.",
                        f"- If there is no user-visible output, reply exactly: {HEARTBEAT_TOKEN}",
                    ]
                )
            )
        else:
            blocks.append(
                "\n".join(
                    [
                        "Instructions:",
                        "- If there is user input, answer it first.",
                        "- Then process each system event in order.",
                        "- If an event requires actions, perform them.",
                        "- End with a short summary of actions/results.",
                    ]
                )
            )
        return "\n\n".join([b for b in blocks if b.strip()]).strip()

    async def _drain_system_events(self, session_id: str, thread_id: str, *, max_events: int) -> list[str]:
        drained: list[PersistedSystemEvent] = []

        def _pop(st: PersistedGatewayAutomationState) -> None:
            sess = st.sessions.get(session_id)
            if sess is None:
                return
            q = sess.system_event_queues.get(thread_id)
            if not q:
                return
            take = q[:max_events]
            del q[: len(take)]
            drained.extend(take)

        if max_events <= 0:
            return []
        await self._store.update(_pop)

        out: list[str] = []
        for ev in drained:
            ts = _ms_to_dt_utc(ev.created_at_ms).isoformat()
            meta = f" meta={json.dumps(ev.meta, ensure_ascii=False)}" if ev.meta else ""
            out.append(f"[{ev.kind} at={ts}{meta}] {ev.text}".strip())
        return out

    def _read_heartbeat_md_block(self) -> str:
        root = self._workspace_root()
        if root is None:
            return "[HEARTBEAT.md]\n(missing workspace)\n[/HEARTBEAT.md]"
        p = root / HEARTBEAT_FILENAME
        try:
            content = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            # Back-compat: older setups stored HEARTBEAT.md under `$ARGUS_HOME_HOST_PATH/HEARTBEAT.md`.
            legacy = Path(self._home_host_path) / HEARTBEAT_FILENAME if self._home_host_path else None
            if legacy is not None and legacy.exists():
                try:
                    content = legacy.read_text(encoding="utf-8")
                except Exception as e:  # noqa: BLE001
                    content = f"(failed to read legacy HEARTBEAT.md: {e})"
            else:
                content = "(missing HEARTBEAT.md)"
        except Exception as e:  # noqa: BLE001
            content = f"(failed to read HEARTBEAT.md: {e})"
        return "[HEARTBEAT.md]\n" + content.strip() + "\n[/HEARTBEAT.md]"

    async def _ensure_initialized(self, live: LiveDockerSession) -> None:
        if live.initialized_result is not None and live.handshake_done:
            return
        # Send initialize and cache result.
        req = {"method": "initialize", "id": None, "params": {"clientInfo": {"name": "argus_gateway", "title": "Argus Gateway Automation", "version": "0.1.0"}}}
        rid, fut = await live.reserve_internal_id()
        req["id"] = rid
        try:
            await live.write_upstream(json.dumps(req))
            resp = await asyncio.wait_for(fut, timeout=30.0)
        except Exception:
            async with live.attach_lock:
                live.pending_internal_requests.pop(str(rid), None)
            raise
        if isinstance(resp, dict) and isinstance(resp.get("result"), dict):
            live.initialized_result = resp["result"]
        # Complete handshake.
        if not live.handshake_done:
            await live.write_upstream(json.dumps({"method": "initialized"}))
            live.handshake_done = True

    async def _rpc(self, live: LiveDockerSession, method: str, params: Any) -> Any:
        rid, fut = await live.reserve_internal_id()
        try:
            await live.write_upstream(json.dumps({"method": method, "id": rid, "params": params}))
            resp = await asyncio.wait_for(fut, timeout=120.0)
        except Exception:
            async with live.attach_lock:
                live.pending_internal_requests.pop(str(rid), None)
            raise
        if not isinstance(resp, dict):
            raise RuntimeError("Invalid JSON-RPC response")
        if "error" in resp and resp["error"] is not None:
            err = resp["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise RuntimeError(msg or "RPC error")
        return resp.get("result")

    async def _cron_loop(self) -> None:
        while True:
            try:
                sleep_s = await self._cron_tick()
                try:
                    await asyncio.wait_for(self._cron_wakeup.wait(), timeout=sleep_s)
                except asyncio.TimeoutError:
                    pass
                self._cron_wakeup.clear()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Cron loop error")
                await asyncio.sleep(2.0)

    async def _cron_tick(self) -> float:
        st = self._store.state
        session_id = st.default_session_id
        if not session_id:
            return 5.0
        sess = st.sessions.get(session_id)
        if sess is None or not sess.cron_jobs:
            return 5.0

        now = _now_ms()
        if self._is_session_backed_off(session_id, now):
            until = self._session_backoff_until_ms.get(session_id) or (now + 5000)
            return max(1.0, min(30.0, (until - now) / 1000.0))
        next_due_ms: Optional[int] = None
        changed = False

        for job in sess.cron_jobs:
            if not job.enabled:
                continue
            try:
                # (Re)compute nextRunAtMs if missing.
                if job.next_run_at_ms is None:
                    base_dt = _ms_to_dt_utc(job.last_run_at_ms or now)
                    job.next_run_at_ms = _dt_to_ms(croniter(job.expr, base_dt).get_next(datetime))
                    changed = True
                # Run if due.
                if job.next_run_at_ms is not None and job.next_run_at_ms <= now:
                    main_tid = sess.main_thread_id
                    if not main_tid:
                        main_tid = await self.ensure_main_thread(session_id)
                        # Refresh session object after persistence updates.
                        sess = self._store.state.sessions.get(session_id) or sess
                    await self.enqueue_system_event(
                        session_id=session_id,
                        thread_id=main_tid,
                        kind="cron",
                        text=job.text,
                        meta={"jobId": job.job_id, "expr": job.expr},
                    )
                    job.last_run_at_ms = now
                    base_dt = _ms_to_dt_utc(now)
                    job.next_run_at_ms = _dt_to_ms(croniter(job.expr, base_dt).get_next(datetime))
                    changed = True
                    self._clear_session_backoff(session_id)
            except Exception as e:
                # Keep job but avoid tight loop; don't mark as executed.
                log.exception("Failed to run cron job %s", job.job_id)
                self._note_session_failure(session_id, what=f"Cron job {job.job_id}", err=e)
                job.next_run_at_ms = now + 60_000
                changed = True

            if job.enabled and job.next_run_at_ms is not None:
                next_due_ms = job.next_run_at_ms if next_due_ms is None else min(next_due_ms, job.next_run_at_ms)

        if changed:
            await self._store.save()

        if next_due_ms is None:
            return 5.0
        delay_ms = max(200, min(30_000, next_due_ms - _now_ms()))
        return delay_ms / 1000.0

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                try:
                    await asyncio.wait_for(self._heartbeat_wakeup.wait(), timeout=15.0)
                except asyncio.TimeoutError:
                    pass
                forced = self._heartbeat_wakeup.is_set()
                self._heartbeat_wakeup.clear()
                await self._heartbeat_tick(forced=forced)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Heartbeat loop error")
                await asyncio.sleep(2.0)

    def _heartbeat_tasks_due(self, session_id: str, thread_id: str, now_ms: int) -> bool:
        last = self._last_heartbeat_run_at_ms.get((session_id, thread_id))
        if last is None:
            return True
        return now_ms - last >= HEARTBEAT_TASKS_INTERVAL_MS

    async def _heartbeat_has_actionable_content(self) -> bool:
        root = self._workspace_root()
        if root is None:
            return False
        p = root / HEARTBEAT_FILENAME
        try:
            raw = await asyncio.to_thread(p.read_text, "utf-8")
        except FileNotFoundError:
            # Back-compat: older setups stored HEARTBEAT.md under `$ARGUS_HOME_HOST_PATH/HEARTBEAT.md`.
            if not self._home_host_path:
                return False
            legacy = Path(self._home_host_path) / HEARTBEAT_FILENAME
            try:
                raw = await asyncio.to_thread(legacy.read_text, "utf-8")
            except FileNotFoundError:
                return False
            except Exception:
                log.exception("Failed to read legacy %s for heartbeat gating", str(legacy))
                return False
        except Exception:
            log.exception("Failed to read %s for heartbeat gating", str(p))
            return False
        return not _is_heartbeat_content_effectively_empty(raw)

    async def _heartbeat_tick(self, *, forced: bool) -> None:
        if _provision_mode() != "docker":
            return
        has_heartbeat_tasks = await self._heartbeat_has_actionable_content()

        st = self._store.state
        session_id = st.default_session_id

        # If we don't have a persisted default session yet, only bootstrap when heartbeat has work.
        if not session_id:
            if not has_heartbeat_tasks:
                return
            try:
                session_id = await self._choose_default_session_id()
            except Exception as e:
                log.warning("Heartbeat bootstrap failed while choosing default session: %s", str(e))
                return
            st = self._store.state

        now_ms = _now_ms()
        if self._is_session_backed_off(session_id, now_ms):
            return

        sess = st.sessions.get(session_id)
        main_tid = sess.main_thread_id if sess is not None else None
        q = (sess.system_event_queues.get(main_tid) or []) if (sess is not None and main_tid) else []
        has_system_events = bool(q)
        if not has_system_events:
            if not has_heartbeat_tasks:
                return
            if main_tid and (not forced and not self._heartbeat_tasks_due(session_id, main_tid, now_ms)):
                return

        try:
            main_tid = await self.ensure_main_thread(session_id)
            self._clear_session_backoff(session_id)
        except Exception as e:
            self._note_session_failure(session_id, what="Heartbeat bootstrap", err=e)
            return

        lane = self.lane(session_id, main_tid)
        if lane.busy:
            return

        async with lane.lock:
            if lane.busy:
                return

            # Re-check work under latest state.
            st2 = self._store.state
            sess2 = st2.sessions.get(session_id)
            if sess2 is None:
                return
            q2 = sess2.system_event_queues.get(main_tid) or []
            has_system_events2 = bool(q2)
            if not has_system_events2:
                if not has_heartbeat_tasks:
                    return
                now2 = _now_ms()
                if not forced and not self._heartbeat_tasks_due(session_id, main_tid, now2):
                    return

            try:
                live, _ = await _ensure_live_docker_session(session_id, allow_create=True)
                await self._ensure_initialized(live)
            except Exception as e:
                self._note_session_failure(session_id, what="Heartbeat prepare", err=e)
                return

            lane.busy = True
            try:
                await self._rpc(live, "thread/resume", {"threadId": main_tid})
                assembled = await self._assemble_turn_input(session_id, main_tid, user_text="", heartbeat=True)
                await self._rpc(
                    live,
                    "turn/start",
                    {
                        "threadId": main_tid,
                        "input": [{"type": "text", "text": assembled}],
                        "cwd": live.cfg.workspace_container_path,
                        "approvalPolicy": "never",
                        "sandboxPolicy": {"type": "dangerFullAccess"},
                    },
                )
                self._last_heartbeat_run_at_ms[(session_id, main_tid)] = _now_ms()
                self._clear_session_backoff(session_id)
            except Exception as e:
                lane.busy = False
                self._note_session_failure(session_id, what="Heartbeat turn/start", err=e)
                return


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

    async def unregister(self, conn_id: str) -> Optional[NodeSession]:
        async with self._lock:
            node_id = self._nodes_by_conn.pop(conn_id, None)
            if not node_id:
                return None
            removed = self._nodes_by_id.pop(node_id, None)
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
            return removed

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

    async def list_runtime_node_ids(self) -> list[str]:
        async with self._lock:
            ids = [s.node_id for s in self._nodes_by_id.values() if (s.node_id or "").startswith("runtime:")]
        ids.sort()
        return ids

    async def resolve_self_runtime_node_id(self, *, default_session_id: Optional[str]) -> Optional[str]:
        runtime_ids = await self.list_runtime_node_ids()
        if len(runtime_ids) == 1:
            return runtime_ids[0]
        if default_session_id:
            cand = f"runtime:{default_session_id}"
            if cand in runtime_ids:
                return cand
        return None

    async def resolve_implicit_runtime_node_id(self) -> Optional[str]:
        runtime_ids = await self.list_runtime_node_ids()
        if len(runtime_ids) == 1:
            return runtime_ids[0]
        return None

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

    home_host_path = os.getenv("ARGUS_HOME_HOST_PATH") or None
    workspace_host_path = os.getenv("ARGUS_WORKSPACE_HOST_PATH") or None
    if home_host_path:
        state_path = Path(home_host_path) / "gateway" / "state.json"
    else:
        state_path = Path("/tmp/argus-gateway-state.json")
        log.warning("ARGUS_HOME_HOST_PATH is not set; automation state will be stored at %s", str(state_path))

    app.state.automation_store = AutomationStateStore(state_path)
    app.state.automation = AutomationManager(
        state_store=app.state.automation_store,
        home_host_path=home_host_path,
        workspace_host_path=workspace_host_path,
    )
    await app.state.automation.start()


@app.on_event("shutdown")
async def _shutdown():
    automation: Optional[AutomationManager] = getattr(app.state, "automation", None)
    if automation is not None:
        await automation.stop()


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
                        "node": {"type": "string", "description": "nodeId/displayName, or 'self'. If omitted, auto-selects the only connected runtime node."},
                        "command": {"type": "string", "description": "Command name to invoke (must be supported by the node)"},
                        "params": {"type": ["object", "array", "string", "number", "boolean", "null"]},
                        "timeoutMs": {"type": "number"},
                        "idempotencyKey": {"type": "string"},
                    },
                    "required": ["command"],
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
            cmd = str(args.get("command") or "").strip()
            node_raw = str(args.get("node") or args.get("nodeId") or "").strip()
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

            if not cmd:
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(
                            content=[{"type": "text", "text": "Missing required field: command"}],
                            structured={"ok": False, "error": {"code": "INVALID_ARGUMENT"}},
                            is_error=True,
                        ),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            # Node selection:
            # - node="self": prefer the runtime node for the default session (or the only runtime node if unique)
            # - node omitted: only allowed when exactly one runtime node is connected
            node_id: Optional[str] = None
            if not node_raw:
                node_id = await app.state.node_registry.resolve_implicit_runtime_node_id()
                if not node_id:
                    runtime_ids = await app.state.node_registry.list_runtime_node_ids()
                    msg_text = (
                        "No runtime node is connected; start/attach a session (or pass a non-runtime node)."
                        if not runtime_ids
                        else "Missing 'node'. Multiple runtime nodes are connected; pass node (or 'self') to disambiguate."
                    )
                    return (
                        _jsonrpc_result(
                            request_id=request_id,
                            result=_mcp_call_tool_result(
                                content=[{"type": "text", "text": msg_text}],
                                structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "node", "runtimeNodes": runtime_ids}},
                                is_error=True,
                            ),
                        ),
                        {"MCP-Protocol-Version": sess.protocol_version},
                    )
            elif node_raw.lower() == "self":
                automation: Optional[AutomationManager] = getattr(app.state, "automation", None)
                default_sid = automation._store.state.default_session_id if automation is not None else None
                node_id = await app.state.node_registry.resolve_self_runtime_node_id(default_session_id=default_sid)
                if not node_id:
                    runtime_ids = await app.state.node_registry.list_runtime_node_ids()
                    return (
                        _jsonrpc_result(
                            request_id=request_id,
                            result=_mcp_call_tool_result(
                                content=[{"type": "text", "text": "node='self' is ambiguous (no unique/default runtime node). Specify a runtime:<sessionId>."}],
                                structured={"ok": False, "error": {"code": "NODE_NOT_FOUND", "node": "self", "runtimeNodes": runtime_ids}},
                                is_error=True,
                            ),
                        ),
                        {"MCP-Protocol-Version": sess.protocol_version},
                    )
            else:
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
                        "node": {"type": "string", "description": "nodeId/displayName, or 'self'. If omitted, auto-selects the only connected runtime node."},
                        "command": {"type": "string", "description": "Command name to invoke (must be supported by the node)"},
                        "params": {"type": ["object", "array", "string", "number", "boolean", "null"]},
                        "timeoutMs": {"type": "number"},
                        "idempotencyKey": {"type": "string"},
                    },
                    "required": ["command"],
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
            cmd = str(args.get("command") or "").strip()
            node_raw = str(args.get("node") or args.get("nodeId") or "").strip()
            timeout_ms = args.get("timeoutMs")
            if timeout_ms is not None:
                try:
                    timeout_ms = int(timeout_ms)
                except Exception:
                    timeout_ms = None
            invoke_params = args.get("params")

            if not cmd:
                return JSONResponse(
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(
                            content=[{"type": "text", "text": "node_invoke requires 'command'"}],
                            structured={"ok": False, "error": {"code": "BAD_INPUT", "message": "missing command"}},
                            is_error=True,
                        ),
                    ),
                    status_code=200,
                )

            node_id: Optional[str] = None
            if not node_raw:
                node_id = await app.state.node_registry.resolve_implicit_runtime_node_id()
                if not node_id:
                    runtime_ids = await app.state.node_registry.list_runtime_node_ids()
                    msg_text = (
                        "No runtime node is connected; start/attach a session (or pass a non-runtime node)."
                        if not runtime_ids
                        else "Missing 'node'. Multiple runtime nodes are connected; pass node (or 'self') to disambiguate."
                    )
                    return JSONResponse(
                        _jsonrpc_result(
                            request_id=request_id,
                            result=_mcp_call_tool_result(
                                content=[{"type": "text", "text": msg_text}],
                                structured={"ok": False, "error": {"code": "BAD_INPUT", "message": "missing node", "runtimeNodes": runtime_ids}},
                                is_error=True,
                            ),
                        ),
                        status_code=200,
                    )
            elif node_raw.lower() == "self":
                automation: Optional[AutomationManager] = getattr(app.state, "automation", None)
                default_sid = automation._store.state.default_session_id if automation is not None else None
                node_id = await app.state.node_registry.resolve_self_runtime_node_id(default_session_id=default_sid)
                if not node_id:
                    runtime_ids = await app.state.node_registry.list_runtime_node_ids()
                    return JSONResponse(
                        _jsonrpc_result(
                            request_id=request_id,
                            result=_mcp_call_tool_result(
                                content=[{"type": "text", "text": "node='self' is ambiguous (no unique/default runtime node). Specify a runtime:<sessionId>."}],
                                structured={"ok": False, "error": {"code": "NOT_FOUND", "message": "node not found", "runtimeNodes": runtime_ids}},
                                is_error=True,
                            ),
                        ),
                        status_code=200,
                    )
            else:
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


def _docker_api_timeout_s() -> float:
    # Keep Docker API calls snappy: the gateway should remain responsive even when Docker is slow/unavailable.
    # Reuse ARGUS_CONNECT_TIMEOUT_S as a coarse upper bound (no new env var).
    try:
        bound = float(os.getenv("ARGUS_CONNECT_TIMEOUT_S", "30"))
    except Exception:
        bound = 30.0
    return max(3.0, min(10.0, bound))


def _docker_create_container_sync(cfg: DockerProvisionConfig, session_id: str):
    try:
        import docker  # type: ignore
        from docker.errors import ImageNotFound, NotFound  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("Docker provision mode requires the 'docker' Python package") from e

    client = docker.from_env(timeout=_docker_api_timeout_s())

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

    # Runtime node-host (inside the same runtime container) connects back to the gateway
    # for background-job style execution (`system.run` + `process.*`).
    try:
        from urllib.parse import quote as _url_quote
    except Exception:  # pragma: no cover
        _url_quote = None  # type: ignore
    node_token = os.getenv("ARGUS_NODE_TOKEN") or gateway_token
    node_ws_url = "ws://gateway:8080/nodes/ws"
    if node_token:
        q = _url_quote(node_token, safe="") if _url_quote is not None else node_token
        node_ws_url = f"{node_ws_url}?token={q}"
    env["ARGUS_NODE_WS_URL"] = node_ws_url
    env["ARGUS_NODE_ID"] = f"runtime:{session_id}"
    env["ARGUS_NODE_DISPLAY_NAME"] = f"runtime-{session_id}"
    env["ARGUS_SESSION_ID"] = session_id
    volumes = {}
    if cfg.home_host_path:
        volumes[cfg.home_host_path] = {"bind": cfg.home_container_path, "mode": "rw"}
    if cfg.workspace_host_path:
        volumes[cfg.workspace_host_path] = {"bind": cfg.workspace_container_path, "mode": "rw"}

    labels = {
        "io.argus.gateway": "apps/api",
        "io.argus.session_id": session_id,
        "io.argus.runtime_layout": RUNTIME_LAYOUT,
        "io.argus.runtime_home_container_path": cfg.home_container_path,
        "io.argus.runtime_workspace_container_path": cfg.workspace_container_path,
    }

    run_kwargs = {"working_dir": cfg.workspace_container_path}

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

    client = docker.from_env(timeout=_docker_api_timeout_s())
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
                "runtimeLayout": labels.get("io.argus.runtime_layout"),
            }
        )

    out.sort(key=lambda x: (x.get("status") != "running", x.get("runtimeLayout") != RUNTIME_LAYOUT, x.get("name") or ""))
    return out


def _docker_get_container_by_session_sync(session_id: str):
    try:
        import docker  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("Docker provision mode requires the 'docker' Python package") from e

    client = docker.from_env(timeout=_docker_api_timeout_s())
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


async def _ensure_live_docker_session(session_id: str, *, allow_create: bool) -> tuple[LiveDockerSession, bool]:
    async with app.state.sessions_lock:
        existing = app.state.sessions.get(session_id)
    if existing is not None and not existing.closed:
        return existing, False

    cfg = _docker_cfg()
    if not cfg.runtime_cmd:
        raise RuntimeError("ARGUS_RUNTIME_CMD is not set")

    created = False
    container = await asyncio.to_thread(_docker_get_container_by_session_sync, session_id)
    if container is None:
        if not allow_create:
            raise KeyError("Unknown session")
        created = True
        container = await asyncio.to_thread(_docker_create_container_sync, cfg, session_id)
    else:
        container = await asyncio.to_thread(_docker_ensure_running_sync, container)

    host = await _docker_wait_for_ip(container, cfg.network, cfg.connect_timeout_s)
    reader, writer = await _wait_for_tcp(host, 7777, cfg.connect_timeout_s)

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
        pending_internal_requests={},
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
                    # Server requests (approvals, etc).
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

                    # Responses.
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
                        await live.resolve_internal_response(msg)
                        await live.deliver_response(msg)
                        continue

                    # Notifications: feed automation.
                    if "method" in msg and "id" not in msg:
                        automation: Optional[AutomationManager] = getattr(app.state, "automation", None)
                        if automation is not None:
                            try:
                                automation.on_upstream_notification(session_id, msg)
                            except Exception:
                                log.exception("Automation notification handler failed")

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
            async with live.attach_lock:
                for fut in live.pending_internal_requests.values():
                    if not fut.done():
                        fut.set_exception(RuntimeError("Session closed"))
                live.pending_internal_requests.clear()
            async with app.state.sessions_lock:
                current = app.state.sessions.get(session_id)
                if current is live:
                    app.state.sessions.pop(session_id, None)

    live.pump_task = asyncio.create_task(pump())

    async with app.state.sessions_lock:
        app.state.sessions[session_id] = live

    log.info("Provisioned session %s -> %s:%s", session_id, host, 7777)
    return live, created


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


def _get_automation_or_500() -> AutomationManager:
    automation: Optional[AutomationManager] = getattr(app.state, "automation", None)
    if automation is None:
        raise HTTPException(status_code=500, detail="Automation is not initialized")
    return automation


@app.get("/automation/state")
async def automation_state(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    st = automation._store.state  # intentional: debug surface
    lanes = []
    for (sid, tid), lane in automation._lanes.items():  # intentional: debug surface
        lanes.append({"sessionId": sid, "threadId": tid, "busy": lane.busy, "followupDepth": len(lane.followups)})
    lanes.sort(key=lambda x: (x["sessionId"], x["threadId"]))
    return {"ok": True, "persisted": st.to_json(), "runtime": {"lanes": lanes}}


@app.get("/automation/cron/jobs")
async def cron_list_jobs(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    st = automation._store.state
    sid = st.default_session_id
    if not sid:
        sid = await automation.ensure_default_ready()
    sess = automation._store.state.sessions.get(sid) or PersistedSessionAutomation()
    return {"ok": True, "sessionId": sid, "jobs": [j.to_json() for j in sess.cron_jobs]}


@app.post("/automation/cron/jobs")
async def cron_create_job(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    expr = str(body.get("expr") or "").strip()
    text = str(body.get("text") or "")
    enabled = bool(body.get("enabled", True))
    job_id = str(body.get("jobId") or "").strip() or uuid.uuid4().hex[:10]
    if not expr:
        raise HTTPException(status_code=400, detail="Missing 'expr'")
    if not text.strip():
        raise HTTPException(status_code=400, detail="Missing 'text'")

    # Validate cron expression.
    try:
        croniter(expr, _ms_to_dt_utc(_now_ms())).get_next(datetime)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid cron expr: {e}") from e

    sid = automation._store.state.default_session_id
    if not sid:
        sid = await automation.ensure_default_ready()

    def _add(st: PersistedGatewayAutomationState) -> None:
        sess = st.sessions.get(sid)
        if sess is None:
            sess = PersistedSessionAutomation()
            st.sessions[sid] = sess
        # Replace if same id exists.
        sess.cron_jobs = [j for j in sess.cron_jobs if j.job_id != job_id]
        sess.cron_jobs.append(PersistedCronJob(job_id=job_id, expr=expr, text=text, enabled=enabled))

    await automation._store.update(_add)
    automation._cron_wakeup.set()
    return {"ok": True, "sessionId": sid, "job": {"jobId": job_id, "expr": expr, "text": text, "enabled": enabled}}


@app.delete("/automation/cron/jobs/{job_id}")
async def cron_delete_job(job_id: str, request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    sid = automation._store.state.default_session_id
    if not sid:
        sid = await automation.ensure_default_ready()

    removed = False

    def _rm(st: PersistedGatewayAutomationState) -> None:
        nonlocal removed
        sess = st.sessions.get(sid)
        if sess is None:
            return
        before = len(sess.cron_jobs)
        sess.cron_jobs = [j for j in sess.cron_jobs if j.job_id != job_id]
        removed = len(sess.cron_jobs) != before

    await automation._store.update(_rm)
    automation._cron_wakeup.set()
    if not removed:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True, "sessionId": sid, "jobId": job_id}


@app.post("/automation/systemEvent/enqueue")
async def automation_enqueue_system_event(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    session_id = str(body.get("sessionId") or "").strip() or None
    if not session_id:
        session_id = automation._store.state.default_session_id
    if not session_id:
        session_id = await automation.ensure_default_ready()

    target = str(body.get("target") or "").strip()
    thread_id = str(body.get("threadId") or "").strip() or None
    if target == "main" or not thread_id:
        thread_id = await automation.ensure_main_thread(session_id)

    kind = str(body.get("kind") or "").strip() or "system"
    text = str(body.get("text") or "")
    if not text.strip():
        raise HTTPException(status_code=400, detail="Missing 'text'")

    ev = await automation.enqueue_system_event(session_id=session_id, thread_id=thread_id, kind=kind, text=text)
    return {"ok": True, "sessionId": session_id, "threadId": thread_id, "event": ev.to_json()}


@app.post("/automation/heartbeat/now")
async def automation_heartbeat_now(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    automation.request_heartbeat_now()
    return {"ok": True}


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


def _format_node_system_event_text(
    *,
    event: str,
    node_id: str,
    display_name: Optional[str],
    platform: Optional[str],
    version: Optional[str],
    ip: Optional[str],
) -> str:
    label = (display_name or "").strip()
    if label and label != node_id:
        label = f"{label} ({node_id})"
    else:
        label = node_id

    details: list[str] = []
    if platform and platform.strip():
        details.append(f"platform={platform.strip()}")
    if version and version.strip():
        details.append(f"version={version.strip()}")
    if ip and ip.strip():
        details.append(f"ip={ip.strip()}")

    text = f"Node {event}: {label}"
    if details:
        text += " · " + " · ".join(details)
    return text


async def _enqueue_node_system_event(
    *,
    event: str,
    node_id: str,
    display_name: Optional[str],
    platform: Optional[str],
    version: Optional[str],
    ip: Optional[str],
) -> None:
    automation: Optional[AutomationManager] = getattr(app.state, "automation", None)
    if automation is None:
        return

    st = automation._store.state
    session_id = st.default_session_id
    if not session_id:
        return
    sess = st.sessions.get(session_id)
    main_tid = sess.main_thread_id if sess is not None else None
    if not main_tid:
        return

    text = _format_node_system_event_text(
        event=event,
        node_id=node_id,
        display_name=display_name,
        platform=platform,
        version=version,
        ip=ip,
    )
    try:
        await automation.enqueue_system_event(
            session_id=session_id,
            thread_id=main_tid,
            kind="node",
            text=text,
            meta={"event": event, "nodeId": node_id, "displayName": display_name, "platform": platform, "version": version, "ip": ip},
        )
    except Exception:
        log.exception("Failed to enqueue node systemEvent: %s", text)

def _session_id_from_node_id(node_id: str) -> Optional[str]:
    node_id = (node_id or "").strip()
    if not node_id:
        return None
    if node_id.startswith("runtime:"):
        sid = node_id.split(":", 1)[1].strip()
        return sid or None
    return None


def _format_process_exited_system_event_text(
    *,
    job_id: str,
    argv: Optional[list[str]],
    cwd: Optional[str],
    exit_code: Optional[int],
    signal: Optional[str],
    timed_out: bool,
    stdout_tail: Optional[str],
    stderr_tail: Optional[str],
) -> str:
    cmd = ""
    if argv and all(isinstance(x, str) for x in argv):
        cmd = " ".join([x for x in argv if x is not None])

    lines: list[str] = []
    lines.append("Background process finished.")
    lines.append(f"jobId: {job_id}")
    if cmd:
        lines.append(f"command: {cmd}")
    if cwd:
        lines.append(f"cwd: {cwd}")
    parts: list[str] = []
    if exit_code is not None:
        parts.append(f"exitCode={exit_code}")
    if signal:
        parts.append(f"signal={signal}")
    if timed_out:
        parts.append("timedOut=true")
    if parts:
        lines.append("result: " + " ".join(parts))

    if stdout_tail and stdout_tail.strip():
        lines.append("\nstdout (tail):\n" + stdout_tail.strip())
    if stderr_tail and stderr_tail.strip():
        lines.append("\nstderr (tail):\n" + stderr_tail.strip())

    lines.append("\nIf you need more output, use `process.logs` with this jobId.")
    return "\n".join(lines).strip()


async def _enqueue_process_exited_system_event(
    *,
    node_id: str,
    payload: dict[str, Any],
) -> None:
    automation: Optional[AutomationManager] = getattr(app.state, "automation", None)
    if automation is None:
        return

    job_id = str(payload.get("jobId") or "").strip()
    if not job_id:
        return

    session_id = payload.get("sessionId")
    if not isinstance(session_id, str) or not session_id.strip():
        session_id = _session_id_from_node_id(node_id) or automation._store.state.default_session_id
    session_id = (session_id or "").strip()
    if not session_id:
        return

    argv = payload.get("argv") if isinstance(payload.get("argv"), list) else None
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    exit_code = payload.get("exitCode")
    exit_code_int: Optional[int] = None
    if isinstance(exit_code, int):
        exit_code_int = int(exit_code)
    signal = payload.get("signal") if isinstance(payload.get("signal"), str) else None
    timed_out = bool(payload.get("timedOut"))
    stdout_tail = payload.get("stdoutTail") if isinstance(payload.get("stdoutTail"), str) else None
    stderr_tail = payload.get("stderrTail") if isinstance(payload.get("stderrTail"), str) else None

    try:
        main_tid = await automation.ensure_main_thread(session_id)
    except Exception:
        log.exception("Failed to ensure main thread for process-exit systemEvent (session %s)", session_id)
        return

    text = _format_process_exited_system_event_text(
        job_id=job_id,
        argv=argv,
        cwd=cwd,
        exit_code=exit_code_int,
        signal=signal,
        timed_out=timed_out,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
    )

    try:
        await automation.enqueue_system_event(
            session_id=session_id,
            thread_id=main_tid,
            kind="process",
            text=text,
            meta={
                "nodeId": node_id,
                "jobId": job_id,
                "argv": argv,
                "cwd": cwd,
                "exitCode": exit_code_int,
                "signal": signal,
                "timedOut": timed_out,
            },
        )
    except Exception:
        log.exception("Failed to enqueue process-exit systemEvent for job %s", job_id)


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
    remote_ip = ws.client.host if ws.client else None
    asyncio.create_task(
        _enqueue_node_system_event(
            event="connected",
            node_id=node_id,
            display_name=display_name,
            platform=platform,
            version=version,
            ip=remote_ip,
        )
    )

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
            if msg.get("type") == "event" and msg.get("event") == "node.process.exited" and isinstance(msg.get("payload"), dict):
                payload = msg["payload"]
                if str(payload.get("nodeId") or "") not in ("", node_id):
                    continue
                await app.state.node_registry.touch(node_id)
                asyncio.create_task(_enqueue_process_exited_system_event(node_id=node_id, payload=payload))
                continue
            if msg.get("type") == "event" and msg.get("event") == "node.heartbeat":
                await app.state.node_registry.touch(node_id)
                continue
    except WebSocketDisconnect:
        pass
    finally:
        removed = await app.state.node_registry.unregister(conn_id)
        if removed is not None:
            asyncio.create_task(
                _enqueue_node_system_event(
                    event="disconnected",
                    node_id=node_id,
                    display_name=display_name,
                    platform=platform,
                    version=version,
                    ip=remote_ip,
                )
            )


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

        requested_session = (ws.query_params.get("session") or "").strip() or None
        session_id = requested_session or uuid.uuid4().hex[:12]
        try:
            live, created = await _ensure_live_docker_session(session_id, allow_create=not bool(requested_session))
        except KeyError:
            await ws.close(code=1008, reason="Unknown session")
            return
        except Exception:
            await ws.close(code=1011, reason="Failed to provision upstream container")
            return

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

                if isinstance(msg, dict) and isinstance(msg.get("method"), str) and str(msg.get("method")).startswith("argus/"):
                    has_id = "id" in msg and msg.get("id") is not None
                    req_id = msg.get("id") if has_id else None
                    method = str(msg.get("method") or "")
                    params = msg.get("params")
                    if not isinstance(params, dict):
                        params = {}
                    automation: Optional[AutomationManager] = getattr(app.state, "automation", None)
                    if automation is None:
                        if has_id:
                            try:
                                await ws.send_text(
                                    json.dumps({"id": req_id, "error": {"code": -32000, "message": "Automation is not available"}})
                                )
                            except Exception:
                                pass
                        continue
                    try:
                        if method == "argus/thread/main/ensure":
                            tid = await automation.ensure_main_thread(session_id)
                            if has_id:
                                await ws.send_text(json.dumps({"id": req_id, "result": {"threadId": tid}}))
                            continue
                        if method == "argus/input/enqueue":
                            text_param = params.get("text")
                            if not isinstance(text_param, str) or not text_param.strip():
                                if has_id:
                                    await ws.send_text(
                                        json.dumps({"id": req_id, "error": {"code": -32602, "message": "Missing 'text'"}})
                                    )
                                continue
                            source_channel = None
                            source_chat_key = None
                            source = params.get("source")
                            if isinstance(source, dict):
                                ch = source.get("channel")
                                ck = source.get("chatKey")
                                if isinstance(ch, str) and ch.strip():
                                    source_channel = ch.strip()
                                if isinstance(ck, str) and ck.strip():
                                    source_chat_key = ck.strip()
                            res = await automation.enqueue_user_input(
                                session_id=session_id,
                                thread_id=(params.get("threadId") if isinstance(params.get("threadId"), str) else None),
                                target=(params.get("target") if isinstance(params.get("target"), str) else None),
                                text=text_param,
                                source_channel=source_channel,
                                source_chat_key=source_chat_key,
                            )
                            if has_id:
                                await ws.send_text(json.dumps({"id": req_id, "result": res}))
                            continue
                        if method == "argus/systemEvent/enqueue":
                            text_param = params.get("text")
                            if not isinstance(text_param, str) or not text_param.strip():
                                if has_id:
                                    await ws.send_text(
                                        json.dumps({"id": req_id, "error": {"code": -32602, "message": "Missing 'text'"}})
                                    )
                                continue
                            kind = params.get("kind")
                            if not isinstance(kind, str) or not kind.strip():
                                kind = "system"
                            target = params.get("target")
                            thread_id = params.get("threadId") if isinstance(params.get("threadId"), str) else None
                            if target == "main" or not (isinstance(thread_id, str) and thread_id.strip()):
                                thread_id = await automation.ensure_main_thread(session_id)
                            else:
                                thread_id = thread_id.strip()
                            ev = await automation.enqueue_system_event(
                                session_id=session_id,
                                thread_id=thread_id,
                                kind=kind.strip(),
                                text=text_param,
                            )
                            if has_id:
                                await ws.send_text(
                                    json.dumps(
                                        {"id": req_id, "result": {"ok": True, "event": ev.to_json(), "threadId": thread_id}}
                                    )
                                )
                            continue
                        if method == "argus/heartbeat/request":
                            automation.request_heartbeat_now()
                            if has_id:
                                await ws.send_text(json.dumps({"id": req_id, "result": {"ok": True}}))
                            continue
                        if has_id:
                            await ws.send_text(
                                json.dumps({"id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})
                            )
                        continue
                    except Exception as e:
                        if has_id:
                            try:
                                await ws.send_text(json.dumps({"id": req_id, "error": {"code": -32000, "message": str(e)}}))
                            except Exception:
                                pass
                    continue

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
