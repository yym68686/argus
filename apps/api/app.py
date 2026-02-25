import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
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
    runtime_cpu_period: Optional[int] = None
    runtime_cpu_quota: Optional[int] = None
    runtime_mem_limit_bytes: Optional[int] = None
    runtime_memswap_limit_bytes: Optional[int] = None
    runtime_pids_limit: Optional[int] = None


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


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)

def _sanitize_path_part(raw: str, *, fallback: str = 'x', max_len: int = 64) -> str:
    s = str(raw or '').strip()
    if not s:
        return fallback
    s = re.sub(r'[^a-zA-Z0-9._-]+', '_', s)
    s = s.strip('._-')
    if not s:
        return fallback
    return s[:max_len]



HEARTBEAT_FILENAME = "HEARTBEAT.md"
HEARTBEAT_TASKS_INTERVAL_MS = 30 * 60 * 1000
HEARTBEAT_TOKEN = "HEARTBEAT_OK"
HEARTBEAT_ACK_MAX_CHARS = 300

CRON_EVENT_KIND = "cron"
CRON_WRITEBACK_EVENT_KIND = "cron_writeback"
CRON_DEFAULT_SESSION_TARGET = "main"
CRON_SESSION_TARGETS: set[str] = {"main", "isolated"}
CRON_WRITEBACK_WHENS: set[str] = {"always", "on-error", "never", "requested"}
CRON_DEFAULT_WRITEBACK_MAX_CHARS = 2000
CRON_DEFAULT_ISOLATED_WRITEBACK_WHEN = "always"
CRON_DEFAULT_WRITEBACK_PROMPT_HINT = True
CRON_DEFAULT_RETENTION_MAX_UNARCHIVED_THREADS = 50

TELEGRAM_MAX_MESSAGE_CHARS = 4000
TELEGRAM_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
TELEGRAM_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
TELEGRAM_THREAD_NOT_FOUND_RE = re.compile(r"message thread not found", re.IGNORECASE)
MEDIA_LINE_RE = re.compile(r"^\s*MEDIA:\s*(.*?)\s*$", re.IGNORECASE)
MARKDOWN_FENCE_RE = re.compile(r"^\s*```")
TELEGRAM_IMAGE_EXTS: set[str] = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
DEFAULT_JSONL_LINE_LIMIT_BYTES = 128 * 1024 * 1024

AGENTS_FILENAME = "AGENTS.md"
AGENTS_TEMPLATE_FILENAME = "AGENTS.default.md"
SOUL_FILENAME = "SOUL.md"
USER_FILENAME = "USER.md"
SKILLS_DIRNAME = "skills"
SKILL_MD_FILENAME = "SKILL.md"

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

def _unwrap_media_quotes(raw: str) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    trimmed = raw.strip()
    if len(trimmed) < 2:
        return None
    first = trimmed[0]
    last = trimmed[-1]
    if first != last or first not in ('"', "'", "`"):
        return None
    return trimmed[1:-1].strip()


def _is_valid_media_ref(ref: str) -> bool:
    if not isinstance(ref, str):
        return False
    s = ref.strip()
    if not s:
        return False
    if len(s) > 4096:
        return False
    if s.startswith("http://") or s.startswith("https://"):
        return True
    # Local attachments must be workspace-relative to avoid leaking host files.
    return s.startswith("./")

def _is_image_media_ref(ref: str) -> bool:
    if not isinstance(ref, str):
        return False
    s = ref.strip()
    if not s:
        return False
    # Drop query/fragment for URLs.
    base = s.split("?", 1)[0].split("#", 1)[0]
    ext = Path(base).suffix.lower()
    return ext in TELEGRAM_IMAGE_EXTS


def _split_media_from_output(raw: str) -> tuple[str, list[str]]:
    """
    Extract MEDIA directives from assistant output.

    Semantics (OpenClaw-inspired):
    - Only treat lines that start with "MEDIA:" (on their own line) as directives.
    - Ignore MEDIA inside fenced code blocks (```).
    - Accept:
      - "MEDIA: ./relative/path"
      - "MEDIA: \"./path with spaces.pdf\"" (quoted)
      - "MEDIA: https://example.com/file.pdf"
    - When at least one valid media ref is found on a MEDIA: line, drop that line from the text.
      Otherwise keep the line unchanged.
    """
    if not isinstance(raw, str) or not raw.strip():
        return "", []

    in_fence = False
    media: list[str] = []
    kept_lines: list[str] = []

    for line in raw.splitlines():
        if MARKDOWN_FENCE_RE.match(line):
            in_fence = not in_fence
            kept_lines.append(line)
            continue
        if in_fence:
            kept_lines.append(line)
            continue

        m = MEDIA_LINE_RE.match(line)
        if not m:
            kept_lines.append(line)
            continue

        payload = (m.group(1) or "").strip()
        if not payload:
            # Keep "MEDIA:" lines with no payload as regular text.
            kept_lines.append(line)
            continue

        unwrapped = _unwrap_media_quotes(payload)
        parts = [unwrapped] if unwrapped is not None else [p for p in payload.split() if p]

        found_any = False
        for part in parts:
            cand = (part or "").strip()
            if _is_valid_media_ref(cand):
                media.append(cand)
                found_any = True

        if not found_any:
            kept_lines.append(line)

    deduped: list[str] = []
    seen: set[str] = set()
    for ref in media:
        if ref in seen:
            continue
        seen.add(ref)
        deduped.append(ref)

    return "\n".join(kept_lines).strip(), deduped


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

def _telegram_get_file_sync(token: str, file_id: str) -> dict[str, Any]:
    res = _telegram_api_call_sync(token, "getFile", {"file_id": file_id})
    if not isinstance(res, dict):
        raise RuntimeError("Telegram getFile failed: bad response")
    return res


def _telegram_download_file_to_path_sync(token: str, file_path: str, dest: Path, *, max_bytes: int) -> int:
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
    if not isinstance(file_path, str) or not file_path.strip():
        raise RuntimeError("Missing Telegram file_path")
    if not isinstance(dest, Path):
        raise RuntimeError("Invalid destination path")

    url = f"https://api.telegram.org/file/bot{token.strip()}/{file_path.lstrip('/')}"
    req = urllib.request.Request(url, method="GET")
    total = 0

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            with tmp.open("wb") as f:
                while True:
                    chunk = resp.read(1024 * 64)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise RuntimeError(f"Telegram file too large (> {max_bytes} bytes)")
                    f.write(chunk)
            tmp.replace(dest)
            return total
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        raise RuntimeError(f"Telegram file download failed: HTTP {e.code} {raw}".strip()) from e
    except Exception as e:
        raise RuntimeError(f"Telegram file download failed: {e}") from e


def _telegram_api_call_multipart_sync(
    token: str,
    method: str,
    params: dict[str, Any],
    *,
    file_field: str,
    file_path: Path,
    filename: Optional[str] = None,
) -> Any:
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
    if not isinstance(method, str) or not method.strip():
        raise RuntimeError("Missing Telegram method")
    if not isinstance(params, dict):
        raise RuntimeError("Telegram params must be an object")
    if not isinstance(file_field, str) or not file_field.strip():
        raise RuntimeError("Missing Telegram file field")
    if not isinstance(file_path, Path):
        raise RuntimeError("Invalid Telegram file path")

    url = f"https://api.telegram.org/bot{token.strip()}/{method.strip()}"

    data: dict[str, Any] = {}
    for k, v in params.items():
        if v is None:
            continue
        data[k] = v

    raw = ""
    try:
        with file_path.open("rb") as f:
            name = filename or file_path.name or "file"
            files = {file_field: (name, f)}
            resp = requests.post(url, data=data, files=files, timeout=30)
            raw = resp.text
            resp.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(f"Telegram {method} failed: HTTP {getattr(e.response, 'status_code', '?')} {raw}".strip()) from e
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Telegram {method} failed: {e}") from e

    try:
        parsed = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Telegram {method} failed: bad JSON response") from e
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Telegram {method} failed: bad response")
    if not parsed.get("ok"):
        desc = parsed.get("description") or "unknown error"
        raise RuntimeError(f"Telegram {method} failed: {desc}")
    return parsed.get("result")


async def _telegram_send_message(
    *,
    token: str,
    target: dict[str, Any],
    text: str,
    parse_mode: Optional[str] = None,
    disable_notification: Optional[bool] = None,
) -> Any:
    params = dict(target)
    params["text"] = text
    if parse_mode:
        params["parse_mode"] = parse_mode
        params["disable_web_page_preview"] = True
    if disable_notification is not None:
        params["disable_notification"] = bool(disable_notification)
    try:
        return await asyncio.to_thread(_telegram_api_call_sync, token, "sendMessage", params)
    except Exception as e:  # noqa: BLE001
        # Thread may be missing/deleted; retry without message_thread_id like OpenClaw.
        if "message_thread_id" in params and TELEGRAM_THREAD_NOT_FOUND_RE.search(str(e) or ""):
            params.pop("message_thread_id", None)
            return await asyncio.to_thread(_telegram_api_call_sync, token, "sendMessage", params)
        raise


async def _telegram_send_document_url(*, token: str, target: dict[str, Any], url: str) -> Any:
    params = dict(target)
    params["document"] = url
    try:
        return await asyncio.to_thread(_telegram_api_call_sync, token, "sendDocument", params)
    except Exception as e:  # noqa: BLE001
        if "message_thread_id" in params and TELEGRAM_THREAD_NOT_FOUND_RE.search(str(e) or ""):
            params.pop("message_thread_id", None)
            return await asyncio.to_thread(_telegram_api_call_sync, token, "sendDocument", params)
        raise


async def _telegram_send_photo_url(*, token: str, target: dict[str, Any], url: str) -> Any:
    params = dict(target)
    params["photo"] = url
    try:
        return await asyncio.to_thread(_telegram_api_call_sync, token, "sendPhoto", params)
    except Exception as e:  # noqa: BLE001
        if "message_thread_id" in params and TELEGRAM_THREAD_NOT_FOUND_RE.search(str(e) or ""):
            params.pop("message_thread_id", None)
            return await asyncio.to_thread(_telegram_api_call_sync, token, "sendPhoto", params)
        raise


async def _telegram_send_document_file(
    *,
    token: str,
    target: dict[str, Any],
    file_path: Path,
    filename: Optional[str] = None,
) -> Any:
    params = dict(target)
    try:
        return await asyncio.to_thread(
            _telegram_api_call_multipart_sync,
            token,
            "sendDocument",
            params,
            file_field="document",
            file_path=file_path,
            filename=filename,
        )
    except Exception as e:  # noqa: BLE001
        if "message_thread_id" in params and TELEGRAM_THREAD_NOT_FOUND_RE.search(str(e) or ""):
            params.pop("message_thread_id", None)
            return await asyncio.to_thread(
                _telegram_api_call_multipart_sync,
                token,
                "sendDocument",
                params,
                file_field="document",
                file_path=file_path,
                filename=filename,
            )
        raise


async def _telegram_send_photo_file(
    *,
    token: str,
    target: dict[str, Any],
    file_path: Path,
    filename: Optional[str] = None,
) -> Any:
    params = dict(target)
    try:
        return await asyncio.to_thread(
            _telegram_api_call_multipart_sync,
            token,
            "sendPhoto",
            params,
            file_field="photo",
            file_path=file_path,
            filename=filename,
        )
    except Exception as e:  # noqa: BLE001
        if "message_thread_id" in params and TELEGRAM_THREAD_NOT_FOUND_RE.search(str(e) or ""):
            params.pop("message_thread_id", None)
            return await asyncio.to_thread(
                _telegram_api_call_multipart_sync,
                token,
                "sendPhoto",
                params,
                file_field="photo",
                file_path=file_path,
                filename=filename,
            )
        raise


def _strip_yaml_front_matter(raw: str) -> str:
    text = raw.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return raw
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1 :]).lstrip("\n")
    return raw


def _parse_yaml_front_matter(raw: str) -> dict[str, str]:
    # Minimal YAML frontmatter parser (single-line key: value pairs only).
    text = str(raw or "").lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    for i in range(1, len(lines)):
        line = lines[i]
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        k = key.strip()
        if not k:
            continue
        v = value.strip()
        if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
            v = v[1:-1]
        out[k] = v
    return out


def _bootstrap_workspace_skill_templates(workspace_root: Path) -> None:
    template_root = _resolve_repo_root() / "docs" / "templates" / SKILLS_DIRNAME
    if not template_root.is_dir():
        return
    dest_root = workspace_root / SKILLS_DIRNAME
    for skill_dir in sorted([p for p in template_root.iterdir() if p.is_dir()]):
        for src in sorted([p for p in skill_dir.rglob("*") if p.is_file()]):
            rel = src.relative_to(skill_dir)
            dest = dest_root / skill_dir.name / rel
            if dest.exists():
                continue
            try:
                _atomic_write_bytes(dest, src.read_bytes())
            except Exception:
                continue


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
    session_target: str = "main"
    writeback: Optional[dict[str, Any]] = None
    retention: Optional[dict[str, Any]] = None

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
        out["sessionTarget"] = self.session_target
        if isinstance(self.writeback, dict) and self.writeback:
            out["writeback"] = dict(self.writeback)
        if isinstance(self.retention, dict) and self.retention:
            out["retention"] = dict(self.retention)
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

        session_target = str(obj.get("sessionTarget") or "main").strip().lower()
        if session_target not in ("main", "isolated"):
            session_target = "main"

        writeback = obj.get("writeback")
        if not isinstance(writeback, dict):
            writeback = None

        retention = obj.get("retention")
        if not isinstance(retention, dict):
            retention = None

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
            session_target=session_target,
            writeback=writeback,
            retention=retention,
        )


@dataclass
class PersistedCronRunMeta:
    run_id: str
    thread_id: str
    turn_id: Optional[str] = None
    started_at_ms: int = 0
    ended_at_ms: Optional[int] = None
    status: str = "running"
    summary: Optional[str] = None
    error: Optional[str] = None
    archived: bool = False

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "runId": self.run_id,
            "threadId": self.thread_id,
            "startedAtMs": int(self.started_at_ms),
            "status": self.status,
            "archived": bool(self.archived),
        }
        if isinstance(self.turn_id, str) and self.turn_id.strip():
            out["turnId"] = self.turn_id.strip()
        if self.ended_at_ms is not None:
            out["endedAtMs"] = int(self.ended_at_ms)
        if isinstance(self.summary, str) and self.summary.strip():
            out["summary"] = self.summary
        if isinstance(self.error, str) and self.error.strip():
            out["error"] = self.error
        return out

    @staticmethod
    def from_json(obj: Any) -> Optional["PersistedCronRunMeta"]:
        if not isinstance(obj, dict):
            return None
        run_id = str(obj.get("runId") or "").strip()
        thread_id = str(obj.get("threadId") or "").strip()
        if not run_id or not thread_id:
            return None
        turn_id = obj.get("turnId")
        turn_id = turn_id.strip() if isinstance(turn_id, str) and turn_id.strip() else None
        started_at_ms = obj.get("startedAtMs")
        try:
            started_at_ms_int = int(started_at_ms) if started_at_ms is not None else _now_ms()
        except Exception:
            started_at_ms_int = _now_ms()
        ended_at_ms = obj.get("endedAtMs")
        if ended_at_ms is not None:
            try:
                ended_at_ms = int(ended_at_ms)
            except Exception:
                ended_at_ms = None
        status = str(obj.get("status") or "running").strip().lower()
        if status not in ("running", "success", "error", "canceled", "timeout", "unknown"):
            status = "unknown"
        summary = obj.get("summary")
        summary = summary if isinstance(summary, str) else None
        error = obj.get("error")
        error = error if isinstance(error, str) else None
        archived = bool(obj.get("archived", False))
        return PersistedCronRunMeta(
            run_id=run_id,
            thread_id=thread_id,
            turn_id=turn_id,
            started_at_ms=started_at_ms_int,
            ended_at_ms=ended_at_ms,
            status=status,
            summary=summary,
            error=error,
            archived=archived,
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


AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def _normalize_agent_id(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    agent_id = raw.strip().lower()
    if not agent_id:
        return ""
    if not AGENT_ID_RE.match(agent_id):
        return ""
    return agent_id


@dataclass
class PersistedAgentRuntime:
    agent_id: str
    session_id: str
    workspace_host_path: str
    created_at_ms: int

    def to_json(self) -> dict[str, Any]:
        return {
            "agentId": self.agent_id,
            "sessionId": self.session_id,
            "workspaceHostPath": self.workspace_host_path,
            "createdAtMs": int(self.created_at_ms),
        }

    @staticmethod
    def from_json(obj: Any) -> Optional["PersistedAgentRuntime"]:
        if not isinstance(obj, dict):
            return None
        agent_id = _normalize_agent_id(obj.get("agentId"))
        session_id = str(obj.get("sessionId") or "").strip()
        workspace_host_path = str(obj.get("workspaceHostPath") or "").strip()
        created_at_ms = obj.get("createdAtMs")
        try:
            created_at_int = int(created_at_ms) if created_at_ms is not None else _now_ms()
        except Exception:
            created_at_int = _now_ms()
        if not agent_id or not session_id or not workspace_host_path:
            return None
        return PersistedAgentRuntime(
            agent_id=agent_id,
            session_id=session_id,
            workspace_host_path=workspace_host_path,
            created_at_ms=created_at_int,
        )


@dataclass
class PersistedSessionAutomation:
    main_thread_id: Optional[str] = None
    cron_jobs: list[PersistedCronJob] = field(default_factory=list)
    cron_runs_by_job: dict[str, list[PersistedCronRunMeta]] = field(default_factory=dict)
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
        cron_runs: dict[str, Any] = {}
        for jid, runs in (self.cron_runs_by_job or {}).items():
            if not isinstance(jid, str) or not jid.strip():
                continue
            if not isinstance(runs, list) or not runs:
                continue
            out_runs: list[dict[str, Any]] = []
            for r in runs:
                if not isinstance(r, PersistedCronRunMeta):
                    continue
                out_runs.append(r.to_json())
            if out_runs:
                cron_runs[jid.strip()] = out_runs
        return {
            "mainThreadId": self.main_thread_id,
            "cronJobs": [j.to_json() for j in self.cron_jobs],
            "cronRunsByJob": cron_runs,
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
        cron_runs_raw = obj.get("cronRunsByJob")
        cron_runs_by_job: dict[str, list[PersistedCronRunMeta]] = {}
        if isinstance(cron_runs_raw, dict):
            for jid, runs_raw in cron_runs_raw.items():
                if not isinstance(jid, str) or not jid.strip():
                    continue
                if not isinstance(runs_raw, list):
                    continue
                out_runs: list[PersistedCronRunMeta] = []
                for r in runs_raw:
                    pr = PersistedCronRunMeta.from_json(r)
                    if pr is not None:
                        out_runs.append(pr)
                if out_runs:
                    cron_runs_by_job[jid.strip()] = out_runs
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
            cron_runs_by_job=cron_runs_by_job,
            system_event_queues=queues,
            last_active_by_thread=last_active_by_thread,
        )


@dataclass
class PersistedGatewayAutomationState:
    version: int = 2
    default_session_id: Optional[str] = None
    sessions: dict[str, PersistedSessionAutomation] = field(default_factory=dict)
    agents: dict[str, PersistedAgentRuntime] = field(default_factory=dict)
    chat_bindings: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": int(self.version),
            "defaultSessionId": self.default_session_id,
            "sessions": {sid: sess.to_json() for sid, sess in self.sessions.items()},
            "agents": {aid: a.to_json() for aid, a in self.agents.items()},
            "chatBindings": dict(self.chat_bindings),
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

        agents_raw = obj.get("agents")
        agents: dict[str, PersistedAgentRuntime] = {}
        if isinstance(agents_raw, dict):
            for aid, raw_agent in agents_raw.items():
                aid_norm = _normalize_agent_id(aid)
                agent = PersistedAgentRuntime.from_json(raw_agent)
                if agent is None:
                    continue
                key = aid_norm or agent.agent_id
                if not key:
                    continue
                agents[key] = agent

        chat_bindings_raw = obj.get("chatBindings")
        chat_bindings: dict[str, str] = {}
        if isinstance(chat_bindings_raw, dict):
            for ck, aid in chat_bindings_raw.items():
                if not isinstance(ck, str) or not ck.strip():
                    continue
                agent_id = _normalize_agent_id(aid)
                if not agent_id:
                    continue
                chat_bindings[ck.strip()] = agent_id

        return PersistedGatewayAutomationState(
            version=version_int,
            default_session_id=default_session_id.strip() if isinstance(default_session_id, str) else None,
            sessions=sessions,
            agents=agents,
            chat_bindings=chat_bindings,
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
    followups: deque[Any] = field(default_factory=deque)
    busy_since_ms: Optional[int] = None
    last_progress_at_ms: Optional[int] = None
    last_unstick_attempt_at_ms: Optional[int] = None


@dataclass
class IsolatedCronTurnContext:
    session_id: str
    job_id: str
    run_id: str
    main_thread_id: str
    writeback_when: str
    writeback_max_chars: int
    writeback_prompt_hint: bool
    retention_max_unarchived_threads: int
    started_at_ms: int


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
        # Back-compat: previously this was the single workspace root for all sessions.
        # After multi-agent support, this is treated as a best-effort fallback only.
        self._workspace_host_path = workspace_host_path

        self._lanes: dict[tuple[str, str], ThreadLane] = {}
        self._cron_wakeup = asyncio.Event()
        self._heartbeat_wakeup = asyncio.Event()
        self._last_heartbeat_run_at_ms: dict[tuple[str, str], int] = {}

        self._tasks: list[asyncio.Task[None]] = []
        self._bootstrap_lock = asyncio.Lock()
        self._main_thread_locks: dict[str, asyncio.Lock] = {}
        self._session_backoff_until_ms: dict[str, int] = {}
        self._session_backoff_failures: dict[str, int] = {}
        self._turn_text_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._isolated_cron_context_by_key: dict[tuple[str, str, str], IsolatedCronTurnContext] = {}
        self._session_restart_locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        await self._store.load()
        await self._ensure_main_agent()
        await self._ensure_workspace_files()
        await self._restore_default_session_if_singleton()
        await self._prune_persisted_sessions()
        self._tasks.append(asyncio.create_task(self._cron_loop()))
        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))
        self._tasks.append(asyncio.create_task(self._lane_watchdog_loop()))

    def get_workspace_host_path_for_session(self, session_id: str) -> Optional[str]:
        sid = session_id.strip() if isinstance(session_id, str) else ""
        if not sid:
            return None
        st = self._store.state
        for agent in (st.agents or {}).values():
            if not isinstance(agent, PersistedAgentRuntime):
                continue
            if agent.session_id == sid and agent.workspace_host_path:
                return agent.workspace_host_path
        return None

    def resolve_agent_for_chat_key(self, chat_key: str) -> str:
        ck = chat_key.strip() if isinstance(chat_key, str) else ""
        if not ck:
            return "main"
        st = self._store.state
        agent_id = st.chat_bindings.get(ck) if isinstance(st.chat_bindings, dict) else None
        agent_id = _normalize_agent_id(agent_id) if isinstance(agent_id, str) else ""
        return agent_id or "main"

    def resolve_agent_session_id(self, agent_id: str) -> Optional[str]:
        aid = _normalize_agent_id(agent_id)
        if not aid:
            return None
        st = self._store.state
        agent = st.agents.get(aid) if isinstance(st.agents, dict) else None
        if isinstance(agent, PersistedAgentRuntime) and isinstance(agent.session_id, str) and agent.session_id.strip():
            return agent.session_id.strip()
        return None

    def resolve_session_id_for_chat_key(self, chat_key: str) -> Optional[str]:
        aid = self.resolve_agent_for_chat_key(chat_key)
        return self.resolve_agent_session_id(aid) or self._store.state.default_session_id

    async def create_agent(self, *, agent_id: str) -> PersistedAgentRuntime:
        if _provision_mode() != "docker":
            raise RuntimeError("Agent containers are only supported in docker provision mode")
        aid = _normalize_agent_id(agent_id)
        if not aid:
            raise ValueError("Invalid agentId (must match [a-zA-Z0-9][a-zA-Z0-9_-]{0,63})")
        if aid == "main":
            raise ValueError("agentId 'main' is reserved")
        if not self._home_host_path:
            raise RuntimeError("ARGUS_HOME_HOST_PATH is required to create agents")

        workspace_host_path = str((Path(self._home_host_path) / f"workspace-{aid}").resolve())
        session_id = uuid.uuid4().hex[:12]
        created_at_ms = _now_ms()

        def _write(st: PersistedGatewayAutomationState) -> None:
            st.version = max(int(getattr(st, "version", 1) or 1), 2)
            if aid in st.agents:
                raise ValueError(f"Agent already exists: {aid}")
            st.agents[aid] = PersistedAgentRuntime(
                agent_id=aid,
                session_id=session_id,
                workspace_host_path=workspace_host_path,
                created_at_ms=created_at_ms,
            )
            if session_id not in st.sessions:
                st.sessions[session_id] = PersistedSessionAutomation()

        await self._store.update(_write)
        await self._ensure_workspace_files_at(Path(workspace_host_path), legacy_home=None)
        # Eagerly create the container so /newagent immediately works even before first user message.
        await _ensure_live_docker_session(session_id, allow_create=True)
        agent = self._store.state.agents.get(aid)
        if not isinstance(agent, PersistedAgentRuntime):
            raise RuntimeError("Failed to persist agent")
        return agent

    async def bind_chat_to_agent(self, *, chat_key: str, agent_id: str) -> PersistedAgentRuntime:
        ck = chat_key.strip() if isinstance(chat_key, str) else ""
        if not ck:
            raise ValueError("Missing chatKey")
        aid = _normalize_agent_id(agent_id)
        if not aid:
            raise ValueError("Invalid agentId")

        def _write(st: PersistedGatewayAutomationState) -> None:
            st.version = max(int(getattr(st, "version", 1) or 1), 2)
            if aid not in st.agents:
                raise ValueError(f"Unknown agent: {aid}")
            st.chat_bindings[ck] = aid

        await self._store.update(_write)
        agent = self._store.state.agents.get(aid)
        if not isinstance(agent, PersistedAgentRuntime):
            raise RuntimeError("Agent not found after update")
        return agent

    def list_agents(self) -> list[dict[str, Any]]:
        st = self._store.state
        out: list[dict[str, Any]] = []
        for aid, agent in (st.agents or {}).items():
            if not isinstance(agent, PersistedAgentRuntime):
                continue
            out.append(
                {
                    "agentId": aid,
                    "sessionId": agent.session_id,
                    "workspaceHostPath": agent.workspace_host_path,
                    "createdAtMs": agent.created_at_ms,
                    "isDefault": bool(aid == "main"),
                }
            )
        out.sort(key=lambda x: (not x.get("isDefault"), x.get("agentId") or ""))
        return out

    async def _ensure_main_agent(self) -> None:
        if not self._home_host_path and not self._workspace_host_path:
            return

        now = _now_ms()
        home = Path(self._home_host_path) if self._home_host_path else None
        main_workspace_host_path = None
        if home is not None:
            main_workspace_host_path = str((home / "workspace").resolve())
        elif self._workspace_host_path:
            main_workspace_host_path = str(Path(self._workspace_host_path).resolve())

        if not main_workspace_host_path:
            return

        def _write(st: PersistedGatewayAutomationState) -> None:
            st.version = max(int(getattr(st, "version", 1) or 1), 2)
            existing = st.agents.get("main")
            main_session_id = None
            if isinstance(existing, PersistedAgentRuntime) and isinstance(existing.session_id, str) and existing.session_id.strip():
                main_session_id = existing.session_id.strip()
            if not main_session_id:
                main_session_id = st.default_session_id.strip() if isinstance(st.default_session_id, str) and st.default_session_id.strip() else None
            if not main_session_id:
                main_session_id = uuid.uuid4().hex[:12]

            st.default_session_id = main_session_id
            st.agents["main"] = PersistedAgentRuntime(
                agent_id="main",
                session_id=main_session_id,
                workspace_host_path=main_workspace_host_path,
                created_at_ms=getattr(existing, "created_at_ms", None) or now,
            )
            if main_session_id not in st.sessions:
                st.sessions[main_session_id] = PersistedSessionAutomation()

        await self._store.update(_write)

    async def _restore_default_session_if_singleton(self) -> None:
        st = self._store.state
        if isinstance(st.default_session_id, str) and st.default_session_id.strip():
            return
        keys = [sid.strip() for sid in st.sessions.keys() if isinstance(sid, str) and sid.strip()]
        if len(keys) != 1:
            return
        sid = keys[0]
        await self._store.update(lambda st2: setattr(st2, "default_session_id", sid))
        log.info("Restored missing defaultSessionId to singleton persisted session %s", sid)

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
        if not live_session_ids:
            # Safety: if Docker returns no live session containers (common right after a clean rebuild, or during
            # transient Docker API issues), pruning would wipe persisted automation state including cron jobs.
            # Keep state; a session can be recreated later with the persisted defaultSessionId.
            log.info("No live docker sessions found; skipping automation-state pruning")
            return
        live_set = set(live_session_ids)

        st = self._store.state
        protected: set[str] = set()
        if isinstance(st.default_session_id, str) and st.default_session_id.strip():
            protected.add(st.default_session_id.strip())
        for agent in (st.agents or {}).values():
            if isinstance(agent, PersistedAgentRuntime) and isinstance(agent.session_id, str) and agent.session_id.strip():
                protected.add(agent.session_id.strip())

        removed = [sid for sid in st.sessions.keys() if sid not in live_set and sid not in protected]
        default_missing = bool(
            st.default_session_id and st.default_session_id not in live_set and st.default_session_id not in protected
        )
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
        if self._home_host_path:
            return Path(self._home_host_path) / "workspace"
        if self._workspace_host_path:
            return Path(self._workspace_host_path)
        return None

    def _workspace_root_for_session(self, session_id: str) -> Optional[Path]:
        sid = session_id.strip() if isinstance(session_id, str) else ""
        if sid:
            p = self.get_workspace_host_path_for_session(sid)
            if isinstance(p, str) and p.strip():
                return Path(p)
        return self._workspace_root()

    async def _ensure_workspace_files(self) -> None:
        root = self._workspace_root()
        if root is None:
            return

        legacy_home = Path(self._home_host_path) if self._home_host_path else None
        try:
            await self._ensure_workspace_files_at(root, legacy_home=legacy_home)
        except Exception:
            log.exception("Failed to initialize workspace templates under %s", str(root))

    async def _ensure_workspace_files_at(self, root: Path, *, legacy_home: Optional[Path]) -> None:
        def _ensure() -> None:
            root.mkdir(parents=True, exist_ok=True)
            for template_name, target_name in WORKSPACE_BOOTSTRAP_TEMPLATES:
                target_path = root / target_name
                if target_path.exists():
                    continue
                # Migration: if legacy HEARTBEAT.md exists in home, keep it (main workspace only).
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

            _bootstrap_workspace_skill_templates(root)

        await asyncio.to_thread(_ensure)

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

    def _mark_lane_busy(self, lane: ThreadLane, *, turn_id: Optional[str] = None) -> None:
        now = _now_ms()
        lane.busy = True
        if lane.busy_since_ms is None:
            lane.busy_since_ms = now
        lane.last_progress_at_ms = now
        if isinstance(turn_id, str) and turn_id.strip():
            lane.active_turn_id = turn_id.strip()

    def _mark_lane_idle(self, lane: ThreadLane) -> None:
        lane.busy = False
        lane.active_turn_id = None
        lane.busy_since_ms = None
        lane.last_progress_at_ms = None

    def _note_lane_progress(self, lane: ThreadLane) -> None:
        lane.last_progress_at_ms = _now_ms()

    def _stuck_turn_timeout_ms(self) -> int:
        # Safety net: if a turn never emits `turn/completed`, lanes can get stuck "busy" forever
        # and block cron/heartbeat/user follow-ups. Default: 15 minutes; set 0 to disable.
        raw = os.getenv("ARGUS_STUCK_TURN_TIMEOUT_S")
        if raw is None or not raw.strip():
            return 15 * 60 * 1000
        try:
            s = float(raw)
        except Exception:
            return 15 * 60 * 1000
        if s <= 0:
            return 0
        return int(s * 1000)

    def _reset_lane_state_for_session(self, session_id: str) -> list[str]:
        affected_tids: list[str] = []
        for (sid, tid), lane in list(self._lanes.items()):
            if sid != session_id:
                continue
            # Keep followups; only clear the active turn state.
            self._mark_lane_idle(lane)
            affected_tids.append(tid)

        # Clear any partial turn text buffers for this session (memory safety).
        for key in list(self._turn_text_by_key.keys()):
            if key[0] == session_id:
                self._turn_text_by_key.pop(key, None)
        return affected_tids

    async def _force_close_live_session(self, session_id: str) -> None:
        live: Optional[LiveDockerSession] = None
        try:
            async with app.state.sessions_lock:
                live = app.state.sessions.pop(session_id, None)
        except Exception:
            live = None
        if live is None:
            return
        try:
            live.closed = True
        except Exception:
            pass
        try:
            live.writer.close()
        except Exception:
            pass
        try:
            if live.pump_task is not None:
                live.pump_task.cancel()
        except Exception:
            pass

    async def _restart_runtime_container(self, session_id: str) -> None:
        if _provision_mode() != "docker":
            return
        try:
            container = await asyncio.to_thread(_docker_get_container_by_session_sync, session_id)
        except Exception as e:
            log.warning("Failed to locate runtime container for session %s: %s", session_id, str(e))
            return
        if container is None:
            return
        try:
            await asyncio.to_thread(container.restart, timeout=10)
        except Exception as e:
            log.warning("Failed to restart runtime container for session %s: %s", session_id, str(e))

    async def _unstick_session(self, session_id: str, *, reason: str) -> None:
        lock = self._session_restart_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_restart_locks[session_id] = lock

        async with lock:
            log.warning("Unsticking session %s (%s): resetting lanes and restarting runtime", session_id, reason)
            await self._force_close_live_session(session_id)
            await self._restart_runtime_container(session_id)
            self._clear_session_backoff(session_id)

            affected_tids = self._reset_lane_state_for_session(session_id)
            for tid in affected_tids:
                lane = self.lane(session_id, tid)
                if lane.followups:
                    asyncio.create_task(self._process_lane_after_turn(session_id, tid))
            self.request_heartbeat_now()

    def on_runtime_session_closed(self, session_id: str) -> None:
        # Called when the TCP session to the runtime closes unexpectedly (crash/OOM/restart).
        affected_tids = self._reset_lane_state_for_session(session_id)
        try:
            for tid in affected_tids:
                lane = self.lane(session_id, tid)
                if lane.followups:
                    asyncio.create_task(self._process_lane_after_turn(session_id, tid))
            self.request_heartbeat_now()
        except RuntimeError:
            # No running loop; ignore.
            pass

    async def _lane_watchdog_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(15.0)
                await self._lane_watchdog_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Lane watchdog error")
                await asyncio.sleep(2.0)

    async def _lane_watchdog_tick(self) -> None:
        timeout_ms = self._stuck_turn_timeout_ms()
        if timeout_ms <= 0:
            return

        now = _now_ms()
        stuck_sessions: set[str] = set()
        for (sid, tid), lane in list(self._lanes.items()):
            if not lane.busy:
                continue
            since = lane.last_progress_at_ms or lane.busy_since_ms or now
            if now - since < timeout_ms:
                continue

            last_attempt = lane.last_unstick_attempt_at_ms
            if last_attempt is not None and now - last_attempt < max(60_000, timeout_ms // 2):
                continue
            lane.last_unstick_attempt_at_ms = now

            log.warning(
                "Lane stuck: session=%s thread=%s activeTurnId=%s busySinceMs=%s lastProgressAtMs=%s followupDepth=%d",
                sid,
                tid,
                lane.active_turn_id,
                lane.busy_since_ms,
                lane.last_progress_at_ms,
                len(lane.followups),
            )
            stuck_sessions.add(sid)

        for sid in stuck_sessions:
            await self._unstick_session(sid, reason="stuck_turn")

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
            if thread_id:
                self._note_lane_progress(self.lane(session_id, thread_id))
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
            if thread_id:
                self._note_lane_progress(self.lane(session_id, thread_id))
            return

        if method == "turn/started":
            if thread_id:
                lane = self.lane(session_id, thread_id)
                self._mark_lane_busy(lane, turn_id=turn_id)
            return

        if method == "turn/completed":
            turn_status_raw = None
            turn_error_message = None
            turn_obj = params.get("turn")
            if isinstance(turn_obj, dict):
                turn_status_raw = turn_obj.get("status")
                err = turn_obj.get("error")
                if isinstance(err, dict):
                    emsg = err.get("message")
                    if isinstance(emsg, str) and emsg.strip():
                        turn_error_message = emsg.strip()

            if thread_id:
                lane = self.lane(session_id, thread_id)
                self._mark_lane_idle(lane)
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
                if key:
                    asyncio.create_task(
                        self._handle_isolated_cron_turn_completed(
                            session_id=session_id,
                            thread_id=thread_id,
                            turn_id=turn_id,
                            final_text=final_text,
                            turn_status_raw=turn_status_raw,
                            turn_error_message=turn_error_message,
                        )
                    )
                if final_text.strip():
                    asyncio.create_task(self._deliver_turn_text(session_id, thread_id, final_text))
            return

    async def _handle_isolated_cron_turn_completed(
        self,
        *,
        session_id: str,
        thread_id: str,
        turn_id: Optional[str],
        final_text: str,
        turn_status_raw: Any,
        turn_error_message: Optional[str],
    ) -> None:
        if not turn_id:
            return
        sid = session_id.strip() if isinstance(session_id, str) else ""
        tid = thread_id.strip() if isinstance(thread_id, str) else ""
        turn_id = turn_id.strip() if isinstance(turn_id, str) else ""
        if not sid or not tid or not turn_id:
            return

        key = (sid, tid, turn_id)
        ctx = self._isolated_cron_context_by_key.pop(key, None)
        if ctx is None:
            return

        status_raw = str(turn_status_raw or "").strip().lower()
        if status_raw == "failed":
            status = "error"
        elif status_raw == "interrupted":
            status = "canceled"
        elif status_raw == "completed":
            status = "success"
        else:
            status = "unknown"

        ended_at_ms = _now_ms()
        summary = str(final_text or "").strip()
        if not summary:
            if status == "error" and isinstance(turn_error_message, str) and turn_error_message.strip():
                summary = turn_error_message.strip()
            else:
                summary = "(no output)"

        if len(summary) > ctx.writeback_max_chars:
            summary = summary[: ctx.writeback_max_chars]

        def _patch_run(st: PersistedGatewayAutomationState) -> None:
            sess = st.sessions.get(sid)
            if sess is None:
                return
            runs = sess.cron_runs_by_job.get(ctx.job_id) or []
            for r in reversed(runs):
                if not isinstance(r, PersistedCronRunMeta):
                    continue
                if r.run_id == ctx.run_id and r.thread_id == tid:
                    r.status = status
                    r.ended_at_ms = ended_at_ms
                    r.summary = summary
                    if status == "error" and isinstance(turn_error_message, str) and turn_error_message.strip():
                        r.error = turn_error_message.strip()
                    break

        await self._store.update(_patch_run)

        try:
            await self._enforce_cron_retention(
                session_id=sid,
                job_id=ctx.job_id,
                max_unarchived_threads=ctx.retention_max_unarchived_threads,
            )
        except Exception:
            log.exception("Cron retention enforcement failed for %s/%s", sid, ctx.job_id)

        when = (ctx.writeback_when or "").strip().lower()
        if when == "never":
            return
        if when == "on-error" and status != "error":
            return

        # Write back a short status/pointer event to the current main thread.
        main_tid = None
        sess_state = self._store.state.sessions.get(sid)
        if sess_state is not None:
            main_tid = sess_state.main_thread_id
        if not isinstance(main_tid, str) or not main_tid.strip():
            try:
                main_tid = await self.ensure_main_thread(sid)
            except Exception:
                main_tid = ctx.main_thread_id

        text = f"Cron: {ctx.job_id} [{status}] {summary}".strip()
        meta = {
            "jobId": ctx.job_id,
            "runId": ctx.run_id,
            "threadId": tid,
            "turnId": turn_id,
            "status": status,
            "endedAtMs": ended_at_ms,
            "wakeHeartbeat": False,
        }
        try:
            await self.enqueue_system_event(
                session_id=sid,
                thread_id=main_tid.strip(),
                kind=CRON_WRITEBACK_EVENT_KIND,
                text=text,
                meta=meta,
            )
        except Exception:
            log.exception("Failed to enqueue cron writeback for %s/%s/%s", sid, ctx.job_id, ctx.run_id)
        return

    async def _enforce_cron_retention(
        self,
        *,
        session_id: str,
        job_id: str,
        max_unarchived_threads: int,
    ) -> None:
        sid = session_id.strip() if isinstance(session_id, str) else ""
        jid = job_id.strip() if isinstance(job_id, str) else ""
        if not sid or not jid:
            return
        max_unarchived_threads = int(max_unarchived_threads or 0)
        if max_unarchived_threads <= 0:
            return

        sess_state = self._store.state.sessions.get(sid)
        if sess_state is None:
            return
        runs = sess_state.cron_runs_by_job.get(jid) or []
        if not runs:
            return

        unarchived: list[PersistedCronRunMeta] = []
        for r in runs:
            if not isinstance(r, PersistedCronRunMeta):
                continue
            if r.archived:
                continue
            if not isinstance(r.thread_id, str) or not r.thread_id.strip():
                continue
            unarchived.append(r)

        if len(unarchived) <= max_unarchived_threads:
            return

        # Prefer archiving the oldest completed runs; never archive a currently-running run.
        candidates = [r for r in unarchived if (r.status or "").strip().lower() != "running"]
        candidates.sort(key=lambda r: (r.started_at_ms or 0, r.run_id))
        surplus = len(unarchived) - max_unarchived_threads
        to_archive = candidates[:surplus]
        if not to_archive:
            return

        live, _ = await _ensure_live_docker_session(sid, allow_create=True)
        await self._ensure_initialized(live)

        archived_ids: set[str] = set()
        for r in to_archive:
            try:
                await self._rpc(live, "thread/archive", {"threadId": r.thread_id})
                archived_ids.add(r.run_id)
            except Exception as e:
                log.warning(
                    "Failed to archive cron run thread for %s/%s runId=%s threadId=%s: %s",
                    sid,
                    jid,
                    r.run_id,
                    r.thread_id,
                    str(e),
                )

        if not archived_ids:
            return

        def _mark_archived(st: PersistedGatewayAutomationState) -> None:
            sess = st.sessions.get(sid)
            if sess is None:
                return
            runs2 = sess.cron_runs_by_job.get(jid) or []
            for r2 in runs2:
                if not isinstance(r2, PersistedCronRunMeta):
                    continue
                if r2.run_id in archived_ids:
                    r2.archived = True

        await self._store.update(_mark_archived)

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
        final_raw = stripped.get("text") if isinstance(stripped.get("text"), str) else text
        final_raw = final_raw.strip()
        if not final_raw:
            return

        clean_text, media_refs = _split_media_from_output(final_raw)
        delivered_any = False

        clean_text = clean_text.strip()
        if clean_text:
            truncated = _telegram_truncate(clean_text)
            html = _markdown_to_telegram_html(truncated)
            try:
                if html:
                    try:
                        await _telegram_send_message(token=token, target=target, text=html, parse_mode="HTML")
                        delivered_any = True
                    except Exception as e:
                        msg = str(e)
                        if TELEGRAM_HTML_PARSE_ERR_RE.search(msg) or TELEGRAM_MESSAGE_TOO_LONG_RE.search(msg):
                            # Fall back to plain text.
                            await _telegram_send_message(token=token, target=target, text=truncated, parse_mode=None)
                            delivered_any = True
                        else:
                            raise
                else:
                    await _telegram_send_message(token=token, target=target, text=truncated, parse_mode=None)
                    delivered_any = True
            except Exception as e:
                log.warning("Failed to deliver to telegram for %s/%s: %s", session_id, thread_id, str(e))

        if media_refs:
            workspace_root = self._workspace_root_for_session(session_id)
            root_resolved = None
            if workspace_root is not None:
                try:
                    root_resolved = workspace_root.resolve()
                except Exception:
                    root_resolved = workspace_root

            for ref in media_refs[:5]:
                try:
                    if ref.startswith("http://") or ref.startswith("https://"):
                        if _is_image_media_ref(ref):
                            try:
                                await _telegram_send_photo_url(token=token, target=target, url=ref)
                            except Exception:
                                await _telegram_send_document_url(token=token, target=target, url=ref)
                        else:
                            await _telegram_send_document_url(token=token, target=target, url=ref)
                        delivered_any = True
                        continue

                    if not ref.startswith("./"):
                        continue
                    if root_resolved is None:
                        raise RuntimeError("workspace root is not configured (ARGUS_HOME_HOST_PATH/ARGUS_WORKSPACE_HOST_PATH)")

                    rel = ref[2:]
                    cand = (root_resolved / rel).resolve()
                    if not cand.is_relative_to(root_resolved):
                        raise RuntimeError("media path escapes workspace root")
                    if not cand.is_file():
                        raise RuntimeError("media file not found")
                    size = cand.stat().st_size
                    if size > TELEGRAM_MAX_UPLOAD_BYTES:
                        raise RuntimeError(f"media file too large ({size} bytes)")

                    if _is_image_media_ref(ref):
                        try:
                            await _telegram_send_photo_file(token=token, target=target, file_path=cand)
                        except Exception:
                            await _telegram_send_document_file(token=token, target=target, file_path=cand)
                    else:
                        await _telegram_send_document_file(token=token, target=target, file_path=cand)
                    delivered_any = True
                except Exception as e:
                    log.warning(
                        "Failed to deliver telegram attachment for %s/%s (ref=%s): %s",
                        session_id,
                        thread_id,
                        ref,
                        str(e),
                    )

        if not delivered_any:
            return

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

        # If defaultSessionId is missing but we have exactly one persisted session, adopt it.
        persisted_ids = [sid.strip() for sid in self._store.state.sessions.keys() if isinstance(sid, str) and sid.strip()]
        if len(persisted_ids) == 1:
            sid = persisted_ids[0]
            await self._store.update(lambda st: setattr(st, "default_session_id", sid))
            return sid

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
        lock = self._main_thread_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._main_thread_locks[session_id] = lock

        async with lock:
            def get_existing(st: PersistedGatewayAutomationState) -> Optional[str]:
                sess = st.sessions.get(session_id)
                return sess.main_thread_id if sess else None

            existing = get_existing(self._store.state)
            live, _ = await _ensure_live_docker_session(session_id, allow_create=True)
            await self._ensure_initialized(live)

            if isinstance(existing, str) and existing.strip():
                try:
                    await self._ensure_thread_loaded_or_resumed(live, existing.strip())
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

    async def set_main_thread(self, session_id: str, thread_id: str) -> str:
        thread_id = thread_id.strip() if isinstance(thread_id, str) else ""
        if not thread_id:
            raise ValueError("thread_id must not be empty")

        lock = self._main_thread_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._main_thread_locks[session_id] = lock

        async with lock:
            live, _ = await _ensure_live_docker_session(session_id, allow_create=True)
            await self._ensure_initialized(live)

            # Ensure the thread exists/is loadable in the upstream app-server.
            await self._ensure_thread_loaded_or_resumed(live, thread_id)

            def _save_main(st: PersistedGatewayAutomationState) -> None:
                if not st.default_session_id:
                    st.default_session_id = session_id
                sess = st.sessions.get(session_id)
                if sess is None:
                    sess = PersistedSessionAutomation()
                    st.sessions[session_id] = sess
                prev_tid = sess.main_thread_id
                sess.main_thread_id = thread_id
                if prev_tid and prev_tid != thread_id:
                    # Migrate pending system events to the new main thread so they don't get stuck.
                    old_q = sess.system_event_queues.pop(prev_tid, None)
                    if old_q:
                        new_q = sess.system_event_queues.get(thread_id)
                        if new_q is None:
                            sess.system_event_queues[thread_id] = old_q
                        else:
                            new_q.extend(old_q)

            await self._store.update(_save_main)
            return thread_id

    async def enqueue_user_input(
        self,
        *,
        session_id: str,
        thread_id: Optional[str],
        target: Optional[str],
        text: str,
        source_channel: Optional[str] = None,
        source_chat_key: Optional[str] = None,
        telegram_images: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        if telegram_images is not None and not isinstance(telegram_images, list):
            telegram_images = None
        if telegram_images:
            telegram_images = [x for x in telegram_images if isinstance(x, dict)]
            if not telegram_images:
                telegram_images = None

        if not text.strip() and telegram_images:
            text = "<media:image>"
        if not text.strip():
            raise ValueError("text must not be empty")

        # Adopt the first actively-used session as the gateway default session if missing.
        # This prevents "TG works but defaultSessionId stays null" after a clean rebuild.
        if not self._store.state.default_session_id:
            await self._store.update(lambda st: setattr(st, "default_session_id", session_id))

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
            lane.followups.append({"text": text, "source_channel": source_channel, "source_chat_key": source_chat_key, "telegram_images": telegram_images})
            return {
                "ok": True,
                "threadId": thread_id,
                "queued": True,
                "started": False,
                "followupDepth": len(lane.followups),
            }

        async with lane.lock:
            if lane.busy:
                lane.followups.append({"text": text, "source_channel": source_channel, "source_chat_key": source_chat_key, "telegram_images": telegram_images})
                return {
                    "ok": True,
                    "threadId": thread_id,
                    "queued": True,
                    "started": False,
                    "followupDepth": len(lane.followups),
                }
            self._mark_lane_busy(lane)

            try:
                # `turn/start` fails with "thread not found" if the app-server process hasn't loaded the thread yet
                # (e.g. after reconnect/restart). Make `argus/input/enqueue` robust by resuming first.
                await self._ensure_thread_loaded_or_resumed(live, thread_id)
                assembled = await self._assemble_turn_input(
                    session_id,
                    thread_id,
                    user_text=text,
                    heartbeat=False,
                    source_channel=source_channel,
                    source_chat_key=source_chat_key,
                )

                local_images: list[str] = []
                if (
                    telegram_images
                    and isinstance(source_channel, str)
                    and source_channel.strip().lower() in ("telegram", "tg")
                ):
                    local_images = await self._prepare_telegram_local_images(
                        session_id=session_id,
                        source_chat_key=source_chat_key,
                        telegram_images=telegram_images,
                    )

                input_items: list[dict[str, Any]] = [{"type": "text", "text": assembled}]
                for pth in local_images:
                    input_items.append({"type": "localImage", "path": pth})

                resp = await self._rpc(
                    live,
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": input_items,
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
                    self._note_lane_progress(lane)
                return {"ok": True, "threadId": thread_id, "queued": False, "started": True, "turnId": turn_id}
            except Exception as e:
                self._mark_lane_idle(lane)
                try:
                    ended_at_ms = _now_ms()

                    def _mark_failed(st: PersistedGatewayAutomationState) -> None:
                        sess = st.sessions.get(sid)
                        if sess is None:
                            return
                        runs = sess.cron_runs_by_job.get(job.job_id) or []
                        for r in reversed(runs):
                            if not isinstance(r, PersistedCronRunMeta):
                                continue
                            if r.run_id == run_id and r.thread_id == thread_id:
                                r.status = "error"
                                r.ended_at_ms = ended_at_ms
                                msg = str(e).strip()
                                r.error = msg or "turn/start failed"
                                r.summary = r.error
                                break

                    await self._store.update(_mark_failed)
                except Exception:
                    pass
                try:
                    await self._enforce_cron_retention(
                        session_id=sid,
                        job_id=job.job_id,
                        max_unarchived_threads=retention_max_unarchived,
                    )
                except Exception:
                    pass
                raise

    async def _prepare_telegram_local_images(
        self,
        *,
        session_id: str,
        source_chat_key: Optional[str],
        telegram_images: list[dict[str, Any]],
    ) -> list[str]:
        if not telegram_images:
            return []

        token = os.getenv("TELEGRAM_BOT_TOKEN") or ""
        if not token.strip():
            return []

        root = self._workspace_root_for_session(session_id)
        if root is None:
            return []

        chat_part = _sanitize_path_part(source_chat_key or session_id or "unknown", fallback="unknown")
        base_dir = root / "inbox" / "telegram" / chat_part
        try:
            base_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        mime_to_ext = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
        }

        out: list[str] = []
        seen: set[str] = set()

        for idx, ref in enumerate(telegram_images[:8], start=1):
            if not isinstance(ref, dict):
                continue
            file_id = ref.get("fileId") or ref.get("file_id")
            if not isinstance(file_id, str) or not file_id.strip():
                continue
            file_id = file_id.strip()
            if file_id in seen:
                continue
            seen.add(file_id)

            unique_id = ref.get("fileUniqueId") or ref.get("file_unique_id") or file_id
            if not isinstance(unique_id, str) or not unique_id.strip():
                unique_id = file_id
            unique_part = _sanitize_path_part(unique_id, fallback=file_id)

            source = ref.get("source") or "message"
            if not isinstance(source, str) or not source.strip():
                source = "message"
            source_part = _sanitize_path_part(source, fallback="message", max_len=16)

            file_name = ref.get("fileName") or ref.get("file_name")
            mime_type = ref.get("mimeType") or ref.get("mime_type")

            try:
                info = await asyncio.to_thread(_telegram_get_file_sync, token, file_id)
            except Exception as e:
                log.warning("Telegram getFile failed for fileId=%s: %s", file_id, str(e))
                continue

            file_path = info.get("file_path") if isinstance(info, dict) else None
            if not isinstance(file_path, str) or not file_path.strip():
                log.warning("Telegram getFile missing file_path for fileId=%s", file_id)
                continue

            file_size = info.get("file_size") if isinstance(info, dict) else None
            if isinstance(file_size, int) and file_size > TELEGRAM_MAX_DOWNLOAD_BYTES:
                log.warning("Telegram file too large for fileId=%s size=%s", file_id, file_size)
                continue

            ext = Path(file_path).suffix.lower()
            if not ext and isinstance(file_name, str) and file_name.strip():
                ext = Path(file_name.strip()).suffix.lower()
            if not ext and isinstance(mime_type, str) and mime_type.strip():
                ext = mime_to_ext.get(mime_type.strip().lower(), "")
            if not ext:
                ext = ".jpg"
            if not ext.startswith(".") or len(ext) > 10:
                ext = ".jpg"

            ts = _now_ms()
            dest = base_dir / f"{ts}_{idx:02d}_{source_part}_{unique_part}{ext}"
            try:
                await asyncio.to_thread(
                    _telegram_download_file_to_path_sync,
                    token,
                    file_path,
                    dest,
                    max_bytes=TELEGRAM_MAX_DOWNLOAD_BYTES,
                )
            except Exception as e:
                log.warning("Telegram download failed for fileId=%s: %s", file_id, str(e))
                continue

            try:
                rel = dest.relative_to(root).as_posix()
            except Exception:
                rel = str(dest)
            out.append(rel)

        return out

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
        if self._system_event_wakes_heartbeat(ev):
            self.request_heartbeat_now()
        return ev

    def request_heartbeat_now(self) -> None:
        self._heartbeat_wakeup.set()

    def _system_event_wakes_heartbeat(self, ev: PersistedSystemEvent) -> bool:
        # Node up/down are status-only: record them, but don't wake heartbeat.
        # Allow explicit override via meta.wakeHeartbeat.
        wake = None
        if isinstance(ev.meta, dict):
            wake = ev.meta.get("wakeHeartbeat")
        if wake is True:
            return True
        if wake is False:
            return False
        return ev.kind != "node"

    def _queue_has_actionable_system_events(self, q: list[PersistedSystemEvent]) -> bool:
        return any(self._system_event_wakes_heartbeat(ev) for ev in q)

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
        if self._queue_has_actionable_system_events(q):
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
            followup_texts: list[str] = []
            telegram_images: list[dict[str, Any]] = []
            telegram_channel: Optional[str] = None
            telegram_chat_key: Optional[str] = None

            while lane.followups:
                item = lane.followups.popleft()
                if isinstance(item, str):
                    followup_texts.append(item)
                    continue
                if not isinstance(item, dict):
                    continue
                t = item.get("text")
                if isinstance(t, str) and t.strip():
                    followup_texts.append(t)
                ch = item.get("source_channel")
                ck = item.get("source_chat_key")
                if isinstance(ch, str) and ch.strip():
                    telegram_channel = ch.strip()
                if isinstance(ck, str) and ck.strip():
                    telegram_chat_key = ck.strip()
                imgs = item.get("telegram_images")
                if isinstance(imgs, list):
                    telegram_images.extend([x for x in imgs if isinstance(x, dict)])
            merged = self._format_followups(followup_texts)

            live, _ = await _ensure_live_docker_session(session_id, allow_create=True)
            await self._ensure_initialized(live)

            self._mark_lane_busy(lane)
            try:
                await self._ensure_thread_loaded_or_resumed(live, thread_id)
                assembled = await self._assemble_turn_input(
                    session_id,
                    thread_id,
                    user_text=merged,
                    heartbeat=False,
                    source_channel=telegram_channel,
                    source_chat_key=telegram_chat_key,
                )

                local_images: list[str] = []
                if (
                    telegram_images
                    and isinstance(telegram_channel, str)
                    and telegram_channel.strip().lower() in ("telegram", "tg")
                ):
                    local_images = await self._prepare_telegram_local_images(
                        session_id=session_id,
                        source_chat_key=telegram_chat_key,
                        telegram_images=telegram_images,
                    )

                input_items: list[dict[str, Any]] = [{"type": "text", "text": assembled}]
                for pth in local_images:
                    input_items.append({"type": "localImage", "path": pth})

                resp = await self._rpc(
                    live,
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": input_items,
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
                self._note_lane_progress(lane)
            except Exception:
                self._mark_lane_idle(lane)
                log.exception("Failed to start follow-up turn for %s/%s", session_id, thread_id)

    def _format_followups(self, texts: list[str]) -> str:
        lines = ["Follow-ups (batched):"]
        for i, t in enumerate(texts, start=1):
            lines.append(f"{i}) {t.strip()}")
        return "\n".join(lines).strip()

    async def _read_project_context_block(self, *, session_id: str, include_heartbeat: bool) -> str:
        root = self._workspace_root_for_session(session_id)
        if root is None:
            return ""

        filenames = list(PROJECT_CONTEXT_FILENAMES)
        if include_heartbeat:
            filenames.append(HEARTBEAT_FILENAME)

        def _read() -> str:
            blocks: list[str] = []

            sid = (session_id or "").strip()
            if sid:
                blocks.append(
                    "\n".join(
                        [
                            "[ARGUS]",
                            f"sessionId: {sid}",
                            f"selfNodeId: runtime:{sid}",
                            'node_invoke 建议：优先用 node="self"；多 runtime 在线时用 node="runtime:<sessionId>"（即 selfNodeId）。',
                            "[/ARGUS]",
                        ]
                    )
                )

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

    async def _read_skills_prompt_block(self, *, session_id: str) -> str:
        root = self._workspace_root_for_session(session_id)
        if root is None:
            return ""

        def _read() -> str:
            skills_root = root / SKILLS_DIRNAME
            if not skills_root.is_dir():
                return ""
            skills: list[dict[str, str]] = []
            for skill_dir in sorted([p for p in skills_root.iterdir() if p.is_dir()]):
                skill_md = skill_dir / SKILL_MD_FILENAME
                if not skill_md.is_file():
                    continue
                try:
                    raw = skill_md.read_text(encoding="utf-8")
                except Exception:
                    continue
                meta = _parse_yaml_front_matter(raw)
                name = (meta.get("name") or skill_dir.name).strip()
                if not name:
                    continue
                description = (meta.get("description") or "").strip()
                location = f"/root/.argus/workspace/{SKILLS_DIRNAME}/{skill_dir.name}/{SKILL_MD_FILENAME}"
                skills.append({"name": name, "description": description, "location": location})

            if not skills:
                return ""

            lines: list[str] = [
                "## Skills (mandatory)",
                "",
                "A skill is a set of local instructions stored in a `SKILL.md` file.",
                "",
                "Trigger rules:",
                "- If the user names a skill (e.g. `$skill-creator`) OR the task clearly matches a skill’s description, you MUST use that skill for this turn.",
                "- After selecting a skill, read its `SKILL.md` at the `location` (read only enough to follow the workflow).",
                "",
                "<available_skills>",
            ]
            for s in skills:
                lines.append(f"- name: {s['name']}")
                if s["description"]:
                    lines.append(f"  description: {s['description']}")
                lines.append(f"  location: {s['location']}")
            lines.append("</available_skills>")
            return "\n".join(lines).strip()

        try:
            return await asyncio.to_thread(_read)
        except Exception:
            log.exception("Failed to read skills from workspace")
            return ""

    async def _assemble_turn_input(
        self,
        session_id: str,
        thread_id: str,
        *,
        user_text: str,
        heartbeat: bool,
        source_channel: Optional[str] = None,
        source_chat_key: Optional[str] = None,
    ) -> str:
        drained = await self._drain_system_events(session_id, thread_id, max_events=20)
        blocks: list[str] = []

        project_context = await self._read_project_context_block(session_id=session_id, include_heartbeat=heartbeat)
        if project_context:
            blocks.append(project_context)

        # Heartbeat turns must stay narrowly focused on HEARTBEAT.md + system events.
        # Including the full skills prompt here can accidentally prime irrelevant user-facing output.
        if not heartbeat:
            skills_block = await self._read_skills_prompt_block(session_id=session_id)
            if skills_block:
                blocks.append(skills_block)

        if not heartbeat:
            ch = source_channel.strip() if isinstance(source_channel, str) else ""
            ck = source_chat_key.strip() if isinstance(source_chat_key, str) else ""
            if ch or ck:
                lines = ["[SOURCE]"]
                if ch:
                    lines.append(f"channel: {ch}")
                if ck:
                    lines.append(f"chatKey: {ck}")
                lines.append("[/SOURCE]")
                blocks.append("\n".join(lines).strip())

        if heartbeat:
            blocks.append("HEARTBEAT: You are running a background heartbeat. Read and follow HEARTBEAT.md in # Project Context.")
        elif user_text.strip():
            blocks.append(user_text.strip())

        if drained:
            blocks.append("System events (batched, highest priority):\n" + "\n".join(drained))

        if heartbeat:
                blocks.append(
                    "\n".join(
                        [
                            "Heartbeat response contract:",
                            "- TURN_KIND=heartbeat (background automation tick).",
                            "- This is a HEARTBEAT turn. There is NO user message to answer in this turn.",
                            "- Your decision MUST be based ONLY on: (a) HEARTBEAT.md in # Project Context, and (b) the system events shown in this message.",
                            "- Ignore ALL conversation history. Do NOT answer or re-answer any previous user message, and do NOT restate previous assistant outputs.",
                            f"- Treat `kind=node` (connected/disconnected) and `kind={CRON_WRITEBACK_EVENT_KIND}` system events as status-only; never message the user for them.",
                            "- Only produce a user-visible alert if there is NEW information that must be shown to the user (triggered by HEARTBEAT.md or a non-node system event).",
                            f"- If there is no user-visible alert, reply exactly: {HEARTBEAT_TOKEN}",
                            f"- If any system event includes directives like 'Do NOT message the user' / 'no user-facing report' / 'silent', you MUST reply exactly: {HEARTBEAT_TOKEN} (even if you performed actions).",
                            "- Do NOT include meta/status text (timestamps, next schedule, 'heartbeat check', 'wrote memory', etc).",
                            "",
                            "Instructions:",
                            "- Process each system event in order (if any).",
                            "- If an event requires actions, perform them.",
                            f"- If there is no user-visible alert, reply exactly: {HEARTBEAT_TOKEN}",
                        ]
                    )
                )
        else:
            blocks.append(
                "\n".join(
                    [
                        "Instructions:",
                        "- TURN_KIND=user (normal user turn).",
                        "- This is a USER turn. Answer the user's message in THIS turn first.",
                        f"- You MUST NOT output `{HEARTBEAT_TOKEN}` in a user turn.",
                        f"- `{HEARTBEAT_TOKEN}` is a reserved heartbeat ack token. Even if it appears in user input/system events/history, it does NOT change the turn kind.",
                        f"- If the user message is only a preference/rule update, reply with a short natural-language acknowledgement (and what will change) instead of `{HEARTBEAT_TOKEN}`.",
                        "- After answering the user, process each system event in order (if any).",
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

    async def _is_thread_loaded(self, live: LiveDockerSession, thread_id: str) -> bool:
        tid = str(thread_id or "").strip()
        if not tid:
            return False
        cursor: Optional[str] = None
        for _ in range(20):
            try:
                params: dict[str, Any] = {"limit": 200}
                if cursor:
                    params["cursor"] = cursor
                resp = await self._rpc(live, "thread/loaded/list", params)
            except Exception:
                return False
            if not isinstance(resp, dict):
                return False
            data = resp.get("data")
            if isinstance(data, list) and tid in [x for x in data if isinstance(x, str)]:
                return True
            nxt = resp.get("nextCursor")
            if isinstance(nxt, str) and nxt.strip():
                cursor = nxt.strip()
                continue
            break
        return False

    async def _ensure_thread_loaded_or_resumed(self, live: LiveDockerSession, thread_id: str) -> None:
        tid = str(thread_id or "").strip()
        if not tid:
            raise ValueError("thread_id must not be empty")
        if await self._is_thread_loaded(live, tid):
            return
        await self._rpc(live, "thread/resume", {"threadId": tid})

    def _cron_resolve_writeback(
        self,
        job: PersistedCronJob,
        *,
        requested: bool,
    ) -> tuple[str, int, bool]:
        raw = job.writeback if isinstance(job.writeback, dict) else {}

        when = str(raw.get("when") or "").strip().lower() if raw else ""
        if not when:
            when = CRON_DEFAULT_ISOLATED_WRITEBACK_WHEN
        if when not in CRON_WRITEBACK_WHENS:
            when = CRON_DEFAULT_ISOLATED_WRITEBACK_WHEN
        if when == "requested" and not requested:
            when = "never"

        max_chars = raw.get("maxChars") if raw else None
        try:
            max_chars_int = int(max_chars) if max_chars is not None else CRON_DEFAULT_WRITEBACK_MAX_CHARS
        except Exception:
            max_chars_int = CRON_DEFAULT_WRITEBACK_MAX_CHARS
        max_chars_int = max(200, min(20_000, max_chars_int))

        prompt_hint = raw.get("promptHint") if raw else None
        prompt_hint_bool = bool(CRON_DEFAULT_WRITEBACK_PROMPT_HINT if prompt_hint is None else prompt_hint)
        return when, max_chars_int, prompt_hint_bool

    def _cron_resolve_retention_max_unarchived_threads(self, job: PersistedCronJob) -> int:
        raw = job.retention if isinstance(job.retention, dict) else {}
        n = raw.get("maxUnarchivedThreads") if raw else None
        try:
            n_int = int(n) if n is not None else CRON_DEFAULT_RETENTION_MAX_UNARCHIVED_THREADS
        except Exception:
            n_int = CRON_DEFAULT_RETENTION_MAX_UNARCHIVED_THREADS
        return max(1, min(2000, n_int))

    async def _start_isolated_cron_run(
        self,
        *,
        session_id: str,
        job: PersistedCronJob,
        triggered_at_ms: int,
        manual: bool,
        writeback_requested: bool,
    ) -> tuple[str, Optional[str], str]:
        if _provision_mode() != "docker":
            raise RuntimeError("Cron automation is only supported in docker provision mode")

        sid = session_id.strip() if isinstance(session_id, str) else ""
        if not sid:
            raise ValueError("session_id must not be empty")

        run_id = uuid.uuid4().hex[:12]

        live, _ = await _ensure_live_docker_session(sid, allow_create=True)
        await self._ensure_initialized(live)

        # Ensure a main thread exists for writeback pointers.
        main_tid = None
        sess_state = self._store.state.sessions.get(sid)
        if sess_state is not None:
            main_tid = sess_state.main_thread_id
        if not isinstance(main_tid, str) or not main_tid.strip():
            main_tid = await self.ensure_main_thread(sid)

        writeback_when, writeback_max_chars, writeback_prompt_hint = self._cron_resolve_writeback(job, requested=writeback_requested)
        retention_max_unarchived = self._cron_resolve_retention_max_unarchived_threads(job)

        # Create a fresh thread for this run.
        result = await self._rpc(
            live,
            "thread/start",
            {"cwd": live.cfg.workspace_container_path, "approvalPolicy": "never", "sandbox": "danger-full-access"},
        )
        thread_id = None
        if isinstance(result, dict):
            thread = result.get("thread")
            if isinstance(thread, dict):
                tid_val = thread.get("id")
                if isinstance(tid_val, str) and tid_val.strip():
                    thread_id = tid_val.strip()
        if not thread_id:
            raise RuntimeError("Invalid thread/start response (missing thread.id)")

        try:
            ts = _ms_to_dt_utc(triggered_at_ms).strftime("%Y-%m-%d %H:%M:%SZ")
            name = f"Cron {job.job_id} {ts} run:{run_id}"
            if len(name) > 120:
                name = name[:120]
            await self._rpc(live, "thread/name/set", {"threadId": thread_id, "name": name})
        except Exception:
            pass

        # Record run start immediately (so /automation/state and cron.runs can locate the thread).
        started_at_ms = _now_ms()
        run_meta = PersistedCronRunMeta(
            run_id=run_id,
            thread_id=thread_id,
            turn_id=None,
            started_at_ms=started_at_ms,
            ended_at_ms=None,
            status="running",
            summary=None,
            error=None,
            archived=False,
        )

        def _record_start(st: PersistedGatewayAutomationState) -> None:
            sess = st.sessions.get(sid)
            if sess is None:
                sess = PersistedSessionAutomation()
                st.sessions[sid] = sess
            runs = sess.cron_runs_by_job.get(job.job_id)
            if runs is None:
                runs = []
                sess.cron_runs_by_job[job.job_id] = runs
            runs.append(run_meta)
            # Keep some bounded history; retention is enforced separately.
            max_keep = max(50, retention_max_unarchived * 2)
            if len(runs) > max_keep:
                del runs[: len(runs) - max_keep]

        await self._store.update(_record_start)

        # Start the turn in the new thread.
        lane = self.lane(sid, thread_id)
        async with lane.lock:
            if lane.busy:
                raise RuntimeError("isolated cron thread lane is unexpectedly busy")
            self._mark_lane_busy(lane)
            try:
                cron_block = "\n".join(
                    [
                        "[CRON]",
                        f"jobId: {job.job_id}",
                        f"expr: {job.expr}",
                        f"runId: {run_id}",
                        f"triggeredAt: {_ms_to_dt_utc(triggered_at_ms).isoformat()}",
                        f"manual: {bool(manual)}",
                        f"writebackWhen: {writeback_when}",
                        f"writebackMaxChars: {writeback_max_chars}",
                        "[/CRON]",
                    ]
                ).strip()
                user_text = cron_block + "\n\n" + (job.text or "").strip()
                assembled = await self._assemble_turn_input(sid, thread_id, user_text=user_text, heartbeat=False)
                if writeback_prompt_hint and writeback_when != "never":
                    assembled = (
                        assembled
                        + "\n\n"
                        + f"Writeback contract: Return your final summary as plain text (no markdown). Keep it under {writeback_max_chars} characters."
                    )

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
                self._note_lane_progress(lane)
            except Exception:
                self._mark_lane_idle(lane)
                raise

        # Patch run meta with the resolved turnId.
        if turn_id:
            def _patch_turn_id(st: PersistedGatewayAutomationState) -> None:
                sess = st.sessions.get(sid)
                if sess is None:
                    return
                runs = sess.cron_runs_by_job.get(job.job_id) or []
                for r in reversed(runs):
                    if isinstance(r, PersistedCronRunMeta) and r.run_id == run_id and r.thread_id == thread_id:
                        r.turn_id = turn_id
                        break

            await self._store.update(_patch_turn_id)

            key = (sid, thread_id, turn_id)
            self._isolated_cron_context_by_key[key] = IsolatedCronTurnContext(
                session_id=sid,
                job_id=job.job_id,
                run_id=run_id,
                main_thread_id=main_tid.strip(),
                writeback_when=writeback_when,
                writeback_max_chars=writeback_max_chars,
                writeback_prompt_hint=writeback_prompt_hint,
                retention_max_unarchived_threads=retention_max_unarchived,
                started_at_ms=started_at_ms,
            )

        return thread_id, turn_id, run_id

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
        if not st.sessions:
            return 5.0

        next_wakeup_ms: Optional[int] = None
        changed = False

        # Iterate all sessions: each agent has its own cron set.
        for session_id, sess in list(st.sessions.items()):
            if sess is None or not sess.cron_jobs:
                continue

            now = _now_ms()
            if self._is_session_backed_off(session_id, now):
                until = self._session_backoff_until_ms.get(session_id) or (now + 5000)
                next_wakeup_ms = until if next_wakeup_ms is None else min(next_wakeup_ms, until)
                continue

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
                        target = (job.session_target or CRON_DEFAULT_SESSION_TARGET).strip().lower()
                        if target not in CRON_SESSION_TARGETS:
                            target = CRON_DEFAULT_SESSION_TARGET

                        if target == "isolated":
                            await self._start_isolated_cron_run(
                                session_id=session_id,
                                job=job,
                                triggered_at_ms=now,
                                manual=False,
                                writeback_requested=False,
                            )
                        else:
                            main_tid = sess.main_thread_id
                            if not main_tid:
                                main_tid = await self.ensure_main_thread(session_id)
                                # Refresh session object after persistence updates.
                                sess = self._store.state.sessions.get(session_id) or sess
                            await self.enqueue_system_event(
                                session_id=session_id,
                                thread_id=main_tid,
                                kind=CRON_EVENT_KIND,
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
                    log.exception("Failed to run cron job %s for session %s", job.job_id, session_id)
                    self._note_session_failure(session_id, what=f"Cron job {job.job_id}", err=e)
                    job.next_run_at_ms = now + 60_000
                    changed = True

                if job.enabled and job.next_run_at_ms is not None:
                    next_wakeup_ms = job.next_run_at_ms if next_wakeup_ms is None else min(next_wakeup_ms, job.next_run_at_ms)

        if changed:
            await self._store.save()

        if next_wakeup_ms is None:
            return 5.0
        delay_ms = max(200, min(30_000, next_wakeup_ms - _now_ms()))
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

    async def _heartbeat_has_actionable_content(self, session_id: str) -> bool:
        root = self._workspace_root_for_session(session_id)
        if root is None:
            return False
        p = root / HEARTBEAT_FILENAME
        try:
            raw = await asyncio.to_thread(p.read_text, "utf-8")
        except FileNotFoundError:
            # Back-compat: older setups stored HEARTBEAT.md under `$ARGUS_HOME_HOST_PATH/HEARTBEAT.md`.
            if not self._home_host_path:
                return False
            home = Path(self._home_host_path)
            # Only apply legacy fallback for the main workspace to avoid accidentally
            # running the same heartbeat tasks across all agents.
            if root != (home / "workspace"):
                return False
            legacy = home / HEARTBEAT_FILENAME
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
        st = self._store.state
        session_ids = [sid for sid in st.sessions.keys() if isinstance(sid, str) and sid.strip()]
        if not session_ids:
            return

        # Start a small number of heartbeat turns per tick to avoid overload when many
        # agents are active.
        started = 0
        for session_id in sorted(session_ids):
            if started >= 3:
                return

            now_ms = _now_ms()
            if self._is_session_backed_off(session_id, now_ms):
                continue

            sess = self._store.state.sessions.get(session_id)
            if sess is None:
                continue
            main_tid = sess.main_thread_id
            q = (sess.system_event_queues.get(main_tid) or []) if main_tid else []
            has_actionable_system_events = self._queue_has_actionable_system_events(q)
            has_heartbeat_tasks = await self._heartbeat_has_actionable_content(session_id)

            if not has_actionable_system_events:
                if not has_heartbeat_tasks:
                    continue
                if main_tid and (not forced and not self._heartbeat_tasks_due(session_id, main_tid, now_ms)):
                    continue

            try:
                main_tid = await self.ensure_main_thread(session_id)
                self._clear_session_backoff(session_id)
            except Exception as e:
                self._note_session_failure(session_id, what="Heartbeat bootstrap", err=e)
                continue

            lane = self.lane(session_id, main_tid)
            if lane.busy:
                continue

            async with lane.lock:
                if lane.busy:
                    continue

                # Re-check work under latest state.
                st2 = self._store.state
                sess2 = st2.sessions.get(session_id)
                if sess2 is None:
                    continue
                q2 = sess2.system_event_queues.get(main_tid) or []
                has_actionable_system_events2 = self._queue_has_actionable_system_events(q2)
                has_heartbeat_tasks2 = await self._heartbeat_has_actionable_content(session_id)
                if not has_actionable_system_events2:
                    if not has_heartbeat_tasks2:
                        continue
                    now2 = _now_ms()
                    if not forced and not self._heartbeat_tasks_due(session_id, main_tid, now2):
                        continue

                try:
                    live, _ = await _ensure_live_docker_session(session_id, allow_create=True)
                    await self._ensure_initialized(live)
                except Exception as e:
                    self._note_session_failure(session_id, what="Heartbeat prepare", err=e)
                    continue

                self._mark_lane_busy(lane)
                try:
                    await self._ensure_thread_loaded_or_resumed(live, main_tid)
                    assembled = await self._assemble_turn_input(session_id, main_tid, user_text="", heartbeat=True)
                    resp = await self._rpc(
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
                    turn_id = None
                    if isinstance(resp, dict):
                        turn = resp.get("turn")
                        if isinstance(turn, dict):
                            tid_val = turn.get("id")
                            if isinstance(tid_val, str) and tid_val.strip():
                                turn_id = tid_val.strip()
                    if turn_id:
                        lane.active_turn_id = turn_id
                        self._note_lane_progress(lane)
                    self._last_heartbeat_run_at_ms[(session_id, main_tid)] = _now_ms()
                    self._clear_session_backoff(session_id)
                    started += 1
                except Exception as e:
                    self._mark_lane_idle(lane)
                    self._note_session_failure(session_id, what="Heartbeat turn/start", err=e)
                    continue


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
    scoped_session_id: Optional[str] = None

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
        scoped_sid = getattr(request.state, "mcp_scoped_session_id", None)
        scoped_sid = scoped_sid.strip() if isinstance(scoped_sid, str) and scoped_sid.strip() else None
        sess = McpSessionState(
            session_id=session_id,
            protocol_version=negotiated,
            # We don't emit server-side notifications, so we can treat initialize as sufficient.
            initialized=True,
            client_info=client_info,
            scoped_session_id=scoped_sid,
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
            "instructions": "This MCP server exposes tools for managing gateway cron jobs, sending messages, and invoking connected nodes. Cron/heartbeat operations are scoped to the calling runtime session by default.",
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
                "name": "message_send",
                "title": "Message Send",
                "description": "Send an outbound message. Currently supports Telegram only. Target uses chatKey format: <chat_id>[:<message_thread_id>].",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "description": "Delivery channel (default: telegram)."},
                        "target": {"type": "string", "description": "Destination chatKey, e.g. '123456789' or '-1001:42'."},
                        "text": {"type": "string", "description": "Message text to send."},
                        "format": {"type": "string", "enum": ["plain", "markdown", "html"], "description": "Text format hint (default: markdown)."},
                        "silent": {"type": "boolean", "description": "Telegram: disable_notification."},
                    },
                    "required": ["target", "text"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "cron",
                "title": "Cron",
                "description": "Manage gateway cron jobs for the calling runtime session only. Jobs can run in the session main thread (systemEvents processed by heartbeat) or as isolated runs (each trigger starts a fresh thread; main thread receives a short writeback pointer).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["status", "list", "add", "update", "remove", "run", "runs"],
                            "description": "Action to perform.",
                        },
                        "includeDisabled": {"type": "boolean", "description": "For list: include disabled jobs."},
                        "jobId": {"type": "string", "description": "For update/remove/run: cron job id."},
                        "expr": {"type": "string", "description": "For add/update: cron expression (UTC-based)."},
                        "text": {"type": "string", "description": "For add/update: payload text to enqueue as a systemEvent when due."},
                        "enabled": {"type": "boolean", "description": "For add/update: enable/disable the job."},
                        "sessionTarget": {"type": "string", "enum": ["main", "isolated"], "description": "For add/update/run: where the job runs. 'main' enqueues to the main thread; 'isolated' runs in a fresh thread per trigger."},
                        "writeback": {
                            "type": "object",
                            "properties": {
                                "when": {"type": "string", "enum": ["always", "on-error", "never", "requested"]},
                                "maxChars": {"type": "number", "description": "Max chars for main-thread writeback summary (default 2000)."},
                                "promptHint": {"type": "boolean", "description": "If true, append a hint to make the final summary short/plain-text."},
                            },
                            "additionalProperties": False,
                        },
                        "retention": {
                            "type": "object",
                            "properties": {
                                "maxUnarchivedThreads": {"type": "number", "description": "Max unarchived run threads to keep per job (default 50)."},
                            },
                            "additionalProperties": False,
                        },
                        "writebackRequested": {"type": "boolean", "description": "For run: if writeback.when=requested, force a writeback for this run."},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
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

        if tool_name == "message_send":
            raw_channel = args.get("channel")
            channel = str(raw_channel or "telegram").strip().lower()
            if channel in ("tg", "telegram"):
                channel = "telegram"

            if channel != "telegram":
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(
                            content=[{"type": "text", "text": f"Unsupported channel: {channel}"}],
                            structured={"ok": False, "error": {"code": "UNSUPPORTED", "field": "channel", "supported": ["telegram"]}},
                            is_error=True,
                        ),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            chat_key = str(args.get("target") or args.get("chatKey") or "").strip()
            if not chat_key:
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(
                            content=[{"type": "text", "text": "Missing required field: target"}],
                            structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "target"}},
                            is_error=True,
                        ),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            text_param = args.get("text")
            if not isinstance(text_param, str) or not text_param.strip():
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(
                            content=[{"type": "text", "text": "Missing required field: text"}],
                            structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "text"}},
                            is_error=True,
                        ),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            token = os.getenv("TELEGRAM_BOT_TOKEN") or ""
            if not token.strip():
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(
                            content=[{"type": "text", "text": "Missing TELEGRAM_BOT_TOKEN"}],
                            structured={"ok": False, "error": {"code": "NOT_CONFIGURED", "message": "Missing TELEGRAM_BOT_TOKEN"}},
                            is_error=True,
                        ),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            target = _telegram_target_from_chat_key(chat_key)
            if target is None:
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(
                            content=[{"type": "text", "text": "Invalid target chatKey"}],
                            structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "target"}},
                            is_error=True,
                        ),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            allow_unknown = str(os.getenv("ARGUS_MCP_MESSAGE_ALLOW_UNKNOWN_TARGETS") or "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
            if not allow_unknown:
                automation: Optional[AutomationManager] = getattr(app.state, "automation", None)
                if automation is None:
                    return (
                        _jsonrpc_result(
                            request_id=request_id,
                            result=_mcp_call_tool_result(
                                content=[{"type": "text", "text": "Automation is not initialized; cannot validate target"}],
                                structured={"ok": False, "error": {"code": "NOT_READY", "message": "automation is not initialized"}},
                                is_error=True,
                            ),
                        ),
                        {"MCP-Protocol-Version": sess.protocol_version},
                    )

                chat_id = target.get("chat_id")
                known = False
                st = automation._store.state
                for sess_state in (st.sessions or {}).values():
                    last_active = getattr(sess_state, "last_active_by_thread", None)
                    if not isinstance(last_active, dict):
                        continue
                    for tgt in last_active.values():
                        ck = getattr(tgt, "chat_key", None)
                        if not isinstance(ck, str) or not ck.strip():
                            continue
                        cand = _telegram_target_from_chat_key(ck)
                        if cand is None:
                            continue
                        if cand.get("chat_id") == chat_id:
                            known = True
                            break
                    if known:
                        break

                if not known:
                    return (
                        _jsonrpc_result(
                            request_id=request_id,
                            result=_mcp_call_tool_result(
                                content=[{"type": "text", "text": "Target not allowed (unknown chatKey). Set ARGUS_MCP_MESSAGE_ALLOW_UNKNOWN_TARGETS=1 to override."}],
                                structured={"ok": False, "error": {"code": "FORBIDDEN", "field": "target"}},
                                is_error=True,
                            ),
                        ),
                        {"MCP-Protocol-Version": sess.protocol_version},
                    )

            raw_format = args.get("format")
            fmt = str(raw_format or "markdown").strip().lower()
            silent = bool(args.get("silent")) if args.get("silent") is not None else None

            truncated = _telegram_truncate(text_param.strip())
            try:
                if fmt == "html":
                    try:
                        res = await _telegram_send_message(
                            token=token,
                            target=target,
                            text=truncated,
                            parse_mode="HTML",
                            disable_notification=silent,
                        )
                    except Exception as e:  # noqa: BLE001
                        msg = str(e)
                        if TELEGRAM_HTML_PARSE_ERR_RE.search(msg) or TELEGRAM_MESSAGE_TOO_LONG_RE.search(msg):
                            res = await _telegram_send_message(
                                token=token,
                                target=target,
                                text=truncated,
                                parse_mode=None,
                                disable_notification=silent,
                            )
                        else:
                            raise
                elif fmt == "plain":
                    res = await _telegram_send_message(
                        token=token,
                        target=target,
                        text=truncated,
                        parse_mode=None,
                        disable_notification=silent,
                    )
                else:
                    html = _markdown_to_telegram_html(truncated)
                    if html:
                        try:
                            res = await _telegram_send_message(
                                token=token,
                                target=target,
                                text=html,
                                parse_mode="HTML",
                                disable_notification=silent,
                            )
                        except Exception as e:  # noqa: BLE001
                            msg = str(e)
                            if TELEGRAM_HTML_PARSE_ERR_RE.search(msg) or TELEGRAM_MESSAGE_TOO_LONG_RE.search(msg):
                                res = await _telegram_send_message(
                                    token=token,
                                    target=target,
                                    text=truncated,
                                    parse_mode=None,
                                    disable_notification=silent,
                                )
                            else:
                                raise
                    else:
                        res = await _telegram_send_message(
                            token=token,
                            target=target,
                            text=truncated,
                            parse_mode=None,
                            disable_notification=silent,
                        )
            except Exception as e:  # noqa: BLE001
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(
                            content=[{"type": "text", "text": f"message_send failed: {e}"}],
                            structured={"ok": False, "error": {"code": "DELIVERY_FAILED", "message": str(e)}},
                            is_error=True,
                        ),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            return (
                _jsonrpc_result(
                    request_id=request_id,
                    result=_mcp_call_tool_result(
                        content=[{"type": "text", "text": "Sent"}],
                        structured={"ok": True, "channel": channel, "target": chat_key, "result": res},
                    ),
                ),
                {"MCP-Protocol-Version": sess.protocol_version},
            )

        if tool_name == "cron":
            action = str(args.get("action") or "").strip().lower()
            allowed_actions = {"status", "list", "runs", "add", "update", "remove", "run"}
            if action not in allowed_actions:
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(
                            content=[{"type": "text", "text": "Invalid cron action"}],
                            structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "action", "allowed": sorted(list(allowed_actions))}},
                            is_error=True,
                        ),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            automation: Optional[AutomationManager] = getattr(app.state, "automation", None)
            if automation is None:
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(
                            content=[{"type": "text", "text": "Automation is not initialized"}],
                            structured={"ok": False, "error": {"code": "NOT_READY", "message": "automation is not initialized"}},
                            is_error=True,
                        ),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            scoped_sid = getattr(request.state, "mcp_scoped_session_id", None) or sess.scoped_session_id
            scoped_sid = scoped_sid.strip() if isinstance(scoped_sid, str) and scoped_sid.strip() else None
            if not scoped_sid:
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(
                            content=[{"type": "text", "text": "Cron tool requires a runtime session scope"}],
                            structured={"ok": False, "error": {"code": "FORBIDDEN", "message": "missing runtime session scope"}},
                            is_error=True,
                        ),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            if "sessionId" in args or "agentId" in args:
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(
                            content=[{"type": "text", "text": "Cron tool is session-scoped; do not pass sessionId/agentId"}],
                            structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "message": "cron tool is session-scoped; remove sessionId/agentId"}},
                            is_error=True,
                        ),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            session_id = scoped_sid

            include_disabled = bool(args.get("includeDisabled"))
            job_id = str(args.get("jobId") or "").strip()
            now_ms = _now_ms()

            def _cron_validate_expr(expr: str) -> Optional[str]:
                try:
                    croniter(expr, _ms_to_dt_utc(now_ms)).get_next(datetime)
                    return None
                except Exception as e:  # noqa: BLE001
                    return str(e)

            def _cron_compute_next_ms(expr: str, *, base_ms: int) -> int:
                base_dt = _ms_to_dt_utc(base_ms)
                return _dt_to_ms(croniter(expr, base_dt).get_next(datetime))

            def _cron_validate_session_target(raw: Any) -> Optional[str]:
                if raw is None:
                    return None
                s = str(raw or "").strip().lower()
                if not s:
                    return "sessionTarget must not be empty"
                if s not in CRON_SESSION_TARGETS:
                    return f"Invalid sessionTarget: {s}"
                return None

            def _cron_normalize_session_target(raw: Any, *, fallback: str) -> str:
                err = _cron_validate_session_target(raw)
                if err is not None:
                    return fallback
                s = str(raw or "").strip().lower()
                return s if s in CRON_SESSION_TARGETS else fallback

            def _cron_normalize_writeback(raw: Any) -> Optional[dict[str, Any]]:
                if raw is None:
                    return None
                if not isinstance(raw, dict):
                    return None
                out: dict[str, Any] = {}
                when = raw.get("when")
                if when is not None:
                    w = str(when or "").strip().lower()
                    if w:
                        if w not in CRON_WRITEBACK_WHENS:
                            raise ValueError(f"Invalid writeback.when: {w}")
                        out["when"] = w
                max_chars = raw.get("maxChars")
                if max_chars is not None:
                    try:
                        mc = int(max_chars)
                    except Exception:
                        raise ValueError("writeback.maxChars must be an integer") from None
                    mc = max(200, min(20_000, mc))
                    out["maxChars"] = mc
                prompt_hint = raw.get("promptHint")
                if prompt_hint is not None:
                    out["promptHint"] = bool(prompt_hint)
                return out or None

            def _cron_normalize_retention(raw: Any) -> Optional[dict[str, Any]]:
                if raw is None:
                    return None
                if not isinstance(raw, dict):
                    return None
                out: dict[str, Any] = {}
                max_unarchived = raw.get("maxUnarchivedThreads")
                if max_unarchived is not None:
                    try:
                        n = int(max_unarchived)
                    except Exception:
                        raise ValueError("retention.maxUnarchivedThreads must be an integer") from None
                    n = max(1, min(2000, n))
                    out["maxUnarchivedThreads"] = n
                return out or None

            if action == "status":
                st = automation._store.state
                sess_state = st.sessions.get(session_id)
                jobs = sess_state.cron_jobs if sess_state is not None else []
                enabled_jobs = [j for j in jobs if j.enabled]
                next_due_ms: Optional[int] = None
                for j in enabled_jobs:
                    cand = j.next_run_at_ms
                    if cand is None:
                        try:
                            cand = _cron_compute_next_ms(j.expr, base_ms=j.last_run_at_ms or now_ms)
                        except Exception:
                            cand = None
                    if cand is None:
                        continue
                    next_due_ms = cand if next_due_ms is None else min(next_due_ms, cand)
                payload = {
                    "ok": True,
                    "sessionId": session_id,
                    "defaultSessionId": st.default_session_id,
                    "jobs": len(jobs),
                    "enabledJobs": len(enabled_jobs),
                    "nextDueAtMs": next_due_ms,
                }
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], structured=payload),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            if action == "list":
                st = automation._store.state
                sess_state = st.sessions.get(session_id)
                jobs = sess_state.cron_jobs if sess_state is not None else []
                out_jobs: list[dict[str, Any]] = []
                for j in jobs:
                    if not include_disabled and not j.enabled:
                        continue
                    d = j.to_json()
                    # Best-effort: compute nextRunAtMs for display if missing.
                    if j.enabled and "nextRunAtMs" not in d:
                        try:
                            d["nextRunAtMs"] = _cron_compute_next_ms(j.expr, base_ms=j.last_run_at_ms or now_ms)
                        except Exception:
                            pass
                    out_jobs.append(d)
                payload = {"ok": True, "sessionId": session_id, "jobs": out_jobs}
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], structured=payload),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            if action == "runs":
                st = automation._store.state
                sess_state = st.sessions.get(session_id)
                runs_by_job = sess_state.cron_runs_by_job if sess_state is not None else {}
                if job_id:
                    runs = runs_by_job.get(job_id) or []
                    out_runs = [r.to_json() for r in runs if isinstance(r, PersistedCronRunMeta)]
                    payload = {"ok": True, "sessionId": session_id, "jobId": job_id, "runs": out_runs}
                else:
                    out: dict[str, Any] = {}
                    if isinstance(runs_by_job, dict):
                        for jid, runs in runs_by_job.items():
                            if not isinstance(jid, str) or not jid.strip():
                                continue
                            if not isinstance(runs, list) or not runs:
                                continue
                            out[jid.strip()] = [r.to_json() for r in runs if isinstance(r, PersistedCronRunMeta)]
                    payload = {"ok": True, "sessionId": session_id, "runsByJob": out}
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], structured=payload),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            if action == "add":
                expr = str(args.get("expr") or "").strip()
                text = str(args.get("text") or "")
                enabled_val = args.get("enabled", True)
                if enabled_val is not None and not isinstance(enabled_val, bool):
                    return (
                        _jsonrpc_result(
                            request_id=request_id,
                            result=_mcp_call_tool_result(
                                content=[{"type": "text", "text": "enabled must be a boolean"}],
                                structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "enabled"}},
                                is_error=True,
                            ),
                        ),
                        {"MCP-Protocol-Version": sess.protocol_version},
                    )
                enabled = bool(enabled_val) if isinstance(enabled_val, bool) else True

                has_session_target = "sessionTarget" in args
                session_target_patch = None
                if has_session_target:
                    err = _cron_validate_session_target(args.get("sessionTarget"))
                    if err is not None:
                        return (
                            _jsonrpc_result(
                                request_id=request_id,
                                result=_mcp_call_tool_result(
                                    content=[{"type": "text", "text": err}],
                                    structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "sessionTarget", "message": err}},
                                    is_error=True,
                                ),
                            ),
                            {"MCP-Protocol-Version": sess.protocol_version},
                        )
                    session_target_patch = _cron_normalize_session_target(args.get("sessionTarget"), fallback=CRON_DEFAULT_SESSION_TARGET)

                has_writeback = "writeback" in args
                writeback_patch: Optional[dict[str, Any]] = None
                if has_writeback:
                    wb_raw = args.get("writeback")
                    if wb_raw is not None and not isinstance(wb_raw, dict):
                        return (
                            _jsonrpc_result(
                                request_id=request_id,
                                result=_mcp_call_tool_result(
                                    content=[{"type": "text", "text": "writeback must be an object"}],
                                    structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "writeback"}},
                                    is_error=True,
                                ),
                            ),
                            {"MCP-Protocol-Version": sess.protocol_version},
                        )
                    if isinstance(wb_raw, dict):
                        try:
                            writeback_patch = _cron_normalize_writeback(wb_raw)
                        except ValueError as e:
                            return (
                                _jsonrpc_result(
                                    request_id=request_id,
                                    result=_mcp_call_tool_result(
                                        content=[{"type": "text", "text": str(e)}],
                                        structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "writeback", "message": str(e)}},
                                        is_error=True,
                                    ),
                                ),
                                {"MCP-Protocol-Version": sess.protocol_version},
                            )
                    else:
                        writeback_patch = None

                has_retention = "retention" in args
                retention_patch: Optional[dict[str, Any]] = None
                if has_retention:
                    ret_raw = args.get("retention")
                    if ret_raw is not None and not isinstance(ret_raw, dict):
                        return (
                            _jsonrpc_result(
                                request_id=request_id,
                                result=_mcp_call_tool_result(
                                    content=[{"type": "text", "text": "retention must be an object"}],
                                    structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "retention"}},
                                    is_error=True,
                                ),
                            ),
                            {"MCP-Protocol-Version": sess.protocol_version},
                        )
                    if isinstance(ret_raw, dict):
                        try:
                            retention_patch = _cron_normalize_retention(ret_raw)
                        except ValueError as e:
                            return (
                                _jsonrpc_result(
                                    request_id=request_id,
                                    result=_mcp_call_tool_result(
                                        content=[{"type": "text", "text": str(e)}],
                                        structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "retention", "message": str(e)}},
                                        is_error=True,
                                    ),
                                ),
                                {"MCP-Protocol-Version": sess.protocol_version},
                            )
                    else:
                        retention_patch = None

                if not expr:
                    return (
                        _jsonrpc_result(
                            request_id=request_id,
                            result=_mcp_call_tool_result(
                                content=[{"type": "text", "text": "Missing required field: expr"}],
                                structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "expr"}},
                                is_error=True,
                            ),
                        ),
                        {"MCP-Protocol-Version": sess.protocol_version},
                    )
                if not text.strip():
                    return (
                        _jsonrpc_result(
                            request_id=request_id,
                            result=_mcp_call_tool_result(
                                content=[{"type": "text", "text": "Missing required field: text"}],
                                structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "text"}},
                                is_error=True,
                            ),
                        ),
                        {"MCP-Protocol-Version": sess.protocol_version},
                    )
                err = _cron_validate_expr(expr)
                if err is not None:
                    return (
                        _jsonrpc_result(
                            request_id=request_id,
                            result=_mcp_call_tool_result(
                                content=[{"type": "text", "text": f"Invalid cron expr: {err}"}],
                                structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "expr", "message": err}},
                                is_error=True,
                            ),
                        ),
                        {"MCP-Protocol-Version": sess.protocol_version},
                    )

                if not job_id:
                    job_id = uuid.uuid4().hex[:10]

                saved: Optional[PersistedCronJob] = None

                def _upsert(st: PersistedGatewayAutomationState) -> None:
                    nonlocal saved
                    sess_state = st.sessions.get(session_id)
                    if sess_state is None:
                        sess_state = PersistedSessionAutomation()
                        st.sessions[session_id] = sess_state
                    existing = next((j for j in sess_state.cron_jobs if j.job_id == job_id), None)
                    prev_expr = existing.expr if existing is not None else None
                    prev_enabled = existing.enabled if existing is not None else None
                    last_run_at_ms = existing.last_run_at_ms if existing is not None else None
                    prev_next = existing.next_run_at_ms if existing is not None else None
                    existing_target = existing.session_target if existing is not None else CRON_DEFAULT_SESSION_TARGET
                    existing_writeback = existing.writeback if existing is not None else None
                    existing_retention = existing.retention if existing is not None else None

                    # Compute next run time:
                    # - if schedule changed or job was re-enabled: base from now
                    # - otherwise: follow the scheduler behavior (base from lastRunAtMs if present)
                    base_ms = last_run_at_ms or now_ms
                    if prev_expr is not None and prev_expr != expr:
                        base_ms = now_ms
                    if prev_enabled is False and enabled is True:
                        base_ms = now_ms

                    next_run_at_ms: Optional[int] = None
                    if enabled:
                        if (
                            prev_expr == expr
                            and prev_enabled == enabled
                            and last_run_at_ms is None
                            and isinstance(prev_next, int)
                            and prev_next > now_ms
                        ):
                            next_run_at_ms = prev_next
                        else:
                            next_run_at_ms = _cron_compute_next_ms(expr, base_ms=base_ms)

                    sess_state.cron_jobs = [j for j in sess_state.cron_jobs if j.job_id != job_id]
                    saved = PersistedCronJob(
                        job_id=job_id,
                        expr=expr,
                        text=text,
                        enabled=enabled,
                        last_run_at_ms=last_run_at_ms,
                        next_run_at_ms=next_run_at_ms,
                        session_target=session_target_patch if has_session_target else existing_target,
                        writeback=writeback_patch if has_writeback else existing_writeback,
                        retention=retention_patch if has_retention else existing_retention,
                    )
                    sess_state.cron_jobs.append(saved)

                await automation._store.update(_upsert)
                automation._cron_wakeup.set()
                payload = {"ok": True, "sessionId": session_id, "job": saved.to_json() if saved is not None else None}
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], structured=payload),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            if action == "update":
                if not job_id:
                    return (
                        _jsonrpc_result(
                            request_id=request_id,
                            result=_mcp_call_tool_result(
                                content=[{"type": "text", "text": "Missing required field: jobId"}],
                                structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "jobId"}},
                                is_error=True,
                            ),
                        ),
                        {"MCP-Protocol-Version": sess.protocol_version},
                    )

                has_expr = "expr" in args
                has_text = "text" in args
                has_enabled = "enabled" in args
                has_session_target = "sessionTarget" in args
                has_writeback = "writeback" in args
                has_retention = "retention" in args
                if not (has_expr or has_text or has_enabled or has_session_target or has_writeback or has_retention):
                    return (
                        _jsonrpc_result(
                            request_id=request_id,
                            result=_mcp_call_tool_result(
                                content=[{"type": "text", "text": "Nothing to update (provide expr/text/enabled/sessionTarget/writeback/retention)"}],
                                structured={"ok": False, "error": {"code": "INVALID_ARGUMENT"}},
                                is_error=True,
                            ),
                        ),
                        {"MCP-Protocol-Version": sess.protocol_version},
                    )

                expr_patch = str(args.get("expr") or "").strip() if has_expr else None
                text_patch = str(args.get("text") or "") if has_text else None
                enabled_patch_raw = args.get("enabled") if has_enabled else None
                session_target_patch = None
                if has_session_target:
                    err = _cron_validate_session_target(args.get("sessionTarget"))
                    if err is not None:
                        return (
                            _jsonrpc_result(
                                request_id=request_id,
                                result=_mcp_call_tool_result(
                                    content=[{"type": "text", "text": err}],
                                    structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "sessionTarget", "message": err}},
                                    is_error=True,
                                ),
                            ),
                            {"MCP-Protocol-Version": sess.protocol_version},
                        )
                    session_target_patch = _cron_normalize_session_target(args.get("sessionTarget"), fallback=CRON_DEFAULT_SESSION_TARGET)

                writeback_patch: Optional[dict[str, Any]] = None
                if has_writeback:
                    wb_raw = args.get("writeback")
                    if wb_raw is not None and not isinstance(wb_raw, dict):
                        return (
                            _jsonrpc_result(
                                request_id=request_id,
                                result=_mcp_call_tool_result(
                                    content=[{"type": "text", "text": "writeback must be an object"}],
                                    structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "writeback"}},
                                    is_error=True,
                                ),
                            ),
                            {"MCP-Protocol-Version": sess.protocol_version},
                        )
                    if isinstance(wb_raw, dict):
                        try:
                            writeback_patch = _cron_normalize_writeback(wb_raw)
                        except ValueError as e:
                            return (
                                _jsonrpc_result(
                                    request_id=request_id,
                                    result=_mcp_call_tool_result(
                                        content=[{"type": "text", "text": str(e)}],
                                        structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "writeback", "message": str(e)}},
                                        is_error=True,
                                    ),
                                ),
                                {"MCP-Protocol-Version": sess.protocol_version},
                            )
                    else:
                        writeback_patch = None

                retention_patch: Optional[dict[str, Any]] = None
                if has_retention:
                    ret_raw = args.get("retention")
                    if ret_raw is not None and not isinstance(ret_raw, dict):
                        return (
                            _jsonrpc_result(
                                request_id=request_id,
                                result=_mcp_call_tool_result(
                                    content=[{"type": "text", "text": "retention must be an object"}],
                                    structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "retention"}},
                                    is_error=True,
                                ),
                            ),
                            {"MCP-Protocol-Version": sess.protocol_version},
                        )
                    if isinstance(ret_raw, dict):
                        try:
                            retention_patch = _cron_normalize_retention(ret_raw)
                        except ValueError as e:
                            return (
                                _jsonrpc_result(
                                    request_id=request_id,
                                    result=_mcp_call_tool_result(
                                        content=[{"type": "text", "text": str(e)}],
                                        structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "retention", "message": str(e)}},
                                        is_error=True,
                                    ),
                                ),
                                {"MCP-Protocol-Version": sess.protocol_version},
                            )
                    else:
                        retention_patch = None

                if has_enabled and enabled_patch_raw is not None and not isinstance(enabled_patch_raw, bool):
                    return (
                        _jsonrpc_result(
                            request_id=request_id,
                            result=_mcp_call_tool_result(
                                content=[{"type": "text", "text": "enabled must be a boolean"}],
                                structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "enabled"}},
                                is_error=True,
                            ),
                        ),
                        {"MCP-Protocol-Version": sess.protocol_version},
                    )
                enabled_patch = bool(enabled_patch_raw) if isinstance(enabled_patch_raw, bool) else None

                if has_expr:
                    if not expr_patch:
                        return (
                            _jsonrpc_result(
                                request_id=request_id,
                                result=_mcp_call_tool_result(
                                    content=[{"type": "text", "text": "expr must not be empty"}],
                                    structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "expr"}},
                                    is_error=True,
                                ),
                            ),
                            {"MCP-Protocol-Version": sess.protocol_version},
                        )
                    err = _cron_validate_expr(expr_patch)
                    if err is not None:
                        return (
                            _jsonrpc_result(
                                request_id=request_id,
                                result=_mcp_call_tool_result(
                                    content=[{"type": "text", "text": f"Invalid cron expr: {err}"}],
                                    structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "expr", "message": err}},
                                    is_error=True,
                                ),
                            ),
                            {"MCP-Protocol-Version": sess.protocol_version},
                        )

                if has_text and text_patch is not None and not text_patch.strip():
                    return (
                        _jsonrpc_result(
                            request_id=request_id,
                            result=_mcp_call_tool_result(
                                content=[{"type": "text", "text": "text must not be empty"}],
                                structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "text"}},
                                is_error=True,
                            ),
                        ),
                        {"MCP-Protocol-Version": sess.protocol_version},
                    )

                updated: Optional[PersistedCronJob] = None
                not_found = False

                def _patch(st: PersistedGatewayAutomationState) -> None:
                    nonlocal updated, not_found
                    sess_state = st.sessions.get(session_id)
                    if sess_state is None:
                        not_found = True
                        return
                    existing = next((j for j in sess_state.cron_jobs if j.job_id == job_id), None)
                    if existing is None:
                        not_found = True
                        return
                    new_expr = expr_patch if expr_patch is not None else existing.expr
                    new_text = text_patch if text_patch is not None else existing.text
                    new_enabled = enabled_patch if enabled_patch is not None else existing.enabled
                    new_session_target = session_target_patch if has_session_target else existing.session_target
                    new_writeback = writeback_patch if has_writeback else existing.writeback
                    new_retention = retention_patch if has_retention else existing.retention

                    base_ms = existing.last_run_at_ms or now_ms
                    if new_expr != existing.expr:
                        base_ms = now_ms
                    if existing.enabled is False and new_enabled is True:
                        base_ms = now_ms

                    next_run_at_ms: Optional[int] = existing.next_run_at_ms
                    if not new_enabled:
                        next_run_at_ms = None
                    else:
                        if (
                            new_expr == existing.expr
                            and new_enabled == existing.enabled
                            and existing.last_run_at_ms is None
                            and isinstance(existing.next_run_at_ms, int)
                            and existing.next_run_at_ms > now_ms
                        ):
                            next_run_at_ms = existing.next_run_at_ms
                        else:
                            next_run_at_ms = _cron_compute_next_ms(new_expr, base_ms=base_ms)

                    updated = PersistedCronJob(
                        job_id=existing.job_id,
                        expr=new_expr,
                        text=new_text,
                        enabled=new_enabled,
                        last_run_at_ms=existing.last_run_at_ms,
                        next_run_at_ms=next_run_at_ms,
                        session_target=new_session_target,
                        writeback=new_writeback,
                        retention=new_retention,
                    )
                    sess_state.cron_jobs = [j for j in sess_state.cron_jobs if j.job_id != job_id]
                    sess_state.cron_jobs.append(updated)

                await automation._store.update(_patch)
                if not_found:
                    return (
                        _jsonrpc_result(
                            request_id=request_id,
                            result=_mcp_call_tool_result(
                                content=[{"type": "text", "text": "Job not found"}],
                                structured={"ok": False, "error": {"code": "NOT_FOUND", "jobId": job_id}},
                                is_error=True,
                            ),
                        ),
                        {"MCP-Protocol-Version": sess.protocol_version},
                    )
                automation._cron_wakeup.set()
                payload = {"ok": True, "sessionId": session_id, "job": updated.to_json() if updated is not None else None}
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], structured=payload),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            if action == "remove":
                if not job_id:
                    return (
                        _jsonrpc_result(
                            request_id=request_id,
                            result=_mcp_call_tool_result(
                                content=[{"type": "text", "text": "Missing required field: jobId"}],
                                structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "jobId"}},
                                is_error=True,
                            ),
                        ),
                        {"MCP-Protocol-Version": sess.protocol_version},
                    )
                removed = False

                def _rm(st: PersistedGatewayAutomationState) -> None:
                    nonlocal removed
                    sess_state = st.sessions.get(session_id)
                    if sess_state is None:
                        return
                    before = len(sess_state.cron_jobs)
                    sess_state.cron_jobs = [j for j in sess_state.cron_jobs if j.job_id != job_id]
                    removed = len(sess_state.cron_jobs) != before
                    if removed:
                        try:
                            sess_state.cron_runs_by_job.pop(job_id, None)
                        except Exception:
                            pass

                await automation._store.update(_rm)
                automation._cron_wakeup.set()
                if not removed:
                    return (
                        _jsonrpc_result(
                            request_id=request_id,
                            result=_mcp_call_tool_result(
                                content=[{"type": "text", "text": "Job not found"}],
                                structured={"ok": False, "error": {"code": "NOT_FOUND", "jobId": job_id}},
                                is_error=True,
                            ),
                        ),
                        {"MCP-Protocol-Version": sess.protocol_version},
                    )
                payload = {"ok": True, "sessionId": session_id, "jobId": job_id}
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], structured=payload),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            if action == "run":
                if not job_id:
                    return (
                        _jsonrpc_result(
                            request_id=request_id,
                            result=_mcp_call_tool_result(
                                content=[{"type": "text", "text": "Missing required field: jobId"}],
                                structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "jobId"}},
                                is_error=True,
                            ),
                        ),
                        {"MCP-Protocol-Version": sess.protocol_version},
                    )

                st = automation._store.state
                sess_state = st.sessions.get(session_id)
                job = None
                if sess_state is not None:
                    job = next((j for j in sess_state.cron_jobs if j.job_id == job_id), None)
                if job is None:
                    return (
                        _jsonrpc_result(
                            request_id=request_id,
                            result=_mcp_call_tool_result(
                                content=[{"type": "text", "text": "Job not found"}],
                                structured={"ok": False, "error": {"code": "NOT_FOUND", "jobId": job_id}},
                                is_error=True,
                            ),
                        ),
                        {"MCP-Protocol-Version": sess.protocol_version},
                    )

                target = (job.session_target or CRON_DEFAULT_SESSION_TARGET).strip().lower()
                if "sessionTarget" in args:
                    err = _cron_validate_session_target(args.get("sessionTarget"))
                    if err is not None:
                        return (
                            _jsonrpc_result(
                                request_id=request_id,
                                result=_mcp_call_tool_result(
                                    content=[{"type": "text", "text": err}],
                                    structured={"ok": False, "error": {"code": "INVALID_ARGUMENT", "field": "sessionTarget", "message": err}},
                                    is_error=True,
                                ),
                            ),
                            {"MCP-Protocol-Version": sess.protocol_version},
                        )
                    target = _cron_normalize_session_target(args.get("sessionTarget"), fallback=target)
                if target not in CRON_SESSION_TARGETS:
                    target = CRON_DEFAULT_SESSION_TARGET

                writeback_requested = bool(args.get("writebackRequested", False))

                started_isolated = False
                run_info: dict[str, Any] = {}
                if target == "isolated":
                    try:
                        thread_id, turn_id, run_id = await automation._start_isolated_cron_run(
                            session_id=session_id,
                            job=job,
                            triggered_at_ms=now_ms,
                            manual=True,
                            writeback_requested=writeback_requested,
                        )
                        started_isolated = True
                        run_info = {"runId": run_id, "threadId": thread_id, "turnId": turn_id}
                    except Exception as e:
                        return (
                            _jsonrpc_result(
                                request_id=request_id,
                                result=_mcp_call_tool_result(
                                    content=[{"type": "text", "text": f"Failed to start isolated cron run: {e}"}],
                                    structured={"ok": False, "error": {"code": "INTERNAL_ERROR", "message": str(e)}},
                                    is_error=True,
                                ),
                            ),
                            {"MCP-Protocol-Version": sess.protocol_version},
                        )
                else:
                    # Ensure a main thread exists for manual run routing.
                    try:
                        main_tid = await automation.ensure_main_thread(session_id)
                    except Exception as e:
                        return (
                            _jsonrpc_result(
                                request_id=request_id,
                                result=_mcp_call_tool_result(
                                    content=[{"type": "text", "text": f"Failed to ensure main thread: {e}"}],
                                    structured={"ok": False, "error": {"code": "INTERNAL_ERROR", "message": str(e)}},
                                    is_error=True,
                                ),
                            ),
                            {"MCP-Protocol-Version": sess.protocol_version},
                        )

                    await automation.enqueue_system_event(
                        session_id=session_id,
                        thread_id=main_tid,
                        kind=CRON_EVENT_KIND,
                        text=job.text,
                        meta={"jobId": job.job_id, "expr": job.expr, "manual": True},
                    )

                def _mark_run(st2: PersistedGatewayAutomationState) -> None:
                    sess2 = st2.sessions.get(session_id)
                    if sess2 is None:
                        return
                    j2 = next((j for j in sess2.cron_jobs if j.job_id == job_id), None)
                    if j2 is None:
                        return
                    j2.last_run_at_ms = now_ms
                    try:
                        j2.next_run_at_ms = _cron_compute_next_ms(j2.expr, base_ms=now_ms)
                    except Exception:
                        j2.next_run_at_ms = None

                await automation._store.update(_mark_run)
                automation._cron_wakeup.set()
                payload = {"ok": True, "sessionId": session_id, "jobId": job_id, "target": target, "started": started_isolated, "enqueued": (not started_isolated)}
                payload.update(run_info)
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], structured=payload),
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
        scoped_sid = getattr(request.state, "mcp_scoped_session_id", None)
        scoped_sid = scoped_sid.strip() if isinstance(scoped_sid, str) and scoped_sid.strip() else None
        sess = McpSessionState(
            session_id=session_id,
            protocol_version=negotiated,
            initialized=False,
            client_info=client_info,
            scoped_session_id=scoped_sid,
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
            "instructions": "This MCP server exposes tools for listing and invoking connected nodes. Requests may be scoped to a runtime session.",
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
    # MCP auth is scoped per runtime session to avoid cross-agent data access.
    #
    # - The gateway reads a master secret from ARGUS_MCP_TOKEN (fallback: ARGUS_TOKEN).
    # - Each runtime container receives a derived per-session token as its ARGUS_MCP_TOKEN.
    # - Requests authenticated with the derived token are scoped to that sessionId.
    # - The raw master token is NOT accepted directly.
    master = os.getenv("ARGUS_MCP_TOKEN") or os.getenv("ARGUS_TOKEN") or None
    request.state.mcp_scoped_session_id = None
    if master is None:
        return
    provided = _extract_token_http(request) or ""
    scoped = _mcp_verify_derived_session_token(master, provided)
    if scoped:
        request.state.mcp_scoped_session_id = scoped
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


_MCP_DERIVED_TOKEN_PREFIX = "argus-mcp-v1"


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _mcp_derive_session_token(master: str, session_id: str) -> str:
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id must not be empty")
    mac = hmac.new(master.encode("utf-8"), sid.encode("utf-8"), hashlib.sha256).digest()
    sig = _b64url_no_pad(mac)[:32]
    return f"{_MCP_DERIVED_TOKEN_PREFIX}.{sid}.{sig}"


def _mcp_verify_derived_session_token(master: str, token: str) -> Optional[str]:
    t = str(token or "").strip()
    if not t:
        return None
    parts = t.split(".", 2)
    if len(parts) != 3:
        return None
    prefix, sid, sig = parts
    if prefix != _MCP_DERIVED_TOKEN_PREFIX:
        return None
    if not sid or not sig:
        return None
    expected = _mcp_derive_session_token(master, sid)
    # Constant-time compare.
    if hmac.compare_digest(expected, t):
        return sid
    return None


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
        jsonl_line_limit_bytes = int(os.getenv("ARGUS_JSONL_LINE_LIMIT_BYTES", str(DEFAULT_JSONL_LINE_LIMIT_BYTES)))
    except Exception:
        jsonl_line_limit_bytes = DEFAULT_JSONL_LINE_LIMIT_BYTES
    container_prefix = os.getenv("ARGUS_CONTAINER_PREFIX", "argus-session")

    cpu_quota, cpu_period = _parse_runtime_cpu_quota_period()
    mem_limit_bytes = _parse_optional_bytes_env("ARGUS_RUNTIME_MEM_LIMIT")
    memswap_limit_bytes = _parse_optional_bytes_env("ARGUS_RUNTIME_MEMSWAP_LIMIT")
    pids_limit = _parse_optional_int_env("ARGUS_RUNTIME_PIDS_LIMIT")

    if memswap_limit_bytes is not None and mem_limit_bytes is None:
        raise RuntimeError("ARGUS_RUNTIME_MEMSWAP_LIMIT requires ARGUS_RUNTIME_MEM_LIMIT")
    if memswap_limit_bytes is not None and mem_limit_bytes is not None and memswap_limit_bytes < mem_limit_bytes:
        raise RuntimeError("ARGUS_RUNTIME_MEMSWAP_LIMIT must be >= ARGUS_RUNTIME_MEM_LIMIT")

    return DockerProvisionConfig(
        image=image,
        network=network,
        home_host_path=home_host_path,
        workspace_host_path=workspace_host_path,
        runtime_cmd=runtime_cmd,
        connect_timeout_s=connect_timeout_s,
        jsonl_line_limit_bytes=jsonl_line_limit_bytes,
        container_prefix=container_prefix,
        runtime_cpu_quota=cpu_quota,
        runtime_cpu_period=cpu_period,
        runtime_mem_limit_bytes=mem_limit_bytes,
        runtime_memswap_limit_bytes=memswap_limit_bytes,
        runtime_pids_limit=pids_limit,
    )


def _resolve_workspace_host_path_for_session(session_id: str) -> Optional[str]:
    sid = session_id.strip() if isinstance(session_id, str) else ""
    if not sid:
        return None
    automation: Optional[AutomationManager] = getattr(app.state, "automation", None)
    if automation is None:
        return None
    try:
        return automation.get_workspace_host_path_for_session(sid)
    except Exception:
        return None


def _docker_api_timeout_s() -> float:
    # Keep Docker API calls snappy: the gateway should remain responsive even when Docker is slow/unavailable.
    # Reuse ARGUS_CONNECT_TIMEOUT_S as a coarse upper bound (no new env var).
    try:
        bound = float(os.getenv("ARGUS_CONNECT_TIMEOUT_S", "30"))
    except Exception:
        bound = 30.0
    return max(3.0, min(10.0, bound))

_MEM_LIMIT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]i?b?|b)?\s*$", re.IGNORECASE)


def _parse_bytes_limit(raw: str) -> int:
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty")
    m = _MEM_LIMIT_RE.match(s)
    if not m:
        raise ValueError(f"invalid bytes spec: {raw!r}")
    n = float(m.group(1))
    unit = (m.group(2) or "b").lower()
    unit = unit.replace("ib", "b")
    factors = {
        "b": 1,
        "k": 1024,
        "kb": 1024,
        "m": 1024**2,
        "mb": 1024**2,
        "g": 1024**3,
        "gb": 1024**3,
        "t": 1024**4,
        "tb": 1024**4,
    }
    factor = factors.get(unit)
    if factor is None:
        raise ValueError(f"invalid unit: {unit!r}")
    out = int(n * factor)
    if out <= 0:
        raise ValueError("must be > 0")
    return out


def _parse_optional_bytes_env(name: str) -> Optional[int]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        return _parse_bytes_limit(raw)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"{name} must be like '512m'/'1g' (got {raw!r}): {e}") from e


def _parse_optional_int_env(name: str) -> Optional[int]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        v = int(raw)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"{name} must be an integer (got {raw!r})") from e
    if v <= 0:
        raise RuntimeError(f"{name} must be > 0 (got {raw!r})")
    return v


def _parse_runtime_cpu_quota_period() -> tuple[Optional[int], Optional[int]]:
    raw = os.getenv("ARGUS_RUNTIME_CPUS")
    if raw is None or not raw.strip():
        return None, None
    try:
        cpus = float(raw)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"ARGUS_RUNTIME_CPUS must be a number (got {raw!r})") from e
    if cpus <= 0:
        raise RuntimeError(f"ARGUS_RUNTIME_CPUS must be > 0 (got {raw!r})")
    # Docker's classic CPU limit: quota/period in microseconds.
    period = 100_000
    quota = max(1_000, int(cpus * period))
    return quota, period


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
    mcp_master = os.getenv("ARGUS_MCP_TOKEN") or gateway_token
    if mcp_master:
        env["ARGUS_MCP_TOKEN"] = _mcp_derive_session_token(mcp_master, session_id)

    # Runtime node-host (inside the same runtime container) connects back to the gateway
    # for background-job style execution (`system.run` + `process.*`).
    try:
        from urllib.parse import quote as _url_quote
    except Exception:  # pragma: no cover
        _url_quote = None  # type: ignore
    node_token = os.getenv("ARGUS_NODE_TOKEN") or gateway_token
    gateway_internal_host = (os.getenv("ARGUS_GATEWAY_INTERNAL_HOST") or "").strip() or None
    if not gateway_internal_host:
        # Prefer the current gateway container name (resolvable on the shared docker network)
        # over a hard-coded service name like "gateway". This makes runtime node-host connect
        # reliably even when the gateway wasn't started via docker-compose.
        try:
            self_id = (os.getenv("HOSTNAME") or "").strip()
            if self_id:
                me = client.containers.get(self_id)
                cand = getattr(me, "name", None)
                if isinstance(cand, str) and cand.strip():
                    gateway_internal_host = cand.strip()
        except Exception:
            gateway_internal_host = None
    if not gateway_internal_host:
        gateway_internal_host = "gateway"

    node_ws_url = f"ws://{gateway_internal_host}:8080/nodes/ws"
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
    if cfg.workspace_host_path:
        labels["io.argus.runtime_workspace_host_path"] = cfg.workspace_host_path

    run_kwargs = {"working_dir": cfg.workspace_container_path}
    if cfg.runtime_mem_limit_bytes is not None:
        run_kwargs["mem_limit"] = cfg.runtime_mem_limit_bytes
    if cfg.runtime_memswap_limit_bytes is not None:
        run_kwargs["memswap_limit"] = cfg.runtime_memswap_limit_bytes
    if cfg.runtime_cpu_quota is not None and cfg.runtime_cpu_period is not None:
        run_kwargs["cpu_quota"] = cfg.runtime_cpu_quota
        run_kwargs["cpu_period"] = cfg.runtime_cpu_period
    if cfg.runtime_pids_limit is not None:
        run_kwargs["pids_limit"] = cfg.runtime_pids_limit

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
    # Multi-agent: mount a per-session workspace when configured in automation state.
    workspace_override = _resolve_workspace_host_path_for_session(session_id)
    if workspace_override:
        if not os.path.isabs(workspace_override):
            raise RuntimeError(f"Invalid workspace override for session {session_id}: not an absolute path")
        cfg = replace(cfg, workspace_host_path=workspace_override)
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
            automation: Optional[AutomationManager] = getattr(app.state, "automation", None)
            if automation is not None:
                try:
                    automation.on_runtime_session_closed(session_id)
                except Exception:
                    log.exception("Automation cleanup failed for closed session %s", session_id)

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
        lanes.append(
            {
                "sessionId": sid,
                "threadId": tid,
                "busy": lane.busy,
                "activeTurnId": lane.active_turn_id,
                "busySinceMs": lane.busy_since_ms,
                "lastProgressAtMs": lane.last_progress_at_ms,
                "followupDepth": len(lane.followups),
            }
        )
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
    session_target = str(body.get("sessionTarget") or CRON_DEFAULT_SESSION_TARGET).strip().lower()
    if session_target not in CRON_SESSION_TARGETS:
        raise HTTPException(status_code=400, detail="Invalid sessionTarget (must be 'main' or 'isolated')")
    writeback_raw = body.get("writeback")
    if writeback_raw is not None and not isinstance(writeback_raw, dict):
        raise HTTPException(status_code=400, detail="writeback must be an object")
    retention_raw = body.get("retention")
    if retention_raw is not None and not isinstance(retention_raw, dict):
        raise HTTPException(status_code=400, detail="retention must be an object")

    writeback: Optional[dict[str, Any]] = None
    if isinstance(writeback_raw, dict):
        writeback = {}
        when = writeback_raw.get("when")
        if when is not None:
            w = str(when or "").strip().lower()
            if w and w not in CRON_WRITEBACK_WHENS:
                raise HTTPException(status_code=400, detail="Invalid writeback.when")
            if w:
                writeback["when"] = w
        max_chars = writeback_raw.get("maxChars")
        if max_chars is not None:
            try:
                mc = int(max_chars)
            except Exception as e:
                raise HTTPException(status_code=400, detail="writeback.maxChars must be an integer") from e
            writeback["maxChars"] = max(200, min(20_000, mc))
        prompt_hint = writeback_raw.get("promptHint")
        if prompt_hint is not None:
            writeback["promptHint"] = bool(prompt_hint)
        if not writeback:
            writeback = None

    retention: Optional[dict[str, Any]] = None
    if isinstance(retention_raw, dict):
        retention = {}
        max_unarchived = retention_raw.get("maxUnarchivedThreads")
        if max_unarchived is not None:
            try:
                n = int(max_unarchived)
            except Exception as e:
                raise HTTPException(status_code=400, detail="retention.maxUnarchivedThreads must be an integer") from e
            retention["maxUnarchivedThreads"] = max(1, min(2000, n))
        if not retention:
            retention = None
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

    saved: Optional[PersistedCronJob] = None

    def _add(st: PersistedGatewayAutomationState) -> None:
        nonlocal saved
        sess = st.sessions.get(sid)
        if sess is None:
            sess = PersistedSessionAutomation()
            st.sessions[sid] = sess
        # Replace if same id exists.
        sess.cron_jobs = [j for j in sess.cron_jobs if j.job_id != job_id]
        saved = PersistedCronJob(
            job_id=job_id,
            expr=expr,
            text=text,
            enabled=enabled,
            session_target=session_target,
            writeback=writeback,
            retention=retention,
        )
        sess.cron_jobs.append(saved)

    await automation._store.update(_add)
    automation._cron_wakeup.set()
    return {"ok": True, "sessionId": sid, "job": saved.to_json() if saved is not None else None}


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
        if removed:
            try:
                sess.cron_runs_by_job.pop(job_id, None)
            except Exception:
                pass

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
            meta={
                "event": event,
                "nodeId": node_id,
                "displayName": display_name,
                "platform": platform,
                "version": version,
                "ip": ip,
                "wakeHeartbeat": False,
            },
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
            allow_create = not bool(requested_session)
            if requested_session:
                # If the client asked for an explicit session and it's already known in persisted automation state,
                # allow re-creating the runtime container (e.g. after a clean rebuild where the host state persists).
                automation: Optional[AutomationManager] = getattr(app.state, "automation", None)
                if automation is not None:
                    st = automation._store.state
                    if requested_session == st.default_session_id or requested_session in st.sessions:
                        allow_create = True
            live, created = await _ensure_live_docker_session(session_id, allow_create=allow_create)
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
                        if method == "argus/agent/list":
                            chat_key = None
                            raw_chat_key = params.get("chatKey")
                            if isinstance(raw_chat_key, str) and raw_chat_key.strip():
                                chat_key = raw_chat_key.strip()
                            source = params.get("source")
                            if chat_key is None and isinstance(source, dict):
                                ck = source.get("chatKey")
                                if isinstance(ck, str) and ck.strip():
                                    chat_key = ck.strip()
                            current_agent = automation.resolve_agent_for_chat_key(chat_key or "")
                            result = {
                                "ok": True,
                                "agents": automation.list_agents(),
                                "currentAgentId": current_agent,
                                "currentSessionId": automation.resolve_agent_session_id(current_agent),
                            }
                            if has_id:
                                await ws.send_text(json.dumps({"id": req_id, "result": result}))
                            continue
                        if method == "argus/agent/resolve":
                            raw_chat_key = params.get("chatKey")
                            if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
                                if has_id:
                                    await ws.send_text(
                                        json.dumps({"id": req_id, "error": {"code": -32602, "message": "Missing 'chatKey'"}})
                                    )
                                continue
                            chat_key = raw_chat_key.strip()
                            agent_id = automation.resolve_agent_for_chat_key(chat_key)
                            sess_id = (
                                automation.resolve_agent_session_id(agent_id) or automation._store.state.default_session_id
                            )
                            result = {"ok": True, "chatKey": chat_key, "agentId": agent_id, "sessionId": sess_id}
                            if has_id:
                                await ws.send_text(json.dumps({"id": req_id, "result": result}))
                            continue
                        if method == "argus/agent/create":
                            raw_aid = params.get("agentId") or params.get("name")
                            aid = _normalize_agent_id(raw_aid)
                            if not aid:
                                if has_id:
                                    await ws.send_text(
                                        json.dumps({"id": req_id, "error": {"code": -32602, "message": "Invalid 'agentId'"}})
                                    )
                                continue
                            agent = await automation.create_agent(agent_id=aid)
                            result = {"ok": True, "agent": agent.to_json()}
                            if has_id:
                                await ws.send_text(json.dumps({"id": req_id, "result": result}))
                            continue
                        if method == "argus/agent/use":
                            raw_chat_key = params.get("chatKey")
                            raw_aid = params.get("agentId") or params.get("name")
                            if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
                                if has_id:
                                    await ws.send_text(
                                        json.dumps({"id": req_id, "error": {"code": -32602, "message": "Missing 'chatKey'"}})
                                    )
                                continue
                            aid = _normalize_agent_id(raw_aid)
                            if not aid:
                                if has_id:
                                    await ws.send_text(
                                        json.dumps({"id": req_id, "error": {"code": -32602, "message": "Invalid 'agentId'"}})
                                    )
                                continue
                            agent = await automation.bind_chat_to_agent(chat_key=raw_chat_key.strip(), agent_id=aid)
                            result = {"ok": True, "chatKey": raw_chat_key.strip(), "agent": agent.to_json()}
                            if has_id:
                                await ws.send_text(json.dumps({"id": req_id, "result": result}))
                            continue
                        if method == "argus/thread/main/set":
                            raw_tid = params.get("threadId")
                            if not isinstance(raw_tid, str) or not raw_tid.strip():
                                if has_id:
                                    await ws.send_text(
                                        json.dumps({"id": req_id, "error": {"code": -32602, "message": "Missing 'threadId'"}})
                                    )
                                continue
                            tid = await automation.set_main_thread(session_id, raw_tid.strip())
                            if has_id:
                                await ws.send_text(json.dumps({"id": req_id, "result": {"ok": True, "threadId": tid}}))
                            continue
                        if method == "argus/input/enqueue":
                            telegram_images = params.get("telegramImages")
                            if not isinstance(telegram_images, list):
                                telegram_images = None
                            else:
                                telegram_images = [x for x in telegram_images if isinstance(x, dict)]
                                if not telegram_images:
                                    telegram_images = None

                            text_param = params.get("text")
                            if not isinstance(text_param, str):
                                text_param = ""
                            if not text_param.strip():
                                if telegram_images:
                                    text_param = "<media:image>"
                                else:
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
                                telegram_images=telegram_images,
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
            line_limit = int(os.getenv("ARGUS_JSONL_LINE_LIMIT_BYTES", str(DEFAULT_JSONL_LINE_LIMIT_BYTES)))
        except Exception:
            line_limit = DEFAULT_JSONL_LINE_LIMIT_BYTES
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
