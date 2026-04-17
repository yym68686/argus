import asyncio
import base64
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from fastapi import Body, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.websockets import WebSocketState

from croniter import croniter


log = logging.getLogger("argus_gateway")

DEFAULT_ARGUS_VERSION = "0.0.0"


def _load_argus_version() -> str:
    override = (os.getenv("ARGUS_VERSION") or "").strip()
    if override:
        return override

    candidates = [
        Path(__file__).resolve().parents[2] / "VERSION",
        Path.cwd() / "VERSION",
    ]
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        if value:
            return value
    return DEFAULT_ARGUS_VERSION


ARGUS_VERSION = _load_argus_version()

RUNTIME_LAYOUT = "root_argus_v1"
RUNTIME_SESSION_ID_RE = re.compile(r"^[a-f0-9]{12}$")
KUBERNETES_SERVICEACCOUNT_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"


def _event_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_log_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k).strip()
            if not key:
                continue
            out[key] = _event_log_value(v)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_event_log_value(v) for v in value]
    return str(value)


def _event_log(level: str, event: str, **fields: Any) -> None:
    payload: dict[str, Any] = {
        "ts": _event_iso_now(),
        "lvl": str(level or "INFO").upper(),
        "svc": "gateway",
        "ev": str(event or "gateway.event"),
    }
    for key, value in fields.items():
        if value is None:
            continue
        payload[str(key)] = _event_log_value(value)
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    level_name = str(level or "info").strip().lower()
    if level_name == "debug":
        log.debug(line)
    elif level_name == "warning":
        log.warning(line)
    elif level_name == "error":
        log.error(line)
    else:
        log.info(line)


def _chat_key_hash(raw: Any) -> Optional[str]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    digest = hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()[:12]
    return f"tg:{digest}"


def _text_hash(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"


class UpstreamSessionClosedError(RuntimeError):
    pass


@dataclass(frozen=True)
class Upstream:
    host: str
    port: int
    token: Optional[str] = None


@dataclass(frozen=True)
class ProvisionModeResolution:
    raw_mode: str
    resolved_mode: str
    reason: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DockerProvisionConfig:
    image: str
    network: str
    home_host_path: Optional[str]
    workspace_host_path: Optional[str]
    home_container_path: str = "/root/.argus"
    workspace_container_path: str = "/workspace"
    runtime_cmd: Optional[str] = None
    connect_timeout_s: float = 30.0
    jsonl_line_limit_bytes: int = 8 * 1024 * 1024
    container_prefix: str = "argus-session"
    runtime_cpu_period: Optional[int] = None
    runtime_cpu_quota: Optional[int] = None
    runtime_mem_limit_bytes: Optional[int] = None
    runtime_memswap_limit_bytes: Optional[int] = None
    runtime_pids_limit: Optional[int] = None


@dataclass(frozen=True)
class FugueProvisionConfig:
    base_url: str
    token: str
    project_id: str
    runtime_id: str
    gateway_internal_host: str
    tenant_id: Optional[str] = None
    image: Optional[str] = None
    runtime_app_id: Optional[str] = None
    runtime_app_name: Optional[str] = None
    runtime_compose_service: Optional[str] = None
    workspace_mount_path: str = "/workspace"
    home_container_path: str = "/root/.argus"
    runtime_cmd: Optional[str] = None
    connect_timeout_s: float = 30.0
    jsonl_line_limit_bytes: int = 8 * 1024 * 1024
    workspace_storage_size: str = "1Gi"
    workspace_storage_class_name: Optional[str] = None
    service_port: int = 7777
    app_name_prefix: str = "argus-session"


@dataclass
class LiveRuntimeSession:
    session_id: str
    provider: str
    runtime_id: str
    runtime_name: str
    workspace_runtime_path: str
    jsonl_line_limit_bytes: int
    upstream_host: str
    upstream_port: int
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
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
    pending_client_turn_starts: dict[str, str]

    turn_owners_by_thread: dict[str, WebSocket]
    non_json_lines_dropped: int = 0

    closed: bool = False

    @property
    def container_id(self) -> str:
        return self.runtime_id

    @property
    def container_name(self) -> str:
        return self.runtime_name

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

    async def close_attached_websockets(self, *, code: int, reason: str) -> None:
        async with self.attach_lock:
            targets = list(self.attached_wss)
        if targets:
            log.info("Closing %s downstream websocket(s) for session %s: %s", len(targets), self.session_id, reason)
        for ws in targets:
            if ws.client_state == WebSocketState.CONNECTED:
                try:
                    await ws.close(code=code, reason=reason)
                except Exception:
                    pass
            await self.detach(ws)

    async def write_upstream(self, text: str) -> None:
        try:
            writer_closing = self.writer.is_closing()
        except Exception:
            writer_closing = False
        if self.closed or writer_closing:
            raise UpstreamSessionClosedError(f"Upstream session {self.session_id} is closed")
        if not text.endswith("\n"):
            text += "\n"
        try:
            self.writer.write(text.encode("utf-8"))
            await self.writer.drain()
        except Exception as e:
            self.closed = True
            try:
                self.writer.close()
            except Exception:
                pass
            raise UpstreamSessionClosedError(f"Upstream session {self.session_id} is closed") from e


def _is_expected_websocket_receive_runtime_error(exc: RuntimeError) -> bool:
    return "WebSocket is not connected" in str(exc or "")


# Backwards-compatible alias while the rest of the module migrates away from docker-specific naming.
LiveDockerSession = LiveRuntimeSession


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


def _normalize_workspace_relative_path(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    s = raw.strip().replace("\\", "/")
    if s.startswith("./"):
        s = s[2:]
    s = s.lstrip("/")
    if not s:
        return None
    p = Path(s)
    if p.is_absolute():
        return None
    parts = p.parts
    if not parts:
        return None
    if any(part in ("", ".", "..") for part in parts):
        return None
    return p.as_posix()


def _workspace_ref(raw: Any) -> Optional[str]:
    rel = _normalize_workspace_relative_path(raw)
    if not rel:
        return None
    return f"./{rel}"



HEARTBEAT_FILENAME = "HEARTBEAT.md"
HEARTBEAT_TASKS_INTERVAL_MS = 30 * 60 * 1000
HEARTBEAT_TOKEN = "HEARTBEAT_OK"
HEARTBEAT_ACK_MAX_CHARS = 300

TURN_KIND_USER = "user"
TURN_KIND_HEARTBEAT = "heartbeat"
TURN_KIND_CRON = "cron"

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
TELEGRAM_DRAFT_FLUSH_INTERVAL_MS = 400
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
WORKSPACE_TEMPLATE_SYNC_RULES: list[tuple[str, str, bool]] = [
    (AGENTS_TEMPLATE_FILENAME, AGENTS_FILENAME, True),
    (SOUL_FILENAME, SOUL_FILENAME, False),
    (USER_FILENAME, USER_FILENAME, False),
    (HEARTBEAT_FILENAME, HEARTBEAT_FILENAME, False),
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


def _telegram_draft_probe_text(raw: Optional[str]) -> str:
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text:
        return ""
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"&nbsp;", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"^[*`~_]+", "", text)
    text = re.sub(r"[*`~_]+$", "", text)
    return text.strip()


def _telegram_prepare_draft_payload(raw: Optional[str]) -> dict[str, Any]:
    if not isinstance(raw, str):
        return {"action": "skip", "text": "", "parseMode": None, "payloadKey": None}

    trimmed = raw.strip()
    if not trimmed:
        return {"action": "skip", "text": "", "parseMode": None, "payloadKey": None}

    probe = _telegram_draft_probe_text(trimmed)
    if probe and HEARTBEAT_TOKEN.startswith(probe):
        return {"action": "hold", "text": "", "parseMode": None, "payloadKey": None}

    stripped = _strip_heartbeat_token(trimmed)
    if stripped.get("shouldSkip"):
        return {"action": "skip", "text": "", "parseMode": None, "payloadKey": None}

    text = stripped.get("text") if isinstance(stripped.get("text"), str) else trimmed
    text = _telegram_truncate(text.strip())
    if not text:
        return {"action": "skip", "text": "", "parseMode": None, "payloadKey": None}

    html = _markdown_to_telegram_html(text)
    if html:
        return {
            "action": "send",
            "text": html,
            "parseMode": "HTML",
            "payloadKey": f"HTML\0{html}",
        }

    return {
        "action": "send",
        "text": text,
        "parseMode": None,
        "payloadKey": f"PLAIN\0{text}",
    }

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


def _telegram_find_split_boundary(text: str) -> int:
    raw = str(text or "")
    if not raw:
        return 0
    min_index = max(1, int(len(raw) * 0.6))
    candidates = (
        "\n\n",
        "\n",
        "。",
        "！",
        "？",
        ". ",
        "! ",
        "? ",
        "；",
        "; ",
        "，",
        ", ",
        "、",
        " ",
        "\t",
    )
    for needle in candidates:
        idx = raw.rfind(needle, min_index)
        if idx >= min_index:
            return idx + len(needle)
    return 0


def _telegram_split_text(text: str) -> list[str]:
    raw = str(text or "")
    if not raw:
        return []
    if len(raw) <= TELEGRAM_MAX_MESSAGE_CHARS:
        return [raw]

    out: list[str] = []
    start = 0
    while start < len(raw):
        end = min(start + TELEGRAM_MAX_MESSAGE_CHARS, len(raw))
        if end < len(raw):
            boundary = _telegram_find_split_boundary(raw[start:end])
            if boundary > 0:
                end = start + boundary
        if end <= start:
            end = min(start + TELEGRAM_MAX_MESSAGE_CHARS, len(raw))
        chunk = raw[start:end]
        if chunk:
            out.append(chunk)
        start = end
    return out


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


def _telegram_chat_id_from_chat_key(chat_key: Any) -> Optional[int]:
    if not isinstance(chat_key, str) or not chat_key.strip():
        return None
    chat_id_raw = chat_key.strip().split(":", 1)[0].strip()
    if not chat_id_raw:
        return None
    try:
        chat_id = int(chat_id_raw)
    except Exception:
        return None
    return chat_id if chat_id != 0 else None


def _telegram_group_binding_key_from_chat_key(chat_key: Any) -> Optional[str]:
    chat_id = _telegram_chat_id_from_chat_key(chat_key)
    if not isinstance(chat_id, int) or chat_id >= 0:
        return None
    return str(chat_id)


def _telegram_topic_id_from_chat_key(chat_key: Any) -> Optional[int]:
    if not isinstance(chat_key, str) or not chat_key.strip():
        return None
    parts = chat_key.strip().split(":", 1)
    if len(parts) != 2:
        return None
    topic_raw = parts[1].strip()
    if not topic_raw:
        return None
    try:
        topic_id = int(topic_raw)
    except Exception:
        return None
    return topic_id if topic_id > 0 else None


def _chat_binding_lookup_keys(chat_key: Any) -> list[str]:
    if not isinstance(chat_key, str):
        return []
    ck = chat_key.strip()
    if not ck:
        return []
    out: list[str] = []
    group_key = _telegram_group_binding_key_from_chat_key(ck)
    # Topic-specific bindings override the group's default binding.
    out.append(ck)
    # Fall back to the group's default binding when a topic has no override.
    if group_key and group_key not in out:
        out.append(group_key)
    return out


def _chat_binding_storage_key(chat_key: Any) -> str:
    if not isinstance(chat_key, str):
        return ""
    ck = chat_key.strip()
    if not ck:
        return ""
    return ck


def _telegram_private_chat_id_from_chat_key(chat_key: Any) -> Optional[int]:
    if not isinstance(chat_key, str) or not chat_key.strip():
        return None
    chat_id_raw = chat_key.strip().split(":", 1)[0].strip()
    if not chat_id_raw or not TELEGRAM_PRIVATE_CHAT_ID_RE.match(chat_id_raw):
        return None
    try:
        chat_id = int(chat_id_raw)
    except Exception:
        return None
    return chat_id if chat_id > 0 else None


def _telegram_draft_streaming_mode() -> str:
    raw = (os.getenv("TELEGRAM_DRAFT_STREAMING") or "auto").strip().lower()
    if raw in ("", "auto", "on", "true", "1", "yes"):
        return "auto"
    if raw in ("force", "always"):
        return "force"
    return "off"


def _new_telegram_draft_id() -> int:
    value = uuid.uuid4().int % 2_000_000_000
    return int(value or 1)


def _normalize_message_phase(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower()


def _create_turn_text_entry() -> dict[str, Any]:
    return {
        "delta": "",
        "fullText": None,
        "turnKind": None,
        "turnStartedAtMs": None,
        "completedCommentaryTexts": [],
        "agentMessagePhasesByItemId": {},
        "agentMessageTextByItemId": {},
        "activeAgentMessageItemId": None,
        "firstFinalItemCompletedAtMs": None,
        "lastFinalItemCompletedAtMs": None,
        "lastFinalItemId": None,
        "lastFinalTextHash": None,
        "lastFinalTextLen": None,
        "draft": None,
        "sourceChannel": None,
        "sourceChatKey": None,
        "cancelId": None,
        "cancelRequestedAtMs": None,
        "cancelPostActivityLogged": False,
    }


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


async def _telegram_send_plain_message_parts(
    *,
    token: str,
    target: dict[str, Any],
    text: str,
    disable_notification: Optional[bool] = None,
) -> list[Any]:
    results: list[Any] = []
    for chunk in _telegram_split_text(text):
        results.append(
            await _telegram_send_message(
                token=token,
                target=target,
                text=chunk,
                parse_mode=None,
                disable_notification=disable_notification,
            )
        )
    return results


async def _telegram_send_markdown_message_parts(
    *,
    token: str,
    target: dict[str, Any],
    text: str,
    disable_notification: Optional[bool] = None,
) -> list[Any]:
    results: list[Any] = []
    for chunk in _telegram_split_text(text):
        html = _markdown_to_telegram_html(chunk)
        if html:
            try:
                results.append(
                    await _telegram_send_message(
                        token=token,
                        target=target,
                        text=html,
                        parse_mode="HTML",
                        disable_notification=disable_notification,
                    )
                )
                continue
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if not (TELEGRAM_HTML_PARSE_ERR_RE.search(msg) or TELEGRAM_MESSAGE_TOO_LONG_RE.search(msg)):
                    raise

        results.append(
            await _telegram_send_message(
                token=token,
                target=target,
                text=chunk,
                parse_mode=None,
                disable_notification=disable_notification,
            )
        )
    return results


async def _telegram_send_message_draft(
    *,
    token: str,
    target: dict[str, Any],
    draft_id: int,
    text: str,
    parse_mode: Optional[str] = None,
) -> Any:
    params = {
        "chat_id": target.get("chat_id"),
        "draft_id": int(draft_id),
        "text": text,
    }
    if "message_thread_id" in target:
        params["message_thread_id"] = target["message_thread_id"]
    if parse_mode:
        params["parse_mode"] = parse_mode
    try:
        return await asyncio.to_thread(_telegram_api_call_sync, token, "sendMessageDraft", params)
    except Exception as e:  # noqa: BLE001
        if "message_thread_id" in params and TELEGRAM_THREAD_NOT_FOUND_RE.search(str(e) or ""):
            params.pop("message_thread_id", None)
            return await asyncio.to_thread(_telegram_api_call_sync, token, "sendMessageDraft", params)
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


def _render_workspace_template_content(template_filename: str, target_filename: str) -> str:
    template_raw = _load_template_text(template_filename) or f"# {target_filename}\n"
    template_clean = _strip_yaml_front_matter(template_raw).strip()
    if template_clean:
        return template_clean + "\n"
    return f"# {target_filename}\n"


def _sync_workspace_template_file(
    workspace_root: Path,
    *,
    template_filename: str,
    target_filename: str,
    overwrite_existing: bool,
) -> None:
    target_path = workspace_root / target_filename
    if target_path.exists() and not overwrite_existing:
        return

    rendered = _render_workspace_template_content(template_filename, target_filename)
    if target_path.exists():
        try:
            if target_path.read_text(encoding="utf-8") == rendered:
                return
        except Exception:
            pass

    _atomic_write_text(target_path, rendered)


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


@dataclass
class PersistedStagedAttachment:
    path: str
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None
    source: str = "message"
    is_image: bool = False
    file_unique_id: Optional[str] = None
    staged_at_ms: int = 0

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "path": self.path,
            "source": self.source,
            "isImage": bool(self.is_image),
            "stagedAtMs": int(self.staged_at_ms or 0),
        }
        if isinstance(self.file_name, str) and self.file_name.strip():
            out["fileName"] = self.file_name.strip()
        if isinstance(self.mime_type, str) and self.mime_type.strip():
            out["mimeType"] = self.mime_type.strip()
        if isinstance(self.file_size, int) and self.file_size >= 0:
            out["fileSize"] = int(self.file_size)
        if isinstance(self.file_unique_id, str) and self.file_unique_id.strip():
            out["fileUniqueId"] = self.file_unique_id.strip()
        return out

    @staticmethod
    def from_json(obj: Any) -> Optional["PersistedStagedAttachment"]:
        if not isinstance(obj, dict):
            return None
        path = _normalize_workspace_relative_path(obj.get("path"))
        if not path:
            return None
        file_name = obj.get("fileName")
        if not isinstance(file_name, str) or not file_name.strip():
            file_name = None
        else:
            file_name = file_name.strip()
        mime_type = obj.get("mimeType")
        if not isinstance(mime_type, str) or not mime_type.strip():
            mime_type = None
        else:
            mime_type = mime_type.strip()
        file_size = obj.get("fileSize")
        if file_size is not None:
            try:
                file_size = int(file_size)
            except Exception:
                file_size = None
        source = str(obj.get("source") or "message").strip() or "message"
        is_image = bool(obj.get("isImage", False))
        file_unique_id = obj.get("fileUniqueId")
        if not isinstance(file_unique_id, str) or not file_unique_id.strip():
            file_unique_id = None
        else:
            file_unique_id = file_unique_id.strip()
        staged_at_ms = obj.get("stagedAtMs")
        try:
            staged_at_ms_int = int(staged_at_ms) if staged_at_ms is not None else 0
        except Exception:
            staged_at_ms_int = 0
        return PersistedStagedAttachment(
            path=path,
            file_name=file_name,
            mime_type=mime_type,
            file_size=file_size,
            source=source,
            is_image=is_image,
            file_unique_id=file_unique_id,
            staged_at_ms=staged_at_ms_int,
        )


AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
CHANNEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
TELEGRAM_PRIVATE_CHAT_ID_RE = re.compile(r"^[1-9]\d{0,19}$")
AUTOMATION_STATE_VERSION = 7
ARGUS_AGENT_MODEL_DEFAULT = "gpt-5.4"
ARGUS_GATEWAY_AGENT_MODELS = ("gpt-5.2", "gpt-5.4")
ARGUS_GATEWAY_AGENT_MODELS_SET = frozenset(ARGUS_GATEWAY_AGENT_MODELS)
ARGUS_AGENT_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
ARGUS_MODEL_CATALOG_CACHE_TTL_MS = 60_000
ARGUS_AGENT_MODEL_FILE = "argus-agent-model"
CHANNEL_ID_GATEWAY = "gateway"
CHANNEL_ID_ZERO_ZERO_PRO = "0-0.pro"
CHANNEL_IDS_BUILTIN = frozenset((CHANNEL_ID_GATEWAY, CHANNEL_ID_ZERO_ZERO_PRO))
CHANNEL_ZERO_ZERO_PRO_BASE_URL = "https://api.0-0.pro/v1"
CHANNEL_ZERO_ZERO_PRO_PROMO_URL = "https://0-0.pro"


def _normalize_runtime_session_id(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    sid = raw.strip().lower()
    if not sid:
        return ""
    if not RUNTIME_SESSION_ID_RE.match(sid):
        return ""
    return sid


def _is_missing_thread_for_archive_error(msg: str) -> bool:
    s = str(msg or "").strip().lower()
    if not s:
        return False
    # Codex app-server uses rollout ids internally for threads; older threads can become
    # unreachable after storage/layout migrations (e.g. workspace-scoped CODEX_HOME).
    if "no rollout found" in s and "thread" in s:
        return True
    return False


def _normalize_agent_id(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    agent_id = raw.strip().lower()
    if not agent_id:
        return ""
    if not AGENT_ID_RE.match(agent_id):
        return ""
    return agent_id


def _parse_telegram_private_user_id(chat_key: Any) -> Optional[int]:
    if not isinstance(chat_key, str):
        return None
    ck = chat_key.strip()
    if not ck:
        return None
    if not TELEGRAM_PRIVATE_CHAT_ID_RE.match(ck):
        return None
    try:
        user_id = int(ck)
    except Exception:
        return None
    return user_id if user_id > 0 else None


def _normalize_agent_short_name(raw: Any) -> str:
    name = _normalize_agent_id(raw)
    # Avoid confusion between "short name" and explicit agentId.
    if re.match(r"^u\d+-", name):
        return ""
    return name


def _user_agent_id(*, user_id: int, short_name: str) -> str:
    return f"u{user_id}-{short_name}"


def _normalize_channel_id(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    channel_id = raw.strip().lower()
    if not channel_id:
        return ""
    if not CHANNEL_ID_RE.match(channel_id):
        return ""
    return channel_id


def _normalize_channel_name(raw: Any) -> str:
    return _normalize_channel_id(raw)


def _normalize_channel_api_key(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _normalize_openai_base_url(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlparse(value)
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    if not parsed.netloc:
        return ""
    if parsed.query or parsed.fragment:
        return ""
    path = parsed.path.rstrip("/")
    for suffix in ("/responses", "/models"):
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    normalized = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
    return normalized.rstrip("/")


def _responses_url_from_base_url(base_url: Any) -> str:
    normalized = _normalize_openai_base_url(base_url)
    if not normalized:
        return ""
    return f"{normalized}/responses"


def _models_url_from_base_url(base_url: Any) -> str:
    normalized = _normalize_openai_base_url(base_url)
    if not normalized:
        return ""
    return f"{normalized}/models"


def _base_url_from_responses_url(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    value = value.rstrip("/")
    if value.lower().endswith("/responses"):
        value = value[: -len("/responses")]
    return value.rstrip("/")


def _gateway_openai_api_key() -> Optional[str]:
    return _normalize_channel_api_key(os.getenv("OPENAI_API_KEY") or os.getenv("ARGUS_OPENAI_API_KEY"))


def _gateway_openai_responses_upstream_url() -> str:
    return (os.getenv("ARGUS_OPENAI_RESPONSES_UPSTREAM_URL") or "https://api.openai.com/v1/responses").strip()


def _mask_secret(raw: Any) -> Optional[str]:
    value = _normalize_channel_api_key(raw)
    if not value:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}***{value[-4:]}"


def _new_custom_channel_id() -> str:
    return f"ch-{uuid.uuid4().hex[:12]}"


def _normalize_agent_model(raw: Any) -> str:
    if raw is None:
        return ""
    model = str(raw).strip()
    if not model or len(model) > 128:
        return ""
    if not ARGUS_AGENT_MODEL_RE.match(model):
        return ""
    return model


def _normalize_model_catalog_entry(raw: Any) -> Optional[dict[str, Any]]:
    if isinstance(raw, dict):
        model_id = _normalize_agent_model(raw.get("id") or raw.get("model") or raw.get("name"))
        if not model_id:
            return None
        out: dict[str, Any] = {"id": model_id}
        owned_by = str(raw.get("owned_by") or raw.get("ownedBy") or "").strip()
        if owned_by:
            out["ownedBy"] = owned_by
        object_type = str(raw.get("object") or "").strip()
        if object_type:
            out["object"] = object_type
        created = raw.get("created")
        if isinstance(created, int):
            out["created"] = created
        return out
    model_id = _normalize_agent_model(raw)
    if not model_id:
        return None
    return {"id": model_id}


def _parse_model_catalog_payload(raw: Any) -> list[dict[str, Any]]:
    items: Any = None
    if isinstance(raw, dict):
        if isinstance(raw.get("data"), list):
            items = raw.get("data")
        elif isinstance(raw.get("models"), list):
            items = raw.get("models")
    elif isinstance(raw, list):
        items = raw
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        entry = _normalize_model_catalog_entry(item)
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        out.append(entry)
    return out


def _model_catalog_error_detail(raw: Any) -> Optional[str]:
    if isinstance(raw, dict):
        err = raw.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message") or "").strip()
            if msg:
                return msg
        for key in ("detail", "message", "error"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _fallback_model_catalog(*, current_model: Optional[str], error: Optional[str], channel: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    model_id = _normalize_agent_model(current_model) or ARGUS_AGENT_MODEL_DEFAULT
    return {
        "availableModels": [model_id],
        "models": [{"id": model_id}],
        "source": "fallback",
        "fetchedAtMs": _now_ms(),
        "error": error,
        "channel": dict(channel) if isinstance(channel, dict) else None,
    }


def _static_model_catalog(
    *,
    models: tuple[str, ...],
    source: str,
    error: Optional[str] = None,
    channel: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    available = []
    seen: set[str] = set()
    for raw in models:
        model_id = _normalize_agent_model(raw)
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        available.append(model_id)
    if not available:
        available = [ARGUS_AGENT_MODEL_DEFAULT]
    return {
        "availableModels": available,
        "models": [{"id": model_id} for model_id in available],
        "source": source,
        "fetchedAtMs": _now_ms(),
        "error": error,
        "channel": dict(channel) if isinstance(channel, dict) else None,
    }


def _merge_current_model_into_catalog(catalog: dict[str, Any], current_model: Optional[str]) -> dict[str, Any]:
    current = _normalize_agent_model(current_model)
    available_raw = catalog.get("availableModels")
    models_raw = catalog.get("models")
    available: list[str] = []
    seen: set[str] = set()
    if isinstance(available_raw, list):
        for raw in available_raw:
            model_id = _normalize_agent_model(raw)
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            available.append(model_id)
    models: list[dict[str, Any]] = []
    seen_models: set[str] = set()
    if isinstance(models_raw, list):
        for raw in models_raw:
            entry = _normalize_model_catalog_entry(raw)
            if not isinstance(entry, dict):
                continue
            model_id = str(entry.get("id") or "").strip()
            if not model_id or model_id in seen_models:
                continue
            seen_models.add(model_id)
            models.append(entry)
    if current and current not in seen:
        available.append(current)
        seen.add(current)
    if current and current not in seen_models:
        models.append({"id": current})
        seen_models.add(current)
    out = dict(catalog)
    out["availableModels"] = available or [_normalize_agent_model(current_model) or ARGUS_AGENT_MODEL_DEFAULT]
    out["models"] = models or [{"id": out["availableModels"][0]}]
    return out


def _fetch_model_catalog_sync(*, models_url: str, api_key: str) -> list[dict[str, Any]]:
    if not models_url or not api_key:
        raise RuntimeError("Model catalog target is not configured")
    try:
        response = requests.get(
            models_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=(10, 30),
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Upstream model request failed: {e}") from e

    try:
        payload = response.json()
    except ValueError as e:
        raise RuntimeError(f"Upstream model response is not valid JSON ({response.status_code})") from e

    if response.status_code >= 400:
        detail = _model_catalog_error_detail(payload)
        if detail:
            raise RuntimeError(f"Upstream model request failed ({response.status_code}): {detail}")
        raise RuntimeError(f"Upstream model request failed ({response.status_code})")

    models = _parse_model_catalog_payload(payload)
    if not models:
        raise RuntimeError("Upstream returned no usable models")
    return models


def _effective_agent_model(agent: Optional["PersistedAgentRuntime"]) -> str:
    if isinstance(agent, PersistedAgentRuntime):
        model = _normalize_agent_model(getattr(agent, "model", None))
        if model:
            return model
    return ARGUS_AGENT_MODEL_DEFAULT


@dataclass
class PersistedAgentRuntime:
    agent_id: str
    session_id: str
    workspace_host_path: str
    created_at_ms: int
    owner_user_id: Optional[int] = None
    short_name: Optional[str] = None
    allowed_user_ids: list[int] = field(default_factory=list)
    model: str = ARGUS_AGENT_MODEL_DEFAULT

    def to_json(self) -> dict[str, Any]:
        return {
            "agentId": self.agent_id,
            "sessionId": self.session_id,
            "workspaceHostPath": self.workspace_host_path,
            "createdAtMs": int(self.created_at_ms),
            "ownerUserId": int(self.owner_user_id) if isinstance(self.owner_user_id, int) else None,
            "shortName": self.short_name,
            "allowedUserIds": list(self.allowed_user_ids or []),
            "model": _effective_agent_model(self),
        }

    @staticmethod
    def from_json(obj: Any) -> Optional["PersistedAgentRuntime"]:
        if not isinstance(obj, dict):
            return None
        agent_id = _normalize_agent_id(obj.get("agentId"))
        session_id = str(obj.get("sessionId") or "").strip()
        workspace_host_path = str(obj.get("workspaceHostPath") or "").strip()
        created_at_ms = obj.get("createdAtMs")
        owner_user_id = None
        owner_raw = obj.get("ownerUserId")
        if owner_raw is not None:
            try:
                owner_int = int(owner_raw)
                if owner_int > 0:
                    owner_user_id = owner_int
            except Exception:
                owner_user_id = None
        short_name = str(obj.get("shortName") or "").strip().lower() or None
        model = _normalize_agent_model(obj.get("model")) or ARGUS_AGENT_MODEL_DEFAULT
        allowed_user_ids: list[int] = []
        allowed_raw = obj.get("allowedUserIds")
        if isinstance(allowed_raw, list):
            seen: set[int] = set()
            for x in allowed_raw:
                try:
                    n = int(x)
                except Exception:
                    continue
                if n <= 0 or n in seen:
                    continue
                seen.add(n)
                allowed_user_ids.append(n)
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
            owner_user_id=owner_user_id,
            short_name=short_name,
            allowed_user_ids=allowed_user_ids,
            model=model,
        )


@dataclass
class PersistedSessionAutomation:
    main_thread_id: Optional[str] = None
    cron_jobs: list[PersistedCronJob] = field(default_factory=list)
    cron_runs_by_job: dict[str, list[PersistedCronRunMeta]] = field(default_factory=dict)
    system_event_queues: dict[str, list[PersistedSystemEvent]] = field(default_factory=dict)
    last_active_by_thread: dict[str, PersistedLastActiveTarget] = field(default_factory=dict)
    pending_telegram_attachments_by_chat: dict[str, list[PersistedStagedAttachment]] = field(default_factory=dict)
    paused_node_ids: list[str] = field(default_factory=list)

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
        pending_attachments: dict[str, Any] = {}
        for chat_key, attachments in self.pending_telegram_attachments_by_chat.items():
            if not isinstance(chat_key, str) or not chat_key.strip():
                continue
            if not isinstance(attachments, list) or not attachments:
                continue
            out_attachments = [a.to_json() for a in attachments if isinstance(a, PersistedStagedAttachment)]
            if out_attachments:
                pending_attachments[chat_key.strip()] = out_attachments
        paused_node_ids: list[str] = []
        seen_node_ids: set[str] = set()
        for node_id in self.paused_node_ids:
            if not isinstance(node_id, str) or not node_id.strip():
                continue
            node_id_norm = node_id.strip()
            if node_id_norm in seen_node_ids:
                continue
            seen_node_ids.add(node_id_norm)
            paused_node_ids.append(node_id_norm)
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
            "pendingTelegramAttachmentsByChat": pending_attachments,
            "pausedNodeIds": paused_node_ids,
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
        pending_attachments_raw = obj.get("pendingTelegramAttachmentsByChat")
        pending_telegram_attachments_by_chat: dict[str, list[PersistedStagedAttachment]] = {}
        if isinstance(pending_attachments_raw, dict):
            for chat_key, raw_attachments in pending_attachments_raw.items():
                if not isinstance(chat_key, str) or not chat_key.strip():
                    continue
                if not isinstance(raw_attachments, list):
                    continue
                attachments: list[PersistedStagedAttachment] = []
                for raw_attachment in raw_attachments:
                    attachment = PersistedStagedAttachment.from_json(raw_attachment)
                    if attachment is not None:
                        attachments.append(attachment)
                if attachments:
                    pending_telegram_attachments_by_chat[chat_key.strip()] = attachments
        paused_node_ids_raw = obj.get("pausedNodeIds")
        paused_node_ids: list[str] = []
        if isinstance(paused_node_ids_raw, list):
            seen_node_ids: set[str] = set()
            for node_id in paused_node_ids_raw:
                if not isinstance(node_id, str) or not node_id.strip():
                    continue
                node_id_norm = node_id.strip()
                if node_id_norm in seen_node_ids:
                    continue
                seen_node_ids.add(node_id_norm)
                paused_node_ids.append(node_id_norm)
        return PersistedSessionAutomation(
            main_thread_id=main_thread_id.strip() if isinstance(main_thread_id, str) else None,
            cron_jobs=cron_jobs,
            cron_runs_by_job=cron_runs_by_job,
            system_event_queues=queues,
            last_active_by_thread=last_active_by_thread,
            pending_telegram_attachments_by_chat=pending_telegram_attachments_by_chat,
            paused_node_ids=paused_node_ids,
        )


@dataclass
class PersistedUserChannel:
    channel_id: str
    name: str
    base_url: str
    api_key: Optional[str] = None
    created_at_ms: int = field(default_factory=_now_ms)
    updated_at_ms: int = field(default_factory=_now_ms)

    def to_json(self, *, redact_secrets: bool = False) -> dict[str, Any]:
        api_key = _normalize_channel_api_key(self.api_key)
        return {
            "channelId": self.channel_id,
            "name": self.name,
            "baseUrl": self.base_url,
            "apiKey": _mask_secret(api_key) if redact_secrets else api_key,
            "createdAtMs": int(self.created_at_ms),
            "updatedAtMs": int(self.updated_at_ms),
        }

    @staticmethod
    def from_json(obj: Any) -> Optional["PersistedUserChannel"]:
        if not isinstance(obj, dict):
            return None
        channel_id = _normalize_channel_id(obj.get("channelId"))
        if not channel_id or channel_id in CHANNEL_IDS_BUILTIN:
            return None
        name = _normalize_channel_name(obj.get("name")) or channel_id
        base_url = _normalize_openai_base_url(obj.get("baseUrl"))
        if not base_url:
            return None
        api_key = _normalize_channel_api_key(obj.get("apiKey"))
        try:
            created_at_ms = int(obj.get("createdAtMs")) if obj.get("createdAtMs") is not None else _now_ms()
        except Exception:
            created_at_ms = _now_ms()
        try:
            updated_at_ms = int(obj.get("updatedAtMs")) if obj.get("updatedAtMs") is not None else created_at_ms
        except Exception:
            updated_at_ms = created_at_ms
        if created_at_ms <= 0:
            created_at_ms = _now_ms()
        if updated_at_ms <= 0:
            updated_at_ms = created_at_ms
        return PersistedUserChannel(
            channel_id=channel_id,
            name=name,
            base_url=base_url,
            api_key=api_key,
            created_at_ms=created_at_ms,
            updated_at_ms=updated_at_ms,
        )


@dataclass
class PersistedUserChannelState:
    current_channel_id: str = CHANNEL_ID_GATEWAY
    custom_channels: dict[str, PersistedUserChannel] = field(default_factory=dict)
    builtin_api_keys: dict[str, str] = field(default_factory=dict)

    def to_json(self, *, redact_secrets: bool = False) -> dict[str, Any]:
        custom_channels: dict[str, Any] = {}
        for channel_id, channel in (self.custom_channels or {}).items():
            if not isinstance(channel, PersistedUserChannel):
                continue
            custom_channels[channel_id] = channel.to_json(redact_secrets=redact_secrets)
        builtin_api_keys: dict[str, Any] = {}
        for channel_id, api_key in (self.builtin_api_keys or {}).items():
            cid = _normalize_channel_id(channel_id)
            if cid not in CHANNEL_IDS_BUILTIN or cid == CHANNEL_ID_GATEWAY:
                continue
            normalized_key = _normalize_channel_api_key(api_key)
            if not normalized_key:
                continue
            builtin_api_keys[cid] = _mask_secret(normalized_key) if redact_secrets else normalized_key
        return {
            "currentChannelId": _normalize_channel_id(self.current_channel_id) or CHANNEL_ID_GATEWAY,
            "customChannels": custom_channels,
            "builtinApiKeys": builtin_api_keys,
        }

    @staticmethod
    def from_json(obj: Any) -> "PersistedUserChannelState":
        if not isinstance(obj, dict):
            return PersistedUserChannelState()
        current_channel_id = _normalize_channel_id(obj.get("currentChannelId")) or CHANNEL_ID_GATEWAY
        custom_channels_raw = obj.get("customChannels")
        custom_channels: dict[str, PersistedUserChannel] = {}
        if isinstance(custom_channels_raw, dict):
            for raw_channel_id, raw_channel in custom_channels_raw.items():
                channel = PersistedUserChannel.from_json(raw_channel)
                channel_id = _normalize_channel_id(raw_channel_id)
                if channel is None:
                    continue
                key = channel_id or channel.channel_id
                if not key or key in CHANNEL_IDS_BUILTIN:
                    continue
                custom_channels[key] = replace(channel, channel_id=key)
        builtin_api_keys_raw = obj.get("builtinApiKeys")
        builtin_api_keys: dict[str, str] = {}
        if isinstance(builtin_api_keys_raw, dict):
            for raw_channel_id, raw_api_key in builtin_api_keys_raw.items():
                channel_id = _normalize_channel_id(raw_channel_id)
                if channel_id not in CHANNEL_IDS_BUILTIN or channel_id == CHANNEL_ID_GATEWAY:
                    continue
                api_key = _normalize_channel_api_key(raw_api_key)
                if not api_key:
                    continue
                builtin_api_keys[channel_id] = api_key
        return PersistedUserChannelState(
            current_channel_id=current_channel_id,
            custom_channels=custom_channels,
            builtin_api_keys=builtin_api_keys,
        )


@dataclass
class PersistedGatewayAutomationState:
    version: int = AUTOMATION_STATE_VERSION
    sessions: dict[str, PersistedSessionAutomation] = field(default_factory=dict)
    agents: dict[str, PersistedAgentRuntime] = field(default_factory=dict)
    chat_bindings: dict[str, str] = field(default_factory=dict)
    user_channels: dict[str, PersistedUserChannelState] = field(default_factory=dict)

    def to_json(self, *, redact_secrets: bool = False) -> dict[str, Any]:
        return {
            "version": int(self.version),
            "sessions": {sid: sess.to_json() for sid, sess in self.sessions.items()},
            "agents": {aid: a.to_json() for aid, a in self.agents.items()},
            "chatBindings": dict(self.chat_bindings),
            "userChannels": {
                user_id: channel_state.to_json(redact_secrets=redact_secrets)
                for user_id, channel_state in (self.user_channels or {}).items()
                if isinstance(user_id, str) and user_id.strip() and isinstance(channel_state, PersistedUserChannelState)
            },
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

        user_channels_raw = obj.get("userChannels")
        user_channels: dict[str, PersistedUserChannelState] = {}
        if isinstance(user_channels_raw, dict):
            for raw_user_id, raw_state in user_channels_raw.items():
                if not isinstance(raw_user_id, str) or not raw_user_id.strip():
                    continue
                try:
                    user_id_int = int(raw_user_id)
                except Exception:
                    continue
                if user_id_int <= 0:
                    continue
                user_channels[str(user_id_int)] = PersistedUserChannelState.from_json(raw_state)

        return PersistedGatewayAutomationState(
            version=version_int,
            sessions=sessions,
            agents=agents,
            chat_bindings=chat_bindings,
            user_channels=user_channels,
        )


class AutomationStateStore:
    def __init__(self, state_path: Path) -> None:
        self._path = state_path
        self._lock = asyncio.Lock()
        self._state = PersistedGatewayAutomationState()
        self._loaded_from_disk_ok: bool = False

    @property
    def state(self) -> PersistedGatewayAutomationState:
        return self._state

    @property
    def loaded_from_disk_ok(self) -> bool:
        return self._loaded_from_disk_ok

    async def load(self) -> PersistedGatewayAutomationState:
        async with self._lock:
            try:
                raw = await asyncio.to_thread(self._path.read_text, "utf-8")
                parsed = json.loads(raw)
                self._state = PersistedGatewayAutomationState.from_json(parsed)
                self._loaded_from_disk_ok = True
            except FileNotFoundError:
                self._state = PersistedGatewayAutomationState()
                self._loaded_from_disk_ok = False
            except Exception:
                log.exception("Failed to load automation state from %s", str(self._path))
                self._state = PersistedGatewayAutomationState()
                self._loaded_from_disk_ok = False
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
    active_turn_kind: Optional[str] = None
    pending_client_turn_kind: Optional[str] = None
    pending_source_channel: Optional[str] = None
    pending_source_chat_key: Optional[str] = None
    cancel_requested: bool = False
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
        self._model_catalog_cache: dict[tuple[int, str], dict[str, Any]] = {}

    async def start(self) -> None:
        await self._store.load()
        await self._migrate_agent_models()
        await self._ensure_agent_model_files()
        await self._gc_orphan_runtime_containers()
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

    def _channel_adjusted_agent_model(self, agent: Optional[PersistedAgentRuntime]) -> str:
        model = _effective_agent_model(agent)
        if not isinstance(agent, PersistedAgentRuntime):
            return model
        owner_user_id = getattr(agent, "owner_user_id", None)
        if not isinstance(owner_user_id, int) or owner_user_id <= 0:
            return model
        user_state = self._get_user_channel_state_for_user(user_id=owner_user_id)
        current_channel_id = self._current_channel_id_for_user_state(user_state=user_state)
        if current_channel_id != CHANNEL_ID_GATEWAY:
            return model
        return model if model in ARGUS_GATEWAY_AGENT_MODELS_SET else ARGUS_AGENT_MODEL_DEFAULT

    def get_effective_model_for_session(self, session_id: str) -> str:
        sid = session_id.strip() if isinstance(session_id, str) else ""
        if not sid:
            return ARGUS_AGENT_MODEL_DEFAULT
        st = self._store.state
        for agent in (st.agents or {}).values():
            if not isinstance(agent, PersistedAgentRuntime):
                continue
            if agent.session_id == sid:
                return self._channel_adjusted_agent_model(agent)
        return ARGUS_AGENT_MODEL_DEFAULT

    def resolve_agent_for_chat_key(self, chat_key: str) -> str:
        st = self._store.state
        if not isinstance(st.chat_bindings, dict):
            return ""
        for lookup_key in _chat_binding_lookup_keys(chat_key):
            agent_id = st.chat_bindings.get(lookup_key)
            agent_id = _normalize_agent_id(agent_id) if isinstance(agent_id, str) else ""
            if agent_id:
                return agent_id
        return ""

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
        return self.resolve_agent_session_id(aid)

    def default_node_id_for_session(self, session_id: str) -> Optional[str]:
        sid = session_id.strip() if isinstance(session_id, str) else ""
        if not sid:
            return None
        return f"runtime:{sid}"

    def get_paused_node_ids_for_session(self, session_id: str) -> list[str]:
        sid = session_id.strip() if isinstance(session_id, str) else ""
        if not sid:
            return []
        default_node_id = self.default_node_id_for_session(sid)
        st = self._store.state
        sess = st.sessions.get(sid) if isinstance(st.sessions, dict) else None
        raw_ids = getattr(sess, "paused_node_ids", None) if isinstance(sess, PersistedSessionAutomation) else None
        if not isinstance(raw_ids, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for node_id in raw_ids:
            if not isinstance(node_id, str) or not node_id.strip():
                continue
            node_id_norm = node_id.strip()
            if default_node_id and node_id_norm == default_node_id:
                continue
            if node_id_norm in seen:
                continue
            seen.add(node_id_norm)
            out.append(node_id_norm)
        return out

    def is_node_paused(self, *, session_id: str, node_id: str) -> bool:
        sid = session_id.strip() if isinstance(session_id, str) else ""
        nid = node_id.strip() if isinstance(node_id, str) else ""
        if not sid or not nid:
            return False
        default_node_id = self.default_node_id_for_session(sid)
        if default_node_id and nid == default_node_id:
            return False
        return nid in self.get_paused_node_ids_for_session(sid)

    async def set_node_paused(self, *, session_id: str, node_id: str, paused: bool) -> bool:
        sid = session_id.strip() if isinstance(session_id, str) else ""
        nid = node_id.strip() if isinstance(node_id, str) else ""
        if not sid:
            raise ValueError("session_id must not be empty")
        if not nid:
            raise ValueError("node_id must not be empty")
        default_node_id = self.default_node_id_for_session(sid)
        if paused and default_node_id and nid == default_node_id:
            raise ValueError("Default node cannot be paused")

        changed = False

        def _write(st: PersistedGatewayAutomationState) -> None:
            nonlocal changed
            st.version = max(int(getattr(st, "version", 1) or 1), AUTOMATION_STATE_VERSION)
            sess = st.sessions.get(sid)
            if sess is None:
                sess = PersistedSessionAutomation()
                st.sessions[sid] = sess
            current: list[str] = []
            seen: set[str] = set()
            for existing in getattr(sess, "paused_node_ids", []) or []:
                if not isinstance(existing, str) or not existing.strip():
                    continue
                existing_norm = existing.strip()
                if default_node_id and existing_norm == default_node_id:
                    continue
                if existing_norm in seen:
                    continue
                seen.add(existing_norm)
                current.append(existing_norm)
            if paused:
                if nid not in seen:
                    current.append(nid)
                    changed = True
            else:
                next_current = [existing for existing in current if existing != nid]
                if len(next_current) != len(current):
                    current = next_current
                    changed = True
            sess.paused_node_ids = current

        await self._store.update(_write)
        return changed

    def _can_user_access_agent(self, *, user_id: int, agent: PersistedAgentRuntime) -> bool:
        if not isinstance(user_id, int) or user_id <= 0:
            return False
        if isinstance(agent.owner_user_id, int) and agent.owner_user_id == user_id:
            return True
        allowed = agent.allowed_user_ids if isinstance(agent.allowed_user_ids, list) else []
        return user_id in allowed

    def _user_main_agent_id(self, *, user_id: int) -> str:
        return _user_agent_id(user_id=user_id, short_name="main")

    def list_agents_for_user(self, *, user_id: int) -> list[dict[str, Any]]:
        st = self._store.state
        out: list[dict[str, Any]] = []
        main_id = self._user_main_agent_id(user_id=user_id)
        prefix = f"u{user_id}-"
        for aid, agent in (st.agents or {}).items():
            if not isinstance(agent, PersistedAgentRuntime):
                continue
            if not self._can_user_access_agent(user_id=user_id, agent=agent):
                continue
            short = agent.short_name
            if not short:
                if isinstance(agent.agent_id, str) and agent.agent_id.startswith(prefix):
                    short = agent.agent_id[len(prefix) :].strip() or None
            entry = agent.to_json()
            entry["model"] = self._channel_adjusted_agent_model(agent)
            entry["isOwner"] = bool(isinstance(agent.owner_user_id, int) and agent.owner_user_id == user_id)
            entry["isDefault"] = bool(aid == main_id)
            if short:
                entry["shortName"] = short
            out.append(entry)
        out.sort(key=lambda x: (not bool(x.get("isDefault")), not bool(x.get("isOwner")), str(x.get("agentId") or "")))
        return out

    def _get_agent(self, agent_id: str) -> Optional[PersistedAgentRuntime]:
        agent = self._store.state.agents.get(agent_id)
        return agent if isinstance(agent, PersistedAgentRuntime) else None

    def get_owner_user_id_for_session(self, session_id: str) -> Optional[int]:
        sid = session_id.strip() if isinstance(session_id, str) else ""
        if not sid:
            return None
        st = self._store.state
        for agent in (st.agents or {}).values():
            if not isinstance(agent, PersistedAgentRuntime):
                continue
            if agent.session_id != sid:
                continue
            owner_user_id = getattr(agent, "owner_user_id", None)
            if isinstance(owner_user_id, int) and owner_user_id > 0:
                return owner_user_id
            return None
        return None

    def _get_user_channel_state_for_user(self, *, user_id: int) -> PersistedUserChannelState:
        if not isinstance(user_id, int) or user_id <= 0:
            return PersistedUserChannelState()
        st = self._store.state
        raw = st.user_channels.get(str(user_id)) if isinstance(getattr(st, "user_channels", None), dict) else None
        return raw if isinstance(raw, PersistedUserChannelState) else PersistedUserChannelState()

    def _resolve_user_channel_id(self, *, user_id: int, raw_channel: Any) -> str:
        user_state = self._get_user_channel_state_for_user(user_id=user_id)
        channel_id = _normalize_channel_id(raw_channel)
        if channel_id in CHANNEL_IDS_BUILTIN:
            return channel_id
        if channel_id and channel_id in (user_state.custom_channels or {}):
            return channel_id
        channel_name = _normalize_channel_name(raw_channel)
        if not channel_name:
            return ""
        if channel_name in CHANNEL_IDS_BUILTIN:
            return channel_name
        for channel in (user_state.custom_channels or {}).values():
            if not isinstance(channel, PersistedUserChannel):
                continue
            if channel.name == channel_name:
                return channel.channel_id
        return ""

    def _current_channel_id_for_user_state(self, *, user_state: PersistedUserChannelState) -> str:
        channel_id = _normalize_channel_id(getattr(user_state, "current_channel_id", None))
        if channel_id in CHANNEL_IDS_BUILTIN:
            return channel_id
        if channel_id and channel_id in (user_state.custom_channels or {}):
            return channel_id
        return CHANNEL_ID_GATEWAY

    def _iter_custom_channels_for_user_state(self, *, user_state: PersistedUserChannelState) -> list[PersistedUserChannel]:
        out: list[PersistedUserChannel] = []
        for channel in (user_state.custom_channels or {}).values():
            if isinstance(channel, PersistedUserChannel):
                out.append(channel)
        out.sort(key=lambda item: (str(item.name or item.channel_id), int(item.created_at_ms)))
        return out

    def _channel_entry_for_user_state(
        self,
        *,
        user_state: PersistedUserChannelState,
        channel_id: str,
        current_channel_id: Optional[str],
    ) -> dict[str, Any]:
        current_id = _normalize_channel_id(current_channel_id)
        normalized_channel_id = _normalize_channel_id(channel_id)
        if normalized_channel_id == CHANNEL_ID_GATEWAY:
            api_key = _gateway_openai_api_key()
            ready = bool(api_key)
            base_url = _base_url_from_responses_url(_gateway_openai_responses_upstream_url())
            return {
                "channelId": CHANNEL_ID_GATEWAY,
                "name": CHANNEL_ID_GATEWAY,
                "kind": "builtin",
                "builtinKind": "gateway",
                "isBuiltin": True,
                "selected": current_id == CHANNEL_ID_GATEWAY,
                "baseUrl": base_url,
                "responsesUrl": _gateway_openai_responses_upstream_url(),
                "modelsUrl": _models_url_from_base_url(base_url),
                "hasApiKey": bool(api_key),
                "apiKeyMasked": None,
                "ready": ready,
                "reason": None if ready else "Gateway OPENAI_API_KEY is not configured",
                "canRename": False,
                "canDelete": False,
                "canSetKey": False,
                "canClearKey": False,
                "websiteUrl": None,
            }
        if normalized_channel_id == CHANNEL_ID_ZERO_ZERO_PRO:
            api_key = _normalize_channel_api_key((user_state.builtin_api_keys or {}).get(CHANNEL_ID_ZERO_ZERO_PRO))
            ready = bool(api_key)
            responses_url = _responses_url_from_base_url(CHANNEL_ZERO_ZERO_PRO_BASE_URL)
            return {
                "channelId": CHANNEL_ID_ZERO_ZERO_PRO,
                "name": CHANNEL_ID_ZERO_ZERO_PRO,
                "kind": "builtin",
                "builtinKind": "0-0.pro",
                "isBuiltin": True,
                "selected": current_id == CHANNEL_ID_ZERO_ZERO_PRO,
                "baseUrl": CHANNEL_ZERO_ZERO_PRO_BASE_URL,
                "responsesUrl": responses_url,
                "modelsUrl": _models_url_from_base_url(CHANNEL_ZERO_ZERO_PRO_BASE_URL),
                "hasApiKey": bool(api_key),
                "apiKeyMasked": _mask_secret(api_key),
                "ready": ready,
                "reason": None if ready else "Missing apiKey",
                "canRename": False,
                "canDelete": False,
                "canSetKey": True,
                "canClearKey": bool(api_key),
                "websiteUrl": CHANNEL_ZERO_ZERO_PRO_PROMO_URL,
            }
        channel = (user_state.custom_channels or {}).get(normalized_channel_id)
        if not isinstance(channel, PersistedUserChannel):
            raise ValueError(f"Unknown channel: {channel_id}")
        api_key = _normalize_channel_api_key(channel.api_key)
        ready = bool(channel.base_url and api_key)
        missing: list[str] = []
        if not channel.base_url:
            missing.append("baseUrl")
        if not api_key:
            missing.append("apiKey")
        return {
            "channelId": channel.channel_id,
            "name": channel.name,
            "kind": "custom",
            "builtinKind": None,
            "isBuiltin": False,
            "selected": current_id == channel.channel_id,
            "baseUrl": channel.base_url,
            "responsesUrl": _responses_url_from_base_url(channel.base_url),
            "modelsUrl": _models_url_from_base_url(channel.base_url),
            "hasApiKey": bool(api_key),
            "apiKeyMasked": _mask_secret(api_key),
            "ready": ready,
            "reason": None if ready else f"Missing {'/'.join(missing)}",
            "canRename": True,
            "canDelete": True,
            "canSetKey": True,
            "canClearKey": bool(api_key),
            "websiteUrl": None,
        }

    def _best_channel_fallback_id_for_user_state(self, *, user_state: PersistedUserChannelState) -> str:
        if self._channel_entry_for_user_state(
            user_state=user_state,
            channel_id=CHANNEL_ID_GATEWAY,
            current_channel_id=None,
        ).get("ready"):
            return CHANNEL_ID_GATEWAY
        if self._channel_entry_for_user_state(
            user_state=user_state,
            channel_id=CHANNEL_ID_ZERO_ZERO_PRO,
            current_channel_id=None,
        ).get("ready"):
            return CHANNEL_ID_ZERO_ZERO_PRO
        for channel in self._iter_custom_channels_for_user_state(user_state=user_state):
            if self._channel_entry_for_user_state(
                user_state=user_state,
                channel_id=channel.channel_id,
                current_channel_id=None,
            ).get("ready"):
                return channel.channel_id
        return CHANNEL_ID_GATEWAY

    def list_channels_for_user(self, *, user_id: int) -> dict[str, Any]:
        user_state = self._get_user_channel_state_for_user(user_id=user_id)
        current_channel_id = self._current_channel_id_for_user_state(user_state=user_state)
        channels: list[dict[str, Any]] = [
            self._channel_entry_for_user_state(
                user_state=user_state,
                channel_id=CHANNEL_ID_GATEWAY,
                current_channel_id=current_channel_id,
            ),
            self._channel_entry_for_user_state(
                user_state=user_state,
                channel_id=CHANNEL_ID_ZERO_ZERO_PRO,
                current_channel_id=current_channel_id,
            ),
        ]
        for channel in self._iter_custom_channels_for_user_state(user_state=user_state):
            channels.append(
                self._channel_entry_for_user_state(
                    user_state=user_state,
                    channel_id=channel.channel_id,
                    current_channel_id=current_channel_id,
                )
            )
        current_channel = next((dict(item) for item in channels if item.get("channelId") == current_channel_id), None)
        return {
            "ok": True,
            "userId": user_id,
            "currentChannelId": current_channel_id,
            "currentChannel": current_channel,
            "channels": channels,
        }

    def get_current_channel_for_user(self, *, user_id: int) -> Optional[dict[str, Any]]:
        listed = self.list_channels_for_user(user_id=user_id)
        current_channel = listed.get("currentChannel")
        return dict(current_channel) if isinstance(current_channel, dict) else None

    def _invalidate_model_catalog_cache(self, *, user_id: Optional[int] = None, channel_id: Optional[str] = None) -> None:
        if not self._model_catalog_cache:
            return
        normalized_channel_id = _normalize_channel_id(channel_id) if channel_id is not None else None
        for key_user_id, key_channel_id in list(self._model_catalog_cache.keys()):
            if user_id is not None and key_user_id != user_id:
                continue
            if normalized_channel_id is not None and key_channel_id != normalized_channel_id:
                continue
            self._model_catalog_cache.pop((key_user_id, key_channel_id), None)

    def _resolve_model_catalog_target_for_user(
        self,
        *,
        user_id: int,
        channel_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("Invalid userId")
        user_state = self._get_user_channel_state_for_user(user_id=user_id)
        current_channel_id = self._current_channel_id_for_user_state(user_state=user_state)
        resolved_channel_id = (
            current_channel_id if channel_id is None else self._resolve_user_channel_id(user_id=user_id, raw_channel=channel_id)
        )
        if not resolved_channel_id:
            raise ValueError(f"Unknown channel: {channel_id}")
        channel_entry = self._channel_entry_for_user_state(
            user_state=user_state,
            channel_id=resolved_channel_id,
            current_channel_id=current_channel_id,
        )
        if not bool(channel_entry.get("ready")):
            raise RuntimeError(str(channel_entry.get("reason") or f"Channel is not ready: {resolved_channel_id}"))

        if resolved_channel_id == CHANNEL_ID_GATEWAY:
            api_key = _gateway_openai_api_key()
            base_url = _base_url_from_responses_url(_gateway_openai_responses_upstream_url())
            responses_url = _gateway_openai_responses_upstream_url()
        elif resolved_channel_id == CHANNEL_ID_ZERO_ZERO_PRO:
            api_key = _normalize_channel_api_key((user_state.builtin_api_keys or {}).get(CHANNEL_ID_ZERO_ZERO_PRO))
            base_url = CHANNEL_ZERO_ZERO_PRO_BASE_URL
            responses_url = _responses_url_from_base_url(base_url)
        else:
            channel = (user_state.custom_channels or {}).get(resolved_channel_id)
            if not isinstance(channel, PersistedUserChannel):
                raise RuntimeError(f"Unknown channel: {resolved_channel_id}")
            api_key = _normalize_channel_api_key(channel.api_key)
            base_url = channel.base_url
            responses_url = _responses_url_from_base_url(base_url)

        if not api_key:
            raise RuntimeError(f"Channel '{channel_entry.get('name') or resolved_channel_id}' is missing apiKey")
        models_url = _models_url_from_base_url(base_url)
        if not models_url:
            raise RuntimeError(f"Channel '{channel_entry.get('name') or resolved_channel_id}' has no usable models URL")
        return {
            "ownerUserId": user_id,
            "channelId": resolved_channel_id,
            "name": str(channel_entry.get("name") or resolved_channel_id),
            "baseUrl": base_url,
            "responsesUrl": responses_url,
            "modelsUrl": models_url,
            "apiKey": api_key,
            "channel": dict(channel_entry),
        }

    async def get_available_models_for_user(
        self,
        *,
        user_id: int,
        current_model: Optional[str] = None,
        channel_id: Optional[str] = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        normalized_current_model = _normalize_agent_model(current_model)
        if not isinstance(user_id, int) or user_id <= 0:
            return _fallback_model_catalog(current_model=normalized_current_model, error="Invalid userId")

        user_state = self._get_user_channel_state_for_user(user_id=user_id)
        current_channel_id = self._current_channel_id_for_user_state(user_state=user_state)
        resolved_channel_id = (
            current_channel_id if channel_id is None else self._resolve_user_channel_id(user_id=user_id, raw_channel=channel_id)
        )
        if not resolved_channel_id:
            channel = self.get_current_channel_for_user(user_id=user_id) if channel_id is None else None
            return _fallback_model_catalog(
                current_model=normalized_current_model,
                error=f"Unknown channel: {channel_id}",
                channel=channel,
            )

        channel_entry = self._channel_entry_for_user_state(
            user_state=user_state,
            channel_id=resolved_channel_id,
            current_channel_id=current_channel_id,
        )
        if resolved_channel_id == CHANNEL_ID_GATEWAY:
            return _static_model_catalog(
                models=ARGUS_GATEWAY_AGENT_MODELS,
                source="static",
                channel=channel_entry,
            )

        try:
            target = self._resolve_model_catalog_target_for_user(user_id=user_id, channel_id=channel_id)
        except Exception as e:
            return _fallback_model_catalog(
                current_model=normalized_current_model,
                error=str(e),
                channel=channel_entry,
            )

        cache_key = (user_id, str(target.get("channelId") or ""))
        now_ms = _now_ms()
        cached = None if force_refresh else self._model_catalog_cache.get(cache_key)
        if isinstance(cached, dict):
            try:
                fetched_at_ms = int(cached.get("fetchedAtMs") or 0)
            except Exception:
                fetched_at_ms = 0
            if fetched_at_ms > 0 and now_ms - fetched_at_ms <= ARGUS_MODEL_CATALOG_CACHE_TTL_MS:
                return _merge_current_model_into_catalog(cached, normalized_current_model)

        try:
            models = await asyncio.to_thread(
                _fetch_model_catalog_sync,
                models_url=str(target.get("modelsUrl") or ""),
                api_key=str(target.get("apiKey") or ""),
            )
        except Exception as e:
            fallback = _fallback_model_catalog(
                current_model=normalized_current_model,
                error=str(e),
                channel=target.get("channel") if isinstance(target.get("channel"), dict) else None,
            )
            self._model_catalog_cache[cache_key] = dict(fallback)
            return fallback

        catalog = {
            "availableModels": [str(item.get("id") or "").strip() for item in models if isinstance(item, dict)],
            "models": models,
            "source": "upstream",
            "fetchedAtMs": now_ms,
            "error": None,
            "channel": dict(target.get("channel")) if isinstance(target.get("channel"), dict) else None,
        }
        self._model_catalog_cache[cache_key] = dict(catalog)
        return _merge_current_model_into_catalog(catalog, normalized_current_model)

    def resolve_openai_proxy_target_for_session(self, session_id: str) -> dict[str, Any]:
        sid = session_id.strip() if isinstance(session_id, str) else ""
        if not sid:
            raise ValueError("Missing sessionId")
        owner_user_id = self.get_owner_user_id_for_session(sid)
        if owner_user_id is None:
            api_key = _gateway_openai_api_key()
            if not api_key:
                raise RuntimeError("Gateway channel is not configured (missing OPENAI_API_KEY)")
            base_url = _base_url_from_responses_url(_gateway_openai_responses_upstream_url())
            return {
                "ownerUserId": None,
                "channelId": CHANNEL_ID_GATEWAY,
                "name": CHANNEL_ID_GATEWAY,
                "baseUrl": base_url,
                "upstreamUrl": _gateway_openai_responses_upstream_url(),
                "modelsUrl": _models_url_from_base_url(base_url),
                "apiKey": api_key,
            }
        target = self._resolve_model_catalog_target_for_user(user_id=owner_user_id)
        upstream_url = str(target.get("responsesUrl") or "").strip()
        if not upstream_url:
            raise RuntimeError(f"Current channel '{target.get('channelId')}' has no usable upstream URL")
        return {
            "ownerUserId": owner_user_id,
            "channelId": str(target.get("channelId") or ""),
            "name": str(target.get("name") or target.get("channelId") or ""),
            "baseUrl": str(target.get("baseUrl") or ""),
            "upstreamUrl": upstream_url,
            "modelsUrl": str(target.get("modelsUrl") or ""),
            "apiKey": target.get("apiKey"),
        }

    async def create_user_channel(self, *, user_id: int, name: str, base_url: str, api_key: str) -> PersistedUserChannel:
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("Invalid userId")
        channel_name = _normalize_channel_name(name)
        if not channel_name:
            raise ValueError("Invalid channel name")
        if channel_name in CHANNEL_IDS_BUILTIN:
            raise ValueError(f"Channel already exists: {channel_name}")
        normalized_base_url = _normalize_openai_base_url(base_url)
        if not normalized_base_url:
            raise ValueError("Invalid baseUrl")
        normalized_api_key = _normalize_channel_api_key(api_key)
        if not normalized_api_key:
            raise ValueError("Missing apiKey")

        created: Optional[PersistedUserChannel] = None

        def _write(st: PersistedGatewayAutomationState) -> None:
            nonlocal created
            st.version = max(int(getattr(st, "version", 1) or 1), AUTOMATION_STATE_VERSION)
            user_key = str(user_id)
            user_state = st.user_channels.get(user_key)
            if not isinstance(user_state, PersistedUserChannelState):
                user_state = PersistedUserChannelState()
                st.user_channels[user_key] = user_state
            for existing in (user_state.custom_channels or {}).values():
                if isinstance(existing, PersistedUserChannel) and existing.name == channel_name:
                    raise ValueError(f"Channel already exists: {channel_name}")
            channel_id = _new_custom_channel_id()
            while channel_id in CHANNEL_IDS_BUILTIN or channel_id in (user_state.custom_channels or {}):
                channel_id = _new_custom_channel_id()
            now = _now_ms()
            created = PersistedUserChannel(
                channel_id=channel_id,
                name=channel_name,
                base_url=normalized_base_url,
                api_key=normalized_api_key,
                created_at_ms=now,
                updated_at_ms=now,
            )
            user_state.custom_channels[channel_id] = created
            if not _normalize_channel_id(getattr(user_state, "current_channel_id", None)):
                user_state.current_channel_id = CHANNEL_ID_GATEWAY

        await self._store.update(_write)
        if created is None:
            raise RuntimeError("Channel was not created")
        self._invalidate_model_catalog_cache(user_id=user_id)
        return created

    async def rename_user_channel(self, *, user_id: int, channel_id: str, new_name: str) -> PersistedUserChannel:
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("Invalid userId")
        resolved_channel_id = self._resolve_user_channel_id(user_id=user_id, raw_channel=channel_id)
        if not resolved_channel_id:
            raise ValueError(f"Unknown channel: {channel_id}")
        if resolved_channel_id in CHANNEL_IDS_BUILTIN:
            raise ValueError("Builtin channel cannot be renamed")
        normalized_name = _normalize_channel_name(new_name)
        if not normalized_name:
            raise ValueError("Invalid channel name")
        if normalized_name in CHANNEL_IDS_BUILTIN:
            raise ValueError(f"Channel already exists: {normalized_name}")

        updated: Optional[PersistedUserChannel] = None

        def _write(st: PersistedGatewayAutomationState) -> None:
            nonlocal updated
            st.version = max(int(getattr(st, "version", 1) or 1), AUTOMATION_STATE_VERSION)
            user_state = st.user_channels.get(str(user_id))
            if not isinstance(user_state, PersistedUserChannelState):
                raise ValueError(f"Unknown channel: {channel_id}")
            channel = (user_state.custom_channels or {}).get(resolved_channel_id)
            if not isinstance(channel, PersistedUserChannel):
                raise ValueError(f"Unknown channel: {channel_id}")
            for existing in (user_state.custom_channels or {}).values():
                if not isinstance(existing, PersistedUserChannel):
                    continue
                if existing.channel_id != resolved_channel_id and existing.name == normalized_name:
                    raise ValueError(f"Channel already exists: {normalized_name}")
            if channel.name == normalized_name:
                updated = channel
                return
            updated = replace(channel, name=normalized_name, updated_at_ms=_now_ms())
            user_state.custom_channels[resolved_channel_id] = updated

        await self._store.update(_write)
        if updated is None:
            raise RuntimeError("Channel was not renamed")
        self._invalidate_model_catalog_cache(user_id=user_id, channel_id=resolved_channel_id)
        return updated

    async def delete_user_channel(self, *, user_id: int, channel_id: str) -> dict[str, Any]:
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("Invalid userId")
        resolved_channel_id = self._resolve_user_channel_id(user_id=user_id, raw_channel=channel_id)
        if not resolved_channel_id:
            raise ValueError(f"Unknown channel: {channel_id}")
        if resolved_channel_id in CHANNEL_IDS_BUILTIN:
            raise ValueError("Builtin channel cannot be deleted")

        deleted: Optional[PersistedUserChannel] = None

        def _write(st: PersistedGatewayAutomationState) -> None:
            nonlocal deleted
            st.version = max(int(getattr(st, "version", 1) or 1), AUTOMATION_STATE_VERSION)
            user_state = st.user_channels.get(str(user_id))
            if not isinstance(user_state, PersistedUserChannelState):
                raise ValueError(f"Unknown channel: {channel_id}")
            selected_before = _normalize_channel_id(getattr(user_state, "current_channel_id", None)) == resolved_channel_id
            deleted = (user_state.custom_channels or {}).pop(resolved_channel_id, None)
            if not isinstance(deleted, PersistedUserChannel):
                raise ValueError(f"Unknown channel: {channel_id}")
            if selected_before:
                user_state.current_channel_id = self._best_channel_fallback_id_for_user_state(user_state=user_state)

        await self._store.update(_write)
        self._invalidate_model_catalog_cache(user_id=user_id, channel_id=resolved_channel_id)
        await self._sync_user_agent_models_for_current_channel(user_id=user_id)
        listed = self.list_channels_for_user(user_id=user_id)
        return {
            "deletedChannelId": resolved_channel_id,
            "deletedName": deleted.name if isinstance(deleted, PersistedUserChannel) else None,
            "currentChannelId": listed.get("currentChannelId"),
            "currentChannel": listed.get("currentChannel"),
        }

    async def select_user_channel(self, *, user_id: int, channel_id: str) -> dict[str, Any]:
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("Invalid userId")
        resolved_channel_id = self._resolve_user_channel_id(user_id=user_id, raw_channel=channel_id)
        if not resolved_channel_id:
            raise ValueError(f"Unknown channel: {channel_id}")

        def _write(st: PersistedGatewayAutomationState) -> None:
            st.version = max(int(getattr(st, "version", 1) or 1), AUTOMATION_STATE_VERSION)
            user_key = str(user_id)
            user_state = st.user_channels.get(user_key)
            if not isinstance(user_state, PersistedUserChannelState):
                user_state = PersistedUserChannelState()
                st.user_channels[user_key] = user_state
            if resolved_channel_id not in CHANNEL_IDS_BUILTIN and resolved_channel_id not in (user_state.custom_channels or {}):
                raise ValueError(f"Unknown channel: {channel_id}")
            if not self._channel_entry_for_user_state(
                user_state=user_state,
                channel_id=resolved_channel_id,
                current_channel_id=None,
            ).get("ready"):
                raise ValueError(f"Channel is not ready: {resolved_channel_id}")
            user_state.current_channel_id = resolved_channel_id

        await self._store.update(_write)
        self._invalidate_model_catalog_cache(user_id=user_id)
        await self._sync_user_agent_models_for_current_channel(user_id=user_id)
        current_channel = self.get_current_channel_for_user(user_id=user_id)
        return {
            "currentChannelId": resolved_channel_id,
            "currentChannel": current_channel,
        }

    async def set_user_channel_api_key(self, *, user_id: int, channel_id: str, api_key: str) -> dict[str, Any]:
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("Invalid userId")
        resolved_channel_id = self._resolve_user_channel_id(user_id=user_id, raw_channel=channel_id)
        if not resolved_channel_id:
            raise ValueError(f"Unknown channel: {channel_id}")
        normalized_api_key = _normalize_channel_api_key(api_key)
        if not normalized_api_key:
            raise ValueError("Missing apiKey")

        def _write(st: PersistedGatewayAutomationState) -> None:
            st.version = max(int(getattr(st, "version", 1) or 1), AUTOMATION_STATE_VERSION)
            user_key = str(user_id)
            user_state = st.user_channels.get(user_key)
            if not isinstance(user_state, PersistedUserChannelState):
                user_state = PersistedUserChannelState()
                st.user_channels[user_key] = user_state
            if resolved_channel_id == CHANNEL_ID_GATEWAY:
                raise ValueError("Gateway channel key is managed by gateway env")
            if resolved_channel_id == CHANNEL_ID_ZERO_ZERO_PRO:
                user_state.builtin_api_keys[CHANNEL_ID_ZERO_ZERO_PRO] = normalized_api_key
                return
            channel = (user_state.custom_channels or {}).get(resolved_channel_id)
            if not isinstance(channel, PersistedUserChannel):
                raise ValueError(f"Unknown channel: {channel_id}")
            user_state.custom_channels[resolved_channel_id] = replace(
                channel,
                api_key=normalized_api_key,
                updated_at_ms=_now_ms(),
            )

        await self._store.update(_write)
        self._invalidate_model_catalog_cache(user_id=user_id, channel_id=resolved_channel_id)
        await self._sync_user_agent_models_for_current_channel(user_id=user_id)
        listed = self.list_channels_for_user(user_id=user_id)
        current_channel = listed.get("currentChannel")
        selected_channel = next(
            (dict(item) for item in listed.get("channels", []) if isinstance(item, dict) and item.get("channelId") == resolved_channel_id),
            None,
        )
        return {
            "channelId": resolved_channel_id,
            "channel": selected_channel,
            "currentChannel": current_channel,
            "currentChannelId": listed.get("currentChannelId"),
        }

    async def clear_user_channel_api_key(self, *, user_id: int, channel_id: str) -> dict[str, Any]:
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("Invalid userId")
        resolved_channel_id = self._resolve_user_channel_id(user_id=user_id, raw_channel=channel_id)
        if not resolved_channel_id:
            raise ValueError(f"Unknown channel: {channel_id}")
        if resolved_channel_id == CHANNEL_ID_GATEWAY:
            raise ValueError("Gateway channel key is managed by gateway env")

        def _write(st: PersistedGatewayAutomationState) -> None:
            st.version = max(int(getattr(st, "version", 1) or 1), AUTOMATION_STATE_VERSION)
            user_state = st.user_channels.get(str(user_id))
            if not isinstance(user_state, PersistedUserChannelState):
                raise ValueError(f"Unknown channel: {channel_id}")
            selected_before = _normalize_channel_id(getattr(user_state, "current_channel_id", None)) == resolved_channel_id
            if resolved_channel_id == CHANNEL_ID_ZERO_ZERO_PRO:
                if CHANNEL_ID_ZERO_ZERO_PRO not in (user_state.builtin_api_keys or {}):
                    raise ValueError(f"Unknown channel: {channel_id}")
                user_state.builtin_api_keys.pop(CHANNEL_ID_ZERO_ZERO_PRO, None)
            else:
                channel = (user_state.custom_channels or {}).get(resolved_channel_id)
                if not isinstance(channel, PersistedUserChannel):
                    raise ValueError(f"Unknown channel: {channel_id}")
                user_state.custom_channels[resolved_channel_id] = replace(
                    channel,
                    api_key=None,
                    updated_at_ms=_now_ms(),
                )
            if selected_before:
                user_state.current_channel_id = self._best_channel_fallback_id_for_user_state(user_state=user_state)

        await self._store.update(_write)
        self._invalidate_model_catalog_cache(user_id=user_id, channel_id=resolved_channel_id)
        await self._sync_user_agent_models_for_current_channel(user_id=user_id)
        listed = self.list_channels_for_user(user_id=user_id)
        current_channel = listed.get("currentChannel")
        selected_channel = next(
            (dict(item) for item in listed.get("channels", []) if isinstance(item, dict) and item.get("channelId") == resolved_channel_id),
            None,
        )
        return {
            "channelId": resolved_channel_id,
            "channel": selected_channel,
            "currentChannel": current_channel,
            "currentChannelId": listed.get("currentChannelId"),
        }

    async def _migrate_agent_models(self) -> None:
        changed = False

        def _write(st: PersistedGatewayAutomationState) -> None:
            nonlocal changed
            st.version = max(int(getattr(st, "version", 1) or 1), AUTOMATION_STATE_VERSION)
            for aid, agent in list((st.agents or {}).items()):
                if not isinstance(agent, PersistedAgentRuntime):
                    continue
                model = _effective_agent_model(agent)
                if _normalize_agent_model(getattr(agent, "model", None)) == model:
                    continue
                st.agents[aid] = replace(agent, model=model)
                changed = True

        if not any(isinstance(agent, PersistedAgentRuntime) for agent in (self._store.state.agents or {}).values()):
            return
        await self._store.update(_write)
        if changed:
            log.info("Migrated persisted agent models to %s", ARGUS_AGENT_MODEL_DEFAULT)

    def _agent_model_file_for_workspace(self, root: Path) -> Path:
        return root / ".codex" / ARGUS_AGENT_MODEL_FILE

    async def _write_agent_model_file(self, root: Path, model: str) -> None:
        normalized = _normalize_agent_model(model) or ARGUS_AGENT_MODEL_DEFAULT

        def _write() -> None:
            path = self._agent_model_file_for_workspace(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(path, normalized + "\n")

        await asyncio.to_thread(_write)

    async def _sync_user_agent_models_for_current_channel(self, *, user_id: int) -> None:
        if not isinstance(user_id, int) or user_id <= 0:
            return
        for agent in (self._store.state.agents or {}).values():
            if not isinstance(agent, PersistedAgentRuntime):
                continue
            if not (isinstance(agent.owner_user_id, int) and agent.owner_user_id == user_id):
                continue
            workspace_host_path = str(agent.workspace_host_path or "").strip()
            if not workspace_host_path:
                continue
            model = self._channel_adjusted_agent_model(agent)
            try:
                root = Path(workspace_host_path)
                await self._ensure_workspace_files_at(root)
                await self._write_agent_model_file(root, model)
                await _sync_session_workspace_file(
                    agent.session_id,
                    root,
                    self._agent_model_file_for_workspace(root),
                )
                await self._sync_live_session_default_model(session_id=agent.session_id, model=model)
            except Exception as e:
                log.warning(
                    "Failed to sync effective model for agent %s (user=%s, session=%s): %s",
                    agent.agent_id,
                    user_id,
                    str(agent.session_id or ""),
                    str(e),
                )

    async def _ensure_agent_model_files(self) -> None:
        for agent in (self._store.state.agents or {}).values():
            if not isinstance(agent, PersistedAgentRuntime):
                continue
            workspace_host_path = str(agent.workspace_host_path or "").strip()
            if not workspace_host_path:
                continue
            root = Path(workspace_host_path)
            try:
                await self._ensure_workspace_files_at(root)
                await self._write_agent_model_file(root, self._channel_adjusted_agent_model(agent))
                await _sync_session_workspace_snapshot(agent.session_id, root)
            except Exception as e:
                log.warning("Failed to persist model file for agent %s at %s: %s", agent.agent_id, str(root), str(e))

    async def _sync_live_session_default_model(self, *, session_id: str, model: str) -> bool:
        return False

    async def ensure_user_main_agent(self, *, user_id: int) -> tuple[PersistedAgentRuntime, bool]:
        _require_user_agent_support()
        if not self._home_host_path:
            raise RuntimeError("ARGUS_HOME_HOST_PATH is required to create user agents")
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("Invalid userId")

        aid = self._user_main_agent_id(user_id=user_id)
        created_at_ms = _now_ms()
        created = False

        def _write(st: PersistedGatewayAutomationState) -> None:
            nonlocal created
            st.version = max(int(getattr(st, "version", 1) or 1), AUTOMATION_STATE_VERSION)
            existing = st.agents.get(aid)
            if isinstance(existing, PersistedAgentRuntime):
                return
            created = True
            session_id = uuid.uuid4().hex[:12]
            workspace_host_path = str((Path(self._home_host_path) / f"workspace-{user_id}-main").resolve())
            st.agents[aid] = PersistedAgentRuntime(
                agent_id=aid,
                session_id=session_id,
                workspace_host_path=workspace_host_path,
                created_at_ms=created_at_ms,
                owner_user_id=user_id,
                short_name="main",
                allowed_user_ids=[],
                model=ARGUS_AGENT_MODEL_DEFAULT,
            )
            if session_id not in st.sessions:
                st.sessions[session_id] = PersistedSessionAutomation()

        await self._store.update(_write)
        agent = self._get_agent(aid)
        if agent is None:
            raise RuntimeError("Failed to persist user main agent")
        root = Path(agent.workspace_host_path)
        await self._ensure_workspace_files_at(root)
        await self._write_agent_model_file(root, self._channel_adjusted_agent_model(agent))
        await _sync_session_workspace_snapshot(agent.session_id, root)
        await _ensure_live_session(agent.session_id, allow_create=True)
        return agent, created

    async def create_user_agent(self, *, user_id: int, short_name: str) -> PersistedAgentRuntime:
        _require_user_agent_support()
        if not self._home_host_path:
            raise RuntimeError("ARGUS_HOME_HOST_PATH is required to create user agents")
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("Invalid userId")
        name = _normalize_agent_short_name(short_name)
        if not name:
            raise ValueError("Invalid agent name")
        if name == "main":
            raise ValueError("agent name 'main' is reserved")

        aid = _user_agent_id(user_id=user_id, short_name=name)
        if not AGENT_ID_RE.match(aid):
            raise ValueError("Invalid agent name (too long)")

        created_at_ms = _now_ms()

        def _write(st: PersistedGatewayAutomationState) -> None:
            st.version = max(int(getattr(st, "version", 1) or 1), AUTOMATION_STATE_VERSION)
            if aid in st.agents:
                raise ValueError(f"Agent already exists: {name}")
            session_id = uuid.uuid4().hex[:12]
            workspace_host_path = str((Path(self._home_host_path) / f"workspace-{user_id}-{name}").resolve())
            st.agents[aid] = PersistedAgentRuntime(
                agent_id=aid,
                session_id=session_id,
                workspace_host_path=workspace_host_path,
                created_at_ms=created_at_ms,
                owner_user_id=user_id,
                short_name=name,
                allowed_user_ids=[],
                model=ARGUS_AGENT_MODEL_DEFAULT,
            )
            if session_id not in st.sessions:
                st.sessions[session_id] = PersistedSessionAutomation()

        await self._store.update(_write)
        agent = self._get_agent(aid)
        if agent is None:
            raise RuntimeError("Failed to persist user agent")
        root = Path(agent.workspace_host_path)
        await self._ensure_workspace_files_at(root)
        await self._write_agent_model_file(root, self._channel_adjusted_agent_model(agent))
        await _sync_session_workspace_snapshot(agent.session_id, root)
        await _ensure_live_session(agent.session_id, allow_create=True)
        return agent

    async def rename_user_agent(self, *, user_id: int, agent_id: str, new_short_name: str) -> PersistedAgentRuntime:
        _require_user_agent_support()
        if not self._home_host_path:
            raise RuntimeError("ARGUS_HOME_HOST_PATH is required to rename user agents")
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("Invalid userId")

        old_aid = _normalize_agent_id(agent_id)
        if not old_aid:
            raise ValueError("Invalid agentId")
        old_agent = self._get_agent(old_aid)
        if old_agent is None:
            raise ValueError(f"Unknown agent: {old_aid}")
        if not (isinstance(old_agent.owner_user_id, int) and old_agent.owner_user_id == user_id):
            raise PermissionError("Forbidden")
        if old_aid == self._user_main_agent_id(user_id=user_id):
            raise ValueError("main agent cannot be renamed")

        name = _normalize_agent_short_name(new_short_name)
        if not name:
            raise ValueError("Invalid agent name")
        if name == "main":
            raise ValueError("agent name 'main' is reserved")

        new_aid = _user_agent_id(user_id=user_id, short_name=name)
        if not AGENT_ID_RE.match(new_aid):
            raise ValueError("Invalid agent name (too long)")
        if new_aid == old_aid:
            return old_agent

        session_id = str(old_agent.session_id or "").strip()
        if not session_id:
            raise RuntimeError("Agent has no sessionId")

        lock = self._session_restart_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_restart_locks[session_id] = lock

        async with lock:
            home_root = Path(self._home_host_path).resolve()
            new_workspace_host_path = str((home_root / f"workspace-{user_id}-{name}").resolve())

            def _write(st: PersistedGatewayAutomationState) -> None:
                st.version = max(int(getattr(st, "version", 1) or 1), AUTOMATION_STATE_VERSION)
                agent = st.agents.get(old_aid)
                if not isinstance(agent, PersistedAgentRuntime):
                    raise ValueError(f"Unknown agent: {old_aid}")
                if not (isinstance(agent.owner_user_id, int) and agent.owner_user_id == user_id):
                    raise PermissionError("Forbidden")
                if old_aid == _user_agent_id(user_id=user_id, short_name="main"):
                    raise ValueError("main agent cannot be renamed")
                if new_aid in st.agents:
                    raise ValueError(f"Agent already exists: {name}")

                ws_raw = str(agent.workspace_host_path or "").strip()
                if not ws_raw:
                    raise RuntimeError("Agent has no workspaceHostPath")
                old_ws = Path(ws_raw).resolve()
                if not str(old_ws).startswith(str(home_root) + os.sep):
                    raise PermissionError("Forbidden")
                if not old_ws.exists() or not old_ws.is_dir():
                    raise FileNotFoundError(f"Workspace not found: {old_ws}")
                new_ws = Path(new_workspace_host_path).resolve()
                if not str(new_ws).startswith(str(home_root) + os.sep):
                    raise PermissionError("Forbidden")
                if new_ws.exists():
                    raise ValueError(f"Workspace already exists: {new_ws}")

                old_ws.rename(new_ws)

                # Update chat bindings to the new agentId.
                for ck, aid in list(st.chat_bindings.items()):
                    if aid == old_aid:
                        st.chat_bindings[ck] = new_aid

                updated = replace(agent, agent_id=new_aid, short_name=name, workspace_host_path=str(new_ws))
                st.agents.pop(old_aid, None)
                st.agents[new_aid] = updated

            await self._store.update(_write)

            agent2 = self._get_agent(new_aid)
            if agent2 is None:
                raise RuntimeError("Agent not found after rename")
            root = Path(agent2.workspace_host_path)
            await self._write_agent_model_file(root, self._channel_adjusted_agent_model(agent2))
            await _sync_session_workspace_snapshot(agent2.session_id, root)

            if _provisioner_requires_runtime_recreation_on_workspace_change():
                try:
                    await self._force_close_live_session(session_id)
                except Exception:
                    pass
                if _provisioner_manages_runtime_sessions():
                    try:
                        await _remove_managed_runtime(session_id)
                    except Exception as e:
                        log.warning("Failed to remove runtime for session %s after rename: %s", session_id, str(e))
            self._clear_session_backoff(session_id)

            return agent2

    async def delete_user_agent(self, *, user_id: int, chat_key: str, agent_id: str) -> dict[str, Any]:
        _require_user_agent_support()
        if not self._home_host_path:
            raise RuntimeError("ARGUS_HOME_HOST_PATH is required to delete user agents")
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("Invalid userId")

        ck = chat_key.strip() if isinstance(chat_key, str) else ""
        if not ck:
            raise ValueError("Missing chatKey")

        aid = _normalize_agent_id(agent_id)
        if not aid:
            raise ValueError("Invalid agentId")

        agent = self._get_agent(aid)
        if agent is None:
            raise ValueError(f"Unknown agent: {aid}")
        if not (isinstance(agent.owner_user_id, int) and agent.owner_user_id == user_id):
            raise PermissionError("Forbidden")

        session_id = str(agent.session_id or "").strip()
        if not session_id:
            raise RuntimeError("Agent has no sessionId")

        lock = self._session_restart_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_restart_locks[session_id] = lock

        deleted_container = False
        archived_workspace_host_path: Optional[str] = None

        async with lock:
            home_root = Path(self._home_host_path).resolve()
            main_id = self._user_main_agent_id(user_id=user_id)

            def _write(st: PersistedGatewayAutomationState) -> None:
                nonlocal archived_workspace_host_path
                st.version = max(int(getattr(st, "version", 1) or 1), AUTOMATION_STATE_VERSION)
                current = st.agents.get(aid)
                if not isinstance(current, PersistedAgentRuntime):
                    raise ValueError(f"Unknown agent: {aid}")
                if not (isinstance(current.owner_user_id, int) and current.owner_user_id == user_id):
                    raise PermissionError("Forbidden")

                sid = str(current.session_id or "").strip()
                if sid:
                    ws_raw = str(current.workspace_host_path or "").strip()
                    if ws_raw:
                        try:
                            old_ws = Path(ws_raw).resolve()
                            if str(old_ws).startswith(str(home_root) + os.sep) and old_ws.exists() and old_ws.is_dir():
                                cand = old_ws.with_name(f"{old_ws.name}.deleted-{sid}")
                                if cand.exists():
                                    cand = old_ws.with_name(f"{old_ws.name}.deleted-{sid}-{_now_ms()}")
                                old_ws.rename(cand)
                                archived_workspace_host_path = str(cand)
                        except Exception as e:
                            log.warning("Failed to archive workspace for deleted agent %s: %s", aid, str(e))

                # Rebind the caller's private chat back to main; unbind other chats/topics.
                for bound_ck, bound_aid in list(st.chat_bindings.items()):
                    if bound_aid != aid:
                        continue
                    if aid == main_id:
                        # Deleting main means the user has no default agent to fall back to.
                        st.chat_bindings.pop(bound_ck, None)
                        continue
                    if bound_ck != ck:
                        st.chat_bindings.pop(bound_ck, None)
                        continue
                    if main_id in st.agents:
                        st.chat_bindings[bound_ck] = main_id
                    else:
                        st.chat_bindings.pop(bound_ck, None)

                st.agents.pop(aid, None)

                # Remove session automation state only if no other agent references it.
                if sid:
                    referenced_elsewhere = False
                    for a2 in st.agents.values():
                        if isinstance(a2, PersistedAgentRuntime) and str(a2.session_id or "").strip() == sid:
                            referenced_elsewhere = True
                            break
                    if not referenced_elsewhere:
                        st.sessions.pop(sid, None)

            await self._store.update(_write)

            try:
                await self._force_close_live_session(session_id)
            except Exception:
                pass

            if _provisioner_manages_runtime_sessions():
                try:
                    deleted_container = await _remove_managed_runtime(session_id)
                except Exception as e:
                    log.warning("Failed to remove runtime for deleted agent %s (session=%s): %s", aid, session_id, str(e))

            self._clear_session_backoff(session_id)

            # Drop in-memory lane state for the deleted session to avoid retrying followups.
            for (sid, tid) in list(self._lanes.keys()):
                if sid == session_id:
                    self._lanes.pop((sid, tid), None)
            for key in list(self._last_heartbeat_run_at_ms.keys()):
                if key[0] == session_id:
                    self._last_heartbeat_run_at_ms.pop(key, None)
            for key in list(self._turn_text_by_key.keys()):
                if key[0] == session_id:
                    self._pop_turn_text_entry(key)
            for key in list(self._isolated_cron_context_by_key.keys()):
                if key[0] == session_id:
                    self._isolated_cron_context_by_key.pop(key, None)
            self._main_thread_locks.pop(session_id, None)

        main_agent = self._get_agent(main_id)
        current_agent_id = main_agent.agent_id if main_agent is not None else None
        current_session_id = self.resolve_agent_session_id(main_id) if current_agent_id else None
        return {
            "deletedAgentId": aid,
            "deletedSessionId": session_id,
            "archivedWorkspaceHostPath": archived_workspace_host_path,
            "deletedRuntime": deleted_container,
            "deletedContainer": deleted_container,
            "currentAgentId": current_agent_id,
            "currentSessionId": current_session_id,
        }

    def can_user_access_agent_id(self, *, user_id: int, agent_id: str) -> bool:
        aid = _normalize_agent_id(agent_id)
        if not aid:
            return False
        agent = self._get_agent(aid)
        if agent is None:
            return False
        return self._can_user_access_agent(user_id=user_id, agent=agent)

    async def set_user_agent_model(self, *, user_id: int, agent_id: str, model: str) -> tuple[PersistedAgentRuntime, bool]:
        _require_user_agent_support()
        if not self._home_host_path:
            raise RuntimeError("ARGUS_HOME_HOST_PATH is required to update user agents")
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("Invalid userId")

        aid = _normalize_agent_id(agent_id)
        if not aid:
            raise ValueError("Invalid agentId")
        normalized_model = _normalize_agent_model(model)
        if not normalized_model:
            raise ValueError("Invalid model")

        current = self._get_agent(aid)
        if current is None:
            raise ValueError(f"Unknown agent: {aid}")
        if not (isinstance(current.owner_user_id, int) and current.owner_user_id == user_id):
            raise PermissionError("Forbidden")
        user_state = self._get_user_channel_state_for_user(user_id=user_id)
        current_channel_id = self._current_channel_id_for_user_state(user_state=user_state)
        if current_channel_id == CHANNEL_ID_GATEWAY and normalized_model not in ARGUS_GATEWAY_AGENT_MODELS_SET:
            raise ValueError("Invalid model for gateway channel")

        if _effective_agent_model(current) != normalized_model:
            def _write(st: PersistedGatewayAutomationState) -> None:
                st.version = max(int(getattr(st, "version", 1) or 1), AUTOMATION_STATE_VERSION)
                agent = st.agents.get(aid)
                if not isinstance(agent, PersistedAgentRuntime):
                    raise ValueError(f"Unknown agent: {aid}")
                if not (isinstance(agent.owner_user_id, int) and agent.owner_user_id == user_id):
                    raise PermissionError("Forbidden")
                st.agents[aid] = replace(agent, model=normalized_model)

            await self._store.update(_write)

        updated = self._get_agent(aid)
        if updated is None:
            raise RuntimeError("Agent not found after model update")
        effective_model = self._channel_adjusted_agent_model(updated)
        root = Path(updated.workspace_host_path)
        await self._ensure_workspace_files_at(root)
        await self._write_agent_model_file(root, effective_model)
        await _sync_session_workspace_file(updated.session_id, root, self._agent_model_file_for_workspace(root))
        synced = await self._sync_live_session_default_model(session_id=updated.session_id, model=effective_model)
        return updated, synced

    async def bind_private_user_to_agent(self, *, user_id: int, chat_key: str, agent_id: str) -> PersistedAgentRuntime:
        ck = chat_key.strip() if isinstance(chat_key, str) else ""
        if not ck:
            raise ValueError("Missing chatKey")
        aid = _normalize_agent_id(agent_id)
        if not aid:
            raise ValueError("Invalid agentId")
        agent = self._get_agent(aid)
        if agent is None:
            raise ValueError(f"Unknown agent: {aid}")
        if not self._can_user_access_agent(user_id=user_id, agent=agent):
            raise PermissionError("Forbidden")

        def _write(st: PersistedGatewayAutomationState) -> None:
            st.version = max(int(getattr(st, "version", 1) or 1), AUTOMATION_STATE_VERSION)
            st.chat_bindings[ck] = aid

        await self._store.update(_write)
        agent2 = self._get_agent(aid)
        if agent2 is None:
            raise RuntimeError("Agent not found after update")
        return agent2

    async def _gc_orphan_runtime_containers(self) -> None:
        mode = _gc_delete_orphan_runtimes_mode()
        if mode == "off":
            return
        if not _provisioner_manages_runtime_sessions():
            log.info(
                "Orphan runtime GC is enabled (%s) but provision mode %s does not manage runtimes; skipping",
                mode,
                _provisioner_mode_name(),
            )
            return
        if not self._store.loaded_from_disk_ok:
            log.info("Orphan runtime GC is enabled (%s) but automation state did not load cleanly; skipping", mode)
            return

        st = self._store.state
        referenced: set[str] = set()
        for sid in (st.sessions or {}).keys():
            if isinstance(sid, str) and sid.strip():
                referenced.add(sid.strip())
        for agent in (st.agents or {}).values():
            if isinstance(agent, PersistedAgentRuntime) and isinstance(agent.session_id, str) and agent.session_id.strip():
                referenced.add(agent.session_id.strip())
        if not referenced:
            log.warning("Orphan runtime GC is enabled (%s) but no referenced sessionIds found; skipping for safety", mode)
            return

        try:
            containers = await _list_managed_sessions()
        except Exception as e:
            log.warning("Failed to list managed sessions for orphan GC: %s", str(e))
            return

        orphans: list[dict[str, Any]] = []
        for c in containers:
            if not isinstance(c, dict):
                continue
            sid = c.get("sessionId")
            layout = c.get("runtimeLayout")
            if layout != RUNTIME_LAYOUT:
                continue
            if not isinstance(sid, str) or not sid.strip():
                continue
            sid_norm = sid.strip()
            if sid_norm in referenced:
                continue
            orphans.append(c)

        if not orphans:
            return

        log.info(
            "Orphan runtime GC: mode=%s referenced=%d total=%d orphan=%d",
            mode,
            len(referenced),
            len(containers),
            len(orphans),
        )
        for o in orphans[:50]:
            cid = str(o.get("containerId") or "")
            log.info(
                "Orphan runtime container: sessionId=%s containerId=%s name=%s status=%s",
                o.get("sessionId"),
                (cid[:12] if cid else None),
                o.get("name"),
                o.get("status"),
            )
        if len(orphans) > 50:
            log.info("Orphan runtime GC: ... and %d more", len(orphans) - 50)

        if mode == "dry-run":
            return

        deleted = 0
        for o in orphans:
            sid = str(o.get("sessionId") or "").strip()
            if not sid:
                continue
            try:
                await self._force_close_live_session(sid)
            except Exception:
                pass
            try:
                deleted_runtime = await _remove_managed_runtime(sid)
                if not deleted_runtime:
                    continue
                deleted += 1
            except Exception as e:
                log.warning("Failed to delete orphan runtime for session %s: %s", sid, str(e))

        log.info("Orphan runtime GC complete: deleted=%d", deleted)

    async def _prune_persisted_sessions(self) -> None:
        # Keep persisted automation state in sync with existing managed runtimes.
        # Users often delete old session containers manually; without pruning, stale sessions can
        # confuse UIs/bots that expect a single "current" session.
        if not _provisioner_manages_runtime_sessions():
            return
        try:
            sessions = await _list_managed_sessions()
        except Exception as e:
            log.warning("Failed to list managed sessions for pruning: %s", str(e))
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
            # Safety: if the current provisioner reports no managed runtimes (common right after a clean rebuild, or
            # during transient provider API issues), pruning would wipe persisted automation state including cron jobs.
            # Keep state; a session can be recreated later from persisted automation state.
            log.info("No live managed sessions found; skipping automation-state pruning")
            return
        live_set = set(live_session_ids)

        st = self._store.state
        protected: set[str] = set()
        for agent in (st.agents or {}).values():
            if isinstance(agent, PersistedAgentRuntime) and isinstance(agent.session_id, str) and agent.session_id.strip():
                protected.add(agent.session_id.strip())

        removed = [sid for sid in st.sessions.keys() if sid not in live_set and sid not in protected]
        if not removed:
            return

        def _prune(st2: PersistedGatewayAutomationState) -> None:
            for sid in removed:
                st2.sessions.pop(sid, None)

        await self._store.update(_prune)
        log.info(
            "Pruned automation state sessions: removed=%d kept=%d",
            len(removed),
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
            derived = _derive_default_workspace_host_path_for_session(
                sid,
                home_host_path=self._home_host_path,
                workspace_base_host_path=self._workspace_host_path,
            )
            if derived:
                return Path(derived)
        return self._workspace_root()

    def _staged_attachment_to_dict(self, attachment: PersistedStagedAttachment) -> dict[str, Any]:
        return attachment.to_json()

    def _normalize_staged_attachment_dict(self, raw: Any) -> Optional[dict[str, Any]]:
        attachment = raw if isinstance(raw, PersistedStagedAttachment) else PersistedStagedAttachment.from_json(raw)
        if attachment is None:
            return None
        return attachment.to_json()

    def _merge_staged_attachment_dicts(self, attachments: list[Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in attachments:
            item = self._normalize_staged_attachment_dict(raw)
            if item is None:
                continue
            path_val = item.get("path")
            key = None
            unique_id = item.get("fileUniqueId")
            if isinstance(unique_id, str) and unique_id.strip():
                key = f"uid:{unique_id.strip()}"
            else:
                rel = _normalize_workspace_relative_path(path_val)
                if rel:
                    key = f"path:{rel}"
            if not key:
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    def _attachment_input_items(self, attachments: list[Any]) -> list[dict[str, Any]]:
        input_items: list[dict[str, Any]] = []
        for item in self._merge_staged_attachment_dicts(attachments):
            path_val = item.get("path")
            rel = _normalize_workspace_relative_path(path_val)
            if not rel:
                continue
            if bool(item.get("isImage", False)):
                input_items.append({"type": "localImage", "path": rel})
        return input_items

    def _format_attachment_block(self, attachments: list[Any]) -> str:
        items = self._merge_staged_attachment_dicts(attachments)
        if not items:
            return ""

        lines = [
            "[ATTACHMENTS]",
            "The following files are already saved in the workspace for this turn:",
        ]
        for idx, item in enumerate(items, start=1):
            rel = _workspace_ref(item.get("path"))
            if not rel:
                continue
            parts = [f"path={rel}"]
            file_name = item.get("fileName")
            if isinstance(file_name, str) and file_name.strip():
                parts.append(f"name={json.dumps(file_name.strip(), ensure_ascii=False)}")
            mime_type = item.get("mimeType")
            if isinstance(mime_type, str) and mime_type.strip():
                parts.append(f"mime={mime_type.strip()}")
            file_size = item.get("fileSize")
            if isinstance(file_size, int) and file_size >= 0:
                parts.append(f"bytes={file_size}")
            if bool(item.get("isImage", False)):
                parts.append("kind=image")
            lines.append(f"{idx}. " + "; ".join(parts))
        lines.append("[/ATTACHMENTS]")
        return "\n".join(lines).strip()

    async def _append_pending_telegram_attachments(
        self,
        *,
        session_id: str,
        chat_key: str,
        attachments: list[Any],
    ) -> list[dict[str, Any]]:
        sid = session_id.strip() if isinstance(session_id, str) else ""
        ck = chat_key.strip() if isinstance(chat_key, str) else ""
        normalized = self._merge_staged_attachment_dicts(attachments)
        if not sid or not ck or not normalized:
            return normalized

        merged_result: list[dict[str, Any]] = []

        def _append(st: PersistedGatewayAutomationState) -> None:
            nonlocal merged_result
            sess = st.sessions.get(sid)
            if sess is None:
                sess = PersistedSessionAutomation()
                st.sessions[sid] = sess
            existing = sess.pending_telegram_attachments_by_chat.get(ck) or []
            merged = self._merge_staged_attachment_dicts([*existing, *normalized])
            stored: list[PersistedStagedAttachment] = []
            for item in merged:
                attachment = PersistedStagedAttachment.from_json(item)
                if attachment is not None:
                    stored.append(attachment)
            sess.pending_telegram_attachments_by_chat[ck] = stored
            merged_result = merged
            st.version = max(int(getattr(st, "version", 1) or 1), AUTOMATION_STATE_VERSION)

        await self._store.update(_append)
        return merged_result

    async def _consume_pending_telegram_attachments(self, *, session_id: str, chat_key: str) -> list[dict[str, Any]]:
        sid = session_id.strip() if isinstance(session_id, str) else ""
        ck = chat_key.strip() if isinstance(chat_key, str) else ""
        if not sid or not ck:
            return []

        out: list[dict[str, Any]] = []

        def _pop(st: PersistedGatewayAutomationState) -> None:
            nonlocal out
            sess = st.sessions.get(sid)
            if sess is None:
                return
            raw_items = sess.pending_telegram_attachments_by_chat.pop(ck, None)
            if not isinstance(raw_items, list) or not raw_items:
                return
            out = self._merge_staged_attachment_dicts(raw_items)
            st.version = max(int(getattr(st, "version", 1) or 1), AUTOMATION_STATE_VERSION)

        await self._store.update(_pop)
        return out

    async def _ensure_workspace_files_at(self, root: Path) -> None:
        def _ensure() -> None:
            root.mkdir(parents=True, exist_ok=True)
            for template_name, target_name, overwrite_existing in WORKSPACE_TEMPLATE_SYNC_RULES:
                _sync_workspace_template_file(
                    root,
                    template_filename=template_name,
                    target_filename=target_name,
                    overwrite_existing=overwrite_existing,
                )

            _bootstrap_workspace_skill_templates(root)

        await asyncio.to_thread(_ensure)

    async def _sync_workspace_agents_file_at(self, root: Path) -> None:
        await asyncio.to_thread(
            _sync_workspace_template_file,
            root,
            template_filename=AGENTS_TEMPLATE_FILENAME,
            target_filename=AGENTS_FILENAME,
            overwrite_existing=True,
        )

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

    def _normalize_turn_kind(self, kind: Any) -> Optional[str]:
        if not isinstance(kind, str):
            return None
        normalized = kind.strip().lower()
        if normalized in (TURN_KIND_USER, TURN_KIND_HEARTBEAT, TURN_KIND_CRON):
            return normalized
        return None

    def _prepare_lane_for_new_turn(self, lane: ThreadLane, *, kind: str) -> None:
        lane.active_turn_kind = self._normalize_turn_kind(kind)
        lane.pending_client_turn_kind = None
        lane.pending_source_channel = None
        lane.pending_source_chat_key = None
        lane.cancel_requested = False

    def _queue_pending_client_turn(self, lane: ThreadLane, *, kind: str) -> None:
        lane.pending_client_turn_kind = self._normalize_turn_kind(kind)
        lane.cancel_requested = False

    def _clear_pending_client_turn(self, lane: ThreadLane) -> None:
        lane.pending_client_turn_kind = None

    def _set_pending_turn_source(
        self,
        lane: ThreadLane,
        *,
        source_channel: Optional[str],
        source_chat_key: Optional[str],
    ) -> None:
        lane.pending_source_channel = source_channel.strip() if isinstance(source_channel, str) and source_channel.strip() else None
        lane.pending_source_chat_key = source_chat_key.strip() if isinstance(source_chat_key, str) and source_chat_key.strip() else None

    def _consume_pending_turn_source(self, lane: ThreadLane) -> tuple[Optional[str], Optional[str]]:
        source_channel = lane.pending_source_channel
        source_chat_key = lane.pending_source_chat_key
        lane.pending_source_channel = None
        lane.pending_source_chat_key = None
        return source_channel, source_chat_key

    def note_client_turn_start_requested(self, session_id: str, thread_id: str) -> None:
        sid = session_id.strip() if isinstance(session_id, str) else ""
        tid = thread_id.strip() if isinstance(thread_id, str) else ""
        if not sid or not tid:
            return
        lane = self.lane(sid, tid)
        if lane.busy:
            return
        self._queue_pending_client_turn(lane, kind=TURN_KIND_USER)

    async def on_client_turn_start_response(self, session_id: str, thread_id: str, msg: dict[str, Any]) -> None:
        sid = session_id.strip() if isinstance(session_id, str) else ""
        tid = thread_id.strip() if isinstance(thread_id, str) else ""
        if not sid or not tid:
            return
        lane = self.lane(sid, tid)
        async with lane.lock:
            if not isinstance(msg, dict):
                if not lane.busy:
                    self._clear_pending_client_turn(lane)
                return
            if msg.get("error") is not None:
                if not lane.busy:
                    self._clear_pending_client_turn(lane)
                return

            turn_id: Optional[str] = None
            result = msg.get("result")
            if isinstance(result, dict):
                turn = result.get("turn")
                if isinstance(turn, dict):
                    tid_val = turn.get("id")
                    if isinstance(tid_val, str) and tid_val.strip():
                        turn_id = tid_val.strip()
            if not turn_id:
                if not lane.busy:
                    self._clear_pending_client_turn(lane)
                return

            if not lane.busy:
                self._mark_lane_busy(lane, turn_id=turn_id)
            elif not lane.active_turn_id:
                lane.active_turn_id = turn_id
            self._note_lane_progress(lane)

    def _resolve_existing_thread_id(
        self,
        *,
        session_id: str,
        thread_id: Optional[str],
        target: Optional[str],
    ) -> Optional[str]:
        if target == "main" or not (isinstance(thread_id, str) and thread_id.strip()):
            sess = self._store.state.sessions.get(session_id)
            if sess is None:
                return None
            main_tid = sess.main_thread_id
            if isinstance(main_tid, str) and main_tid.strip():
                return main_tid.strip()
            return None
        return thread_id.strip()

    async def _interrupt_user_turn_background(
        self,
        *,
        session_id: str,
        thread_id: str,
        turn_id: str,
        cancel_id: Optional[str],
    ) -> None:
        started_at_ms = _now_ms()
        _event_log(
            "info",
            "gw.cancel.interrupt_sent",
            cancel_id=cancel_id,
            session_id=session_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        try:
            live, _ = await _ensure_live_session(session_id, allow_create=False)
            await self._ensure_initialized(live)
            await self._rpc(live, "turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
            _event_log(
                "info",
                "gw.cancel.interrupt_result",
                cancel_id=cancel_id,
                session_id=session_id,
                thread_id=thread_id,
                turn_id=turn_id,
                result="ok",
                duration_ms=max(0, _now_ms() - started_at_ms),
            )
        except Exception as e:
            lane = self._lanes.get((session_id, thread_id))
            if lane is not None:
                async with lane.lock:
                    if lane.busy and lane.active_turn_id == turn_id and lane.active_turn_kind == TURN_KIND_USER:
                        lane.cancel_requested = False
            _event_log(
                "warning",
                "gw.cancel.interrupt_result",
                cancel_id=cancel_id,
                session_id=session_id,
                thread_id=thread_id,
                turn_id=turn_id,
                result="error",
                duration_ms=max(0, _now_ms() - started_at_ms),
                err_kind=type(e).__name__,
                err_msg=str(e),
            )

    async def cancel_active_user_turn(
        self,
        *,
        session_id: str,
        thread_id: Optional[str],
        target: Optional[str],
        cancel_id: Optional[str] = None,
        rpc_id: Optional[Any] = None,
    ) -> dict[str, Any]:
        sid = session_id.strip() if isinstance(session_id, str) else ""
        target_norm = target.strip() if isinstance(target, str) and target.strip() else None
        request_cancel_id = cancel_id.strip() if isinstance(cancel_id, str) and cancel_id.strip() else None
        resolved_thread_id = self._resolve_existing_thread_id(session_id=sid, thread_id=thread_id, target=target_norm)
        lane: Optional[ThreadLane] = None

        def _entry_cancel_id(turn_id_value: Optional[str]) -> Optional[str]:
            tid = turn_id_value.strip() if isinstance(turn_id_value, str) and turn_id_value.strip() else None
            if not tid or not resolved_thread_id:
                return None
            entry = self._turn_text_by_key.get((sid, resolved_thread_id, tid))
            if not isinstance(entry, dict):
                return None
            raw = entry.get("cancelId")
            return raw.strip() if isinstance(raw, str) and raw.strip() else None

        def _log_cancel_decision(
            *,
            result: str,
            reason: Optional[str],
            turn_id_value: Optional[str] = None,
            active_kind_value: Optional[str] = None,
            already_requested_value: Optional[bool] = None,
            active_cancel_id: Optional[str] = None,
        ) -> None:
            lane_busy = bool(lane.busy) if lane is not None else False
            active_kind_local = active_kind_value
            if active_kind_local is None and lane is not None:
                active_kind_local = lane.active_turn_kind or lane.pending_client_turn_kind
            _event_log(
                "info",
                "gw.cancel.decide",
                cancel_id=request_cancel_id,
                rpc_id=rpc_id,
                session_id=sid or session_id,
                target=target_norm,
                thread_id=resolved_thread_id,
                turn_id=turn_id_value,
                lane_busy=lane_busy,
                active_kind=active_kind_local,
                cancel_requested=bool(lane.cancel_requested) if lane is not None else False,
                followup_depth=len(lane.followups) if lane is not None else 0,
                result=result,
                reason=reason,
                already_requested=already_requested_value,
                active_cancel_id=active_cancel_id,
            )

        if not sid or not resolved_thread_id:
            _log_cancel_decision(result="rejected", reason="NO_ACTIVE_USER_TURN")
            return {"ok": True, "cancelRequested": False, "reason": "NO_ACTIVE_USER_TURN", "threadId": resolved_thread_id}

        lane = self._lanes.get((sid, resolved_thread_id))
        if lane is None:
            _log_cancel_decision(result="rejected", reason="NO_ACTIVE_USER_TURN")
            return {"ok": True, "cancelRequested": False, "reason": "NO_ACTIVE_USER_TURN", "threadId": resolved_thread_id}

        turn_id: Optional[str] = None
        already_requested = False
        active_kind_snapshot: Optional[str] = None
        for _ in range(40):
            async with lane.lock:
                if not lane.busy:
                    self._clear_pending_client_turn(lane)
                    _log_cancel_decision(result="rejected", reason="NO_ACTIVE_USER_TURN")
                    return {"ok": True, "cancelRequested": False, "reason": "NO_ACTIVE_USER_TURN", "threadId": resolved_thread_id}

                active_kind_snapshot = lane.active_turn_kind or lane.pending_client_turn_kind
                if active_kind_snapshot != TURN_KIND_USER:
                    active_cancel_id = _entry_cancel_id(lane.active_turn_id)
                    _log_cancel_decision(
                        result="rejected",
                        reason="NON_USER_TURN",
                        turn_id_value=lane.active_turn_id,
                        active_kind_value=active_kind_snapshot,
                        active_cancel_id=active_cancel_id,
                    )
                    return {
                        "ok": True,
                        "cancelRequested": False,
                        "reason": "NON_USER_TURN",
                        "activeKind": active_kind_snapshot,
                        "threadId": resolved_thread_id,
                        "turnId": lane.active_turn_id,
                        "activeCancelId": active_cancel_id,
                    }

                if lane.cancel_requested:
                    already_requested = True
                    turn_id = lane.active_turn_id
                    break

                if isinstance(lane.active_turn_id, str) and lane.active_turn_id.strip():
                    turn_id = lane.active_turn_id.strip()
                    lane.cancel_requested = True
                    break

            await asyncio.sleep(0.05)

        if not turn_id:
            _log_cancel_decision(
                result="rejected",
                reason="TURN_NOT_READY",
                active_kind_value=active_kind_snapshot,
            )
            return {
                "ok": True,
                "cancelRequested": False,
                "reason": "TURN_NOT_READY",
                "activeKind": lane.active_turn_kind or lane.pending_client_turn_kind,
                "threadId": resolved_thread_id,
            }

        active_cancel_id = _entry_cancel_id(turn_id)
        effective_cancel_id = (active_cancel_id or request_cancel_id) if already_requested else request_cancel_id
        if not effective_cancel_id:
            effective_cancel_id = f"gwcan_{uuid.uuid4().hex[:12]}"
        requested_at_ms = _now_ms()
        if not already_requested:
            self._remember_turn_cancel_context(
                session_id=sid,
                thread_id=resolved_thread_id,
                turn_id=turn_id,
                cancel_id=effective_cancel_id,
                requested_at_ms=requested_at_ms,
            )
            _log_cancel_decision(
                result="accepted",
                reason=None,
                turn_id_value=turn_id,
                active_kind_value=TURN_KIND_USER,
                already_requested_value=False,
                active_cancel_id=effective_cancel_id,
            )
        else:
            _log_cancel_decision(
                result="already_requested",
                reason=None,
                turn_id_value=turn_id,
                active_kind_value=TURN_KIND_USER,
                already_requested_value=True,
                active_cancel_id=effective_cancel_id,
            )

        if not already_requested:
            asyncio.create_task(
                self._interrupt_user_turn_background(
                    session_id=sid,
                    thread_id=resolved_thread_id,
                    turn_id=turn_id,
                    cancel_id=effective_cancel_id,
                )
            )
        return {
            "ok": True,
            "cancelRequested": True,
            "alreadyRequested": already_requested,
            "threadId": resolved_thread_id,
            "turnId": turn_id,
            "cancelId": effective_cancel_id,
            "activeCancelId": active_cancel_id,
        }

    def _mark_lane_busy(self, lane: ThreadLane, *, turn_id: Optional[str] = None) -> None:
        now = _now_ms()
        lane.busy = True
        if lane.busy_since_ms is None:
            lane.busy_since_ms = now
        lane.last_progress_at_ms = now
        if lane.active_turn_kind is None and lane.pending_client_turn_kind is not None:
            lane.active_turn_kind = lane.pending_client_turn_kind
            lane.pending_client_turn_kind = None
        if isinstance(turn_id, str) and turn_id.strip():
            lane.active_turn_id = turn_id.strip()

    def _mark_lane_idle(self, lane: ThreadLane) -> None:
        lane.busy = False
        lane.active_turn_id = None
        lane.active_turn_kind = None
        lane.pending_client_turn_kind = None
        lane.pending_source_channel = None
        lane.pending_source_chat_key = None
        lane.cancel_requested = False
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
                self._pop_turn_text_entry(key)
        return affected_tids

    def _get_turn_text_entry(self, key: tuple[str, str, str]) -> dict[str, Any]:
        entry = self._turn_text_by_key.get(key)
        if isinstance(entry, dict):
            return entry
        entry = _create_turn_text_entry()
        self._turn_text_by_key[key] = entry
        return entry

    def _cancel_turn_draft_task(self, draft_state: Any) -> None:
        if not isinstance(draft_state, dict):
            return
        task = draft_state.get("flushTask")
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
        draft_state["flushTask"] = None

    def _pop_turn_text_entry(self, key: tuple[str, str, str]) -> Optional[dict[str, Any]]:
        entry = self._turn_text_by_key.pop(key, None)
        if not isinstance(entry, dict):
            return None
        self._cancel_turn_draft_task(entry.get("draft"))
        return entry

    def _remember_turn_cancel_context(
        self,
        *,
        session_id: str,
        thread_id: Optional[str],
        turn_id: Optional[str],
        cancel_id: Optional[str],
        requested_at_ms: Optional[int],
    ) -> None:
        tid = thread_id.strip() if isinstance(thread_id, str) and thread_id.strip() else None
        run_id = turn_id.strip() if isinstance(turn_id, str) and turn_id.strip() else None
        cid = cancel_id.strip() if isinstance(cancel_id, str) and cancel_id.strip() else None
        if not tid or not run_id or not cid:
            return
        entry = self._get_turn_text_entry((session_id, tid, run_id))
        entry["cancelId"] = cid
        if isinstance(requested_at_ms, int) and requested_at_ms > 0:
            entry["cancelRequestedAtMs"] = requested_at_ms

    def _log_turn_post_cancel_activity(
        self,
        *,
        session_id: str,
        thread_id: Optional[str],
        turn_id: Optional[str],
        method: str,
        entry: dict[str, Any],
    ) -> None:
        cancel_id = entry.get("cancelId")
        if not isinstance(cancel_id, str) or not cancel_id.strip():
            return
        if bool(entry.get("cancelPostActivityLogged")):
            return
        entry["cancelPostActivityLogged"] = True
        requested_at_ms = entry.get("cancelRequestedAtMs")
        since_cancel_ms = None
        if isinstance(requested_at_ms, int) and requested_at_ms > 0:
            since_cancel_ms = max(0, _now_ms() - requested_at_ms)
        _event_log(
            "warning",
            "gw.turn.post_cancel_activity",
            cancel_id=cancel_id,
            session_id=session_id,
            thread_id=thread_id,
            turn_id=turn_id,
            method=method,
            since_cancel_ms=since_cancel_ms,
        )

    def _remember_turn_source_context(
        self,
        *,
        session_id: str,
        thread_id: Optional[str],
        turn_id: Optional[str],
        source_channel: Optional[str],
        source_chat_key: Optional[str],
    ) -> None:
        tid = thread_id.strip() if isinstance(thread_id, str) and thread_id.strip() else None
        run_id = turn_id.strip() if isinstance(turn_id, str) and turn_id.strip() else None
        channel = source_channel.strip() if isinstance(source_channel, str) and source_channel.strip() else None
        chat_key = source_chat_key.strip() if isinstance(source_chat_key, str) and source_chat_key.strip() else None
        if not tid or not run_id or not channel or not chat_key:
            return
        entry = self._get_turn_text_entry((session_id, tid, run_id))
        entry["sourceChannel"] = channel
        entry["sourceChatKey"] = chat_key

    def _peek_turn_source_context(
        self,
        *,
        session_id: str,
        thread_id: Optional[str],
        turn_id: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        tid = thread_id.strip() if isinstance(thread_id, str) and thread_id.strip() else None
        run_id = turn_id.strip() if isinstance(turn_id, str) and turn_id.strip() else None
        if not tid or not run_id:
            return None, None
        entry = self._turn_text_by_key.get((session_id, tid, run_id))
        if not isinstance(entry, dict):
            return None, None
        source_channel = entry.get("sourceChannel")
        source_chat_key = entry.get("sourceChatKey")
        channel = source_channel.strip() if isinstance(source_channel, str) and source_channel.strip() else None
        chat_key = source_chat_key.strip() if isinstance(source_chat_key, str) and source_chat_key.strip() else None
        return channel, chat_key

    def _inject_source_context_into_notification(
        self,
        params: dict[str, Any],
        *,
        source_channel: Optional[str],
        source_chat_key: Optional[str],
    ) -> None:
        channel = source_channel.strip() if isinstance(source_channel, str) and source_channel.strip() else None
        chat_key = source_chat_key.strip() if isinstance(source_chat_key, str) and source_chat_key.strip() else None
        if not channel or not chat_key:
            return
        if channel.lower() not in ("telegram", "tg"):
            return
        params["sourceChannel"] = channel
        params["sourceChatKey"] = chat_key
        params["source"] = {"channel": channel, "chatKey": chat_key}

    def _inject_turn_kind_into_notification(
        self,
        params: dict[str, Any],
        *,
        turn_kind: Optional[str],
    ) -> None:
        normalized = self._normalize_turn_kind(turn_kind)
        if normalized is None:
            return
        params["turnKind"] = normalized

    async def _touch_last_active_target(
        self,
        *,
        session_id: str,
        thread_id: Optional[str],
        source_channel: Optional[str],
        source_chat_key: Optional[str],
    ) -> None:
        tid = thread_id.strip() if isinstance(thread_id, str) and thread_id.strip() else None
        channel = source_channel.strip() if isinstance(source_channel, str) and source_channel.strip() else None
        chat_key = source_chat_key.strip() if isinstance(source_chat_key, str) and source_chat_key.strip() else None
        if not tid or not channel or not chat_key:
            return

        def _touch(st: PersistedGatewayAutomationState) -> None:
            sess = st.sessions.get(session_id)
            if sess is None:
                sess = PersistedSessionAutomation()
                st.sessions[session_id] = sess
            sess.last_active_by_thread[tid] = PersistedLastActiveTarget(
                channel=channel,
                chat_key=chat_key,
                at_ms=_now_ms(),
            )

        await self._store.update(_touch)

    def _resolve_telegram_target_for_source(
        self,
        *,
        source_channel: Optional[str],
        source_chat_key: Optional[str],
    ) -> Optional[tuple[str, dict[str, Any], str]]:
        channel = source_channel.strip().lower() if isinstance(source_channel, str) and source_channel.strip() else ""
        if channel not in ("telegram", "tg"):
            return None
        token = os.getenv("TELEGRAM_BOT_TOKEN") or ""
        if not token.strip():
            return None
        chat_key = source_chat_key.strip() if isinstance(source_chat_key, str) and source_chat_key.strip() else ""
        if not chat_key:
            return None
        target = _telegram_target_from_chat_key(chat_key)
        if target is None:
            return None
        return token, target, chat_key

    def _resolve_telegram_target_for_turn(
        self,
        session_id: str,
        thread_id: str,
        turn_id: Optional[str] = None,
    ) -> Optional[tuple[str, dict[str, Any], str]]:
        if isinstance(turn_id, str) and turn_id.strip():
            entry = self._turn_text_by_key.get((session_id, thread_id, turn_id.strip()))
            if isinstance(entry, dict):
                resolved = self._resolve_telegram_target_for_source(
                    source_channel=entry.get("sourceChannel"),
                    source_chat_key=entry.get("sourceChatKey"),
                )
                if resolved is not None:
                    return resolved
        st = self._store.state
        sess = st.sessions.get(session_id)
        if sess is None:
            return None
        tgt = sess.last_active_by_thread.get(thread_id)
        if tgt is None or not isinstance(tgt, PersistedLastActiveTarget):
            return None
        return self._resolve_telegram_target_for_source(
            source_channel=tgt.channel,
            source_chat_key=tgt.chat_key,
        )

    def _turn_draft_candidate_text(self, entry: dict[str, Any]) -> str:
        phases = entry.get("agentMessagePhasesByItemId")
        item_texts = entry.get("agentMessageTextByItemId")
        active_item_id = entry.get("activeAgentMessageItemId")
        if isinstance(phases, dict) and isinstance(item_texts, dict) and isinstance(active_item_id, str) and active_item_id:
            if phases.get(active_item_id) != "commentary":
                text = item_texts.get(active_item_id)
                if isinstance(text, str) and text.strip():
                    return text

        commentary_prefix = ""
        commentary_texts = entry.get("completedCommentaryTexts")
        if isinstance(commentary_texts, list):
            commentary_prefix = "".join(x for x in commentary_texts if isinstance(x, str))
        if not commentary_prefix:
            return ""

        delta = entry.get("delta")
        if not isinstance(delta, str) or not delta.strip():
            return ""
        delta_candidate = delta
        if delta_candidate.startswith(commentary_prefix):
            delta_candidate = delta_candidate[len(commentary_prefix):]
        return delta_candidate if delta_candidate.strip() else ""

    def _queue_telegram_turn_draft_flush(
        self,
        session_id: str,
        thread_id: str,
        turn_id: str,
        *,
        text: str,
        source: str = "unknown",
    ) -> None:
        if not isinstance(text, str) or not text.strip():
            return
        mode = _telegram_draft_streaming_mode()
        if mode == "off":
            return
        resolved = self._resolve_telegram_target_for_turn(session_id, thread_id, turn_id)
        if resolved is None:
            return
        _token, _target, chat_key = resolved
        if mode != "force" and _telegram_private_chat_id_from_chat_key(chat_key) is None:
            return

        key = (session_id, thread_id, turn_id)
        entry = self._turn_text_by_key.get(key)
        if not isinstance(entry, dict):
            return
        draft = entry.get("draft")
        if not isinstance(draft, dict):
            draft = {
                "draftId": _new_telegram_draft_id(),
                "latestText": "",
                "latestSource": "unknown",
                "latestQueuedAtMs": 0,
                "sentPayloadKey": None,
                "lastSentAtMs": None,
                "lastSentSource": None,
                "lastSentTextHash": None,
                "lastSentTextLen": None,
                "nextAllowedAtMs": 0,
                "flushTask": None,
                "disabled": False,
            }
            entry["draft"] = draft
        if bool(draft.get("disabled")):
            return
        draft["latestText"] = text
        draft["latestSource"] = source.strip() if isinstance(source, str) and source.strip() else "unknown"
        draft["latestQueuedAtMs"] = _now_ms()
        task = draft.get("flushTask")
        if isinstance(task, asyncio.Task) and not task.done():
            return
        draft["flushTask"] = asyncio.create_task(self._flush_telegram_turn_draft(session_id, thread_id, turn_id))

    async def _flush_telegram_turn_draft(self, session_id: str, thread_id: str, turn_id: str) -> None:
        key = (session_id, thread_id, turn_id)
        while True:
            entry = self._turn_text_by_key.get(key)
            if not isinstance(entry, dict):
                return
            draft = entry.get("draft")
            if not isinstance(draft, dict):
                return

            prepared = _telegram_prepare_draft_payload(draft.get("latestText"))
            action = prepared.get("action")
            if action == "hold":
                draft["flushTask"] = None
                return
            if action != "send":
                draft["flushTask"] = None
                return

            payload_text = prepared.get("text") if isinstance(prepared.get("text"), str) else ""
            payload_parse_mode = prepared.get("parseMode") if isinstance(prepared.get("parseMode"), str) else None
            payload_key = prepared.get("payloadKey") if isinstance(prepared.get("payloadKey"), str) else None
            if not payload_text or not payload_key:
                draft["flushTask"] = None
                return

            sent_payload_key = draft.get("sentPayloadKey")
            if isinstance(sent_payload_key, str) and sent_payload_key == payload_key:
                draft["flushTask"] = None
                return

            next_allowed_at_ms = draft.get("nextAllowedAtMs")
            if isinstance(next_allowed_at_ms, int) and next_allowed_at_ms > _now_ms():
                await asyncio.sleep((next_allowed_at_ms - _now_ms()) / 1000.0)
                continue

            resolved = self._resolve_telegram_target_for_turn(session_id, thread_id, turn_id)
            if resolved is None:
                draft["disabled"] = True
                draft["flushTask"] = None
                return
            token, target, chat_key = resolved

            mode = _telegram_draft_streaming_mode()
            if mode == "off":
                draft["disabled"] = True
                draft["flushTask"] = None
                return
            if mode != "force" and _telegram_private_chat_id_from_chat_key(chat_key) is None:
                draft["disabled"] = True
                draft["flushTask"] = None
                return

            draft_id = int(draft.get("draftId") or 0) or _new_telegram_draft_id()
            draft["draftId"] = draft_id
            draft_source = draft.get("latestSource") if isinstance(draft.get("latestSource"), str) else None
            queued_at_ms = draft.get("latestQueuedAtMs")
            queue_lag_ms = None
            if isinstance(queued_at_ms, int) and queued_at_ms > 0:
                queue_lag_ms = max(0, _now_ms() - queued_at_ms)
            source_text = draft.get("latestText") if isinstance(draft.get("latestText"), str) else ""
            source_text = source_text.strip()
            source_text_hash = _text_hash(source_text)
            send_started_at_ms = _now_ms()
            try:
                await _telegram_send_message_draft(
                    token=token,
                    target=target,
                    draft_id=draft_id,
                    text=payload_text,
                    parse_mode=payload_parse_mode,
                )
            except asyncio.CancelledError:
                draft["flushTask"] = None
                raise
            except Exception as e:  # noqa: BLE001
                draft["disabled"] = True
                draft["flushTask"] = None
                log.info("Telegram draft streaming disabled for %s/%s: %s", session_id, thread_id, str(e))
                _event_log(
                    "warning",
                    "gw.tg.draft_flush",
                    session_id=session_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    draft_id=draft_id,
                    draft_source=draft_source,
                    chat_key_hash=_chat_key_hash(chat_key),
                    text_len=len(source_text),
                    text_hash=source_text_hash,
                    queue_lag_ms=queue_lag_ms,
                    send_duration_ms=max(0, _now_ms() - send_started_at_ms),
                    result="error",
                    err_kind=type(e).__name__,
                    err_msg=str(e),
                )
                return

            draft["sentPayloadKey"] = payload_key
            sent_at_ms = _now_ms()
            draft["lastSentAtMs"] = sent_at_ms
            draft["lastSentSource"] = draft_source
            draft["lastSentTextHash"] = source_text_hash
            draft["lastSentTextLen"] = len(source_text)
            draft["nextAllowedAtMs"] = sent_at_ms + TELEGRAM_DRAFT_FLUSH_INTERVAL_MS

            if draft_source != "delta":
                _event_log(
                    "info",
                    "gw.tg.draft_flush",
                    session_id=session_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    draft_id=draft_id,
                    draft_source=draft_source,
                    chat_key_hash=_chat_key_hash(chat_key),
                    text_len=len(source_text),
                    text_hash=source_text_hash,
                    queue_lag_ms=queue_lag_ms,
                    send_duration_ms=max(0, sent_at_ms - send_started_at_ms),
                    result="ok",
                )

            prepared_after = _telegram_prepare_draft_payload(draft.get("latestText"))
            latest_after_key = prepared_after.get("payloadKey") if isinstance(prepared_after.get("payloadKey"), str) else None
            if latest_after_key != payload_key:
                continue

            draft["flushTask"] = None
            return

    async def _force_close_live_session(self, session_id: str) -> None:
        await _close_live_session(session_id)

    async def _restart_runtime_container(self, session_id: str) -> None:
        if not _provisioner_manages_runtime_sessions():
            return
        try:
            restarted = await _restart_managed_runtime(session_id)
        except Exception as e:
            log.warning("Failed to restart runtime for session %s: %s", session_id, str(e))
            return
        if not restarted:
            return

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

    def _remember_turn_kind(
        self,
        *,
        session_id: str,
        thread_id: Optional[str],
        turn_id: Optional[str],
        kind: Any,
    ) -> None:
        tid = thread_id.strip() if isinstance(thread_id, str) and thread_id.strip() else None
        run_id = turn_id.strip() if isinstance(turn_id, str) and turn_id.strip() else None
        normalized = self._normalize_turn_kind(kind)
        if not tid or not run_id or normalized is None:
            return
        entry = self._get_turn_text_entry((session_id, tid, run_id))
        entry["turnKind"] = normalized

    def _resolve_turn_kind_for_notification(
        self,
        *,
        session_id: str,
        thread_id: Optional[str],
        turn_id: Optional[str],
    ) -> Optional[str]:
        tid = thread_id.strip() if isinstance(thread_id, str) and thread_id.strip() else None
        run_id = turn_id.strip() if isinstance(turn_id, str) and turn_id.strip() else None
        if tid and run_id:
            entry = self._turn_text_by_key.get((session_id, tid, run_id))
            if isinstance(entry, dict):
                normalized = self._normalize_turn_kind(entry.get("turnKind"))
                if normalized is not None:
                    return normalized
        if not tid:
            return None
        lane = self._lanes.get((session_id, tid))
        if lane is None:
            return None
        return self._normalize_turn_kind(lane.active_turn_kind or lane.pending_client_turn_kind)

    def _is_heartbeat_commentary_notification(self, session_id: str, msg: dict[str, Any]) -> bool:
        # Heartbeat commentary is internal automation chatter; downstream clients
        # should only receive the final ack/token or the final user-visible text.
        if not isinstance(msg, dict):
            return False
        method = msg.get("method")
        if method not in ("item/started", "item/agentMessage/delta", "item/completed"):
            return False
        params = msg.get("params")
        if not isinstance(params, dict):
            return False

        thread_id_raw = params.get("threadId")
        thread_id = thread_id_raw.strip() if isinstance(thread_id_raw, str) and thread_id_raw.strip() else None
        if not thread_id:
            return False

        turn_id: Optional[str] = None
        turn_id_raw = params.get("turnId")
        if isinstance(turn_id_raw, str) and turn_id_raw.strip():
            turn_id = turn_id_raw.strip()
        else:
            turn = params.get("turn")
            if isinstance(turn, dict):
                tid_val = turn.get("id")
                if isinstance(tid_val, str) and tid_val.strip():
                    turn_id = tid_val.strip()

        if self._resolve_turn_kind_for_notification(
            session_id=session_id,
            thread_id=thread_id,
            turn_id=turn_id,
        ) != TURN_KIND_HEARTBEAT:
            return False

        if method in ("item/started", "item/completed"):
            item = params.get("item")
            if not isinstance(item, dict) or item.get("type") != "agentMessage":
                return False
            return _normalize_message_phase(item.get("phase")) == "commentary"

        if not turn_id:
            return False
        entry = self._turn_text_by_key.get((session_id, thread_id, turn_id))
        if not isinstance(entry, dict):
            return False
        phases = entry.get("agentMessagePhasesByItemId")
        if not isinstance(phases, dict):
            return False
        item_id = params.get("itemId")
        candidate_item_id = item_id.strip() if isinstance(item_id, str) and item_id.strip() else None
        if candidate_item_id is None:
            active_item_id = entry.get("activeAgentMessageItemId")
            if isinstance(active_item_id, str) and active_item_id.strip():
                candidate_item_id = active_item_id.strip()
        if candidate_item_id is None:
            return False
        return phases.get(candidate_item_id) == "commentary"

    def on_upstream_notification(self, session_id: str, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        params = msg.get("params")
        if not isinstance(params, dict):
            params = {}
            msg["params"] = params

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

        if method == "item/started":
            if not key:
                return
            item = params.get("item")
            if not isinstance(item, dict) or item.get("type") != "agentMessage":
                return
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id.strip():
                return
            item_id = item_id.strip()
            entry = self._get_turn_text_entry(key)
            phase = _normalize_message_phase(item.get("phase"))
            phases = entry.get("agentMessagePhasesByItemId")
            if not isinstance(phases, dict):
                phases = {}
                entry["agentMessagePhasesByItemId"] = phases
            phases[item_id] = phase
            item_texts = entry.get("agentMessageTextByItemId")
            if not isinstance(item_texts, dict):
                item_texts = {}
                entry["agentMessageTextByItemId"] = item_texts
            item_texts.setdefault(item_id, "")
            entry["activeAgentMessageItemId"] = item_id
            source_channel, source_chat_key = self._peek_turn_source_context(
                session_id=session_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            self._inject_source_context_into_notification(
                params,
                source_channel=source_channel,
                source_chat_key=source_chat_key,
            )
            self._inject_turn_kind_into_notification(
                params,
                turn_kind=self._resolve_turn_kind_for_notification(
                    session_id=session_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                ),
            )
            if thread_id:
                self._note_lane_progress(self.lane(session_id, thread_id))
            return

        if method == "item/agentMessage/delta":
            if not key:
                return
            delta = params.get("delta")
            if not isinstance(delta, str) or not delta:
                return
            entry = self._get_turn_text_entry(key)
            entry["delta"] = str(entry.get("delta") or "") + delta
            item_id = params.get("itemId")
            item_id = item_id.strip() if isinstance(item_id, str) and item_id.strip() else None
            phases = entry.get("agentMessagePhasesByItemId")
            if not isinstance(phases, dict):
                phases = {}
                entry["agentMessagePhasesByItemId"] = phases
            item_texts = entry.get("agentMessageTextByItemId")
            if not isinstance(item_texts, dict):
                item_texts = {}
                entry["agentMessageTextByItemId"] = item_texts

            candidate_item_id = item_id
            if candidate_item_id and candidate_item_id not in phases:
                phases[candidate_item_id] = None
            if not candidate_item_id:
                active_item_id = entry.get("activeAgentMessageItemId")
                if isinstance(active_item_id, str) and active_item_id.strip():
                    candidate_item_id = active_item_id.strip()

            draft_candidate = ""
            if candidate_item_id:
                entry["activeAgentMessageItemId"] = candidate_item_id
                if phases.get(candidate_item_id) == "commentary":
                    draft_candidate = ""
                else:
                    item_texts[candidate_item_id] = str(item_texts.get(candidate_item_id) or "") + delta
                    draft_candidate = str(item_texts.get(candidate_item_id) or "")
            else:
                draft_candidate = self._turn_draft_candidate_text(entry)

            if draft_candidate.strip():
                self._queue_telegram_turn_draft_flush(
                    session_id,
                    thread_id,
                    turn_id,
                    text=draft_candidate,
                    source="delta",
                )
            source_channel, source_chat_key = self._peek_turn_source_context(
                session_id=session_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            self._inject_source_context_into_notification(
                params,
                source_channel=source_channel,
                source_chat_key=source_chat_key,
            )
            self._inject_turn_kind_into_notification(
                params,
                turn_kind=self._resolve_turn_kind_for_notification(
                    session_id=session_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                ),
            )
            if thread_id:
                self._note_lane_progress(self.lane(session_id, thread_id))
            self._log_turn_post_cancel_activity(
                session_id=session_id,
                thread_id=thread_id,
                turn_id=turn_id,
                method=method,
                entry=entry,
            )
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
            entry = self._get_turn_text_entry(key)
            item_id = item.get("id")
            item_id = item_id.strip() if isinstance(item_id, str) and item_id.strip() else None
            phase = _normalize_message_phase(item.get("phase"))
            phases = entry.get("agentMessagePhasesByItemId")
            if not isinstance(phases, dict):
                phases = {}
                entry["agentMessagePhasesByItemId"] = phases
            item_texts = entry.get("agentMessageTextByItemId")
            if not isinstance(item_texts, dict):
                item_texts = {}
                entry["agentMessageTextByItemId"] = item_texts
            if item_id:
                phases[item_id] = phase
                item_texts[item_id] = text
            if phase == "commentary":
                commentary_texts = entry.get("completedCommentaryTexts")
                if not isinstance(commentary_texts, list):
                    commentary_texts = []
                    entry["completedCommentaryTexts"] = commentary_texts
                commentary_texts.append(text)
            else:
                item_completed_at_ms = _now_ms()
                if not isinstance(entry.get("firstFinalItemCompletedAtMs"), int):
                    entry["firstFinalItemCompletedAtMs"] = item_completed_at_ms
                entry["lastFinalItemCompletedAtMs"] = item_completed_at_ms
                entry["lastFinalItemId"] = item_id
                entry["lastFinalTextHash"] = _text_hash(text)
                entry["lastFinalTextLen"] = len(text.strip())
                entry["fullText"] = text
                if item_id:
                    entry["activeAgentMessageItemId"] = item_id
                self._queue_telegram_turn_draft_flush(
                    session_id,
                    thread_id,
                    turn_id,
                    text=text,
                    source="item_completed",
                )
            source_channel, source_chat_key = self._peek_turn_source_context(
                session_id=session_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            self._inject_source_context_into_notification(
                params,
                source_channel=source_channel,
                source_chat_key=source_chat_key,
            )
            self._inject_turn_kind_into_notification(
                params,
                turn_kind=self._resolve_turn_kind_for_notification(
                    session_id=session_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                ),
            )
            if phase != "commentary":
                turn_started_at_ms = entry.get("turnStartedAtMs")
                since_turn_started_ms = None
                if isinstance(turn_started_at_ms, int) and turn_started_at_ms > 0:
                    since_turn_started_ms = max(0, _now_ms() - turn_started_at_ms)
                _event_log(
                    "info",
                    "gw.turn.final_item_completed",
                    session_id=session_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    item_id=item_id,
                    phase=phase,
                    source_channel=source_channel,
                    chat_key_hash=_chat_key_hash(source_chat_key),
                    text_len=len(text.strip()),
                    text_hash=_text_hash(text),
                    since_turn_started_ms=since_turn_started_ms,
                )
            if thread_id:
                self._note_lane_progress(self.lane(session_id, thread_id))
            self._log_turn_post_cancel_activity(
                session_id=session_id,
                thread_id=thread_id,
                turn_id=turn_id,
                method=method,
                entry=entry,
            )
            return

        if method == "turn/started":
            if thread_id:
                lane = self.lane(session_id, thread_id)
                source_channel, source_chat_key = self._consume_pending_turn_source(lane)
                self._remember_turn_source_context(
                    session_id=session_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    source_channel=source_channel,
                    source_chat_key=source_chat_key,
                )
                self._inject_source_context_into_notification(
                    params,
                    source_channel=source_channel,
                    source_chat_key=source_chat_key,
                )
                self._inject_turn_kind_into_notification(
                    params,
                    turn_kind=lane.active_turn_kind,
                )
                self._mark_lane_busy(lane, turn_id=turn_id)
                self._remember_turn_kind(
                    session_id=session_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    kind=lane.active_turn_kind,
                )
                entry = self._get_turn_text_entry(key) if key else None
                cancel_id = None
                if isinstance(entry, dict):
                    if not isinstance(entry.get("turnStartedAtMs"), int):
                        entry["turnStartedAtMs"] = _now_ms()
                    raw_cancel_id = entry.get("cancelId")
                    if isinstance(raw_cancel_id, str) and raw_cancel_id.strip():
                        cancel_id = raw_cancel_id.strip()
                _event_log(
                    "info",
                    "gw.turn.started",
                    cancel_id=cancel_id,
                    session_id=session_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    turn_kind=lane.active_turn_kind,
                    source_channel=source_channel,
                    chat_key_hash=_chat_key_hash(source_chat_key),
                    followup_depth=len(lane.followups),
                )
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
                turn_kind = self._resolve_turn_kind_for_notification(
                    session_id=session_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                )
                followup_depth_after = len(lane.followups)
                self._mark_lane_idle(lane)
                asyncio.create_task(self._process_lane_after_turn(session_id, thread_id))
                asyncio.create_task(self._maybe_request_heartbeat(session_id, thread_id))

                final_text = ""
                source_channel = None
                source_chat_key = None
                cancel_id = None
                cancel_requested_at_ms = None
                turn_started_at_ms = None
                first_final_item_completed_at_ms = None
                last_final_item_completed_at_ms = None
                last_final_item_id = None
                last_final_item_text_hash = None
                last_final_item_text_len = None
                last_draft_sent_at_ms = None
                last_draft_source = None
                last_draft_text_hash = None
                last_draft_text_len = None
                if key:
                    entry = self._pop_turn_text_entry(key)
                    if isinstance(entry, dict):
                        source_channel = entry.get("sourceChannel") if isinstance(entry.get("sourceChannel"), str) else None
                        source_chat_key = entry.get("sourceChatKey") if isinstance(entry.get("sourceChatKey"), str) else None
                        raw_cancel_id = entry.get("cancelId")
                        if isinstance(raw_cancel_id, str) and raw_cancel_id.strip():
                            cancel_id = raw_cancel_id.strip()
                        raw_cancel_requested_at_ms = entry.get("cancelRequestedAtMs")
                        if isinstance(raw_cancel_requested_at_ms, int) and raw_cancel_requested_at_ms > 0:
                            cancel_requested_at_ms = raw_cancel_requested_at_ms
                        raw_turn_started_at_ms = entry.get("turnStartedAtMs")
                        if isinstance(raw_turn_started_at_ms, int) and raw_turn_started_at_ms > 0:
                            turn_started_at_ms = raw_turn_started_at_ms
                        raw_first_final_item_completed_at_ms = entry.get("firstFinalItemCompletedAtMs")
                        if isinstance(raw_first_final_item_completed_at_ms, int) and raw_first_final_item_completed_at_ms > 0:
                            first_final_item_completed_at_ms = raw_first_final_item_completed_at_ms
                        raw_last_final_item_completed_at_ms = entry.get("lastFinalItemCompletedAtMs")
                        if isinstance(raw_last_final_item_completed_at_ms, int) and raw_last_final_item_completed_at_ms > 0:
                            last_final_item_completed_at_ms = raw_last_final_item_completed_at_ms
                        raw_last_final_item_id = entry.get("lastFinalItemId")
                        if isinstance(raw_last_final_item_id, str) and raw_last_final_item_id.strip():
                            last_final_item_id = raw_last_final_item_id.strip()
                        raw_last_final_item_text_hash = entry.get("lastFinalTextHash")
                        if isinstance(raw_last_final_item_text_hash, str) and raw_last_final_item_text_hash.strip():
                            last_final_item_text_hash = raw_last_final_item_text_hash.strip()
                        raw_last_final_item_text_len = entry.get("lastFinalTextLen")
                        if isinstance(raw_last_final_item_text_len, int) and raw_last_final_item_text_len >= 0:
                            last_final_item_text_len = raw_last_final_item_text_len
                        draft_state = entry.get("draft")
                        if isinstance(draft_state, dict):
                            raw_last_draft_sent_at_ms = draft_state.get("lastSentAtMs")
                            if isinstance(raw_last_draft_sent_at_ms, int) and raw_last_draft_sent_at_ms > 0:
                                last_draft_sent_at_ms = raw_last_draft_sent_at_ms
                            raw_last_draft_source = draft_state.get("lastSentSource")
                            if isinstance(raw_last_draft_source, str) and raw_last_draft_source.strip():
                                last_draft_source = raw_last_draft_source.strip()
                            raw_last_draft_text_hash = draft_state.get("lastSentTextHash")
                            if isinstance(raw_last_draft_text_hash, str) and raw_last_draft_text_hash.strip():
                                last_draft_text_hash = raw_last_draft_text_hash.strip()
                            raw_last_draft_text_len = draft_state.get("lastSentTextLen")
                            if isinstance(raw_last_draft_text_len, int) and raw_last_draft_text_len >= 0:
                                last_draft_text_len = raw_last_draft_text_len
                        full = entry.get("fullText")
                        delta = entry.get("delta")
                        commentary_prefix = ""
                        commentary_texts = entry.get("completedCommentaryTexts")
                        if isinstance(commentary_texts, list):
                            commentary_prefix = "".join(x for x in commentary_texts if isinstance(x, str))
                        if isinstance(full, str) and full.strip():
                            final_text = full
                        elif isinstance(delta, str) and delta.strip():
                            delta_candidate = delta
                            if commentary_prefix and delta_candidate.startswith(commentary_prefix):
                                delta_candidate = delta_candidate[len(commentary_prefix):]
                            if delta_candidate.strip():
                                final_text = delta_candidate
                self._inject_source_context_into_notification(
                    params,
                    source_channel=source_channel,
                    source_chat_key=source_chat_key,
                )
                self._inject_turn_kind_into_notification(
                    params,
                    turn_kind=turn_kind,
                )
                turn_status = str(turn_status_raw or "").strip().lower() or None
                now_ms = _now_ms()
                since_cancel_ms = None
                if isinstance(cancel_requested_at_ms, int):
                    since_cancel_ms = max(0, now_ms - cancel_requested_at_ms)
                turn_duration_ms = None
                if isinstance(turn_started_at_ms, int):
                    turn_duration_ms = max(0, now_ms - turn_started_at_ms)
                since_first_final_item_completed_ms = None
                if isinstance(first_final_item_completed_at_ms, int):
                    since_first_final_item_completed_ms = max(0, now_ms - first_final_item_completed_at_ms)
                since_last_final_item_completed_ms = None
                if isinstance(last_final_item_completed_at_ms, int):
                    since_last_final_item_completed_ms = max(0, now_ms - last_final_item_completed_at_ms)
                since_last_draft_sent_ms = None
                if isinstance(last_draft_sent_at_ms, int):
                    since_last_draft_sent_ms = max(0, now_ms - last_draft_sent_at_ms)
                _event_log(
                    "info",
                    "gw.turn.completed",
                    cancel_id=cancel_id,
                    session_id=session_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    turn_status=turn_status,
                    turn_kind=turn_kind,
                    source_channel=source_channel,
                    chat_key_hash=_chat_key_hash(source_chat_key),
                    followup_depth_after=followup_depth_after,
                    final_text_len=len(final_text) if isinstance(final_text, str) else 0,
                    final_text_hash=_text_hash(final_text),
                    turn_duration_ms=turn_duration_ms,
                    last_final_item_id=last_final_item_id,
                    last_final_item_text_len=last_final_item_text_len,
                    last_final_item_text_hash=last_final_item_text_hash,
                    since_first_final_item_completed_ms=since_first_final_item_completed_ms,
                    since_last_final_item_completed_ms=since_last_final_item_completed_ms,
                    last_draft_source=last_draft_source,
                    last_draft_text_len=last_draft_text_len,
                    last_draft_text_hash=last_draft_text_hash,
                    since_last_draft_sent_ms=since_last_draft_sent_ms,
                    since_cancel_ms=since_cancel_ms,
                    turn_error=turn_error_message,
                )
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
                if final_text.strip() and str(turn_status_raw or "").strip().lower() != "interrupted":
                    asyncio.create_task(
                        self._deliver_turn_text(
                            session_id,
                            thread_id,
                            final_text,
                            turn_id=turn_id,
                            source_channel=source_channel,
                            source_chat_key=source_chat_key,
                        )
                    )
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

        live, _ = await _ensure_live_session(sid, allow_create=True)
        await self._ensure_initialized(live)

        archived_ids: set[str] = set()
        for r in to_archive:
            try:
                await self._ensure_thread_loaded_or_resumed(live, r.thread_id)
            except Exception as e:
                msg = str(e)
                if _is_missing_thread_for_archive_error(msg):
                    # Treat missing threads as already-archived so we don't spam logs or retry forever.
                    archived_ids.add(r.run_id)
                    log.info(
                        "Cron retention: thread already missing; marking archived for %s/%s runId=%s threadId=%s (%s)",
                        sid,
                        jid,
                        r.run_id,
                        r.thread_id,
                        msg,
                    )
                else:
                    log.warning(
                        "Failed to load cron run thread for archiving %s/%s runId=%s threadId=%s: %s",
                        sid,
                        jid,
                        r.run_id,
                        r.thread_id,
                        msg,
                    )
                continue
            try:
                await self._rpc(live, "thread/archive", {"threadId": r.thread_id})
                archived_ids.add(r.run_id)
            except Exception as e:
                msg = str(e)
                if _is_missing_thread_for_archive_error(msg):
                    # Treat missing threads as already-archived so we don't spam logs or retry forever.
                    archived_ids.add(r.run_id)
                    log.info(
                        "Cron retention: thread already missing; marking archived for %s/%s runId=%s threadId=%s (%s)",
                        sid,
                        jid,
                        r.run_id,
                        r.thread_id,
                        msg,
                    )
                else:
                    log.warning(
                        "Failed to archive cron run thread for %s/%s runId=%s threadId=%s: %s",
                        sid,
                        jid,
                        r.run_id,
                        r.thread_id,
                        msg,
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

    async def _deliver_turn_text(
        self,
        session_id: str,
        thread_id: str,
        text: str,
        *,
        turn_id: Optional[str] = None,
        source_channel: Optional[str] = None,
        source_chat_key: Optional[str] = None,
    ) -> None:
        # Gateway-owned delivery: send final text on turn/completed, splitting when needed.
        if not isinstance(text, str) or not text.strip():
            return
        delivery_started_at_ms = _now_ms()
        resolved = self._resolve_telegram_target_for_source(
            source_channel=source_channel,
            source_chat_key=source_chat_key,
        )
        if resolved is None:
            resolved = self._resolve_telegram_target_for_turn(session_id, thread_id, turn_id)
        if resolved is None:
            _event_log(
                "warning",
                "gw.tg.final_delivery.skip",
                session_id=session_id,
                thread_id=thread_id,
                turn_id=turn_id,
                source_channel=source_channel,
                chat_key_hash=_chat_key_hash(source_chat_key),
                reason="missing_target",
                text_len=len(text.strip()),
                text_hash=_text_hash(text),
            )
            return
        token, target, resolved_chat_key = resolved

        stripped = _strip_heartbeat_token(text)
        if stripped.get("shouldSkip"):
            _event_log(
                "info",
                "gw.tg.final_delivery.skip",
                session_id=session_id,
                thread_id=thread_id,
                turn_id=turn_id,
                source_channel=source_channel,
                chat_key_hash=_chat_key_hash(resolved_chat_key),
                reason="heartbeat_filtered",
                text_len=len(text.strip()),
                text_hash=_text_hash(text),
            )
            return
        final_raw = stripped.get("text") if isinstance(stripped.get("text"), str) else text
        final_raw = final_raw.strip()
        if not final_raw:
            _event_log(
                "info",
                "gw.tg.final_delivery.skip",
                session_id=session_id,
                thread_id=thread_id,
                turn_id=turn_id,
                source_channel=source_channel,
                chat_key_hash=_chat_key_hash(resolved_chat_key),
                reason="empty_after_strip",
                text_len=len(text.strip()),
                text_hash=_text_hash(text),
            )
            return

        clean_text, media_refs = _split_media_from_output(final_raw)
        delivered_any = False
        text_send_failed = False
        text_part_count = 0
        attachment_attempt_count = 0
        attachment_delivered_count = 0
        attachment_failed_count = 0

        clean_text = clean_text.strip()
        clean_text_hash = _text_hash(clean_text)
        if clean_text:
            text_part_count = len(_telegram_split_text(clean_text))
        _event_log(
            "info",
            "gw.tg.final_delivery.start",
            session_id=session_id,
            thread_id=thread_id,
            turn_id=turn_id,
            source_channel=source_channel,
            chat_key_hash=_chat_key_hash(resolved_chat_key),
            final_text_len=len(final_raw),
            final_text_hash=_text_hash(final_raw),
            clean_text_len=len(clean_text),
            clean_text_hash=clean_text_hash,
            text_part_count=text_part_count,
            media_ref_count=len(media_refs),
        )
        if clean_text:
            text_send_started_at_ms = _now_ms()
            try:
                results = await _telegram_send_markdown_message_parts(token=token, target=target, text=clean_text)
                if results:
                    delivered_any = True
                _event_log(
                    "info",
                    "gw.tg.final_delivery.text_result",
                    session_id=session_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    chat_key_hash=_chat_key_hash(resolved_chat_key),
                    result="ok",
                    clean_text_len=len(clean_text),
                    clean_text_hash=clean_text_hash,
                    text_part_count=text_part_count,
                    sent_part_count=len(results),
                    send_duration_ms=max(0, _now_ms() - text_send_started_at_ms),
                )
            except Exception as e:
                text_send_failed = True
                log.warning("Failed to deliver to telegram for %s/%s: %s", session_id, thread_id, str(e))
                _event_log(
                    "warning",
                    "gw.tg.final_delivery.text_result",
                    session_id=session_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    chat_key_hash=_chat_key_hash(resolved_chat_key),
                    result="error",
                    clean_text_len=len(clean_text),
                    clean_text_hash=clean_text_hash,
                    text_part_count=text_part_count,
                    send_duration_ms=max(0, _now_ms() - text_send_started_at_ms),
                    err_kind=type(e).__name__,
                    err_msg=str(e),
                )

        if media_refs:
            workspace_root = self._workspace_root_for_session(session_id)
            root_resolved = None
            if workspace_root is not None:
                try:
                    root_resolved = workspace_root.resolve()
                except Exception:
                    root_resolved = workspace_root

            for ref in media_refs[:5]:
                attachment_attempt_count += 1
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
                        attachment_delivered_count += 1
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
                        pulled = await _pull_session_workspace_file_to_local(
                            session_id,
                            relative_path=rel,
                            dest_path=cand,
                            max_bytes=TELEGRAM_MAX_UPLOAD_BYTES,
                        )
                        if not pulled:
                            raise RuntimeError("media file not found")
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
                    attachment_delivered_count += 1
                except Exception as e:
                    attachment_failed_count += 1
                    log.warning(
                        "Failed to deliver telegram attachment for %s/%s (ref=%s): %s",
                        session_id,
                        thread_id,
                        ref,
                        str(e),
                    )

        overall_result = "ok"
        if text_send_failed and delivered_any:
            overall_result = "partial"
        elif text_send_failed and not delivered_any:
            overall_result = "error"
        elif not delivered_any:
            overall_result = "no_delivery"
        _event_log(
            "info" if overall_result in ("ok", "no_delivery") else "warning",
            "gw.tg.final_delivery.result",
            session_id=session_id,
            thread_id=thread_id,
            turn_id=turn_id,
            source_channel=source_channel,
            chat_key_hash=_chat_key_hash(resolved_chat_key),
            result=overall_result,
            delivered_any=delivered_any,
            clean_text_len=len(clean_text),
            clean_text_hash=clean_text_hash,
            text_part_count=text_part_count,
            media_ref_count=len(media_refs),
            attachment_attempt_count=attachment_attempt_count,
            attachment_delivered_count=attachment_delivered_count,
            attachment_failed_count=attachment_failed_count,
            duration_ms=max(0, _now_ms() - delivery_started_at_ms),
        )
        if not delivered_any:
            return

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
            live, _ = await _ensure_live_session(session_id, allow_create=True)
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
                {
                    "cwd": live.workspace_runtime_path,
                    "approvalPolicy": "never",
                    "sandbox": "danger-full-access",
                    "model": self.get_effective_model_for_session(session_id),
                },
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
            live, _ = await _ensure_live_session(session_id, allow_create=True)
            await self._ensure_initialized(live)

            # Ensure the thread exists/is loadable in the upstream app-server.
            await self._ensure_thread_loaded_or_resumed(live, thread_id)

            def _save_main(st: PersistedGatewayAutomationState) -> None:
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
        telegram_attachments: Optional[list[dict[str, Any]]] = None,
        defer_if_no_text: bool = False,
    ) -> dict[str, Any]:
        if telegram_attachments is not None and not isinstance(telegram_attachments, list):
            telegram_attachments = None
        if telegram_attachments:
            telegram_attachments = [x for x in telegram_attachments if isinstance(x, dict)]
            if not telegram_attachments:
                telegram_attachments = None

        user_text = text if isinstance(text, str) else ""
        text_has_content = bool(user_text.strip())

        source_channel_norm = source_channel.strip() if isinstance(source_channel, str) and source_channel.strip() else None
        source_chat_key_norm = source_chat_key.strip() if isinstance(source_chat_key, str) and source_chat_key.strip() else None
        is_telegram_source = bool(source_channel_norm and source_channel_norm.lower() in ("telegram", "tg"))

        local_attachments: list[dict[str, Any]] = []
        if telegram_attachments and is_telegram_source:
            staged = await self._stage_telegram_attachments(
                session_id=session_id,
                source_chat_key=source_chat_key_norm,
                telegram_attachments=telegram_attachments,
            )
            local_attachments = self._merge_staged_attachment_dicts(staged)
            if not local_attachments:
                raise RuntimeError("Failed to save Telegram attachments")

        if not text_has_content:
            if not local_attachments:
                raise ValueError("text must not be empty")
            if defer_if_no_text:
                pending_attachments = local_attachments
                if is_telegram_source and source_chat_key_norm:
                    pending_attachments = await self._append_pending_telegram_attachments(
                        session_id=session_id,
                        chat_key=source_chat_key_norm,
                        attachments=local_attachments,
                    )
                return {
                    "ok": True,
                    "queued": False,
                    "started": False,
                    "staged": True,
                    "stagedAttachmentCount": len(local_attachments),
                    "pendingAttachmentCount": len(pending_attachments),
                    "stagedAttachments": local_attachments,
                    "attachments": pending_attachments or local_attachments,
                }
            user_text = "<media:image>" if any(bool(item.get("isImage", False)) for item in local_attachments) else "<attachment>"

        pending_attachments: list[dict[str, Any]] = []
        if is_telegram_source and source_chat_key_norm:
            pending_attachments = await self._consume_pending_telegram_attachments(
                session_id=session_id,
                chat_key=source_chat_key_norm,
            )
        local_attachments = self._merge_staged_attachment_dicts([*pending_attachments, *local_attachments])

        live, _ = await _ensure_live_session(session_id, allow_create=True)
        await self._ensure_initialized(live)

        if target == "main" or not (isinstance(thread_id, str) and thread_id.strip()):
            thread_id = await self.ensure_main_thread(session_id)
        else:
            thread_id = thread_id.strip()

        lane = self.lane(session_id, thread_id)

        def _log_followup_queued() -> None:
            _event_log(
                "info",
                "gw.followup.queued",
                session_id=session_id,
                thread_id=thread_id,
                active_turn_id=lane.active_turn_id,
                active_kind=lane.active_turn_kind or lane.pending_client_turn_kind,
                source_channel=source_channel_norm,
                chat_key_hash=_chat_key_hash(source_chat_key_norm),
                followup_depth=len(lane.followups),
                text_len=len(user_text),
                attachment_count=len(local_attachments),
            )

        if lane.busy:
            lane.followups.append({
                "text": user_text,
                "source_channel": source_channel_norm,
                "source_chat_key": source_chat_key_norm,
                "local_attachments": local_attachments,
            })
            _log_followup_queued()
            return {
                "ok": True,
                "threadId": thread_id,
                "queued": True,
                "started": False,
                "followupDepth": len(lane.followups),
                "attachmentCount": len(local_attachments),
            }

        async with lane.lock:
            if lane.busy:
                lane.followups.append({
                    "text": user_text,
                    "source_channel": source_channel_norm,
                    "source_chat_key": source_chat_key_norm,
                    "local_attachments": local_attachments,
                })
                _log_followup_queued()
                return {
                    "ok": True,
                    "threadId": thread_id,
                    "queued": True,
                    "started": False,
                    "followupDepth": len(lane.followups),
                    "attachmentCount": len(local_attachments),
                }
            self._prepare_lane_for_new_turn(lane, kind=TURN_KIND_USER)
            self._set_pending_turn_source(
                lane,
                source_channel=source_channel_norm,
                source_chat_key=source_chat_key_norm,
            )
            self._mark_lane_busy(lane)

            try:
                await self._ensure_thread_loaded_or_resumed(live, thread_id)
                assembled = await self._assemble_turn_input(
                    session_id,
                    thread_id,
                    user_text=user_text,
                    heartbeat=False,
                    source_channel=source_channel_norm,
                    source_chat_key=source_chat_key_norm,
                    local_attachments=local_attachments,
                )

                input_items: list[dict[str, Any]] = [{"type": "text", "text": assembled}]
                input_items.extend(self._attachment_input_items(local_attachments))

                resp = await self._rpc(
                    live,
                    "turn/start",
                    self._turn_start_params(session_id=session_id, thread_id=thread_id, input_items=input_items, live=live),
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
                    self._remember_turn_source_context(
                        session_id=session_id,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        source_channel=source_channel_norm,
                        source_chat_key=source_chat_key_norm,
                    )
                await self._touch_last_active_target(
                    session_id=session_id,
                    thread_id=thread_id,
                    source_channel=source_channel_norm,
                    source_chat_key=source_chat_key_norm,
                )
                return {
                    "ok": True,
                    "threadId": thread_id,
                    "queued": False,
                    "started": True,
                    "turnId": turn_id,
                    "attachmentCount": len(local_attachments),
                }
            except Exception:
                self._mark_lane_idle(lane)
                raise

    async def _stage_telegram_attachments(
        self,
        *,
        session_id: str,
        source_chat_key: Optional[str],
        telegram_attachments: list[dict[str, Any]],
    ) -> list[PersistedStagedAttachment]:
        if not telegram_attachments:
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

        out: list[PersistedStagedAttachment] = []
        seen: set[str] = set()

        mime_to_ext.update(
            {
                "application/pdf": ".pdf",
                "text/plain": ".txt",
                "text/markdown": ".md",
                "application/json": ".json",
                "text/csv": ".csv",
                "application/zip": ".zip",
                "application/x-zip-compressed": ".zip",
                "application/gzip": ".gz",
                "application/x-gzip": ".gz",
                "audio/mpeg": ".mp3",
                "audio/mp4": ".m4a",
                "audio/ogg": ".ogg",
                "video/mp4": ".mp4",
                "video/webm": ".webm",
            }
        )

        for idx, ref in enumerate(telegram_attachments[:8], start=1):
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

            kind = ref.get("kind") or "file"
            if not isinstance(kind, str) or not kind.strip():
                kind = "file"
            kind_part = _sanitize_path_part(kind, fallback="file", max_len=16)

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
            mime_norm = mime_type.strip().lower() if isinstance(mime_type, str) and mime_type.strip() else None
            is_image = bool(kind == "photo" or (mime_norm and mime_norm.startswith("image/")) or ext in TELEGRAM_IMAGE_EXTS)
            if not ext:
                ext = ".jpg" if is_image else ".bin"
            if not ext.startswith(".") or len(ext) > 10:
                ext = ".jpg" if is_image else ".bin"

            if not isinstance(file_name, str) or not file_name.strip():
                file_name = Path(file_path).name or f"{kind_part}{ext}"
            else:
                file_name = file_name.strip()

            file_stem = _sanitize_path_part(Path(file_name).stem, fallback=kind_part, max_len=48)

            ts = _now_ms()
            dest = base_dir / f"{ts}_{idx:02d}_{source_part}_{file_stem}_{unique_part}{ext}"
            try:
                downloaded_size = await asyncio.to_thread(
                    _telegram_download_file_to_path_sync,
                    token,
                    file_path,
                    dest,
                    max_bytes=TELEGRAM_MAX_DOWNLOAD_BYTES,
                )
            except Exception as e:
                log.warning("Telegram download failed for fileId=%s: %s", file_id, str(e))
                continue

            rel = None
            try:
                rel = dest.relative_to(root).as_posix()
            except Exception:
                rel = str(dest)
            rel_norm = _normalize_workspace_relative_path(rel)
            if not rel_norm:
                continue
            await _sync_session_workspace_file(session_id, root, dest)
            out.append(
                PersistedStagedAttachment(
                    path=rel_norm,
                    file_name=file_name,
                    mime_type=mime_type.strip() if isinstance(mime_type, str) and mime_type.strip() else None,
                    file_size=int(file_size) if isinstance(file_size, int) and file_size >= 0 else int(downloaded_size),
                    source=source.strip(),
                    is_image=is_image,
                    file_unique_id=unique_id.strip() if isinstance(unique_id, str) and unique_id.strip() else None,
                    staged_at_ms=ts,
                )
            )

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

    def _pop_next_followup(self, lane: ThreadLane) -> Optional[dict[str, Any]]:
        while lane.followups:
            item = lane.followups.popleft()
            if isinstance(item, str):
                text = item.strip()
                if text:
                    return {
                        "text": text,
                        "source_channel": None,
                        "source_chat_key": None,
                        "local_attachments": [],
                    }
                continue
            if not isinstance(item, dict):
                continue

            text_raw = item.get("text")
            text = text_raw.strip() if isinstance(text_raw, str) and text_raw.strip() else ""
            attachments_raw = item.get("local_attachments")
            local_attachments: list[dict[str, Any]] = []
            if isinstance(attachments_raw, list):
                local_attachments = [x for x in attachments_raw if isinstance(x, (dict, PersistedStagedAttachment))]
            if not text and not local_attachments:
                continue

            source_channel = item.get("source_channel")
            if not isinstance(source_channel, str) or not source_channel.strip():
                source_channel = None
            else:
                source_channel = source_channel.strip()

            source_chat_key = item.get("source_chat_key")
            if not isinstance(source_chat_key, str) or not source_chat_key.strip():
                source_chat_key = None
            else:
                source_chat_key = source_chat_key.strip()

            return {
                "text": text,
                "source_channel": source_channel,
                "source_chat_key": source_chat_key,
                "local_attachments": local_attachments,
            }
        return None

    async def _process_lane_after_turn(self, session_id: str, thread_id: str) -> None:
        lane = self.lane(session_id, thread_id)
        if lane.busy:
            return
        async with lane.lock:
            if lane.busy:
                return
            next_followup = self._pop_next_followup(lane)
            if next_followup is None:
                return
            # Process queued user inputs strictly FIFO. Once groups share one main
            # thread across topics, batching follow-ups would collapse distinct topic
            # sources into a single turn and misroute replies.
            merged = str(next_followup.get("text") or "").strip()
            telegram_channel = next_followup.get("source_channel")
            telegram_chat_key = next_followup.get("source_chat_key")
            local_attachments = self._merge_staged_attachment_dicts(
                next_followup.get("local_attachments") if isinstance(next_followup.get("local_attachments"), list) else []
            )
            _event_log(
                "info",
                "gw.followup.dequeued",
                session_id=session_id,
                thread_id=thread_id,
                source_channel=telegram_channel if isinstance(telegram_channel, str) and telegram_channel.strip() else None,
                chat_key_hash=_chat_key_hash(telegram_chat_key),
                followup_depth_before=len(lane.followups) + 1,
                remaining_followup_depth=len(lane.followups),
                text_len=len(merged),
                attachment_count=len(local_attachments),
            )

            live, _ = await _ensure_live_session(session_id, allow_create=True)
            await self._ensure_initialized(live)

            self._prepare_lane_for_new_turn(lane, kind=TURN_KIND_USER)
            self._set_pending_turn_source(
                lane,
                source_channel=telegram_channel,
                source_chat_key=telegram_chat_key,
            )
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
                    local_attachments=local_attachments,
                )

                input_items: list[dict[str, Any]] = [{"type": "text", "text": assembled}]
                input_items.extend(self._attachment_input_items(local_attachments))

                resp = await self._rpc(
                    live,
                    "turn/start",
                    self._turn_start_params(session_id=session_id, thread_id=thread_id, input_items=input_items, live=live),
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
                self._remember_turn_source_context(
                    session_id=session_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    source_channel=telegram_channel,
                    source_chat_key=telegram_chat_key,
                )
                await self._touch_last_active_target(
                    session_id=session_id,
                    thread_id=thread_id,
                    source_channel=telegram_channel,
                    source_chat_key=telegram_chat_key,
                )
            except Exception:
                self._mark_lane_idle(lane)
                log.exception("Failed to start follow-up turn for %s/%s", session_id, thread_id)

    async def _read_project_context_block(self, *, session_id: str, include_heartbeat: bool) -> str:
        root = self._workspace_root_for_session(session_id)
        if root is None:
            return ""

        try:
            await _sync_session_prompt_context_to_local(session_id, root, include_heartbeat=include_heartbeat)
        except Exception:
            log.exception("Failed to sync remote prompt context for session %s", session_id)

        try:
            await self._sync_workspace_agents_file_at(root)
            await _sync_session_workspace_file(session_id, root, root / AGENTS_FILENAME)
        except Exception:
            log.exception("Failed to refresh AGENTS.md from template at %s", str(root))

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
                            '交互式 CLI：先用 command="system.run" + params={"argv":[...],"pty":true,"yieldMs":0} 拿到 jobId，再继续调用 process.write / process.send_keys / process.submit / process.paste。',
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
                location = f"/workspace/{SKILLS_DIRNAME}/{skill_dir.name}/{SKILL_MD_FILENAME}"
                skills.append({"name": name, "description": description, "location": location})

            if not skills:
                return ""

            lines: list[str] = [
                "## Skills",
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
        local_attachments: Optional[list[Any]] = None,
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

        turn_kind = "heartbeat" if heartbeat else "user"
        blocks.append("\n".join(["[TURN]", f"TURN_KIND: {turn_kind}", "[/TURN]"]).strip())

        user_text_clean = user_text.strip() if isinstance(user_text, str) else ""
        if heartbeat:
            user_text_clean = ""
        blocks.append("\n".join(["[USER_TEXT]", user_text_clean, "[/USER_TEXT]"]).strip())

        if not heartbeat:
            attachment_block = self._format_attachment_block(local_attachments or [])
            if attachment_block:
                blocks.append(attachment_block)

        if heartbeat:
            blocks.append(
                "HEARTBEAT: You are running a background heartbeat. Read and follow HEARTBEAT.md in # Project Context."
            )

        if drained:
            blocks.append("System events (batched, highest priority):\n" + "\n".join(drained))

        if heartbeat:
                blocks.append(
                    "\n".join(
                        [
                            "Heartbeat response contract:",
                            "- TURN_KIND=heartbeat (background automation tick).",
                            "- USER_TEXT is provided in the [USER_TEXT] block above and MUST be empty in a heartbeat turn.",
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
                        "- USER_TEXT is provided in the [USER_TEXT] block above and is the user's message for this turn.",
                        "- If an [ATTACHMENTS] block is present, those files already exist in the workspace and are part of this turn.",
                        "- Prefer referencing uploaded files by their workspace-relative paths from [ATTACHMENTS].",
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

    async def _ensure_initialized(self, live: LiveRuntimeSession) -> None:
        if live.initialized_result is not None and live.handshake_done:
            return
        # Send initialize and cache result.
        req = {"method": "initialize", "id": None, "params": {"clientInfo": {"name": "argus_gateway", "title": "Argus Gateway Automation", "version": ARGUS_VERSION}}}
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

    async def _rpc(self, live: LiveRuntimeSession, method: str, params: Any) -> Any:
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

    def _turn_start_params(self, *, session_id: str, thread_id: str, input_items: list[dict[str, Any]], live: LiveRuntimeSession) -> dict[str, Any]:
        return {
            "threadId": thread_id,
            "input": input_items,
            "cwd": live.workspace_runtime_path,
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "dangerFullAccess"},
            "model": self.get_effective_model_for_session(session_id),
        }

    async def _is_thread_loaded(self, live: LiveRuntimeSession, thread_id: str) -> bool:
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

    async def _ensure_thread_loaded_or_resumed(self, live: LiveRuntimeSession, thread_id: str) -> None:
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
        _require_runtime_automation_support(feature="Cron automation")

        sid = session_id.strip() if isinstance(session_id, str) else ""
        if not sid:
            raise ValueError("session_id must not be empty")

        run_id = uuid.uuid4().hex[:12]

        live, _ = await _ensure_live_session(sid, allow_create=True)
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
            {
                "cwd": live.workspace_runtime_path,
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
                "model": self.get_effective_model_for_session(sid),
            },
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
            self._prepare_lane_for_new_turn(lane, kind=TURN_KIND_CRON)
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
                    self._turn_start_params(
                        session_id=sid,
                        thread_id=thread_id,
                        input_items=[{"type": "text", "text": assembled}],
                        live=live,
                    ),
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
            await _pull_session_workspace_file_to_local(
                session_id,
                relative_path=HEARTBEAT_FILENAME,
                dest_path=p,
                max_bytes=512 * 1024,
            )
        except Exception:
            log.exception("Failed to sync HEARTBEAT.md for session %s", session_id)
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
        if not _provisioner_supports_runtime_automation():
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
                    live, _ = await _ensure_live_session(session_id, allow_create=True)
                    await self._ensure_initialized(live)
                except Exception as e:
                    self._note_session_failure(session_id, what="Heartbeat prepare", err=e)
                    continue

                self._prepare_lane_for_new_turn(lane, kind=TURN_KIND_HEARTBEAT)
                self._mark_lane_busy(lane)
                try:
                    await self._ensure_thread_loaded_or_resumed(live, main_tid)
                    assembled = await self._assemble_turn_input(session_id, main_tid, user_text="", heartbeat=True)
                    resp = await self._rpc(
                        live,
                        "turn/start",
                        self._turn_start_params(
                            session_id=session_id,
                            thread_id=main_tid,
                            input_items=[{"type": "text", "text": assembled}],
                            live=live,
                        ),
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
    scoped_session_id: Optional[str] = None
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
        self._nodes_by_key: dict[tuple[str, str], NodeSession] = {}
        self._nodes_by_conn: dict[str, tuple[str, str]] = {}
        self._pending: dict[str, tuple[tuple[str, str], asyncio.Future[NodeInvokeResult]]] = {}

    def _norm_scope(self, scope_session_id: Optional[str]) -> str:
        return scope_session_id.strip() if isinstance(scope_session_id, str) and scope_session_id.strip() else ""

    async def register(self, session: NodeSession) -> None:
        key = (self._norm_scope(session.scoped_session_id), session.node_id)
        async with self._lock:
            existing = self._nodes_by_key.get(key)
            if existing is not None:
                self._nodes_by_conn.pop(existing.conn_id, None)
                self._nodes_by_key.pop(key, None)
                for rid, (pending_key, fut) in list(self._pending.items()):
                    if pending_key != key:
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
            self._nodes_by_key[key] = session
            self._nodes_by_conn[session.conn_id] = key

    async def unregister(self, conn_id: str) -> Optional[NodeSession]:
        async with self._lock:
            key = self._nodes_by_conn.pop(conn_id, None)
            if not key:
                return None
            removed = self._nodes_by_key.pop(key, None)
            for rid, (pending_key, fut) in list(self._pending.items()):
                if pending_key != key:
                    continue
                if fut.done():
                    self._pending.pop(rid, None)
                    continue
                fut.set_result(
                    NodeInvokeResult(ok=False, error={"code": "NODE_DISCONNECTED", "message": "node disconnected"})
                )
                self._pending.pop(rid, None)
            return removed

    async def list_connected(self, *, scope_session_id: Optional[str] = None) -> list[dict[str, Any]]:
        scope = self._norm_scope(scope_session_id) if scope_session_id is not None else None
        async with self._lock:
            sessions = list(self._nodes_by_key.items())
        out: list[dict[str, Any]] = []
        for (sid, _), s in sessions:
            if scope is not None and sid != scope:
                continue
            out.append(
                {
                    "nodeId": s.node_id,
                    "sessionId": sid or None,
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

    async def list_runtime_node_ids(self, *, scope_session_id: Optional[str] = None) -> list[str]:
        scope = self._norm_scope(scope_session_id) if scope_session_id is not None else None
        async with self._lock:
            ids = [
                s.node_id
                for (sid, _), s in self._nodes_by_key.items()
                if (s.node_id or "").startswith("runtime:") and (scope is None or sid == scope)
            ]
        ids.sort()
        return ids

    async def resolve_self_runtime_node_id(self, *, scope_session_id: Optional[str] = None) -> Optional[str]:
        scope = self._norm_scope(scope_session_id) if scope_session_id is not None else None
        if scope:
            cand = f"runtime:{scope}"
            async with self._lock:
                if (scope, cand) in self._nodes_by_key:
                    return cand

        runtime_ids = await self.list_runtime_node_ids(scope_session_id=scope)
        if len(runtime_ids) == 1:
            return runtime_ids[0]
        return None

    async def resolve_implicit_runtime_node_id(self, *, scope_session_id: Optional[str] = None) -> Optional[str]:
        runtime_ids = await self.list_runtime_node_ids(scope_session_id=scope_session_id)
        if len(runtime_ids) == 1:
            return runtime_ids[0]
        return None

    async def resolve_node_id(self, ident: str, *, scope_session_id: Optional[str] = None) -> Optional[str]:
        key = (ident or "").strip()
        if not key:
            return None
        scope = self._norm_scope(scope_session_id) if scope_session_id is not None else None
        async with self._lock:
            exact = [
                s.node_id
                for (sid, node_id), s in self._nodes_by_key.items()
                if node_id == key and (scope is None or sid == scope)
            ]
            if len(exact) == 1:
                return exact[0]
            if len(exact) > 1:
                return None

            matches = [
                s.node_id
                for (sid, _), s in self._nodes_by_key.items()
                if (s.display_name or "").strip().lower() == key.lower() and (scope is None or sid == scope)
            ]
        if len(matches) == 1:
            return matches[0]
        return None

    async def resolve_scope_session_id(self, node_id: str, *, scope_session_id: Optional[str] = None) -> Optional[str]:
        nid = (node_id or "").strip()
        if not nid:
            return None
        scope = self._norm_scope(scope_session_id) if scope_session_id is not None else None
        async with self._lock:
            if scope is not None:
                if (scope, nid) in self._nodes_by_key:
                    return scope or None
                return None
            matches = [sid or None for (sid, existing_node_id), _ in self._nodes_by_key.items() if existing_node_id == nid]
        if len(matches) == 1:
            return matches[0]
        return None

    async def touch(self, node_id: str, *, scope_session_id: Optional[str] = None) -> None:
        scope = self._norm_scope(scope_session_id) if scope_session_id is not None else None
        now_ms = int(time.time() * 1000)
        async with self._lock:
            if scope is not None:
                s = self._nodes_by_key.get((scope, node_id))
                if s is not None:
                    s.last_seen_ms = now_ms
                return
            # Global touch: only if unique across scopes
            matches = [s for (sid, nid), s in self._nodes_by_key.items() if nid == node_id]
            if len(matches) == 1:
                matches[0].last_seen_ms = now_ms

    async def invoke(
        self,
        *,
        node_id: str,
        scope_session_id: Optional[str] = None,
        command: str,
        params: Any = None,
        timeout_ms: Optional[int] = None,
    ) -> NodeInvokeResult:
        scope = self._norm_scope(scope_session_id) if scope_session_id is not None else None
        async with self._lock:
            if scope is not None:
                session = self._nodes_by_key.get((scope, node_id))
                key = (scope, node_id)
            else:
                matches = [(k, s) for k, s in self._nodes_by_key.items() if k[1] == node_id]
                if len(matches) != 1:
                    return NodeInvokeResult(ok=False, error={"code": "NOT_CONNECTED", "message": "node not connected"})
                key, session = matches[0]
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
            self._pending[request_id] = (key, fut)
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

def _configured_provision_mode() -> str:
    return (os.getenv("ARGUS_PROVISION_MODE") or "").strip().lower()


def _configured_home_host_path() -> Optional[str]:
    return (
        os.getenv("ARGUS_FUGUE_HOME_PATH")
        or os.getenv("ARGUS_HOME_HOST_PATH")
        or None
    )


def _configured_workspace_host_path() -> Optional[str]:
    return (
        os.getenv("ARGUS_FUGUE_WORKSPACE_PATH")
        or os.getenv("ARGUS_WORKSPACE_HOST_PATH")
        or None
    )


def _running_in_kubernetes() -> bool:
    host = (os.getenv("KUBERNETES_SERVICE_HOST") or "").strip()
    return bool(host and os.path.exists(KUBERNETES_SERVICEACCOUNT_TOKEN_PATH))


def _docker_endpoint_hint() -> Optional[str]:
    docker_host = (os.getenv("DOCKER_HOST") or "").strip()
    if docker_host:
        return f"DOCKER_HOST={docker_host}"
    docker_sock = (os.getenv("DOCKER_SOCK") or "/var/run/docker.sock").strip() or "/var/run/docker.sock"
    if os.path.exists(docker_sock):
        return docker_sock
    return None


def _fugue_detection_values() -> dict[str, str]:
    return {
        "base_url": (os.getenv("ARGUS_FUGUE_BASE_URL") or os.getenv("FUGUE_BASE_URL") or "").strip().rstrip("/"),
        "token": (os.getenv("ARGUS_FUGUE_TOKEN") or os.getenv("FUGUE_TOKEN") or "").strip(),
        "project_id": (os.getenv("ARGUS_FUGUE_PROJECT_ID") or os.getenv("FUGUE_PROJECT_ID") or "").strip(),
        "image": (os.getenv("ARGUS_FUGUE_RUNTIME_IMAGE") or os.getenv("ARGUS_RUNTIME_IMAGE") or "").strip(),
        "runtime_app_id": (os.getenv("ARGUS_FUGUE_RUNTIME_APP_ID") or "").strip(),
        "runtime_app_name": (os.getenv("ARGUS_FUGUE_RUNTIME_APP_NAME") or "").strip(),
        "runtime_compose_service": (os.getenv("ARGUS_FUGUE_RUNTIME_COMPOSE_SERVICE") or "").strip(),
        "gateway_internal_host": (
            os.getenv("ARGUS_GATEWAY_INTERNAL_HOST") or os.getenv("ARGUS_FUGUE_GATEWAY_INTERNAL_HOST") or ""
        ).strip(),
        "gateway_compose_service": (os.getenv("ARGUS_FUGUE_GATEWAY_COMPOSE_SERVICE") or "").strip(),
    }


def _fugue_detection_missing_fields() -> list[str]:
    values = _fugue_detection_values()
    required_fields = {
        "base_url": "ARGUS_FUGUE_BASE_URL/FUGUE_BASE_URL",
        "token": "ARGUS_FUGUE_TOKEN/FUGUE_TOKEN",
        "project_id": "ARGUS_FUGUE_PROJECT_ID/FUGUE_PROJECT_ID",
    }
    missing = [env_name for field, env_name in required_fields.items() if not values.get(field)]
    has_runtime_source = any(
        bool(values.get(field)) for field in ("image", "runtime_app_id", "runtime_app_name", "runtime_compose_service")
    )
    if not has_runtime_source:
        missing.append(
            "ARGUS_FUGUE_RUNTIME_IMAGE/ARGUS_RUNTIME_IMAGE or "
            "ARGUS_FUGUE_RUNTIME_APP_ID/ARGUS_FUGUE_RUNTIME_APP_NAME/ARGUS_FUGUE_RUNTIME_COMPOSE_SERVICE"
        )
    has_gateway_target = any(bool(values.get(field)) for field in ("gateway_internal_host", "gateway_compose_service"))
    if not has_gateway_target:
        missing.append(
            "ARGUS_GATEWAY_INTERNAL_HOST/ARGUS_FUGUE_GATEWAY_INTERNAL_HOST or ARGUS_FUGUE_GATEWAY_COMPOSE_SERVICE"
        )
    return missing


def _has_any_fugue_detection_env() -> bool:
    values = _fugue_detection_values()
    return any(bool(value) for value in values.values())


@functools.lru_cache(maxsize=1)
def _resolve_provision_mode() -> ProvisionModeResolution:
    raw_mode = _configured_provision_mode()
    explicit_mode = raw_mode or "auto"

    if explicit_mode in ("static", "docker", "fugue"):
        return ProvisionModeResolution(
            raw_mode=explicit_mode,
            resolved_mode=explicit_mode,
            reason=f"explicit ARGUS_PROVISION_MODE={explicit_mode}",
        )

    if explicit_mode not in ("", "auto"):
        raise RuntimeError(
            f"Unsupported ARGUS_PROVISION_MODE={raw_mode!r}; supported modes are: 'auto', 'static', 'docker', 'fugue'"
        )

    warnings: list[str] = []
    fugue_missing = _fugue_detection_missing_fields()
    if not fugue_missing:
        reason = "auto-detected fugue from configured Fugue API settings"
        if _running_in_kubernetes():
            reason += " inside Kubernetes"
        return ProvisionModeResolution(
            raw_mode="auto",
            resolved_mode="fugue",
            reason=reason,
        )

    if _has_any_fugue_detection_env():
        warnings.append(
            "Fugue environment detected but incomplete; missing "
            + ", ".join(fugue_missing)
            + ". Falling back to another provisioner."
        )

    docker_hint = _docker_endpoint_hint()
    if docker_hint:
        return ProvisionModeResolution(
            raw_mode="auto",
            resolved_mode="docker",
            reason=f"auto-detected docker from {docker_hint}",
            warnings=tuple(warnings),
        )

    if _running_in_kubernetes():
        warnings.append(
            "Kubernetes runtime detected but Fugue configuration is incomplete and no Docker endpoint is available; falling back to static mode."
        )

    return ProvisionModeResolution(
        raw_mode="auto",
        resolved_mode="static",
        reason="auto-fell back to static because no complete Fugue configuration or Docker endpoint was detected",
        warnings=tuple(warnings),
    )


def _provision_mode() -> str:
    return _resolve_provision_mode().resolved_mode


def _gc_delete_orphan_runtimes_mode() -> str:
    raw = (os.getenv("ARGUS_GC_DELETE_ORPHAN_RUNTIMES") or "").strip().lower()
    if raw in ("0", "false", "no", "off", "disabled"):
        return "off"
    if not raw:
        # Default: enabled (delete).
        return "delete"
    if raw in ("dry-run", "dryrun", "dry_run", "plan", "preview"):
        return "dry-run"
    if raw in ("1", "true", "yes", "on", "delete"):
        return "delete"
    log.warning(
        "Invalid ARGUS_GC_DELETE_ORPHAN_RUNTIMES=%r; expected one of: off|dry-run|true",
        raw,
    )
    return "off"


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


class ProvisionerOperationUnsupportedError(RuntimeError):
    pass


class RuntimeProvisioner:
    name = "static"

    @property
    def supports_user_agents(self) -> bool:
        return False

    @property
    def supports_runtime_automation(self) -> bool:
        return False

    @property
    def manages_runtime_sessions(self) -> bool:
        return False

    @property
    def requires_runtime_recreation_on_workspace_change(self) -> bool:
        return False

    async def ensure_live_session(self, session_id: str, *, allow_create: bool) -> tuple[LiveRuntimeSession, bool]:
        raise ProvisionerOperationUnsupportedError(
            f"Provision mode '{self.name}' does not manage runtime sessions"
        )

    async def list_sessions(self) -> list[dict[str, Any]]:
        raise ProvisionerOperationUnsupportedError(
            f"Provision mode '{self.name}' does not expose managed sessions"
        )

    async def delete_session(self, session_id: str) -> None:
        raise ProvisionerOperationUnsupportedError(
            f"Provision mode '{self.name}' does not support deleting managed sessions"
        )

    async def remove_runtime(self, session_id: str) -> bool:
        raise ProvisionerOperationUnsupportedError(
            f"Provision mode '{self.name}' does not support runtime removal"
        )

    async def restart_runtime(self, session_id: str) -> bool:
        raise ProvisionerOperationUnsupportedError(
            f"Provision mode '{self.name}' does not support runtime restart"
        )


class StaticRuntimeProvisioner(RuntimeProvisioner):
    def __init__(self, *, name: str = "static") -> None:
        self.name = name or "static"


class DockerRuntimeProvisioner(RuntimeProvisioner):
    name = "docker"

    @property
    def supports_user_agents(self) -> bool:
        return True

    @property
    def supports_runtime_automation(self) -> bool:
        return True

    @property
    def manages_runtime_sessions(self) -> bool:
        return True

    @property
    def requires_runtime_recreation_on_workspace_change(self) -> bool:
        return True

    async def ensure_live_session(self, session_id: str, *, allow_create: bool) -> tuple[LiveRuntimeSession, bool]:
        return await _ensure_live_docker_session(session_id, allow_create=allow_create)

    async def list_sessions(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(_docker_list_argus_containers_sync)

    async def delete_session(self, session_id: str) -> None:
        container = await asyncio.to_thread(_docker_get_container_by_session_sync, session_id)
        if container is None:
            raise KeyError("Unknown session")
        await _close_live_session(session_id)
        await asyncio.to_thread(_docker_remove_container_sync, container)

    async def remove_runtime(self, session_id: str) -> bool:
        container = await asyncio.to_thread(_docker_get_container_by_session_sync, session_id)
        if container is None:
            return False
        await asyncio.to_thread(_docker_remove_container_sync, container)
        return True

    async def restart_runtime(self, session_id: str) -> bool:
        container = await asyncio.to_thread(_docker_get_container_by_session_sync, session_id)
        if container is None:
            return False
        await asyncio.to_thread(container.restart, timeout=10)
        return True


class FugueRuntimeProvisioner(RuntimeProvisioner):
    name = "fugue"

    @property
    def supports_user_agents(self) -> bool:
        return True

    @property
    def supports_runtime_automation(self) -> bool:
        return True

    @property
    def manages_runtime_sessions(self) -> bool:
        return True

    async def ensure_live_session(self, session_id: str, *, allow_create: bool) -> tuple[LiveRuntimeSession, bool]:
        return await _ensure_live_fugue_session(session_id, allow_create=allow_create)

    async def list_sessions(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(_fugue_list_argus_sessions_sync)

    async def delete_session(self, session_id: str) -> None:
        deleted = await asyncio.to_thread(_fugue_delete_session_sync, session_id, True)
        if not deleted:
            raise KeyError("Unknown session")
        await _close_live_session(session_id)

    async def remove_runtime(self, session_id: str) -> bool:
        return await asyncio.to_thread(_fugue_delete_session_sync, session_id, True)

    async def restart_runtime(self, session_id: str) -> bool:
        return await asyncio.to_thread(_fugue_restart_session_sync, session_id)


def _build_runtime_provisioner() -> RuntimeProvisioner:
    mode = _provision_mode()
    if mode in ("", "static"):
        return StaticRuntimeProvisioner()
    if mode == "docker":
        return DockerRuntimeProvisioner()
    if mode == "fugue":
        return FugueRuntimeProvisioner()
    raise RuntimeError(
        f"Unsupported resolved provision mode {mode!r}; supported modes are: 'static', 'docker', 'fugue'"
    )


def _runtime_provisioner() -> RuntimeProvisioner:
    provisioner = getattr(app.state, "runtime_provisioner", None)
    if provisioner is None:
        provisioner = _build_runtime_provisioner()
        app.state.runtime_provisioner = provisioner
    return provisioner


def _provisioner_mode_name() -> str:
    return _runtime_provisioner().name


def _provisioner_supports_user_agents() -> bool:
    return _runtime_provisioner().supports_user_agents


def _provisioner_supports_runtime_automation() -> bool:
    return _runtime_provisioner().supports_runtime_automation


def _provisioner_manages_runtime_sessions() -> bool:
    return _runtime_provisioner().manages_runtime_sessions


def _provisioner_requires_runtime_recreation_on_workspace_change() -> bool:
    return _runtime_provisioner().requires_runtime_recreation_on_workspace_change


def _require_user_agent_support() -> None:
    if not _provisioner_supports_user_agents():
        raise RuntimeError(f"User agents are not supported in provision mode '{_provisioner_mode_name()}'")


def _require_runtime_automation_support(*, feature: str) -> None:
    if not _provisioner_supports_runtime_automation():
        raise RuntimeError(f"{feature} is not supported in provision mode '{_provisioner_mode_name()}'")


async def _ensure_live_session(session_id: str, *, allow_create: bool) -> tuple[LiveRuntimeSession, bool]:
    return await _runtime_provisioner().ensure_live_session(session_id, allow_create=allow_create)


async def _list_managed_sessions() -> list[dict[str, Any]]:
    return await _runtime_provisioner().list_sessions()


async def _delete_managed_session(session_id: str) -> None:
    await _runtime_provisioner().delete_session(session_id)


async def _remove_managed_runtime(session_id: str) -> bool:
    return await _runtime_provisioner().remove_runtime(session_id)


async def _restart_managed_runtime(session_id: str) -> bool:
    return await _runtime_provisioner().restart_runtime(session_id)


def _fugue_workspace_enabled() -> bool:
    return isinstance(_runtime_provisioner(), FugueRuntimeProvisioner)


def _fugue_relative_workspace_path(cfg: FugueProvisionConfig, workspace_path: str) -> Optional[str]:
    path_str = str(workspace_path or "").replace("\\", "/").strip()
    if not path_str:
        return None
    base = cfg.workspace_mount_path.rstrip("/")
    if path_str == base:
        return None
    prefix = base + "/"
    if not path_str.startswith(prefix):
        return None
    return _normalize_workspace_relative_path(path_str[len(prefix) :])


async def _sync_session_workspace_snapshot(session_id: str, root: Path) -> None:
    if not _fugue_workspace_enabled():
        return
    cfg = _fugue_cfg()
    try:
        await asyncio.to_thread(_fugue_push_workspace_snapshot_sync, cfg, session_id, root)
    except KeyError:
        return


async def _sync_session_workspace_file(session_id: str, root: Path, path_obj: Path) -> None:
    if not _fugue_workspace_enabled():
        return
    rel = _relative_workspace_path(root, path_obj)
    if not rel or not path_obj.is_file():
        return
    data = await asyncio.to_thread(path_obj.read_bytes)
    cfg = _fugue_cfg()
    try:
        await asyncio.to_thread(_fugue_put_workspace_file_sync, cfg, session_id, rel, data)
    except KeyError:
        return


async def _pull_session_workspace_file_to_local(
    session_id: str,
    *,
    relative_path: str,
    dest_path: Path,
    max_bytes: int,
) -> bool:
    if not _fugue_workspace_enabled():
        return False
    rel = _normalize_workspace_relative_path(relative_path)
    if not rel:
        return False
    cfg = _fugue_cfg()
    data = await asyncio.to_thread(_fugue_read_workspace_file_bytes_sync, cfg, session_id, rel, max_bytes=max_bytes)
    if data is None:
        return False

    def _write() -> None:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(dest_path, data)

    await asyncio.to_thread(_write)
    return True


async def _sync_session_prompt_context_to_local(session_id: str, root: Path, *, include_heartbeat: bool) -> None:
    if not _fugue_workspace_enabled():
        return
    filenames = list(PROJECT_CONTEXT_FILENAMES)
    if include_heartbeat:
        filenames.append(HEARTBEAT_FILENAME)
    for name in filenames:
        try:
            await _pull_session_workspace_file_to_local(
                session_id,
                relative_path=name,
                dest_path=root / name,
                max_bytes=512 * 1024,
            )
        except Exception:
            log.exception("Failed to sync workspace file %s for session %s", name, session_id)

    try:
        cfg = _fugue_cfg()
        entries = await asyncio.to_thread(_fugue_list_workspace_tree_sync, cfg, session_id, SKILLS_DIRNAME, depth=3)
    except Exception:
        log.exception("Failed to sync skills tree for session %s", session_id)
        return

    for entry in entries:
        path_val = entry.get("path")
        kind = str(entry.get("kind") or "").strip().lower()
        if kind != "file" or not isinstance(path_val, str) or not path_val.endswith(f"/{SKILL_MD_FILENAME}"):
            continue
        rel = _fugue_relative_workspace_path(cfg, path_val)
        if not rel:
            continue
        try:
            await _pull_session_workspace_file_to_local(
                session_id,
                relative_path=rel,
                dest_path=root / rel,
                max_bytes=512 * 1024,
            )
        except Exception:
            log.exception("Failed to sync skill file %s for session %s", rel, session_id)


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


app = FastAPI(title="Argus gateway", version=ARGUS_VERSION)

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
    mode_resolution = _resolve_provision_mode()
    app.state.runtime_provisioner = _build_runtime_provisioner()
    app.state.sessions_lock = asyncio.Lock()
    app.state.sessions: dict[str, LiveRuntimeSession] = {}
    app.state.node_registry = NodeRegistry()
    app.state.mcp_lock = asyncio.Lock()
    app.state.mcp_sessions: dict[str, McpSessionState] = {}
    log.info(
        "Argus gateway starting (version=%s, configured_provision_mode=%s, provision_mode=%s, reason=%s)",
        ARGUS_VERSION,
        mode_resolution.raw_mode,
        _provisioner_mode_name(),
        mode_resolution.reason,
    )
    for warning in mode_resolution.warnings:
        log.warning("%s", warning)
    if _provisioner_manages_runtime_sessions() and not (os.getenv("ARGUS_RUNTIME_CMD") or "").strip():
        log.warning(
            "Managed runtime provisioning is enabled (mode=%s) but ARGUS_RUNTIME_CMD is unset; "
            "session startup may fail because the runtime image requires APP_SERVER_CMD.",
            _provisioner_mode_name(),
        )

    home_host_path = _configured_home_host_path()
    workspace_host_path = _configured_workspace_host_path()
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


@app.post("/openai/v1/responses")
def openai_responses_proxy(request: Request, body: Any = Body(...)):
    _openai_proxy_require_token(request)

    if request.url.query:
        raise HTTPException(status_code=403, detail="Query strings are not allowed on this endpoint")

    automation = _get_automation_or_500()
    scoped_session_id = getattr(request.state, "openai_scoped_session_id", None)
    try:
        target = automation.resolve_openai_proxy_target_for_session(str(scoped_session_id or ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    upstream_url = str(target.get("upstreamUrl") or "").strip()
    openai_api_key = _normalize_channel_api_key(target.get("apiKey"))
    if not upstream_url or not openai_api_key:
        raise HTTPException(status_code=503, detail="OpenAI proxy target is not configured")

    hop_by_hop = {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "authorization",
    }
    forward_headers: dict[str, str] = {}
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in hop_by_hop:
            continue
        if v is None:
            continue
        forward_headers[k] = v

    forward_headers["Authorization"] = f"Bearer {openai_api_key}"

    try:
        upstream = requests.post(
            upstream_url,
            json=body,
            headers=forward_headers,
            stream=True,
            timeout=(10, 600),
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"OpenAI upstream request failed: {e}") from e

    content_type = upstream.headers.get("content-type") or "application/octet-stream"

    def iter_bytes():
        try:
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    if "text/event-stream" in content_type.lower():
        return StreamingResponse(iter_bytes(), status_code=upstream.status_code, media_type=content_type)

    content = upstream.content
    upstream.close()
    return Response(content=content, status_code=upstream.status_code, media_type=content_type)


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
                "version": ARGUS_VERSION,
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
                "description": "Invoke a command on a connected node (device). For interactive CLIs, start with system.run using params={\"argv\":[...],\"pty\":true,\"yieldMs\":0}, then continue with process.write / process.send_keys / process.submit / process.paste using the returned jobId.",
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
            scoped_sid = getattr(request.state, "mcp_scoped_session_id", None) or sess.scoped_session_id
            nodes = await app.state.node_registry.list_connected(scope_session_id=scoped_sid)
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

            # Treat targets present in persisted chatBindings as allowed. This is more stable than
            # lastActiveByThread, which can be overwritten by the most recent inbound message.
            chat_id = target.get("chat_id")
            known = False
            st = automation._store.state
            bindings = getattr(st, "chat_bindings", None)
            if isinstance(bindings, dict):
                for ck in bindings.keys():
                    if not isinstance(ck, str) or not ck.strip():
                        continue
                    cand = _telegram_target_from_chat_key(ck)
                    if cand is None:
                        continue
                    if cand.get("chat_id") == chat_id:
                        known = True
                        break

            if not known:
                return (
                    _jsonrpc_result(
                        request_id=request_id,
                        result=_mcp_call_tool_result(
                            content=[{"type": "text", "text": "Target not allowed (unknown chatKey). Expected target to exist in chatBindings."}],
                            structured={"ok": False, "error": {"code": "FORBIDDEN", "field": "target"}},
                            is_error=True,
                        ),
                    ),
                    {"MCP-Protocol-Version": sess.protocol_version},
                )

            raw_format = args.get("format")
            fmt = str(raw_format or "markdown").strip().lower()
            silent = bool(args.get("silent")) if args.get("silent") is not None else None

            raw_text = text_param.strip()
            send_results: list[Any] = []
            try:
                if fmt == "html":
                    truncated = _telegram_truncate(raw_text)
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
                    send_results = [res]
                elif fmt == "plain":
                    send_results = await _telegram_send_plain_message_parts(
                        token=token,
                        target=target,
                        text=raw_text,
                        disable_notification=silent,
                    )
                else:
                    send_results = await _telegram_send_markdown_message_parts(
                        token=token,
                        target=target,
                        text=raw_text,
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
                        structured={
                            "ok": True,
                            "channel": channel,
                            "target": chat_key,
                            "result": send_results[-1] if send_results else None,
                            "results": send_results,
                            "parts": len(send_results),
                        },
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
            # - node="self": prefer the runtime node for the scoped session (or the only runtime node if unique)
            # - node omitted: only allowed when exactly one runtime node is connected
            scoped_sid = getattr(request.state, "mcp_scoped_session_id", None) or sess.scoped_session_id
            node_id: Optional[str] = None
            if not node_raw:
                node_id = await app.state.node_registry.resolve_implicit_runtime_node_id(scope_session_id=scoped_sid)
                if not node_id:
                    runtime_ids = await app.state.node_registry.list_runtime_node_ids(scope_session_id=scoped_sid)
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
                node_id = await app.state.node_registry.resolve_self_runtime_node_id(scope_session_id=scoped_sid)
                if not node_id:
                    runtime_ids = await app.state.node_registry.list_runtime_node_ids(scope_session_id=scoped_sid)
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
                node_id = await app.state.node_registry.resolve_node_id(node_raw, scope_session_id=scoped_sid)
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

            res: NodeInvokeResult = await _invoke_node_checked(
                node_id=node_id,
                scope_session_id=scoped_sid,
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
                "version": ARGUS_VERSION,
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
                "description": "Invoke a command on a connected node (device). For interactive CLIs, start with system.run using params={\"argv\":[...],\"pty\":true,\"yieldMs\":0}, then continue with process.write / process.send_keys / process.submit / process.paste using the returned jobId.",
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
            scoped_sid = getattr(request.state, "mcp_scoped_session_id", None) or sess.scoped_session_id
            nodes = await app.state.node_registry.list_connected(scope_session_id=scoped_sid)
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
            scoped_sid = getattr(request.state, "mcp_scoped_session_id", None) or sess.scoped_session_id
            if not node_raw:
                node_id = await app.state.node_registry.resolve_implicit_runtime_node_id(scope_session_id=scoped_sid)
                if not node_id:
                    runtime_ids = await app.state.node_registry.list_runtime_node_ids(scope_session_id=scoped_sid)
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
                node_id = await app.state.node_registry.resolve_self_runtime_node_id(scope_session_id=scoped_sid)
                if not node_id:
                    runtime_ids = await app.state.node_registry.list_runtime_node_ids(scope_session_id=scoped_sid)
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
                node_id = await app.state.node_registry.resolve_node_id(node_raw, scope_session_id=scoped_sid)
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

            res: NodeInvokeResult = await _invoke_node_checked(
                node_id=node_id,
                scope_session_id=scoped_sid,
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


_NODE_DERIVED_TOKEN_PREFIX = "argus-node-v1"


def _node_derive_session_token(master: str, session_id: str) -> str:
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id must not be empty")
    mac = hmac.new(master.encode("utf-8"), sid.encode("utf-8"), hashlib.sha256).digest()
    sig = _b64url_no_pad(mac)[:32]
    return f"{_NODE_DERIVED_TOKEN_PREFIX}.{sid}.{sig}"


def _node_verify_derived_session_token(master: str, token: str) -> Optional[str]:
    t = str(token or "").strip()
    if not t:
        return None
    parts = t.split(".", 2)
    if len(parts) != 3:
        return None
    prefix, sid, sig = parts
    if prefix != _NODE_DERIVED_TOKEN_PREFIX:
        return None
    if not sid or not sig:
        return None
    try:
        expected = _node_derive_session_token(master, sid)
    except Exception:
        return None
    if expected != t:
        return None
    return sid


_OPENAI_PROXY_DERIVED_TOKEN_PREFIX = "argus-openai-v1"


def _openai_proxy_derive_session_token(master: str, session_id: str) -> str:
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id must not be empty")
    mac = hmac.new(master.encode("utf-8"), sid.encode("utf-8"), hashlib.sha256).digest()
    sig = _b64url_no_pad(mac)[:32]
    return f"{_OPENAI_PROXY_DERIVED_TOKEN_PREFIX}.{sid}.{sig}"


def _openai_proxy_verify_derived_session_token(master: str, token: str) -> Optional[str]:
    t = str(token or "").strip()
    if not t:
        return None
    parts = t.split(".", 2)
    if len(parts) != 3:
        return None
    prefix, sid, sig = parts
    if prefix != _OPENAI_PROXY_DERIVED_TOKEN_PREFIX:
        return None
    if not sid or not sig:
        return None
    expected = _openai_proxy_derive_session_token(master, sid)
    if hmac.compare_digest(expected, t):
        return sid
    return None


def _openai_proxy_require_token(request: Request):
    # OpenAI API key proxy auth is scoped per runtime session.
    #
    # - The gateway reads a master secret from ARGUS_OPENAI_TOKEN (fallback: ARGUS_TOKEN).
    # - Each runtime container receives a derived per-session token as ARGUS_OPENAI_TOKEN.
    # - Requests authenticated with the derived token are accepted.
    # - The raw master token is NOT accepted directly.
    master = os.getenv("ARGUS_OPENAI_TOKEN") or os.getenv("ARGUS_TOKEN") or None
    request.state.openai_scoped_session_id = None
    if master is None:
        raise HTTPException(status_code=503, detail="OpenAI proxy is not configured")
    provided = _extract_token_http(request) or ""
    scoped = _openai_proxy_verify_derived_session_token(master, provided)
    if scoped:
        request.state.openai_scoped_session_id = scoped
        return
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

    home_host_path = _configured_home_host_path()
    workspace_host_path = _configured_workspace_host_path()
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


def _fugue_cfg() -> FugueProvisionConfig:
    base_url = (os.getenv("ARGUS_FUGUE_BASE_URL") or os.getenv("FUGUE_BASE_URL") or "").strip().rstrip("/")
    token = (os.getenv("ARGUS_FUGUE_TOKEN") or os.getenv("FUGUE_TOKEN") or "").strip()
    project_id = (os.getenv("ARGUS_FUGUE_PROJECT_ID") or os.getenv("FUGUE_PROJECT_ID") or "").strip()
    tenant_id = (os.getenv("ARGUS_FUGUE_TENANT_ID") or os.getenv("FUGUE_TENANT_ID") or "").strip() or None
    image = (os.getenv("ARGUS_FUGUE_RUNTIME_IMAGE") or os.getenv("ARGUS_RUNTIME_IMAGE") or "").strip()
    runtime_app_id = (os.getenv("ARGUS_FUGUE_RUNTIME_APP_ID") or "").strip() or None
    runtime_app_name = (os.getenv("ARGUS_FUGUE_RUNTIME_APP_NAME") or "").strip() or None
    runtime_compose_service = (os.getenv("ARGUS_FUGUE_RUNTIME_COMPOSE_SERVICE") or "").strip() or None
    runtime_id = (os.getenv("ARGUS_FUGUE_RUNTIME_ID") or os.getenv("FUGUE_RUNTIME_ID") or "runtime_managed_shared").strip() or "runtime_managed_shared"
    gateway_internal_host = (
        os.getenv("ARGUS_GATEWAY_INTERNAL_HOST") or os.getenv("ARGUS_FUGUE_GATEWAY_INTERNAL_HOST") or ""
    ).strip()
    gateway_compose_service = (os.getenv("ARGUS_FUGUE_GATEWAY_COMPOSE_SERVICE") or "").strip() or None
    runtime_cmd = (os.getenv("ARGUS_RUNTIME_CMD") or "").strip() or None
    workspace_mount_path = (os.getenv("ARGUS_FUGUE_WORKSPACE_MOUNT_PATH") or "/workspace").strip() or "/workspace"
    workspace_storage_size = (os.getenv("ARGUS_FUGUE_WORKSPACE_STORAGE_SIZE") or "1Gi").strip() or "1Gi"
    workspace_storage_class_name = (os.getenv("ARGUS_FUGUE_WORKSPACE_STORAGE_CLASS") or "").strip() or None
    app_name_prefix = (os.getenv("ARGUS_FUGUE_APP_NAME_PREFIX") or "argus-session").strip().lower() or "argus-session"
    connect_timeout_s = float(os.getenv("ARGUS_CONNECT_TIMEOUT_S", "30"))
    try:
        jsonl_line_limit_bytes = int(os.getenv("ARGUS_JSONL_LINE_LIMIT_BYTES", str(DEFAULT_JSONL_LINE_LIMIT_BYTES)))
    except Exception:
        jsonl_line_limit_bytes = DEFAULT_JSONL_LINE_LIMIT_BYTES
    try:
        service_port = int(os.getenv("ARGUS_FUGUE_SERVICE_PORT", "7777"))
    except Exception:
        service_port = 7777

    if not base_url:
        raise RuntimeError("ARGUS_FUGUE_BASE_URL is required in fugue provision mode")
    if not token:
        raise RuntimeError("ARGUS_FUGUE_TOKEN is required in fugue provision mode")
    if not project_id:
        raise RuntimeError("ARGUS_FUGUE_PROJECT_ID or FUGUE_PROJECT_ID is required in fugue provision mode")
    if not image and not runtime_app_id and not runtime_app_name and not runtime_compose_service:
        raise RuntimeError(
            "One of ARGUS_FUGUE_RUNTIME_IMAGE, ARGUS_RUNTIME_IMAGE, ARGUS_FUGUE_RUNTIME_APP_ID, "
            "ARGUS_FUGUE_RUNTIME_APP_NAME, or ARGUS_FUGUE_RUNTIME_COMPOSE_SERVICE is required in fugue provision mode"
        )
    if not gateway_internal_host:
        if not gateway_compose_service:
            raise RuntimeError(
                "ARGUS_GATEWAY_INTERNAL_HOST or ARGUS_FUGUE_GATEWAY_COMPOSE_SERVICE is required in fugue provision mode"
            )
        gateway_internal_host = _fugue_compose_service_alias(project_id, gateway_compose_service)
    if service_port <= 0:
        raise RuntimeError("ARGUS_FUGUE_SERVICE_PORT must be > 0")
    if not workspace_mount_path.startswith("/"):
        raise RuntimeError("ARGUS_FUGUE_WORKSPACE_MOUNT_PATH must be an absolute container path")

    return FugueProvisionConfig(
        base_url=base_url,
        token=token,
        project_id=project_id,
        tenant_id=tenant_id,
        image=image or None,
        runtime_app_id=runtime_app_id,
        runtime_app_name=runtime_app_name,
        runtime_compose_service=runtime_compose_service,
        runtime_id=runtime_id,
        gateway_internal_host=gateway_internal_host,
        workspace_mount_path=workspace_mount_path,
        runtime_cmd=runtime_cmd,
        connect_timeout_s=connect_timeout_s,
        jsonl_line_limit_bytes=jsonl_line_limit_bytes,
        workspace_storage_size=workspace_storage_size,
        workspace_storage_class_name=workspace_storage_class_name,
        service_port=service_port,
        app_name_prefix=app_name_prefix,
    )


def _session_app_name(prefix: str, session_id: str) -> str:
    sid = _normalize_runtime_session_id(session_id)
    if not sid:
        raise RuntimeError("Invalid session id")
    prefix_norm = re.sub(r"[^a-z0-9-]+", "-", (prefix or "").strip().lower()).strip("-") or "argus-session"
    return f"{prefix_norm}-{sid}"


def _fugue_slugify(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text


def _fugue_compose_service_alias(project_id: str, compose_service: str) -> str:
    base = _fugue_slugify(project_id.replace("_", "-"))
    suffix = _fugue_slugify(compose_service)
    if not suffix:
        return base[:63] if base else ""
    if not base:
        return suffix[:63]
    name = f"{base}-{suffix}"
    if len(name) <= 63:
        return name
    max_base_len = 63 - len(suffix) - 1
    if max_base_len <= 0:
        return name[:63]
    return f"{base[:max_base_len]}-{suffix}"


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


def _resolve_agent_model_for_session(session_id: str) -> str:
    sid = session_id.strip() if isinstance(session_id, str) else ""
    if not sid:
        return ARGUS_AGENT_MODEL_DEFAULT
    automation: Optional[AutomationManager] = getattr(app.state, "automation", None)
    if automation is None:
        return ARGUS_AGENT_MODEL_DEFAULT
    try:
        return automation.get_effective_model_for_session(sid)
    except Exception:
        return ARGUS_AGENT_MODEL_DEFAULT


def _derive_default_workspace_host_path_for_session(
    session_id: str,
    *,
    home_host_path: Optional[str],
    workspace_base_host_path: Optional[str],
) -> Optional[str]:
    sid = _normalize_runtime_session_id(session_id)
    if not sid:
        return None
    # NOTE: `ARGUS_WORKSPACE_HOST_PATH` is treated as a *base directory* for per-session workspaces.
    if workspace_base_host_path:
        if not os.path.isabs(workspace_base_host_path):
            return None
        return str((Path(workspace_base_host_path) / f"sess-{sid}").resolve())
    if home_host_path:
        if not os.path.isabs(home_host_path):
            return None
        return str((Path(home_host_path) / "workspaces" / f"sess-{sid}").resolve())
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
    env["APP_WORKSPACE"] = cfg.workspace_container_path
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
    node_master = os.getenv("ARGUS_NODE_TOKEN") or gateway_token
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
    if node_master:
        derived = _node_derive_session_token(node_master, session_id)
        q = _url_quote(derived, safe="") if _url_quote is not None else derived
        node_ws_url = f"{node_ws_url}?token={q}"
    env["ARGUS_NODE_WS_URL"] = node_ws_url
    env["ARGUS_NODE_ID"] = f"runtime:{session_id}"
    env["ARGUS_NODE_DISPLAY_NAME"] = f"runtime-{session_id}"
    env["ARGUS_SESSION_ID"] = session_id
    env["ARGUS_CODEX_MODEL"] = _resolve_agent_model_for_session(session_id)

    # Codex runtime (optional): configure gateway MCP URL for tool access.
    env["ARGUS_CODEX_MCP_URL"] = f"http://{gateway_internal_host}:8080/mcp"

    # Optional: OpenAI Responses proxy (keeps per-user provider secrets on the gateway).
    openai_master = (os.getenv("ARGUS_OPENAI_TOKEN") or gateway_token or "").strip() or None
    if openai_master:
        env["ARGUS_OPENAI_TOKEN"] = _openai_proxy_derive_session_token(openai_master, session_id)
        env["ARGUS_CODEX_OPENAI_PROXY_BASE_URL"] = f"http://{gateway_internal_host}:8080/openai/v1"

    volumes = {}
    if not cfg.workspace_host_path:
        raise RuntimeError("workspace root is not configured (ARGUS_HOME_HOST_PATH/ARGUS_WORKSPACE_HOST_PATH)")
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
                "provider": "docker",
                "runtimeId": c.id,
                "runtimeName": c.name,
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


async def _close_live_session(session_id: str) -> None:
    live: Optional[LiveRuntimeSession] = None
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


async def _activate_live_session(live: LiveRuntimeSession) -> LiveRuntimeSession:
    session_id = live.session_id

    async def pump() -> None:
        buf = bytearray()
        try:
            while True:
                raw = await _read_jsonl_line(live.reader, buf, max_line_bytes=live.jsonl_line_limit_bytes)
                if raw is None:
                    log.info("Upstream TCP closed for session %s", session_id)
                    break
                text = raw.decode("utf-8", errors="replace").rstrip("\r")

                try:
                    msg: Any = json.loads(text)
                except Exception:
                    live.non_json_lines_dropped += 1
                    preview = text.strip()
                    if len(preview) > 240:
                        preview = preview[:240] + "..."
                    if live.non_json_lines_dropped <= 3:
                        log.warning(
                            "Dropping non-JSON upstream line for session %s (%d): %s",
                            session_id,
                            live.non_json_lines_dropped,
                            preview,
                        )
                    elif live.non_json_lines_dropped == 4:
                        log.warning("Further non-JSON upstream lines will be suppressed for session %s", session_id)
                    continue

                if not isinstance(msg, dict):
                    live.non_json_lines_dropped += 1
                    preview = text.strip()
                    if len(preview) > 240:
                        preview = preview[:240] + "..."
                    if live.non_json_lines_dropped <= 3:
                        log.warning(
                            "Dropping non-object upstream JSON for session %s (%d): %s",
                            session_id,
                            live.non_json_lines_dropped,
                            preview,
                        )
                    elif live.non_json_lines_dropped == 4:
                        log.warning("Further malformed upstream frames will be suppressed for session %s", session_id)
                    continue

                should_broadcast = True
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
                    client_turn_start_thread_id: Optional[str] = None
                    async with live.attach_lock:
                        if rid in live.pending_initialize_ids:
                            live.pending_initialize_ids.discard(rid)
                            if isinstance(msg.get("result"), dict):
                                live.initialized_result = msg["result"]
                                should_resolve = True
                        client_turn_start_thread_id = live.pending_client_turn_starts.pop(rid, None)
                    if should_resolve:
                        await live.resolve_initialize_waiters()
                    if client_turn_start_thread_id:
                        automation: Optional[AutomationManager] = getattr(app.state, "automation", None)
                        if automation is not None:
                            try:
                                await automation.on_client_turn_start_response(session_id, client_turn_start_thread_id, msg)
                            except Exception:
                                log.exception("Automation client turn-start response handler failed")
                    await live.resolve_internal_response(msg)
                    await live.deliver_response(msg)
                    continue

                if "method" in msg and "id" not in msg:
                    automation = getattr(app.state, "automation", None)
                    if automation is not None:
                        try:
                            automation.on_upstream_notification(session_id, msg)
                            if automation._is_heartbeat_commentary_notification(session_id, msg):
                                should_broadcast = False
                            text = json.dumps(msg)
                        except Exception:
                            log.exception("Automation notification handler failed")

                if msg.get("method") == "turn/completed":
                    params = msg.get("params")
                    if isinstance(params, dict):
                        tid = params.get("threadId")
                        if isinstance(tid, str) and tid.strip():
                            await live.clear_turn_owner(tid.strip())

                if should_broadcast:
                    await live.broadcast(text)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("Pump failed for session %s: %s", session_id, str(e))
        finally:
            try:
                live.closed = True
                live.writer.close()
            except Exception:
                pass
            try:
                async with live.attach_lock:
                    for fut in live.pending_internal_requests.values():
                        if not fut.done():
                            fut.set_exception(RuntimeError("Session closed"))
                    live.pending_internal_requests.clear()
            except Exception:
                pass
            try:
                async with app.state.sessions_lock:
                    current = app.state.sessions.get(session_id)
                    if current is live:
                        app.state.sessions.pop(session_id, None)
            except Exception:
                pass
            try:
                await live.broadcast(json.dumps({"method": "argus/session_closed", "params": {"id": session_id}}))
            except Exception:
                pass
            try:
                await live.close_attached_websockets(code=1011, reason="Upstream session closed")
            except Exception:
                pass
            try:
                automation = getattr(app.state, "automation", None)
                if automation is not None:
                    try:
                        automation.on_runtime_session_closed(session_id)
                    except Exception:
                        log.exception("Automation cleanup failed for closed session %s", session_id)
            except Exception:
                pass

    live.pump_task = asyncio.create_task(pump())

    async with app.state.sessions_lock:
        app.state.sessions[session_id] = live

    log.info("Provisioned session %s -> %s:%s", session_id, live.upstream_host, live.upstream_port)
    return live


async def _ensure_live_docker_session(session_id: str, *, allow_create: bool) -> tuple[LiveRuntimeSession, bool]:
    async with app.state.sessions_lock:
        existing = app.state.sessions.get(session_id)
    if existing is not None:
        try:
            writer_closing = existing.writer.is_closing()
        except Exception:
            writer_closing = False
        if not existing.closed and not writer_closing:
            return existing, False
        log.info("Ignoring stale live session for %s (closed=%s, writer_closing=%s)", session_id, existing.closed, writer_closing)

    cfg = _docker_cfg()
    # Resolve a per-session workspace host path (used as the *only* persistent mount for the runtime).
    #
    # Priority:
    # 1) Persisted agent workspace override (Telegram DM isolation, admin-created agents, etc.)
    # 2) Derived per-session workspace under ARGUS_WORKSPACE_HOST_PATH (base) or ARGUS_HOME_HOST_PATH/workspaces
    workspace_override = _resolve_workspace_host_path_for_session(session_id)
    workspace_host_path = workspace_override or _derive_default_workspace_host_path_for_session(
        session_id,
        home_host_path=cfg.home_host_path,
        workspace_base_host_path=cfg.workspace_host_path,
    )
    if not workspace_host_path:
        raise RuntimeError("workspace root is not configured (ARGUS_HOME_HOST_PATH/ARGUS_WORKSPACE_HOST_PATH)")
    if not os.path.isabs(workspace_host_path):
        raise RuntimeError(f"Invalid workspaceHostPath for session {session_id}: not an absolute path")
    try:
        Path(workspace_host_path).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log.warning("Failed to create workspace directory for session %s at %s: %s", session_id, workspace_host_path, str(e))
    cfg = replace(cfg, workspace_host_path=workspace_host_path)
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

    live = LiveRuntimeSession(
        session_id=session_id,
        provider="docker",
        runtime_id=container.id,
        runtime_name=container.name,
        workspace_runtime_path=cfg.workspace_container_path,
        jsonl_line_limit_bytes=cfg.jsonl_line_limit_bytes,
        upstream_host=host,
        upstream_port=7777,
        reader=reader,
        writer=writer,
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
        pending_client_turn_starts={},
        turn_owners_by_thread={},
    )
    live = await _activate_live_session(live)
    return live, created


def _fugue_headers(cfg: FugueProvisionConfig) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {cfg.token}",
    }


def _fugue_response_detail(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        for key in ("error", "detail", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _fugue_request_json_sync(
    cfg: FugueProvisionConfig,
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    body: Optional[dict[str, Any]] = None,
    expected_statuses: tuple[int, ...] = (200,),
) -> Any:
    url = cfg.base_url + path
    kwargs: dict[str, Any] = {
        "headers": _fugue_headers(cfg),
        "params": params,
        "timeout": max(10.0, cfg.connect_timeout_s),
    }
    if body is not None:
        kwargs["json"] = body
    response = requests.request(method, url, **kwargs)
    payload: Any = None
    try:
        if response.content:
            payload = response.json()
    except Exception:
        payload = None
    if response.status_code not in expected_statuses:
        detail = _fugue_response_detail(payload)
        if (
            response.status_code == 404
            and not cfg.tenant_id
            and path == "/v1/apps/import-image"
        ):
            extra = (
                "set ARGUS_FUGUE_TENANT_ID/FUGUE_TENANT_ID when using a multi-tenant Fugue bootstrap key"
            )
            detail = f"{detail}; {extra}" if detail else extra
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"Fugue API {method} {path} failed with {response.status_code}{suffix}")
    return payload


def _fugue_app_name(cfg: FugueProvisionConfig, session_id: str) -> str:
    return _session_app_name(cfg.app_name_prefix, session_id)


def _fugue_build_runtime_env(cfg: FugueProvisionConfig, session_id: str) -> dict[str, str]:
    env: dict[str, str] = {
        "APP_HOME": cfg.home_container_path,
        "APP_WORKSPACE": cfg.workspace_mount_path,
        "ARGUS_RUNTIME_LAYOUT": RUNTIME_LAYOUT,
        "ARGUS_NODE_ID": f"runtime:{session_id}",
        "ARGUS_NODE_DISPLAY_NAME": f"runtime-{session_id}",
        "ARGUS_NODE_WS_URL": f"ws://{cfg.gateway_internal_host}:8080/nodes/ws",
        "ARGUS_SESSION_ID": session_id,
        "ARGUS_CODEX_MODEL": _resolve_agent_model_for_session(session_id),
        "ARGUS_CODEX_MCP_URL": f"http://{cfg.gateway_internal_host}:8080/mcp",
    }
    if cfg.runtime_cmd:
        env["APP_SERVER_CMD"] = cfg.runtime_cmd

    gateway_token = os.getenv("ARGUS_TOKEN") or None
    mcp_master = os.getenv("ARGUS_MCP_TOKEN") or gateway_token
    if mcp_master:
        env["ARGUS_MCP_TOKEN"] = _mcp_derive_session_token(mcp_master, session_id)

    try:
        quoted = urllib.parse.quote
    except Exception:
        quoted = None
    node_master = os.getenv("ARGUS_NODE_TOKEN") or gateway_token
    if node_master:
        derived = _node_derive_session_token(node_master, session_id)
        q = quoted(derived, safe="") if quoted is not None else derived
        env["ARGUS_NODE_WS_URL"] = f"ws://{cfg.gateway_internal_host}:8080/nodes/ws?token={q}"

    openai_master = (os.getenv("ARGUS_OPENAI_TOKEN") or gateway_token or "").strip() or None
    if openai_master:
        env["ARGUS_OPENAI_TOKEN"] = _openai_proxy_derive_session_token(openai_master, session_id)
        env["ARGUS_CODEX_OPENAI_PROXY_BASE_URL"] = f"http://{cfg.gateway_internal_host}:8080/openai/v1"

    return env


def _fugue_list_apps_sync(cfg: FugueProvisionConfig) -> list[dict[str, Any]]:
    payload = _fugue_request_json_sync(
        cfg,
        "GET",
        "/v1/apps",
        params={
            "include_live_status": "true",
            "include_resource_usage": "false",
            **({"tenant_id": cfg.tenant_id} if cfg.tenant_id else {}),
        },
        expected_statuses=(200,),
    )
    apps = payload.get("apps") if isinstance(payload, dict) else None
    return [app for app in apps if isinstance(app, dict)] if isinstance(apps, list) else []


def _fugue_find_session_app_sync(cfg: FugueProvisionConfig, session_id: str) -> Optional[dict[str, Any]]:
    expected_name = _fugue_app_name(cfg, session_id)
    for app_data in _fugue_list_apps_sync(cfg):
        if str(app_data.get("project_id") or "").strip() != cfg.project_id:
            continue
        if str(app_data.get("name") or "").strip() != expected_name:
            continue
        return app_data
    return None


def _fugue_find_app_by_name_sync(cfg: FugueProvisionConfig, app_name: str) -> Optional[dict[str, Any]]:
    expected_name = (app_name or "").strip()
    if not expected_name:
        return None
    for app_data in _fugue_list_apps_sync(cfg):
        if str(app_data.get("project_id") or "").strip() != cfg.project_id:
            continue
        if str(app_data.get("name") or "").strip() != expected_name:
            continue
        return app_data
    return None


def _fugue_find_app_by_compose_service_sync(cfg: FugueProvisionConfig, compose_service: str) -> dict[str, Any]:
    expected_service = _fugue_slugify(compose_service)
    if not expected_service:
        raise RuntimeError("Fugue runtime compose service is empty")
    matches: list[dict[str, Any]] = []
    for app_data in _fugue_list_apps_sync(cfg):
        if str(app_data.get("project_id") or "").strip() != cfg.project_id:
            continue
        source = app_data.get("source") if isinstance(app_data.get("source"), dict) else {}
        if _fugue_slugify(str(source.get("compose_service") or "").strip()) != expected_service:
            continue
        matches.append(app_data)
    if not matches:
        raise RuntimeError(
            f"Fugue app with compose_service={expected_service!r} was not found in project {cfg.project_id}"
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"Multiple Fugue apps in project {cfg.project_id} use compose_service={expected_service!r}; "
            "set ARGUS_FUGUE_RUNTIME_APP_ID or ARGUS_FUGUE_RUNTIME_APP_NAME explicitly"
        )
    return matches[0]


def _fugue_get_app_sync(cfg: FugueProvisionConfig, app_id: str) -> dict[str, Any]:
    payload = _fugue_request_json_sync(cfg, "GET", f"/v1/apps/{app_id}", expected_statuses=(200,))
    app_data = payload.get("app") if isinstance(payload, dict) else None
    if not isinstance(app_data, dict):
        raise RuntimeError(f"Invalid Fugue app payload for {app_id}")
    return app_data


def _fugue_runtime_image_from_app(app_data: dict[str, Any]) -> str:
    source = app_data.get("source") if isinstance(app_data.get("source"), dict) else {}
    spec = app_data.get("spec") if isinstance(app_data.get("spec"), dict) else {}
    for candidate in (
        source.get("resolved_image_ref"),
        spec.get("image"),
        source.get("image_ref"),
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    app_name = str(app_data.get("name") or app_data.get("id") or "").strip() or "unknown"
    raise RuntimeError(f"Fugue app {app_name} does not expose a usable runtime image reference yet")


def _fugue_resolve_runtime_image_sync(cfg: FugueProvisionConfig) -> str:
    if cfg.image:
        return cfg.image

    app_data: Optional[dict[str, Any]] = None
    if cfg.runtime_app_id:
        app_data = _fugue_get_app_sync(cfg, cfg.runtime_app_id)
    elif cfg.runtime_app_name:
        app_data = _fugue_find_app_by_name_sync(cfg, cfg.runtime_app_name)
        if app_data is None:
            raise RuntimeError(
                f"Fugue runtime app named {cfg.runtime_app_name!r} was not found in project {cfg.project_id}"
            )
    elif cfg.runtime_compose_service:
        app_data = _fugue_find_app_by_compose_service_sync(cfg, cfg.runtime_compose_service)

    if not isinstance(app_data, dict):
        raise RuntimeError("Fugue runtime image source is not configured")

    if str(app_data.get("project_id") or "").strip() != cfg.project_id:
        raise RuntimeError(
            f"Configured Fugue runtime app {app_data.get('id') or app_data.get('name')!r} is not in project {cfg.project_id}"
        )
    return _fugue_runtime_image_from_app(app_data)


def _fugue_create_session_app_sync(cfg: FugueProvisionConfig, session_id: str) -> dict[str, Any]:
    if not cfg.runtime_cmd:
        raise RuntimeError("ARGUS_RUNTIME_CMD is required in fugue provision mode")
    request_body: dict[str, Any] = {
        "project_id": cfg.project_id,
        "image_ref": _fugue_resolve_runtime_image_sync(cfg),
        "name": _fugue_app_name(cfg, session_id),
        "description": f"Argus managed runtime session {session_id}",
        "runtime_id": cfg.runtime_id,
        "replicas": 1,
        "network_mode": "internal",
        "service_port": cfg.service_port,
        "env": _fugue_build_runtime_env(cfg, session_id),
        "persistent_storage": {
            "storage_size": cfg.workspace_storage_size,
            "mounts": [
                {
                    "kind": "directory",
                    "path": cfg.workspace_mount_path,
                }
            ],
        },
    }
    if cfg.tenant_id:
        request_body["tenant_id"] = cfg.tenant_id
    if cfg.workspace_storage_class_name:
        request_body["persistent_storage"]["storage_class_name"] = cfg.workspace_storage_class_name
    payload = _fugue_request_json_sync(
        cfg,
        "POST",
        "/v1/apps/import-image",
        body=request_body,
        expected_statuses=(202,),
    )
    app_data = payload.get("app") if isinstance(payload, dict) else None
    if not isinstance(app_data, dict):
        raise RuntimeError("Invalid Fugue import-image response")
    return app_data


def _fugue_wait_for_app_ready_sync(cfg: FugueProvisionConfig, app_id: str) -> dict[str, Any]:
    deadline = time.time() + max(60.0, cfg.connect_timeout_s * 6.0)
    last_phase = ""
    last_message = ""
    while True:
        app_data = _fugue_get_app_sync(cfg, app_id)
        status = app_data.get("status") if isinstance(app_data.get("status"), dict) else {}
        phase = str(status.get("phase") or "").strip().lower()
        last_phase = phase or last_phase
        last_message = str(status.get("last_message") or "").strip() or last_message
        internal_service = app_data.get("internal_service")
        if phase in ("deployed", "scaled", "migrated", "failed-over") and isinstance(internal_service, dict):
            host = str(internal_service.get("host") or "").strip()
            port = internal_service.get("port")
            if host and isinstance(port, int) and port > 0:
                return app_data
        if phase in ("failed", "deleted"):
            detail = f" ({last_message})" if last_message else ""
            raise RuntimeError(f"Fugue app {app_id} entered phase {phase}{detail}")
        if time.time() >= deadline:
            detail = f" message={last_message}" if last_message else ""
            raise TimeoutError(f"Timed out waiting for Fugue app {app_id} to become ready (phase={last_phase}){detail}")
        time.sleep(1.0)


def _fugue_ensure_session_app_ready_sync(cfg: FugueProvisionConfig, session_id: str, allow_create: bool) -> tuple[dict[str, Any], bool]:
    app_data = _fugue_find_session_app_sync(cfg, session_id)
    created = False
    if app_data is None:
        if not allow_create:
            raise KeyError("Unknown session")
        app_data = _fugue_create_session_app_sync(cfg, session_id)
        created = True
    app_id = str(app_data.get("id") or "").strip()
    if not app_id:
        raise RuntimeError(f"Fugue app id missing for session {session_id}")
    return _fugue_wait_for_app_ready_sync(cfg, app_id), created


def _fugue_internal_service_target(app_data: dict[str, Any]) -> tuple[str, int]:
    internal_service = app_data.get("internal_service")
    if not isinstance(internal_service, dict):
        raise RuntimeError("Fugue app is missing internal service metadata")
    host = str(internal_service.get("host") or "").strip()
    port = internal_service.get("port")
    if not host or not isinstance(port, int) or port <= 0:
        raise RuntimeError("Fugue app internal service metadata is incomplete")
    return host, port


def _fugue_session_record_from_app(session_id: str, app_data: dict[str, Any]) -> dict[str, Any]:
    status = app_data.get("status") if isinstance(app_data.get("status"), dict) else {}
    return {
        "sessionId": session_id,
        "provider": "fugue",
        "runtimeId": app_data.get("id"),
        "runtimeName": app_data.get("name"),
        "containerId": app_data.get("id"),
        "name": app_data.get("name"),
        "status": status.get("phase"),
        "runtimeLayout": RUNTIME_LAYOUT,
    }


def _fugue_list_argus_sessions_sync() -> list[dict[str, Any]]:
    cfg = _fugue_cfg()
    prefix = _fugue_app_name(cfg, "000000000000")[:-12]
    out: list[dict[str, Any]] = []
    for app_data in _fugue_list_apps_sync(cfg):
        if str(app_data.get("project_id") or "").strip() != cfg.project_id:
            continue
        name = str(app_data.get("name") or "").strip()
        if not name.startswith(prefix):
            continue
        sid = name[len(prefix) :]
        sid = _normalize_runtime_session_id(sid)
        if not sid:
            continue
        out.append(_fugue_session_record_from_app(sid, app_data))
    out.sort(key=lambda item: str(item.get("name") or ""))
    return out


def _fugue_delete_session_sync(session_id: str, force: bool) -> bool:
    cfg = _fugue_cfg()
    app_data = _fugue_find_session_app_sync(cfg, session_id)
    if app_data is None:
        return False
    app_id = str(app_data.get("id") or "").strip()
    if not app_id:
        return False
    _fugue_request_json_sync(
        cfg,
        "DELETE",
        f"/v1/apps/{app_id}",
        params={"force": "true" if force else "false"},
        expected_statuses=(202,),
    )
    return True


def _fugue_restart_session_sync(session_id: str) -> bool:
    cfg = _fugue_cfg()
    app_data = _fugue_find_session_app_sync(cfg, session_id)
    if app_data is None:
        return False
    app_id = str(app_data.get("id") or "").strip()
    if not app_id:
        return False
    _fugue_request_json_sync(cfg, "POST", f"/v1/apps/{app_id}/restart", body={}, expected_statuses=(202,))
    return True


def _relative_workspace_path(root: Path, target: Path) -> Optional[str]:
    try:
        rel = target.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return None
    rel = rel.strip()
    if not rel or rel == ".":
        return None
    return rel


def _fugue_workspace_path(cfg: FugueProvisionConfig, relative_path: str) -> str:
    rel = str(relative_path or "").replace("\\", "/").strip().lstrip("/")
    if not rel:
        return cfg.workspace_mount_path
    return f"{cfg.workspace_mount_path.rstrip('/')}/{rel}"


def _decode_fugue_filesystem_content(content: Any, encoding: Any) -> bytes:
    raw = content if isinstance(content, str) else ""
    enc = str(encoding or "utf-8").strip().lower()
    if enc in ("", "utf-8", "utf8", "text"):
        return raw.encode("utf-8")
    if enc == "base64":
        return base64.b64decode(raw.encode("utf-8"))
    raise RuntimeError(f"Unsupported Fugue filesystem encoding: {enc}")


def _fugue_put_workspace_file_for_app_sync(
    cfg: FugueProvisionConfig,
    app_id: str,
    relative_path: str,
    data: bytes,
) -> None:
    if not app_id:
        raise RuntimeError("Fugue app id missing")
    try:
        content = data.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        content = base64.b64encode(data).decode("ascii")
        encoding = "base64"
    _fugue_request_json_sync(
        cfg,
        "PUT",
        f"/v1/apps/{app_id}/filesystem/file",
        params={"component": "app"},
        body={
            "path": _fugue_workspace_path(cfg, relative_path),
            "content": content,
            "encoding": encoding,
            "mkdir_parents": True,
        },
        expected_statuses=(200,),
    )


def _fugue_put_workspace_file_sync(cfg: FugueProvisionConfig, session_id: str, relative_path: str, data: bytes) -> None:
    app_data = _fugue_find_session_app_sync(cfg, session_id)
    if app_data is None:
        raise KeyError("Unknown session")
    app_id = str(app_data.get("id") or "").strip()
    _fugue_put_workspace_file_for_app_sync(cfg, app_id, relative_path, data)


def _fugue_push_workspace_snapshot_sync(cfg: FugueProvisionConfig, session_id: str, root: Path) -> None:
    if not root.exists() or not root.is_dir():
        return
    app_data = _fugue_find_session_app_sync(cfg, session_id)
    if app_data is None:
        raise KeyError("Unknown session")
    app_id = str(app_data.get("id") or "").strip()
    for path_obj in sorted(root.rglob("*")):
        if not path_obj.is_file():
            continue
        rel = _relative_workspace_path(root, path_obj)
        if not rel:
            continue
        try:
            data = path_obj.read_bytes()
        except Exception:
            continue
        _fugue_put_workspace_file_for_app_sync(cfg, app_id, rel, data)


def _fugue_read_workspace_file_bytes_sync(cfg: FugueProvisionConfig, session_id: str, relative_path: str, *, max_bytes: int) -> Optional[bytes]:
    app_data = _fugue_find_session_app_sync(cfg, session_id)
    if app_data is None:
        return None
    app_id = str(app_data.get("id") or "").strip()
    if not app_id:
        return None
    try:
        payload = _fugue_request_json_sync(
            cfg,
            "GET",
            f"/v1/apps/{app_id}/filesystem/file",
            params={
                "component": "app",
                "path": _fugue_workspace_path(cfg, relative_path),
                "max_bytes": str(max(1, int(max_bytes))),
            },
            expected_statuses=(200,),
        )
    except RuntimeError as e:
        if " 404" in str(e):
            return None
        raise
    if not isinstance(payload, dict):
        return None
    return _decode_fugue_filesystem_content(payload.get("content"), payload.get("encoding"))


def _fugue_list_workspace_tree_sync(cfg: FugueProvisionConfig, session_id: str, relative_path: str, *, depth: int) -> list[dict[str, Any]]:
    app_data = _fugue_find_session_app_sync(cfg, session_id)
    if app_data is None:
        return []
    app_id = str(app_data.get("id") or "").strip()
    if not app_id:
        return []
    try:
        payload = _fugue_request_json_sync(
            cfg,
            "GET",
            f"/v1/apps/{app_id}/filesystem/tree",
            params={
                "component": "app",
                "path": _fugue_workspace_path(cfg, relative_path),
                "depth": str(max(1, int(depth))),
            },
            expected_statuses=(200,),
        )
    except RuntimeError as e:
        if " 404" in str(e):
            return []
        raise
    entries = payload.get("entries") if isinstance(payload, dict) else None
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


async def _ensure_live_fugue_session(session_id: str, *, allow_create: bool) -> tuple[LiveRuntimeSession, bool]:
    async with app.state.sessions_lock:
        existing = app.state.sessions.get(session_id)
    if existing is not None:
        try:
            writer_closing = existing.writer.is_closing()
        except Exception:
            writer_closing = False
        if not existing.closed and not writer_closing:
            return existing, False
        log.info("Ignoring stale live session for %s (closed=%s, writer_closing=%s)", session_id, existing.closed, writer_closing)

    cfg = _fugue_cfg()
    workspace_host_path = _resolve_workspace_host_path_for_session(session_id) or _derive_default_workspace_host_path_for_session(
        session_id,
        home_host_path=_configured_home_host_path(),
        workspace_base_host_path=_configured_workspace_host_path(),
    )
    if not workspace_host_path:
        raise RuntimeError("workspace root is not configured (ARGUS_HOME_HOST_PATH/ARGUS_WORKSPACE_HOST_PATH)")
    if not os.path.isabs(workspace_host_path):
        raise RuntimeError(f"Invalid workspaceHostPath for session {session_id}: not an absolute path")
    workspace_root = Path(workspace_host_path)
    try:
        workspace_root.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log.warning("Failed to create local workspace mirror for session %s at %s: %s", session_id, workspace_host_path, str(e))

    app_data, created = await asyncio.to_thread(_fugue_ensure_session_app_ready_sync, cfg, session_id, allow_create)
    await asyncio.to_thread(_fugue_push_workspace_snapshot_sync, cfg, session_id, workspace_root)
    host, port = _fugue_internal_service_target(app_data)
    reader, writer = await _wait_for_tcp(host, port, cfg.connect_timeout_s)

    live = LiveRuntimeSession(
        session_id=session_id,
        provider="fugue",
        runtime_id=str(app_data.get("id") or ""),
        runtime_name=str(app_data.get("name") or _fugue_app_name(cfg, session_id)),
        workspace_runtime_path=cfg.workspace_mount_path,
        jsonl_line_limit_bytes=cfg.jsonl_line_limit_bytes,
        upstream_host=host,
        upstream_port=port,
        reader=reader,
        writer=writer,
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
        pending_client_turn_starts={},
        turn_owners_by_thread={},
    )
    live = await _activate_live_session(live)
    return live, created


@app.get("/sessions")
async def list_sessions(request: Request):
    _http_require_token(request)
    if not _provisioner_manages_runtime_sessions():
        raise HTTPException(
            status_code=400,
            detail=f"Managed sessions are not available in provision mode '{_provisioner_mode_name()}'",
        )
    try:
        sessions = await _list_managed_sessions()
    except Exception as e:
        log.exception("Failed to list managed sessions")
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"sessions": sessions}


@app.get("/nodes")
async def list_nodes(request: Request):
    _http_require_token(request)
    nodes = await _list_nodes_with_state()
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

    res: NodeInvokeResult = await _invoke_node_checked(
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


async def _automation_model_payload(
    automation: AutomationManager,
    *,
    agent: Optional[PersistedAgentRuntime] = None,
    user_id: Optional[int] = None,
) -> dict[str, Any]:
    current_model = automation._channel_adjusted_agent_model(agent)
    resolved_user_id = user_id if isinstance(user_id, int) and user_id > 0 else None
    if resolved_user_id is None and isinstance(getattr(agent, "owner_user_id", None), int):
        owner_user_id = int(getattr(agent, "owner_user_id"))
        if owner_user_id > 0:
            resolved_user_id = owner_user_id
    if resolved_user_id is not None:
        return await automation.get_available_models_for_user(
            user_id=resolved_user_id,
            current_model=current_model,
        )
    return _fallback_model_catalog(current_model=current_model, error=None)


def _automation_require_chat_key(body: Any) -> str:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    raw_chat_key = body.get("chatKey")
    if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
        raise HTTPException(status_code=400, detail="Missing 'chatKey'")
    return raw_chat_key.strip()


def _automation_require_session_id_for_chat_key(automation: AutomationManager, chat_key: str) -> str:
    sid = automation.resolve_session_id_for_chat_key(chat_key)
    if not sid:
        raise HTTPException(status_code=400, detail="Unknown session (run /start)")
    return sid


def _node_status_payload(automation: AutomationManager, node: dict[str, Any]) -> dict[str, Any]:
    out = dict(node)
    session_id = str(node.get("sessionId") or "").strip()
    node_id = str(node.get("nodeId") or "").strip()
    default_node_id = automation.default_node_id_for_session(session_id)
    is_default = bool(default_node_id and node_id == default_node_id)
    paused = bool(session_id and node_id and automation.is_node_paused(session_id=session_id, node_id=node_id))
    out["isDefault"] = is_default
    out["paused"] = paused
    out["canPause"] = bool(session_id and node_id and not is_default)
    out["status"] = "paused" if paused else "ready"
    return out


async def _list_nodes_with_state(*, scope_session_id: Optional[str] = None) -> list[dict[str, Any]]:
    automation = _get_automation_or_500()
    nodes = await app.state.node_registry.list_connected(scope_session_id=scope_session_id)
    out = [_node_status_payload(automation, node) for node in nodes]
    out.sort(
        key=lambda item: (
            0 if item.get("isDefault") else 1,
            str(item.get("displayName") or item.get("nodeId") or "").lower(),
        )
    )
    return out


def _node_command_allowed_while_paused(command: str) -> bool:
    cmd = str(command or "").strip()
    return cmd.startswith("process.")


async def _invoke_node_checked(
    *,
    node_id: str,
    scope_session_id: Optional[str] = None,
    command: str,
    params: Any = None,
    timeout_ms: Optional[int] = None,
) -> NodeInvokeResult:
    automation = _get_automation_or_500()
    resolved_scope = await app.state.node_registry.resolve_scope_session_id(node_id, scope_session_id=scope_session_id)
    if resolved_scope and automation.is_node_paused(session_id=resolved_scope, node_id=node_id):
        if not _node_command_allowed_while_paused(command):
            return NodeInvokeResult(
                ok=False,
                error={
                    "code": "NODE_PAUSED",
                    "message": "node is paused; only process.* commands are allowed until it is resumed",
                },
            )
    return await app.state.node_registry.invoke(
        node_id=node_id,
        scope_session_id=scope_session_id,
        command=command,
        params=params,
        timeout_ms=timeout_ms,
    )


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
    return {"ok": True, "persisted": st.to_json(redact_secrets=True), "runtime": {"lanes": lanes}}


@app.post("/automation/user/bootstrap")
async def automation_user_bootstrap(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw_chat_key = body.get("chatKey")
    if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
        raise HTTPException(status_code=400, detail="Missing 'chatKey'")
    chat_key = raw_chat_key.strip()

    user_id = _parse_telegram_private_user_id(chat_key)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Invalid 'chatKey' (expected a Telegram private chat id)")

    agent, created_main = await automation.ensure_user_main_agent(user_id=user_id)

    # Keep the current binding if it points to an accessible agent. Otherwise, bind to main.
    st = automation._store.state
    bound = st.chat_bindings.get(chat_key) if isinstance(getattr(st, "chat_bindings", None), dict) else None
    current_agent_id = bound if isinstance(bound, str) and bound.strip() else None
    if current_agent_id and not automation.can_user_access_agent_id(user_id=user_id, agent_id=current_agent_id):
        current_agent_id = None
    if not current_agent_id:
        await automation.bind_private_user_to_agent(user_id=user_id, chat_key=chat_key, agent_id=agent.agent_id)
        current_agent_id = agent.agent_id
    current_session_id = automation.resolve_agent_session_id(current_agent_id) if current_agent_id else None
    current_agent = automation._get_agent(current_agent_id) if current_agent_id else None
    channel_info = automation.list_channels_for_user(user_id=user_id)
    model_info = await _automation_model_payload(automation, agent=current_agent, user_id=user_id)

    return {
        "ok": True,
        "userId": user_id,
        "chatKey": chat_key,
        "createdMain": created_main,
        "currentAgentId": current_agent_id,
        "currentSessionId": current_session_id,
        "currentModel": automation._channel_adjusted_agent_model(current_agent),
        "availableModels": model_info.get("availableModels"),
        "models": model_info.get("models"),
        "modelSource": model_info.get("source"),
        "modelError": model_info.get("error"),
        "currentChannelId": channel_info.get("currentChannelId"),
        "currentChannel": channel_info.get("currentChannel"),
        "agent": agent.to_json(),
    }


@app.post("/automation/agent/list")
async def automation_agent_list(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw_chat_key = body.get("chatKey")
    if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
        raise HTTPException(status_code=400, detail="Missing 'chatKey'")
    chat_key = raw_chat_key.strip()

    user_id = _parse_telegram_private_user_id(chat_key)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Invalid 'chatKey'")

    main_id = _user_agent_id(user_id=user_id, short_name="main")
    if not automation.can_user_access_agent_id(user_id=user_id, agent_id=main_id):
        raise HTTPException(status_code=400, detail="Not initialized (run /start)")

    st = automation._store.state
    bound = st.chat_bindings.get(chat_key) if isinstance(getattr(st, "chat_bindings", None), dict) else None
    current_agent_id = bound if isinstance(bound, str) and bound.strip() else None
    if current_agent_id and not automation.can_user_access_agent_id(user_id=user_id, agent_id=current_agent_id):
        current_agent_id = None
    if not current_agent_id:
        current_agent_id = main_id
    current_session_id = automation.resolve_agent_session_id(current_agent_id) if current_agent_id else None
    current_agent = automation._get_agent(current_agent_id) if current_agent_id else None
    channel_info = automation.list_channels_for_user(user_id=user_id)
    model_info = await _automation_model_payload(automation, agent=current_agent, user_id=user_id)

    return {
        "ok": True,
        "agents": automation.list_agents_for_user(user_id=user_id),
        "currentAgentId": current_agent_id,
        "currentSessionId": current_session_id,
        "currentModel": automation._channel_adjusted_agent_model(current_agent),
        "availableModels": model_info.get("availableModels"),
        "models": model_info.get("models"),
        "modelSource": model_info.get("source"),
        "modelError": model_info.get("error"),
        "currentChannelId": channel_info.get("currentChannelId"),
        "currentChannel": channel_info.get("currentChannel"),
    }


@app.post("/automation/agent/resolve")
async def automation_agent_resolve(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw_chat_key = body.get("chatKey")
    if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
        raise HTTPException(status_code=400, detail="Missing 'chatKey'")
    chat_key = raw_chat_key.strip()

    user_id = _parse_telegram_private_user_id(chat_key)
    if user_id is None:
        agent_id = automation.resolve_agent_for_chat_key(chat_key)
        if not agent_id:
            raise HTTPException(status_code=404, detail="No agent bound for chatKey")
        agent = automation._get_agent(agent_id)
        sess_id = automation.resolve_agent_session_id(agent_id)
        if not sess_id:
            raise HTTPException(status_code=404, detail="Unknown agent")
        model_info = await _automation_model_payload(automation, agent=agent)
        return {
            "ok": True,
            "chatKey": chat_key,
            "agentId": agent_id,
            "sessionId": sess_id,
            "model": automation._channel_adjusted_agent_model(agent),
            "availableModels": model_info.get("availableModels"),
            "models": model_info.get("models"),
            "modelSource": model_info.get("source"),
            "modelError": model_info.get("error"),
        }

    st = automation._store.state
    bound = st.chat_bindings.get(chat_key) if isinstance(getattr(st, "chat_bindings", None), dict) else None
    bound_id = bound.strip() if isinstance(bound, str) and bound.strip() else None
    if bound_id and automation.can_user_access_agent_id(user_id=user_id, agent_id=bound_id):
        agent_id = bound_id
    else:
        main_id = _user_agent_id(user_id=user_id, short_name="main")
        if automation.can_user_access_agent_id(user_id=user_id, agent_id=main_id):
            agent_id = main_id
        else:
            raise HTTPException(status_code=400, detail="Not initialized (run /start)")
    sess_id = automation.resolve_agent_session_id(agent_id)
    if not sess_id:
        raise HTTPException(status_code=500, detail="Agent has no sessionId")
    agent = automation._get_agent(agent_id)
    channel_info = automation.list_channels_for_user(user_id=user_id)
    model_info = await _automation_model_payload(automation, agent=agent, user_id=user_id)
    return {
        "ok": True,
        "chatKey": chat_key,
        "agentId": agent_id,
        "sessionId": sess_id,
        "model": automation._channel_adjusted_agent_model(agent),
        "availableModels": model_info.get("availableModels"),
        "models": model_info.get("models"),
        "modelSource": model_info.get("source"),
        "modelError": model_info.get("error"),
        "currentChannelId": channel_info.get("currentChannelId"),
        "currentChannel": channel_info.get("currentChannel"),
    }


@app.post("/automation/agent/create")
async def automation_agent_create(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw_chat_key = body.get("chatKey")
    raw_name = body.get("agentId") or body.get("name")
    if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
        raise HTTPException(status_code=400, detail="Missing 'chatKey'")
    chat_key = raw_chat_key.strip()

    user_id = _parse_telegram_private_user_id(chat_key)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Invalid 'chatKey'")

    main_id = _user_agent_id(user_id=user_id, short_name="main")
    if not automation.can_user_access_agent_id(user_id=user_id, agent_id=main_id):
        raise HTTPException(status_code=400, detail="Not initialized (run /start)")

    try:
        agent = await automation.create_user_agent(user_id=user_id, short_name=str(raw_name or ""))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    model_info = await _automation_model_payload(automation, agent=agent, user_id=user_id)
    return {
        "ok": True,
        "agent": agent.to_json(),
        "availableModels": model_info.get("availableModels"),
        "models": model_info.get("models"),
        "modelSource": model_info.get("source"),
        "modelError": model_info.get("error"),
    }


@app.post("/automation/agent/use")
async def automation_agent_use(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw_chat_key = body.get("chatKey")
    raw_aid = body.get("agentId") or body.get("name")
    if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
        raise HTTPException(status_code=400, detail="Missing 'chatKey'")
    chat_key = raw_chat_key.strip()

    user_id = _parse_telegram_private_user_id(chat_key)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Invalid 'chatKey'")

    main_id = _user_agent_id(user_id=user_id, short_name="main")
    if not automation.can_user_access_agent_id(user_id=user_id, agent_id=main_id):
        raise HTTPException(status_code=400, detail="Not initialized (run /start)")

    aid_raw = _normalize_agent_id(raw_aid)
    if not aid_raw:
        raise HTTPException(status_code=400, detail="Invalid 'agentId'")
    if re.match(r"^u\d+-", aid_raw):
        agent_id = aid_raw
    else:
        agent_id = _user_agent_id(user_id=user_id, short_name=aid_raw)

    try:
        agent = await automation.bind_private_user_to_agent(user_id=user_id, chat_key=chat_key, agent_id=agent_id)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    model_info = await _automation_model_payload(automation, agent=agent, user_id=user_id)
    return {
        "ok": True,
        "chatKey": chat_key,
        "agent": agent.to_json(),
        "availableModels": model_info.get("availableModels"),
        "models": model_info.get("models"),
        "modelSource": model_info.get("source"),
        "modelError": model_info.get("error"),
    }


@app.post("/automation/agent/model/set")
async def automation_agent_model_set(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw_chat_key = body.get("chatKey")
    raw_aid = body.get("agentId") or body.get("name")
    raw_model = body.get("model")
    if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
        raise HTTPException(status_code=400, detail="Missing 'chatKey'")
    chat_key = raw_chat_key.strip()

    user_id = _parse_telegram_private_user_id(chat_key)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Invalid 'chatKey'")

    main_id = _user_agent_id(user_id=user_id, short_name="main")
    if not automation.can_user_access_agent_id(user_id=user_id, agent_id=main_id):
        raise HTTPException(status_code=400, detail="Not initialized (run /start)")

    aid_raw = _normalize_agent_id(raw_aid)
    if not aid_raw:
        raise HTTPException(status_code=400, detail="Invalid 'agentId'")
    if re.match(r"^u\d+-", aid_raw):
        agent_id = aid_raw
    else:
        agent_id = _user_agent_id(user_id=user_id, short_name=aid_raw)

    try:
        agent, synced = await automation.set_user_agent_model(user_id=user_id, agent_id=agent_id, model=str(raw_model or ""))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    model_info = await _automation_model_payload(automation, agent=agent, user_id=user_id)

    return {
        "ok": True,
        "chatKey": chat_key,
        "agent": agent.to_json(),
        "liveModelSynced": synced,
        "availableModels": model_info.get("availableModels"),
        "models": model_info.get("models"),
        "modelSource": model_info.get("source"),
        "modelError": model_info.get("error"),
    }


@app.post("/automation/agent/rename")
async def automation_agent_rename(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw_chat_key = body.get("chatKey")
    raw_aid = body.get("agentId") or body.get("name")
    raw_new = body.get("newAgentId") or body.get("newName")
    if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
        raise HTTPException(status_code=400, detail="Missing 'chatKey'")
    chat_key = raw_chat_key.strip()

    user_id = _parse_telegram_private_user_id(chat_key)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Invalid 'chatKey'")

    main_id = _user_agent_id(user_id=user_id, short_name="main")
    if not automation.can_user_access_agent_id(user_id=user_id, agent_id=main_id):
        raise HTTPException(status_code=400, detail="Not initialized (run /start)")

    aid_raw = _normalize_agent_id(raw_aid)
    if not aid_raw:
        raise HTTPException(status_code=400, detail="Invalid 'agentId'")
    if re.match(r"^u\d+-", aid_raw):
        agent_id = aid_raw
    else:
        agent_id = _user_agent_id(user_id=user_id, short_name=aid_raw)

    try:
        agent = await automation.rename_user_agent(user_id=user_id, agent_id=agent_id, new_short_name=str(raw_new or ""))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "chatKey": chat_key, "agent": agent.to_json()}


@app.post("/automation/agent/delete")
async def automation_agent_delete(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw_chat_key = body.get("chatKey")
    raw_aid = body.get("agentId") or body.get("name")
    if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
        raise HTTPException(status_code=400, detail="Missing 'chatKey'")
    chat_key = raw_chat_key.strip()

    user_id = _parse_telegram_private_user_id(chat_key)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Invalid 'chatKey'")

    main_id = _user_agent_id(user_id=user_id, short_name="main")
    if not automation.can_user_access_agent_id(user_id=user_id, agent_id=main_id):
        raise HTTPException(status_code=400, detail="Not initialized (run /start)")

    aid_raw = _normalize_agent_id(raw_aid)
    if not aid_raw:
        raise HTTPException(status_code=400, detail="Invalid 'agentId'")
    if re.match(r"^u\d+-", aid_raw):
        agent_id = aid_raw
    else:
        agent_id = _user_agent_id(user_id=user_id, short_name=aid_raw)

    try:
        result = await automation.delete_user_agent(user_id=user_id, chat_key=chat_key, agent_id=agent_id)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "chatKey": chat_key, **(result or {})}


@app.post("/automation/channel/list")
async def automation_channel_list(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw_chat_key = body.get("chatKey")
    if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
        raise HTTPException(status_code=400, detail="Missing 'chatKey'")
    chat_key = raw_chat_key.strip()

    user_id = _parse_telegram_private_user_id(chat_key)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Invalid 'chatKey'")

    return {"chatKey": chat_key, **automation.list_channels_for_user(user_id=user_id)}


@app.post("/automation/channel/select")
async def automation_channel_select(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw_chat_key = body.get("chatKey")
    raw_channel = body.get("channelId") or body.get("id") or body.get("name")
    if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
        raise HTTPException(status_code=400, detail="Missing 'chatKey'")
    chat_key = raw_chat_key.strip()

    user_id = _parse_telegram_private_user_id(chat_key)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Invalid 'chatKey'")
    if not isinstance(raw_channel, str) or not raw_channel.strip():
        raise HTTPException(status_code=400, detail="Missing 'channelId'")

    try:
        await automation.select_user_channel(user_id=user_id, channel_id=raw_channel)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    listed = automation.list_channels_for_user(user_id=user_id)
    return {"chatKey": chat_key, **listed}


@app.post("/automation/channel/create")
async def automation_channel_create(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw_chat_key = body.get("chatKey")
    raw_name = body.get("name") or body.get("channelName")
    raw_base_url = body.get("baseUrl")
    raw_api_key = body.get("apiKey")
    if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
        raise HTTPException(status_code=400, detail="Missing 'chatKey'")
    chat_key = raw_chat_key.strip()

    user_id = _parse_telegram_private_user_id(chat_key)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Invalid 'chatKey'")

    try:
        created = await automation.create_user_channel(
            user_id=user_id,
            name=str(raw_name or ""),
            base_url=str(raw_base_url or ""),
            api_key=str(raw_api_key or ""),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    listed = automation.list_channels_for_user(user_id=user_id)
    created_entry = next(
        (dict(item) for item in listed.get("channels", []) if isinstance(item, dict) and item.get("channelId") == created.channel_id),
        None,
    )
    return {"ok": True, "chatKey": chat_key, "channel": created_entry, **listed}


@app.post("/automation/channel/rename")
async def automation_channel_rename(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw_chat_key = body.get("chatKey")
    raw_channel = body.get("channelId") or body.get("id") or body.get("name")
    raw_new_name = body.get("newName") or body.get("channelName")
    if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
        raise HTTPException(status_code=400, detail="Missing 'chatKey'")
    chat_key = raw_chat_key.strip()

    user_id = _parse_telegram_private_user_id(chat_key)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Invalid 'chatKey'")
    if not isinstance(raw_channel, str) or not raw_channel.strip():
        raise HTTPException(status_code=400, detail="Missing 'channelId'")

    try:
        updated = await automation.rename_user_channel(user_id=user_id, channel_id=raw_channel, new_name=str(raw_new_name or ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    listed = automation.list_channels_for_user(user_id=user_id)
    updated_entry = next(
        (dict(item) for item in listed.get("channels", []) if isinstance(item, dict) and item.get("channelId") == updated.channel_id),
        None,
    )
    return {"ok": True, "chatKey": chat_key, "channel": updated_entry, **listed}


@app.post("/automation/channel/delete")
async def automation_channel_delete(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw_chat_key = body.get("chatKey")
    raw_channel = body.get("channelId") or body.get("id") or body.get("name")
    if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
        raise HTTPException(status_code=400, detail="Missing 'chatKey'")
    chat_key = raw_chat_key.strip()

    user_id = _parse_telegram_private_user_id(chat_key)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Invalid 'chatKey'")
    if not isinstance(raw_channel, str) or not raw_channel.strip():
        raise HTTPException(status_code=400, detail="Missing 'channelId'")

    try:
        deleted = await automation.delete_user_channel(user_id=user_id, channel_id=raw_channel)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    listed = automation.list_channels_for_user(user_id=user_id)
    return {"ok": True, "chatKey": chat_key, **deleted, **listed}


@app.post("/automation/channel/key/set")
async def automation_channel_key_set(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw_chat_key = body.get("chatKey")
    raw_channel = body.get("channelId") or body.get("id") or body.get("name")
    raw_api_key = body.get("apiKey")
    if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
        raise HTTPException(status_code=400, detail="Missing 'chatKey'")
    chat_key = raw_chat_key.strip()

    user_id = _parse_telegram_private_user_id(chat_key)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Invalid 'chatKey'")
    if not isinstance(raw_channel, str) or not raw_channel.strip():
        raise HTTPException(status_code=400, detail="Missing 'channelId'")

    try:
        result = await automation.set_user_channel_api_key(user_id=user_id, channel_id=raw_channel, api_key=str(raw_api_key or ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    listed = automation.list_channels_for_user(user_id=user_id)
    return {"ok": True, "chatKey": chat_key, **result, **listed}


@app.post("/automation/channel/key/clear")
async def automation_channel_key_clear(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw_chat_key = body.get("chatKey")
    raw_channel = body.get("channelId") or body.get("id") or body.get("name")
    if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
        raise HTTPException(status_code=400, detail="Missing 'chatKey'")
    chat_key = raw_chat_key.strip()

    user_id = _parse_telegram_private_user_id(chat_key)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Invalid 'chatKey'")
    if not isinstance(raw_channel, str) or not raw_channel.strip():
        raise HTTPException(status_code=400, detail="Missing 'channelId'")

    try:
        result = await automation.clear_user_channel_api_key(user_id=user_id, channel_id=raw_channel)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    listed = automation.list_channels_for_user(user_id=user_id)
    return {"ok": True, "chatKey": chat_key, **result, **listed}


@app.post("/automation/chat/bind")
async def automation_chat_bind(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    raw_chat_key = body.get("chatKey")
    raw_aid = body.get("agentId") or body.get("name")
    raw_actor = body.get("actorUserId") or body.get("userId")
    if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
        raise HTTPException(status_code=400, detail="Missing 'chatKey'")
    chat_key = raw_chat_key.strip()
    group_binding_key = _telegram_group_binding_key_from_chat_key(chat_key)
    topic_id = _telegram_topic_id_from_chat_key(chat_key)
    is_topic_chat = isinstance(group_binding_key, str) and group_binding_key and chat_key != group_binding_key
    is_general_topic = topic_id == 1

    actor_user_id: Optional[int] = None
    if raw_actor is not None:
        try:
            actor_user_id = int(raw_actor)
        except Exception:
            actor_user_id = None
    if actor_user_id is None or actor_user_id <= 0:
        raise HTTPException(status_code=400, detail="Missing/invalid 'actorUserId'")

    # Prevent binding someone else's private chat key.
    private_id = _parse_telegram_private_user_id(chat_key)
    if private_id is not None and private_id != actor_user_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    aid_raw = _normalize_agent_id(raw_aid)
    if not aid_raw:
        raise HTTPException(status_code=400, detail="Invalid 'agentId'")
    if re.match(r"^u\d+-", aid_raw):
        agent_id = aid_raw
    else:
        agent_id = _user_agent_id(user_id=actor_user_id, short_name=aid_raw)

    agent = automation._get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Unknown agent")
    if not automation._can_user_access_agent(user_id=actor_user_id, agent=agent):
        raise HTTPException(status_code=403, detail="Forbidden")

    st0 = automation._store.state
    existing_bindings = getattr(st0, "chat_bindings", None)
    group_default_agent_id = (
        existing_bindings.get(group_binding_key)
        if isinstance(existing_bindings, dict) and isinstance(group_binding_key, str) and group_binding_key
        else None
    )
    group_has_any_binding = False
    if isinstance(existing_bindings, dict) and isinstance(group_binding_key, str) and group_binding_key:
        prefix = f"{group_binding_key}:"
        for bound_ck in existing_bindings.keys():
            if not isinstance(bound_ck, str) or not bound_ck.strip():
                continue
            ck_norm = bound_ck.strip()
            if ck_norm == group_binding_key or ck_norm.startswith(prefix):
                group_has_any_binding = True
                break

    binding_chat_key = _chat_binding_storage_key(chat_key)
    if not binding_chat_key:
        raise HTTPException(status_code=400, detail="Invalid 'chatKey'")
    if is_topic_chat:
        if is_general_topic or not group_has_any_binding:
            binding_chat_key = group_binding_key or binding_chat_key
        elif (
            isinstance(group_binding_key, str)
            and group_binding_key
            and isinstance(group_default_agent_id, str)
            and group_default_agent_id.strip() == agent_id
        ):
            binding_chat_key = group_binding_key
        else:
            binding_chat_key = chat_key

    def _write(st: PersistedGatewayAutomationState) -> None:
        st.version = max(int(getattr(st, "version", 1) or 1), AUTOMATION_STATE_VERSION)
        # Topic switches should not overwrite the group's default binding or
        # other topics' overrides. Only the chosen binding key is updated.
        if (
            is_topic_chat
            and not is_general_topic
            and isinstance(group_binding_key, str)
            and group_binding_key
            and isinstance(group_default_agent_id, str)
            and group_default_agent_id.strip() == agent_id
        ):
            st.chat_bindings.pop(chat_key, None)
        else:
            st.chat_bindings[binding_chat_key] = agent_id

    await automation._store.update(_write)
    return {
        "ok": True,
        "chatKey": chat_key,
        "bindingChatKey": binding_chat_key,
        "agentId": agent_id,
        "sessionId": agent.session_id,
        "agent": agent.to_json(),
    }


@app.post("/automation/node/list")
async def automation_node_list(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    chat_key = _automation_require_chat_key(body)
    sid = _automation_require_session_id_for_chat_key(automation, chat_key)

    nodes = await _list_nodes_with_state(scope_session_id=sid)
    default_node_id = automation.default_node_id_for_session(sid)
    paused_node_ids = automation.get_paused_node_ids_for_session(sid)
    return {
        "ok": True,
        "chatKey": chat_key,
        "sessionId": sid,
        "currentNodeId": default_node_id,
        "defaultNodeId": default_node_id,
        "defaultNodeConnected": any(node.get("nodeId") == default_node_id for node in nodes),
        "pausedNodeIds": paused_node_ids,
        "nodes": nodes,
    }


@app.post("/automation/node/pause")
async def automation_node_pause(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    chat_key = _automation_require_chat_key(body)
    sid = _automation_require_session_id_for_chat_key(automation, chat_key)

    node_raw = str(body.get("nodeId") or body.get("node") or "").strip()
    if not node_raw:
        raise HTTPException(status_code=400, detail="Missing 'nodeId'")

    node_id = await app.state.node_registry.resolve_node_id(node_raw, scope_session_id=sid)
    if not node_id:
        raise HTTPException(status_code=404, detail="Node not found")
    default_node_id = automation.default_node_id_for_session(sid)
    if default_node_id and node_id == default_node_id:
        raise HTTPException(status_code=400, detail="Default node cannot be paused")

    await automation.set_node_paused(session_id=sid, node_id=node_id, paused=True)
    nodes = await _list_nodes_with_state(scope_session_id=sid)
    return {
        "ok": True,
        "chatKey": chat_key,
        "sessionId": sid,
        "nodeId": node_id,
        "paused": True,
        "defaultNodeId": default_node_id,
        "pausedNodeIds": automation.get_paused_node_ids_for_session(sid),
        "nodes": nodes,
    }


@app.post("/automation/node/resume")
async def automation_node_resume(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    chat_key = _automation_require_chat_key(body)
    sid = _automation_require_session_id_for_chat_key(automation, chat_key)

    node_raw = str(body.get("nodeId") or body.get("node") or "").strip()
    if not node_raw:
        raise HTTPException(status_code=400, detail="Missing 'nodeId'")

    node_id = await app.state.node_registry.resolve_node_id(node_raw, scope_session_id=sid)
    if not node_id and node_raw in automation.get_paused_node_ids_for_session(sid):
        node_id = node_raw
    if not node_id:
        raise HTTPException(status_code=404, detail="Node not found")

    await automation.set_node_paused(session_id=sid, node_id=node_id, paused=False)
    nodes = await _list_nodes_with_state(scope_session_id=sid)
    return {
        "ok": True,
        "chatKey": chat_key,
        "sessionId": sid,
        "nodeId": node_id,
        "paused": False,
        "defaultNodeId": automation.default_node_id_for_session(sid),
        "pausedNodeIds": automation.get_paused_node_ids_for_session(sid),
        "nodes": nodes,
    }


@app.post("/automation/node/token")
async def automation_node_token(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e
    chat_key = _automation_require_chat_key(body)
    sid = _automation_require_session_id_for_chat_key(automation, chat_key)

    master = os.getenv("ARGUS_NODE_TOKEN") or os.getenv("ARGUS_TOKEN") or None
    token = _node_derive_session_token(master, sid) if master else None
    return {"ok": True, "chatKey": chat_key, "sessionId": sid, "path": "/nodes/ws", "token": token}


@app.get("/automation/cron/jobs")
async def cron_list_jobs(request: Request):
    _http_require_token(request)
    automation = _get_automation_or_500()
    sid = (request.query_params.get("sessionId") or "").strip() or None
    if not sid:
        aid = _normalize_agent_id(request.query_params.get("agentId"))
        if aid:
            sid = automation.resolve_agent_session_id(aid)
            if not sid:
                raise HTTPException(status_code=404, detail="Unknown agent")
    if not sid:
        raise HTTPException(status_code=400, detail="Missing 'sessionId' (or 'agentId')")
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

    sid = str(body.get("sessionId") or "").strip() or None
    if not sid:
        aid = _normalize_agent_id(body.get("agentId"))
        if aid:
            sid = automation.resolve_agent_session_id(aid)
            if not sid:
                raise HTTPException(status_code=404, detail="Unknown agent")
    if not sid:
        raise HTTPException(status_code=400, detail="Missing 'sessionId' (or 'agentId')")

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
    sid = (request.query_params.get("sessionId") or "").strip() or None
    if not sid:
        aid = _normalize_agent_id(request.query_params.get("agentId"))
        if aid:
            sid = automation.resolve_agent_session_id(aid)
            if not sid:
                raise HTTPException(status_code=404, detail="Unknown agent")
    if not sid:
        raise HTTPException(status_code=400, detail="Missing 'sessionId' (or 'agentId')")

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
        aid = _normalize_agent_id(body.get("agentId"))
        if aid:
            session_id = automation.resolve_agent_session_id(aid)
            if not session_id:
                raise HTTPException(status_code=404, detail="Unknown agent")
    if not session_id:
        raise HTTPException(status_code=400, detail="Missing 'sessionId' (or 'agentId')")

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
    if not _provisioner_manages_runtime_sessions():
        raise HTTPException(
            status_code=400,
            detail=f"Managed sessions are not available in provision mode '{_provisioner_mode_name()}'",
        )
    try:
        await _delete_managed_session(session_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="Session not found") from e
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Failed to delete managed session %s", session_id)
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
    session_id: Optional[str] = None,
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
    sid = session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
    if not sid:
        return
    sess = st.sessions.get(sid)
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
            session_id=sid,
            thread_id=main_tid,
            kind="node",
            text=text,
            meta={
                "event": event,
                "sessionId": sid,
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
    scoped_session_id: Optional[str] = None,
) -> None:
    automation: Optional[AutomationManager] = getattr(app.state, "automation", None)
    if automation is None:
        return

    job_id = str(payload.get("jobId") or "").strip()
    if not job_id:
        return

    session_id = payload.get("sessionId")
    if not isinstance(session_id, str) or not session_id.strip():
        session_id = scoped_session_id or _session_id_from_node_id(node_id)
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
    provided = _extract_token(ws) or ""
    master = os.getenv("ARGUS_NODE_TOKEN") or os.getenv("ARGUS_TOKEN") or None
    scope_from_token: Optional[str] = None

    if master is not None:
        scope_from_token = _node_verify_derived_session_token(master, provided)

    await ws.accept()
    if master is not None and not (isinstance(scope_from_token, str) and scope_from_token.strip()):
        await ws.close(code=1008, reason="Unauthorized")
        return
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

    scoped_session_id = scope_from_token.strip() if isinstance(scope_from_token, str) and scope_from_token.strip() else None
    if not scoped_session_id:
        scoped_session_id = _session_id_from_node_id(node_id)

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
        scoped_session_id=scoped_session_id,
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
            session_id=scoped_session_id,
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
                await app.state.node_registry.touch(node_id, scope_session_id=scoped_session_id)
                await app.state.node_registry.handle_invoke_result(payload)
                continue
            if msg.get("type") == "event" and msg.get("event") == "node.process.exited" and isinstance(msg.get("payload"), dict):
                payload = msg["payload"]
                if str(payload.get("nodeId") or "") not in ("", node_id):
                    continue
                await app.state.node_registry.touch(node_id, scope_session_id=scoped_session_id)
                asyncio.create_task(
                    _enqueue_process_exited_system_event(node_id=node_id, payload=payload, scoped_session_id=scoped_session_id)
                )
                continue
            if msg.get("type") == "event" and msg.get("event") == "node.heartbeat":
                await app.state.node_registry.touch(node_id, scope_session_id=scoped_session_id)
                continue
    except WebSocketDisconnect:
        pass
    finally:
        removed = await app.state.node_registry.unregister(conn_id)
        if removed is not None:
            asyncio.create_task(
                _enqueue_node_system_event(
                    event="disconnected",
                    session_id=removed.scoped_session_id,
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

    if _provisioner_manages_runtime_sessions():
        expected = os.getenv("ARGUS_TOKEN")
        if expected and provided != expected:
            await ws.accept()
            await ws.close(code=1008, reason="Unauthorized")
            return

        await ws.accept()

        requested_session_raw = (ws.query_params.get("session") or "").strip() or None
        requested_session = _normalize_runtime_session_id(requested_session_raw) if requested_session_raw else None
        if requested_session_raw and not requested_session:
            await ws.close(code=1008, reason="Invalid session")
            return
        session_id = requested_session or uuid.uuid4().hex[:12]
        try:
            allow_create = not bool(requested_session)
            if requested_session:
                # If the client asked for an explicit session and it's already known in persisted automation state,
                # allow re-creating the runtime container (e.g. after a clean rebuild where the host state persists).
                automation: Optional[AutomationManager] = getattr(app.state, "automation", None)
                if automation is not None:
                    st = automation._store.state
                    if requested_session in st.sessions:
                        allow_create = True
                    else:
                        for a in (st.agents or {}).values():
                            if isinstance(a, PersistedAgentRuntime) and a.session_id == requested_session:
                                allow_create = True
                                break
                if not allow_create:
                    derived = _derive_default_workspace_host_path_for_session(
                        requested_session,
                        home_host_path=_configured_home_host_path(),
                        workspace_base_host_path=_configured_workspace_host_path(),
                    )
                    if derived and Path(derived).is_dir():
                        allow_create = True
            live, created = await _ensure_live_session(session_id, allow_create=allow_create)
        except KeyError:
            await ws.close(code=1008, reason="Unknown session")
            return
        except Exception:
            await ws.close(code=1011, reason="Failed to provision upstream runtime")
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
                                "mode": _provisioner_mode_name(),
                                "provider": live.provider,
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
                except RuntimeError as exc:
                    if _is_expected_websocket_receive_runtime_error(exc):
                        break
                    raise

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
                                    json.dumps(
                                        {"id": req_id, "error": {"code": -32000, "message": "Automation is not available"}}
                                    )
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

                        if method == "argus/user/bootstrap":
                            raw_chat_key = params.get("chatKey")
                            if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
                                if has_id:
                                    await ws.send_text(
                                        json.dumps({"id": req_id, "error": {"code": -32602, "message": "Missing 'chatKey'"}})
                                    )
                                continue
                            chat_key = raw_chat_key.strip()
                            user_id = _parse_telegram_private_user_id(chat_key)
                            if user_id is None:
                                if has_id:
                                    await ws.send_text(
                                        json.dumps({"id": req_id, "error": {"code": -32602, "message": "Invalid 'chatKey'"}})
                                    )
                                continue
                            agent, created_main = await automation.ensure_user_main_agent(user_id=user_id)

                            # Keep the current binding if it points to an accessible agent. Otherwise, bind to main.
                            st = automation._store.state
                            bound = (
                                st.chat_bindings.get(chat_key) if isinstance(getattr(st, "chat_bindings", None), dict) else None
                            )
                            current_agent_id = bound if isinstance(bound, str) and bound.strip() else None
                            if current_agent_id and not automation.can_user_access_agent_id(user_id=user_id, agent_id=current_agent_id):
                                current_agent_id = None
                            if not current_agent_id:
                                await automation.bind_private_user_to_agent(
                                    user_id=user_id, chat_key=chat_key, agent_id=agent.agent_id
                                )
                                current_agent_id = agent.agent_id
                            current_session_id = automation.resolve_agent_session_id(current_agent_id) if current_agent_id else None
                            result = {
                                "ok": True,
                                "userId": user_id,
                                "chatKey": chat_key,
                                "createdMain": created_main,
                                "currentAgentId": current_agent_id,
                                "currentSessionId": current_session_id,
                                "agent": agent.to_json(),
                            }
                            if has_id:
                                await ws.send_text(json.dumps({"id": req_id, "result": result}))
                            continue

                        if method == "argus/agent/list":
                            raw_chat_key = params.get("chatKey")
                            if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
                                if has_id:
                                    await ws.send_text(
                                        json.dumps({"id": req_id, "error": {"code": -32602, "message": "Missing 'chatKey'"}})
                                    )
                                continue
                            chat_key = raw_chat_key.strip()
                            user_id = _parse_telegram_private_user_id(chat_key)
                            if user_id is None:
                                if has_id:
                                    await ws.send_text(
                                        json.dumps({"id": req_id, "error": {"code": -32602, "message": "Invalid 'chatKey'"}})
                                    )
                                continue
                            main_id = _user_agent_id(user_id=user_id, short_name="main")
                            if not automation.can_user_access_agent_id(user_id=user_id, agent_id=main_id):
                                raise ValueError("Not initialized (run /start)")

                            st = automation._store.state
                            bound = (
                                st.chat_bindings.get(chat_key) if isinstance(getattr(st, "chat_bindings", None), dict) else None
                            )
                            current_agent_id = bound if isinstance(bound, str) and bound.strip() else None
                            if current_agent_id and not automation.can_user_access_agent_id(user_id=user_id, agent_id=current_agent_id):
                                current_agent_id = None
                            if not current_agent_id:
                                current_agent_id = main_id
                            current_session_id = automation.resolve_agent_session_id(current_agent_id) if current_agent_id else None
                            result = {
                                "ok": True,
                                "agents": automation.list_agents_for_user(user_id=user_id),
                                "currentAgentId": current_agent_id,
                                "currentSessionId": current_session_id,
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
                            user_id = _parse_telegram_private_user_id(chat_key)
                            if user_id is None:
                                agent_id = automation.resolve_agent_for_chat_key(chat_key)
                                if not agent_id:
                                    raise ValueError("No agent bound for chatKey")
                                sess_id = automation.resolve_agent_session_id(agent_id)
                                if not sess_id:
                                    raise RuntimeError("Unknown agent")
                                result = {"ok": True, "chatKey": chat_key, "agentId": agent_id, "sessionId": sess_id}
                            else:
                                st = automation._store.state
                                bound = (
                                    st.chat_bindings.get(chat_key)
                                    if isinstance(getattr(st, "chat_bindings", None), dict)
                                    else None
                                )
                                bound_id = bound.strip() if isinstance(bound, str) and bound.strip() else None
                                if bound_id and automation.can_user_access_agent_id(user_id=user_id, agent_id=bound_id):
                                    agent_id = bound_id
                                else:
                                    main_id = _user_agent_id(user_id=user_id, short_name="main")
                                    if automation.can_user_access_agent_id(user_id=user_id, agent_id=main_id):
                                        agent_id = main_id
                                    else:
                                        raise ValueError("Not initialized (run /start)")
                                sess_id = automation.resolve_agent_session_id(agent_id)
                                if not sess_id:
                                    raise RuntimeError("Agent has no sessionId")
                                result = {"ok": True, "chatKey": chat_key, "agentId": agent_id, "sessionId": sess_id}
                            if has_id:
                                await ws.send_text(json.dumps({"id": req_id, "result": result}))
                            continue

                        if method == "argus/node/token":
                            raw_chat_key = params.get("chatKey")
                            if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
                                if has_id:
                                    await ws.send_text(
                                        json.dumps({"id": req_id, "error": {"code": -32602, "message": "Missing 'chatKey'"}})
                                    )
                                continue
                            chat_key = raw_chat_key.strip()
                            sid = automation.resolve_session_id_for_chat_key(chat_key) or session_id
                            master = os.getenv("ARGUS_NODE_TOKEN") or os.getenv("ARGUS_TOKEN") or None
                            token = _node_derive_session_token(master, sid) if master else None
                            result = {
                                "ok": True,
                                "chatKey": chat_key,
                                "sessionId": sid,
                                "path": "/nodes/ws",
                                "token": token,
                            }
                            if has_id:
                                await ws.send_text(json.dumps({"id": req_id, "result": result}))
                            continue

                        if method == "argus/agent/create":
                            raw_chat_key = params.get("chatKey")
                            raw_name = params.get("agentId") or params.get("name")
                            if not isinstance(raw_chat_key, str) or not raw_chat_key.strip():
                                if has_id:
                                    await ws.send_text(
                                        json.dumps({"id": req_id, "error": {"code": -32602, "message": "Missing 'chatKey'"}})
                                    )
                                continue
                            chat_key = raw_chat_key.strip()
                            user_id = _parse_telegram_private_user_id(chat_key)
                            if user_id is None:
                                if has_id:
                                    await ws.send_text(
                                        json.dumps({"id": req_id, "error": {"code": -32602, "message": "Invalid 'chatKey'"}})
                                    )
                                continue
                            main_id = _user_agent_id(user_id=user_id, short_name="main")
                            if not automation.can_user_access_agent_id(user_id=user_id, agent_id=main_id):
                                raise ValueError("Not initialized (run /start)")
                            agent = await automation.create_user_agent(user_id=user_id, short_name=str(raw_name or ""))
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
                            chat_key = raw_chat_key.strip()
                            user_id = _parse_telegram_private_user_id(chat_key)
                            if user_id is None:
                                if has_id:
                                    await ws.send_text(
                                        json.dumps({"id": req_id, "error": {"code": -32602, "message": "Invalid 'chatKey'"}})
                                    )
                                continue
                            main_id = _user_agent_id(user_id=user_id, short_name="main")
                            if not automation.can_user_access_agent_id(user_id=user_id, agent_id=main_id):
                                raise ValueError("Not initialized (run /start)")
                            aid_raw = _normalize_agent_id(raw_aid)
                            if not aid_raw:
                                if has_id:
                                    await ws.send_text(
                                        json.dumps({"id": req_id, "error": {"code": -32602, "message": "Invalid 'agentId'"}})
                                    )
                                continue
                            if re.match(r"^u\d+-", aid_raw):
                                agent_id = aid_raw
                            else:
                                agent_id = _user_agent_id(user_id=user_id, short_name=aid_raw)
                            agent = await automation.bind_private_user_to_agent(
                                user_id=user_id, chat_key=chat_key, agent_id=agent_id
                            )
                            result = {"ok": True, "chatKey": chat_key, "agent": agent.to_json()}
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
                            telegram_attachments: list[dict[str, Any]] = []
                            defer_if_no_text = False

                            raw_attachments = params.get("telegramAttachments")
                            if isinstance(raw_attachments, list):
                                telegram_attachments.extend([x for x in raw_attachments if isinstance(x, dict)])
                                if telegram_attachments:
                                    defer_if_no_text = True

                            raw_images = params.get("telegramImages")
                            if isinstance(raw_images, list):
                                telegram_attachments.extend([x for x in raw_images if isinstance(x, dict)])

                            if not telegram_attachments:
                                telegram_attachments = None

                            text_param = params.get("text")
                            if not isinstance(text_param, str):
                                text_param = ""
                            if not text_param.strip() and not telegram_attachments:
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
                                telegram_attachments=telegram_attachments,
                                defer_if_no_text=defer_if_no_text,
                            )
                            if has_id:
                                await ws.send_text(json.dumps({"id": req_id, "result": res}))
                            continue

                        if method == "argus/turn/cancel":
                            cancel_id = params.get("cancelId") if isinstance(params.get("cancelId"), str) else None
                            _event_log(
                                "info",
                                "gw.cancel.request_received",
                                cancel_id=cancel_id,
                                rpc_id=req_id,
                                session_id=session_id,
                                thread_id=(params.get("threadId") if isinstance(params.get("threadId"), str) else None),
                                target=(params.get("target") if isinstance(params.get("target"), str) else None),
                            )
                            res = await automation.cancel_active_user_turn(
                                session_id=session_id,
                                thread_id=(params.get("threadId") if isinstance(params.get("threadId"), str) else None),
                                target=(params.get("target") if isinstance(params.get("target"), str) else None),
                                cancel_id=cancel_id,
                                rpc_id=req_id,
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
                                    json.dumps({"id": req_id, "result": {"ok": True, "event": ev.to_json(), "threadId": thread_id}})
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
                        if method == "argus/turn/cancel":
                            _event_log(
                                "warning",
                                "gw.cancel.request_error",
                                cancel_id=(params.get("cancelId") if isinstance(params.get("cancelId"), str) else None),
                                rpc_id=req_id,
                                session_id=session_id,
                                thread_id=(params.get("threadId") if isinstance(params.get("threadId"), str) else None),
                                target=(params.get("target") if isinstance(params.get("target"), str) else None),
                                err_kind=type(e).__name__,
                                err_msg=str(e),
                            )
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
                                tid_norm = tid.strip()
                                await live.set_turn_owner(thread_id=tid_norm, ws=ws)
                                automation: Optional[AutomationManager] = getattr(app.state, "automation", None)
                                if automation is not None:
                                    try:
                                        automation.note_client_turn_start_requested(session_id, tid_norm)
                                    except Exception:
                                        log.exception("Automation client turn-start request handler failed")
                                async with live.attach_lock:
                                    live.pending_client_turn_starts[str(upstream_id)] = tid_norm
                    await live.write_upstream(json.dumps(rewritten))
                    continue

                await live.write_upstream(text)
        except UpstreamSessionClosedError:
            log.info("Session %s upstream closed while proxying websocket traffic", session_id)
            if ws.client_state == WebSocketState.CONNECTED:
                try:
                    await ws.close(code=1011, reason="Upstream session closed")
                except Exception:
                    pass
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
                try:
                    text = await ws.receive_text()
                except RuntimeError as exc:
                    if _is_expected_websocket_receive_runtime_error(exc):
                        break
                    raise
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
