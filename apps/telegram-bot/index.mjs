import process from "node:process";
import fs from "node:fs/promises";
import path from "node:path";
import WebSocket from "ws";

function log(...args) {
  // eslint-disable-next-line no-console
  console.log(new Date().toISOString(), ...args);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function parseJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function isNonEmptyString(value) {
  return typeof value === "string" && value.trim().length > 0;
}

function stripOuterQuotes(value) {
  if (!isNonEmptyString(value)) return null;
  const trimmed = value.trim();
  if (
    (trimmed.startsWith("\"") && trimmed.endsWith("\"")) ||
    (trimmed.startsWith("'") && trimmed.endsWith("'"))
  ) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

function clampNumber(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

async function ensureDirForFile(filePath) {
  const dir = path.dirname(filePath);
  await fs.mkdir(dir, { recursive: true });
}

async function dirExists(dirPath) {
  try {
    const st = await fs.stat(dirPath);
    return st.isDirectory();
  } catch {
    return false;
  }
}

async function pathExists(targetPath) {
  try {
    await fs.stat(targetPath);
    return true;
  } catch {
    return false;
  }
}

class StateStore {
  constructor(statePath) {
    this.path = statePath;
    this.state = {
      version: 2,
      defaultSessionId: null,
      lastUpdateId: null,
      threads: {},
      lastActiveByThread: {}
    };
    this._writeChain = Promise.resolve();
  }

  async load() {
    try {
      const raw = await fs.readFile(this.path, "utf-8");
      const parsed = parseJson(raw);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return;
      const threads = parsed.threads && typeof parsed.threads === "object" && !Array.isArray(parsed.threads) ? parsed.threads : {};
      const lastActiveByThreadRaw =
        parsed.lastActiveByThread && typeof parsed.lastActiveByThread === "object" && !Array.isArray(parsed.lastActiveByThread)
          ? parsed.lastActiveByThread
          : {};
      const lastActiveByThread = {};
      for (const [threadId, entry] of Object.entries(lastActiveByThreadRaw)) {
        if (!isNonEmptyString(threadId)) continue;
        if (!entry || typeof entry !== "object" || Array.isArray(entry)) continue;
        const chatKey = isNonEmptyString(entry.chatKey) ? entry.chatKey : null;
        const atMs = Number.isFinite(entry.atMs) ? entry.atMs : null;
        if (!chatKey) continue;
        lastActiveByThread[threadId] = { chatKey, ...(Number.isFinite(atMs) ? { atMs } : {}) };
      }
      this.state = {
        version: 2,
        defaultSessionId: isNonEmptyString(parsed.defaultSessionId) ? parsed.defaultSessionId : null,
        lastUpdateId: Number.isFinite(parsed.lastUpdateId) ? parsed.lastUpdateId : null,
        threads,
        lastActiveByThread
      };
    } catch (e) {
      if (e && typeof e === "object" && "code" in e && e.code === "ENOENT") return;
      log("Failed to read state, continuing with empty state:", e instanceof Error ? e.message : String(e));
    }
  }

  save() {
    this._writeChain = this._writeChain.then(async () => {
      await ensureDirForFile(this.path);
      const tmp = `${this.path}.tmp`;
      const raw = JSON.stringify(this.state, null, 2) + "\n";
      await fs.writeFile(tmp, raw, "utf-8");
      await fs.rename(tmp, this.path);
    }).catch((e) => {
      log("Failed to write state:", e instanceof Error ? e.message : String(e));
    });
    return this._writeChain;
  }

  getThreadId(chatKey) {
    const entry = this.state.threads?.[chatKey];
    return entry && typeof entry === "object" && isNonEmptyString(entry.threadId) ? entry.threadId : null;
  }

  setThreadId(chatKey, threadId) {
    if (!this.state.threads || typeof this.state.threads !== "object") this.state.threads = {};
    this.state.threads[chatKey] = { threadId };
    return this.save();
  }

  setDefaultSessionId(sessionId) {
    this.state.defaultSessionId = sessionId;
    return this.save();
  }

  setLastUpdateId(updateId) {
    this.state.lastUpdateId = updateId;
    return this.save();
  }

  getLastActiveChatKey(threadId) {
    const entry = this.state.lastActiveByThread?.[threadId];
    return entry && typeof entry === "object" && isNonEmptyString(entry.chatKey) ? entry.chatKey : null;
  }

  setLastActiveChatKey(threadId, chatKey) {
    if (!isNonEmptyString(threadId) || !isNonEmptyString(chatKey)) return this.save();
    if (!this.state.lastActiveByThread || typeof this.state.lastActiveByThread !== "object") this.state.lastActiveByThread = {};
    this.state.lastActiveByThread[threadId] = { chatKey, atMs: Date.now() };
    return this.save();
  }
}

class SerialQueue {
  constructor() {
    this._chain = Promise.resolve();
  }

  enqueue(fn) {
    const run = async () => await fn();
    const next = this._chain.then(run, run);
    this._chain = next.catch(() => {});
    return next;
  }
}

class TelegramApi {
  constructor(token) {
    this.token = token;
    this.base = `https://api.telegram.org/bot${token}`;
  }

  async call(method, params) {
    const res = await fetch(`${this.base}/${method}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(params ?? {})
    });
    const data = await res.json().catch(() => null);
    if (!data || typeof data !== "object") {
      throw new Error(`Telegram ${method} failed: bad response`);
    }
    if (!data.ok) {
      throw new Error(`Telegram ${method} failed: ${data.description || "unknown error"}`);
    }
    return data.result;
  }

  async getMe() {
    return await this.call("getMe", {});
  }

  async getUpdates(params) {
    return await this.call("getUpdates", params);
  }

  async sendMessage(params) {
    return await this.call("sendMessage", params);
  }

  async sendChatAction(params) {
    return await this.call("sendChatAction", params);
  }
}

function buildUrlWithParams(url, params) {
  const u = new URL(url);
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    u.searchParams.set(k, String(v));
  }
  return u.toString();
}

function redactUrlSecrets(rawUrl) {
  const raw = stripOuterQuotes(rawUrl);
  if (!isNonEmptyString(raw)) return rawUrl;
  try {
    const u = new URL(raw);
    const keys = ["token", "access_token", "authorization", "auth", "bearer"];
    for (const k of keys) {
      if (u.searchParams.has(k)) u.searchParams.set(k, "***");
    }
    return u.toString();
  } catch {
    return raw.replace(/([?&](?:token|access_token|authorization|auth|bearer)=)[^&#]+/gi, "$1***");
  }
}

function stripSessionFromWsUrl(wsUrl) {
  const raw = stripOuterQuotes(wsUrl);
  if (!isNonEmptyString(raw)) return null;
  try {
    const u = new URL(raw);
    u.searchParams.delete("session");
    u.hash = "";
    return u.toString();
  } catch {
    return raw;
  }
}

function deriveHttpBaseFromWsUrl(wsUrl) {
  const raw = stripOuterQuotes(wsUrl);
  if (!isNonEmptyString(raw)) return null;
  try {
    const u = new URL(raw);
    u.protocol = u.protocol === "wss:" ? "https:" : "http:";
    u.pathname = "";
    u.search = "";
    u.hash = "";
    return u.toString().replace(/\/$/, "");
  } catch {
    return null;
  }
}

function deriveGatewayFromHostEnv(hostEnv, { runningInDocker = false } = {}) {
  let raw = stripOuterQuotes(hostEnv);
  if (!isNonEmptyString(raw)) raw = runningInDocker ? "gateway" : "127.0.0.1";

  if (raw.startsWith("http://") || raw.startsWith("https://")) {
    const u = new URL(raw);
    if (runningInDocker && (u.hostname === "127.0.0.1" || u.hostname === "localhost")) {
      u.hostname = "gateway";
    }
    const httpBase = `${u.protocol}//${u.host}`;
    const wsProto = u.protocol === "https:" ? "wss:" : "ws:";
    const wsBase = `${wsProto}//${u.host}/ws`;
    return { httpBase, wsBase };
  }

  if (raw.startsWith("ws://") || raw.startsWith("wss://")) {
    const ws = new URL(raw);
    if (runningInDocker && (ws.hostname === "127.0.0.1" || ws.hostname === "localhost")) {
      ws.hostname = "gateway";
    }
    const wsBase = stripSessionFromWsUrl(ws.toString()) || ws.toString();
    const httpProto = ws.protocol === "wss:" ? "https:" : "http:";
    const httpBase = `${httpProto}//${ws.host}`;
    return { httpBase, wsBase };
  }

  if (runningInDocker) {
    if (raw === "127.0.0.1" || raw === "localhost") raw = "gateway";
    if (raw.startsWith("127.0.0.1:")) raw = `gateway:${raw.slice("127.0.0.1:".length)}`;
    if (raw.startsWith("localhost:")) raw = `gateway:${raw.slice("localhost:".length)}`;
  }

  const hostPort = raw.includes(":") ? raw : `${raw}:8080`;
  return { httpBase: `http://${hostPort}`, wsBase: `ws://${hostPort}/ws` };
}

function isArgusSessionMessage(msg) {
  return msg && typeof msg === "object" && msg.method === "argus/session" && msg.params && typeof msg.params === "object";
}

class ArgusClient {
  constructor({ gatewayHttpUrl, gatewayWsUrl, token, cwd }) {
    this.gatewayHttpUrl = gatewayHttpUrl.replace(/\/+$/, "");
    this.gatewayWsUrl = gatewayWsUrl;
    this.token = token || null;
    this.cwd = cwd;

    this.sessionId = null;
    this.ws = null;
    this.nextId = 1;
    this.pending = new Map();
    this.initialized = false;

    this._connecting = null;
    this._onSessionId = null;

    this.turnsByKey = new Map();
    this.onTurnCompleted = null;
    this.onDisconnected = null;
  }

  _isWsOpen() {
    return this.ws && this.ws.readyState === WebSocket.OPEN;
  }

  _buildWsUrl({ sessionId } = {}) {
    const params = {};
    if (this.token) params.token = this.token;
    if (sessionId) params.session = sessionId;
    return buildUrlWithParams(this.gatewayWsUrl, params);
  }

  async _httpJson(method, pathName, body) {
    const url = `${this.gatewayHttpUrl}${pathName}`;
    const headers = { "content-type": "application/json" };
    if (this.token) headers.authorization = `Bearer ${this.token}`;
    const res = await fetch(url, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`Gateway ${method} ${pathName} failed: ${res.status} ${text}`.trim());
    }
    return await res.json();
  }

  async listSessions() {
    const data = await this._httpJson("GET", "/sessions");
    const sessions = data?.sessions;
    return Array.isArray(sessions) ? sessions : [];
  }

  async connectToSession(sessionId) {
    if (this._isWsOpen() && this.sessionId === sessionId) {
      await this.initialize();
      return;
    }
    if (this._connecting) return await this._connecting;
    this._connecting = (async () => {
      await this._connect(sessionId);
      this.sessionId = sessionId;
      await this.initialize();
    })();
    try {
      await this._connecting;
    } finally {
      this._connecting = null;
    }
  }

  async connectNewSession() {
    if (this._connecting) return await this._connecting;
    this._connecting = (async () => {
      const sidPromise = new Promise((resolve, reject) => {
        this._onSessionId = { resolve, reject };
      });
      await this._connect(null);
      const sid = await Promise.race([
        sidPromise,
        new Promise((_, reject) => setTimeout(() => reject(new Error("Timed out waiting for session id")), 15000))
      ]);
      this.sessionId = sid;
      await this.initialize();
      return sid;
    })();
    try {
      return await this._connecting;
    } finally {
      this._connecting = null;
    }
  }

  async _connect(sessionIdOrNull) {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      try {
        this.ws.close(1000, "reconnect");
      } catch {
        // ignore
      }
    }

    this.initialized = false;
    this.nextId = 1;
    for (const [, p] of this.pending) p.reject(new Error("reconnecting"));
    this.pending.clear();

    const url = this._buildWsUrl({ sessionId: sessionIdOrNull });
    log("Connecting to gateway WS:", redactUrlSecrets(url));
    const ws = new WebSocket(url);
    this.ws = ws;

    await new Promise((resolve, reject) => {
      const cleanup = () => {
        ws.off("open", onOpen);
        ws.off("error", onError);
        ws.off("close", onClose);
      };
      const onOpen = () => {
        cleanup();
        resolve();
      };
      const onError = (err) => {
        cleanup();
        const message = err instanceof Error ? err.message : String(err);
        reject(new Error(message ? `WebSocket error: ${message}` : "WebSocket error"));
      };
      const onClose = (code, reason) => {
        cleanup();
        const text = reason ? reason.toString() : "";
        reject(new Error(`WebSocket closed (${code}${text ? `: ${text}` : ""})`));
      };
      ws.on("open", onOpen);
      ws.on("error", onError);
      ws.on("close", onClose);
    });

    ws.on("message", (data) => {
      const text = typeof data === "string" ? data : data.toString("utf-8");
      this._handleWire(text);
    });

    ws.on("close", () => {
      this.initialized = false;
      this.ws = null;
      const err = new Error("WebSocket closed");
      for (const [, p] of this.pending) p.reject(err);
      this.pending.clear();
      this.turnsByKey.clear();
      const cb = this.onDisconnected;
      if (typeof cb === "function") {
        try {
          cb();
        } catch {
          // ignore
        }
      }
    });
  }

  _send(obj) {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) throw new Error("Not connected to gateway");
    ws.send(JSON.stringify(obj));
  }

  rpc(method, params, { timeoutMs } = {}) {
    const id = this.nextId++;
    const req = { method, id, ...(params !== undefined ? { params } : {}) };
    this._send(req);
    const ms = clampNumber(timeoutMs, 120000);
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => {
        if (!this.pending.has(id)) return;
        this.pending.delete(id);
        reject(new Error(`Timeout waiting for ${method} (${id})`));
      }, ms);
      this.pending.set(id, {
        resolve: (v) => {
          clearTimeout(t);
          resolve(v);
        },
        reject: (e) => {
          clearTimeout(t);
          reject(e);
        }
      });
    });
  }

  async initialize() {
    if (this.initialized) return;
    await this.rpc("initialize", { clientInfo: { name: "argus_tg_bot", title: "Argus Telegram Bot", version: "0.1.0" } });
    this._send({ method: "initialized" });
    this.initialized = true;
  }

  async startThread() {
    const result = await this.rpc("thread/start", { cwd: this.cwd, approvalPolicy: "never", sandbox: "danger-full-access" });
    const tid = result?.thread?.id;
    if (!isNonEmptyString(tid)) throw new Error("Invalid thread/start response");
    return tid;
  }

  async resumeThread(threadId) {
    await this.rpc("thread/resume", { threadId });
  }

  async enqueueInput({ text, threadId, target }) {
    if (!isNonEmptyString(text)) throw new Error("Missing text");
    const params = { text };
    if (isNonEmptyString(threadId)) params.threadId = threadId;
    if (isNonEmptyString(target)) params.target = target;
    return await this.rpc("argus/input/enqueue", params, { timeoutMs: 120000 });
  }

  _handleServerRequest(msg) {
    const id = msg.id;
    const method = msg.method;
    if (method === "item/commandExecution/requestApproval" || method === "item/fileChange/requestApproval") {
      this._send({ id, result: { decision: "decline" } });
      return;
    }
    if (typeof method === "string" && method.endsWith("/requestApproval")) {
      this._send({ id, result: { decision: "decline" } });
      return;
    }
    this._send({ id, error: { code: -32601, message: `Unsupported server request: ${method}` } });
  }

  _handleNotification(msg) {
    if (isArgusSessionMessage(msg)) {
      const sid = msg.params?.id;
      if (isNonEmptyString(sid)) {
        if (!this.sessionId) log("Gateway assigned sessionId:", sid);
        this.sessionId = sid;
        if (this._onSessionId) {
          this._onSessionId.resolve(sid);
          this._onSessionId = null;
        }
      }
      return;
    }
    const params = msg.params && typeof msg.params === "object" ? msg.params : {};
    const threadId = isNonEmptyString(params.threadId) ? params.threadId : null;
    const turnId = isNonEmptyString(params.turnId)
      ? params.turnId
      : isNonEmptyString(params.turn?.id)
        ? params.turn.id
        : null;
    const key = threadId && turnId ? `${threadId}:${turnId}` : null;

    if (msg.method === "item/agentMessage/delta") {
      if (!key) return;
      const delta = params.delta;
      if (!isNonEmptyString(delta)) return;
      const existing = this.turnsByKey.get(key) || { delta: "", fullText: null };
      existing.delta += delta;
      this.turnsByKey.set(key, existing);
      return;
    }

    if (msg.method === "item/completed") {
      if (!key) return;
      const item = params.item && typeof params.item === "object" ? params.item : null;
      if (!item || item.type !== "agentMessage") return;
      if (!isNonEmptyString(item.text)) return;
      const existing = this.turnsByKey.get(key) || { delta: "", fullText: null };
      existing.fullText = item.text;
      this.turnsByKey.set(key, existing);
      return;
    }

    if (msg.method === "turn/completed") {
      if (!key) return;
      const existing = this.turnsByKey.get(key) || { delta: "", fullText: null };
      this.turnsByKey.delete(key);
      const finalText = isNonEmptyString(existing.fullText) ? existing.fullText : existing.delta;
      const cb = this.onTurnCompleted;
      if (typeof cb === "function") {
        try {
          const res = cb({ threadId, turnId, text: finalText });
          if (res && typeof res.then === "function") res.catch(() => {});
        } catch {
          // ignore
        }
      }
    }
  }

  _handleWire(text) {
    const msg = parseJson(text);
    if (!msg) return;

    if (typeof msg.id === "number" && typeof msg.method === "string") {
      this._handleServerRequest(msg);
      return;
    }

    if (typeof msg.id === "number") {
      const p = this.pending.get(msg.id);
      if (!p) return;
      this.pending.delete(msg.id);
      if (msg.error) p.reject(new Error(msg.error.message || "RPC error"));
      else p.resolve(msg.result ?? null);
      return;
    }

    if (typeof msg.method === "string") {
      this._handleNotification(msg);
    }
  }
}

function chatKeyFromMessage(message) {
  const chatId = message?.chat?.id;
  if (!Number.isFinite(chatId)) return null;
  const topicId = message?.message_thread_id;
  if (Number.isFinite(topicId)) return `${chatId}:${topicId}`;
  return String(chatId);
}

function sendTargetFromMessage(message) {
  const chatId = message?.chat?.id;
  if (!Number.isFinite(chatId)) return null;
  const out = { chat_id: chatId };
  const topicId = message?.message_thread_id;
  // Telegram rejects sendMessage/sendMedia with message_thread_id=1 ("thread not found").
  // For typing indicators we still include it (see TypingController).
  const normalizedTopicId = Number.isFinite(topicId) ? Math.trunc(topicId) : null;
  if (Number.isFinite(normalizedTopicId) && normalizedTopicId !== 1) out.message_thread_id = normalizedTopicId;
  return out;
}

function typingTargetFromMessage(message) {
  const chatId = message?.chat?.id;
  if (!Number.isFinite(chatId)) return null;
  const out = { chat_id: chatId };
  const topicId = message?.message_thread_id;
  if (Number.isFinite(topicId)) out.message_thread_id = Math.trunc(topicId);
  return out;
}

function sendTargetFromChatKey(chatKey) {
  if (!isNonEmptyString(chatKey)) return null;
  const [chatIdRaw, topicRaw] = chatKey.split(":", 2);
  const chatId = Number(chatIdRaw);
  if (!Number.isFinite(chatId)) return null;
  const out = { chat_id: chatId };
  const topicId = Number(topicRaw);
  const normalizedTopicId = Number.isFinite(topicId) ? Math.trunc(topicId) : null;
  if (Number.isFinite(normalizedTopicId) && normalizedTopicId !== 1) out.message_thread_id = normalizedTopicId;
  return out;
}

function isServiceMessage(message) {
  if (!message || typeof message !== "object") return true;
  const serviceKeys = [
    "new_chat_members",
    "left_chat_member",
    "new_chat_title",
    "new_chat_photo",
    "delete_chat_photo",
    "group_chat_created",
    "supergroup_chat_created",
    "channel_chat_created",
    "message_auto_delete_timer_changed",
    "migrate_to_chat_id",
    "migrate_from_chat_id",
    "pinned_message",
    "invoice",
    "successful_payment"
  ];
  return serviceKeys.some((k) => k in message);
}

function parseCommand(text, botUsername) {
  if (!isNonEmptyString(text)) return null;
  const trimmed = text.trim();
  if (!trimmed.startsWith("/")) return null;
  const first = trimmed.split(/\s+/, 1)[0];
  const withoutSlash = first.slice(1);
  const [cmdRaw, at] = withoutSlash.split("@", 2);
  const cmd = (cmdRaw || "").toLowerCase();
  if (!cmd) return null;
  if (at && botUsername && at.toLowerCase() !== botUsername.toLowerCase()) return null;
  if (cmd === "new" || cmd === "where") return cmd;
  return null;
}

function truncateTelegramMessage(text) {
  const max = 4000;
  if (text.length <= max) return text;
  const suffix = "\n…(truncated)";
  return text.slice(0, Math.max(0, max - suffix.length)) + suffix;
}

const TELEGRAM_HTML_PARSE_ERR_RE = /can't parse entities|parse entities|find end of the entity/i;
const TELEGRAM_MESSAGE_TOO_LONG_RE = /message is too long/i;

function escapeHtml(text) {
  return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function escapeHtmlAttr(text) {
  return escapeHtml(text).replace(/"/g, "&quot;");
}

function markdownToTelegramHtml(markdown) {
  const input = String(markdown ?? "");
  if (!input.trim()) return "";

  const normalized = input.replace(/\r\n?/g, "\n");

  function renderCodeBlock(raw) {
    const safe = escapeHtml(String(raw ?? "").replace(/\n+$/, ""));
    const withNl = safe.endsWith("\n") ? safe : `${safe}\n`;
    return `<pre><code>${withNl}</code></pre>`;
  }

  function renderInlineSpans(raw) {
    if (!isNonEmptyString(raw)) return "";
    let text = String(raw);

    const placeholders = new Map();
    let idx = 0;
    const store = (html) => {
      const key = `\u0000${idx++}\u0000`;
      placeholders.set(key, html);
      return key;
    };

    // Inline code: `code`
    text = text.replace(/`([^`\n]+)`/g, (_m, code) => store(`<code>${escapeHtml(code)}</code>`));

    // Links: [label](url)
    text = text.replace(/\[([^\]\n]+)\]\(([^)\n]+)\)/g, (_m, label, href) => {
      const rawHref = stripOuterQuotes(href);
      const safeLabel = escapeHtml(label);
      if (!isNonEmptyString(rawHref)) return safeLabel;
      const safeHref = escapeHtmlAttr(rawHref);
      return store(`<a href="${safeHref}">${safeLabel}</a>`);
    });

    // Escape the rest (so user-provided <tag> won't break Telegram HTML).
    text = escapeHtml(text);

    // Basic emphasis (best-effort).
    text = text
      .replace(/\*\*([^*\n]+?)\*\*/g, "<b>$1</b>")
      .replace(/__([^_\n]+?)__/g, "<b>$1</b>")
      .replace(/~~([^~\n]+?)~~/g, "<s>$1</s>")
      .replace(/(^|[^\w*])\*([^*\n]+?)\*(?!\*)/g, "$1<i>$2</i>")
      .replace(/(^|[^\w_])_([^_\n]+?)_(?!_)/g, "$1<i>$2</i>");

    // Restore placeholders.
    for (const [key, html] of placeholders.entries()) {
      text = text.split(key).join(html);
    }
    return text;
  }

  function renderInlineBlock(block) {
    const lines = String(block ?? "").split("\n");
    const out = [];
    for (const lineRaw of lines) {
      const line = lineRaw ?? "";
      if (!line.trim()) {
        out.push("");
        continue;
      }

      // Headings (# ...)
      const headingMatch = /^\s{0,3}#{1,6}\s+(.+?)\s*$/.exec(line);
      if (headingMatch) {
        out.push(`<b>${renderInlineSpans(headingMatch[1])}</b>`);
        continue;
      }

      // Blockquote (> ...)
      const quoteMatch = /^\s{0,3}>\s?(.*)$/.exec(line);
      if (quoteMatch) {
        out.push(`│ ${renderInlineSpans(quoteMatch[1])}`);
        continue;
      }

      // Unordered list (-/*/+ ...)
      const bulletMatch = /^\s{0,3}[-*+]\s+(.+?)\s*$/.exec(line);
      if (bulletMatch) {
        out.push(`• ${renderInlineSpans(bulletMatch[1])}`);
        continue;
      }

      out.push(renderInlineSpans(line));
    }
    return out.join("\n");
  }

  // Handle fenced code blocks (```).
  let out = "";
  let i = 0;
  while (i < normalized.length) {
    const start = normalized.indexOf("```", i);
    if (start === -1) {
      out += renderInlineBlock(normalized.slice(i));
      break;
    }

    out += renderInlineBlock(normalized.slice(i, start));

    const end = normalized.indexOf("```", start + 3);
    if (end === -1) {
      // Unclosed fence: treat the rest as normal text.
      out += renderInlineBlock(normalized.slice(start));
      break;
    }

    let codeStart = start + 3;
    if (normalized[codeStart] === "\n") {
      codeStart += 1;
    } else {
      const nl = normalized.indexOf("\n", codeStart);
      if (nl !== -1 && nl < end) codeStart = nl + 1;
    }
    const code = normalized.slice(codeStart, end);

    if (out && !out.endsWith("\n")) out += "\n";
    out += renderCodeBlock(code);

    i = end + 3;
    if (i < normalized.length && normalized[i] === "\n") {
      out += "\n";
      i += 1;
    }
  }

  return String(out ?? "").trim();
}

class TypingController {
  constructor(tg, { intervalSeconds = 6, ttlMs = 2 * 60_000 } = {}) {
    this.tg = tg;
    this.intervalMs = Math.max(1000, Math.floor(Number(intervalSeconds) * 1000));
    this.ttlMs = Math.max(10_000, Math.floor(Number(ttlMs)));
    this.activeByChatKey = new Map();
  }

  _normalizeTarget(target) {
    if (!target || typeof target !== "object") return null;
    const chatId = target.chat_id;
    if (!Number.isFinite(chatId)) return null;
    const out = { chat_id: chatId };
    const topicId = target.message_thread_id;
    // NOTE: Telegram typing indicators accept message_thread_id=1 (General topic).
    if (Number.isFinite(topicId)) out.message_thread_id = Math.trunc(topicId);
    return out;
  }

  async _sendTyping(target) {
    try {
      await this.tg.sendChatAction({ ...target, action: "typing" });
    } catch {
      // ignore typing failures
    }
  }

  start(chatKey, target) {
    if (!isNonEmptyString(chatKey)) return;
    const normalized = this._normalizeTarget(target);
    if (!normalized) return;

    const expiresAtMs = Date.now() + this.ttlMs;
    const existing = this.activeByChatKey.get(chatKey);
    if (existing) {
      existing.expiresAtMs = expiresAtMs;
      existing.target = normalized;
      void this._sendTyping(normalized);
      return;
    }

    const entry = {
      expiresAtMs,
      target: normalized,
      timer: null
    };
    this.activeByChatKey.set(chatKey, entry);

    entry.timer = setInterval(() => {
      const current = this.activeByChatKey.get(chatKey);
      if (!current) return;
      if (Date.now() >= current.expiresAtMs) {
        this.stop(chatKey);
        return;
      }
      void this._sendTyping(current.target);
    }, this.intervalMs);

    void this._sendTyping(normalized);
  }

  stop(chatKey) {
    const entry = this.activeByChatKey.get(chatKey);
    if (!entry) return;
    if (entry.timer) clearInterval(entry.timer);
    this.activeByChatKey.delete(chatKey);
  }

  stopAll() {
    for (const chatKey of this.activeByChatKey.keys()) {
      this.stop(chatKey);
    }
  }
}

async function main() {
  const telegramToken = process.env.TELEGRAM_BOT_TOKEN;
  if (!isNonEmptyString(telegramToken)) {
    // eslint-disable-next-line no-console
    console.error("Missing TELEGRAM_BOT_TOKEN");
    process.exit(1);
  }

  const gatewayWsUrlRaw =
    stripSessionFromWsUrl(process.env.ARGUS_GATEWAY_WS_URL) ||
    stripSessionFromWsUrl(process.env.NEXT_PUBLIC_ARGUS_WS_URL);
  const gatewayHttpUrlRaw = stripOuterQuotes(process.env.ARGUS_GATEWAY_HTTP_URL);

  const runningInDocker = await pathExists("/.dockerenv");
  const fromHost = deriveGatewayFromHostEnv(process.env.HOST, { runningInDocker });
  const gatewayWsUrl = gatewayWsUrlRaw || fromHost.wsBase;
  const gatewayHttpUrl = gatewayHttpUrlRaw || deriveHttpBaseFromWsUrl(gatewayWsUrl) || fromHost.httpBase;

  const argusToken = isNonEmptyString(process.env.ARGUS_TOKEN) ? process.env.ARGUS_TOKEN : null;
  const cwd = process.env.ARGUS_CWD || "/root/.argus/workspace";
  const statePathEnv = stripOuterQuotes(process.env.STATE_PATH);
  let statePath;
  if (isNonEmptyString(statePathEnv)) {
    statePath = statePathEnv;
  } else if (await dirExists("/data")) {
    statePath = "/data/state.json";
  } else {
    statePath = path.resolve(process.cwd(), "state.json");
  }

  const tg = new TelegramApi(telegramToken);
  const typing = new TypingController(tg);
  const me = await tg.getMe();
  const botUsername = isNonEmptyString(me?.username) ? me.username : null;
  log("Telegram bot:", botUsername ? `@${botUsername}` : "(unknown)");

  const state = new StateStore(statePath);
  await state.load();
  log("State path:", statePath);

  const argus = new ArgusClient({ gatewayHttpUrl, gatewayWsUrl, token: argusToken, cwd });
  let deliverEnabled = false;

  argus.onTurnCompleted = async ({ threadId, text }) => {
    if (!deliverEnabled) return;
    if (!isNonEmptyString(threadId)) return;
    const chatKey = state.getLastActiveChatKey(threadId);
    if (!isNonEmptyString(chatKey)) return;
    try {
      const target = sendTargetFromChatKey(chatKey);
      if (!target) return;
      const finalText = isNonEmptyString(text) ? text : "(no output)";
      const truncated = truncateTelegramMessage(finalText);
      const html = markdownToTelegramHtml(truncated);
      try {
        await tg.sendMessage({ ...target, text: html || "(no output)", parse_mode: "HTML" });
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        if (TELEGRAM_HTML_PARSE_ERR_RE.test(msg) || TELEGRAM_MESSAGE_TOO_LONG_RE.test(msg)) {
          await tg.sendMessage({ ...target, text: truncated });
          return;
        }
        throw e;
      }
    } catch (e) {
      log("Failed to deliver turn/completed:", e instanceof Error ? e.message : String(e));
    } finally {
      typing.stop(chatKey);
    }
  };
  argus.onDisconnected = () => {
    deliverEnabled = false;
    typing.stopAll();
  };

  const queue = new SerialQueue();

  async function ensureDefaultSession() {
    const preferred = state.state.defaultSessionId;
    if (isNonEmptyString(preferred)) {
      try {
        await argus.connectToSession(preferred);
        argus.sessionId = preferred;
        deliverEnabled = true;
        return preferred;
      } catch (e) {
        log("Failed to attach to defaultSessionId, will reselect:", e instanceof Error ? e.message : String(e));
      }
    }

    let sessions = [];
    try {
      sessions = await argus.listSessions();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      const cause = e instanceof Error && e.cause instanceof Error ? e.cause.message : null;
      log("Failed to list sessions, will try creating a new one:", cause ? `${msg} (${cause})` : msg);
    }

    if (sessions.length > 0 && isNonEmptyString(sessions[0]?.sessionId)) {
      const sid = sessions[0].sessionId;
      await argus.connectToSession(sid);
      argus.sessionId = sid;
      await state.setDefaultSessionId(sid);
      deliverEnabled = true;
      return sid;
    }

    const sid = await argus.connectNewSession();
    await state.setDefaultSessionId(sid);
    deliverEnabled = true;
    return sid;
  }

  await queue.enqueue(async () => {
    try {
      const sid = await ensureDefaultSession();
      log("Using defaultSessionId:", sid);
    } catch (e) {
      log("Gateway not ready yet; will retry on demand:", e instanceof Error ? e.message : String(e));
    }
  });

  let offset;
  if (Number.isFinite(state.state.lastUpdateId)) {
    offset = state.state.lastUpdateId + 1;
  } else {
    try {
      const backlog = await tg.getUpdates({ offset: 0, timeout: 0, allowed_updates: ["message"] });
      if (Array.isArray(backlog) && backlog.length > 0) {
        const last = backlog[backlog.length - 1]?.update_id;
        if (Number.isFinite(last)) {
          offset = last + 1;
          void state.setLastUpdateId(last);
          log(`Skipped ${backlog.length} backlog updates (starting fresh).`);
        } else {
          offset = 0;
        }
      } else {
        offset = 0;
      }
    } catch (e) {
      offset = 0;
      log("Failed to flush backlog updates:", e instanceof Error ? e.message : String(e));
    }
  }

  log("Polling Telegram updates…");
  // eslint-disable-next-line no-constant-condition
  while (true) {
    let updates = [];
    try {
      updates = await tg.getUpdates({ offset, timeout: 50, allowed_updates: ["message"] });
    } catch (e) {
      log("getUpdates failed:", e instanceof Error ? e.message : String(e));
      await sleep(2000);
      continue;
    }

    if (!Array.isArray(updates) || updates.length === 0) continue;

    for (const upd of updates) {
      const updateId = upd?.update_id;
      if (Number.isFinite(updateId)) {
        offset = updateId + 1;
        void state.setLastUpdateId(updateId);
      }

      const message = upd?.message;
      if (!message || typeof message !== "object") continue;
      if (isServiceMessage(message)) continue;
      if (message.from?.is_bot) continue;

      const target = sendTargetFromMessage(message);
      const typingTarget = typingTargetFromMessage(message);
      const chatKey = chatKeyFromMessage(message);
      if (!target || !chatKey) continue;

      const text = isNonEmptyString(message.text) ? message.text : null;
      const cmd = parseCommand(text || "", botUsername);

      // Show typing cue immediately; Telegram requires periodic refresh for long runs.
      // Note: sendChatAction accepts message_thread_id=1 (General topic), while sendMessage may not.
      typing.start(chatKey, typingTarget);

      queue.enqueue(async () => {
        try {
          await ensureDefaultSession();

          if (cmd === "where") {
            const sid = state.state.defaultSessionId;
            const tid = state.getThreadId(chatKey);
            await tg.sendMessage({
              ...target,
              text: `sessionId: ${sid || "(none)"}\nchatKey: ${chatKey}\nthreadId: ${tid || "(none)"}`
            });
            typing.stop(chatKey);
            return;
          }

          if (cmd === "new") {
            const tid = await argus.startThread();
            await state.setThreadId(chatKey, tid);
            await state.setLastActiveChatKey(tid, chatKey);
            await tg.sendMessage({ ...target, text: `ok (new thread): ${tid}` });
            typing.stop(chatKey);
            return;
          }

          if (!isNonEmptyString(text)) {
            await tg.sendMessage({ ...target, text: "暂不支持该消息类型（目前仅支持文本）。" });
            typing.stop(chatKey);
            return;
          }

          const chatType = message?.chat?.type;
          let threadId = state.getThreadId(chatKey);
          let enqueueTarget = null;

          if (isNonEmptyString(threadId)) {
            // Use mapped thread.
          } else if (chatType === "private") {
            enqueueTarget = "main";
          } else {
            threadId = await argus.startThread();
            await state.setThreadId(chatKey, threadId);
          }

          const res = await argus.enqueueInput({ text, threadId, target: enqueueTarget });
          const effectiveThreadId = isNonEmptyString(res?.threadId) ? res.threadId : threadId;
          if (isNonEmptyString(effectiveThreadId)) {
            await state.setLastActiveChatKey(effectiveThreadId, chatKey);
            if (!isNonEmptyString(threadId)) {
              await state.setThreadId(chatKey, effectiveThreadId);
            }
          }
        } catch (e) {
          const msg = e instanceof Error ? e.message : String(e);
          await tg.sendMessage({ ...target, text: `error: ${msg}` });
          typing.stop(chatKey);
        }
      });
    }
  }
}

await main();
