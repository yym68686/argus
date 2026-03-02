import process from "node:process";
import crypto from "node:crypto";
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

function nowMs() {
  return Date.now();
}

function randomToken(bytes = 9) {
  try {
    return crypto.randomBytes(bytes).toString("base64url");
  } catch {
    return Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
  }
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
      version: 3,
      lastUpdateId: null,
      threadsBySession: {}
    };
    this._writeChain = Promise.resolve();
  }

  async load() {
    try {
      const raw = await fs.readFile(this.path, "utf-8");
      const parsed = parseJson(raw);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return;

      const threadsBySession =
        parsed.threadsBySession && typeof parsed.threadsBySession === "object" && !Array.isArray(parsed.threadsBySession)
          ? parsed.threadsBySession
          : {};

      // Back-compat: v2 stored a flat threads map (assumed to belong to a single session).
      const legacyThreads =
        parsed.threads && typeof parsed.threads === "object" && !Array.isArray(parsed.threads) ? parsed.threads : null;
      if (legacyThreads && Object.keys(legacyThreads).length > 0) {
        const sid = "default";
        if (!threadsBySession[sid] || typeof threadsBySession[sid] !== "object" || Array.isArray(threadsBySession[sid])) {
          threadsBySession[sid] = legacyThreads;
        }
      }

      this.state = {
        version: 3,
        lastUpdateId: Number.isFinite(parsed.lastUpdateId) ? parsed.lastUpdateId : null,
        threadsBySession
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

  _threadsForSession(sessionId) {
    const sid = isNonEmptyString(sessionId) ? sessionId : "default";
    if (!this.state.threadsBySession || typeof this.state.threadsBySession !== "object" || Array.isArray(this.state.threadsBySession)) {
      this.state.threadsBySession = {};
    }
    const bucket = this.state.threadsBySession[sid];
    if (!bucket || typeof bucket !== "object" || Array.isArray(bucket)) {
      this.state.threadsBySession[sid] = {};
      return this.state.threadsBySession[sid];
    }
    return bucket;
  }

  getThreadId(sessionId, chatKey) {
    const bucket = this._threadsForSession(sessionId);
    const entry = bucket?.[chatKey];
    return entry && typeof entry === "object" && isNonEmptyString(entry.threadId) ? entry.threadId : null;
  }

  setThreadId(sessionId, chatKey, threadId) {
    const bucket = this._threadsForSession(sessionId);
    bucket[chatKey] = { threadId };
    return this.save();
  }

  setLastUpdateId(updateId) {
    this.state.lastUpdateId = updateId;
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

class CallbackStore {
  constructor({ ttlMs = 30 * 60_000, maxEntries = 5000 } = {}) {
    this.ttlMs = Math.max(10_000, Math.floor(Number(ttlMs)));
    this.maxEntries = Math.max(100, Math.floor(Number(maxEntries)));
    this.byToken = new Map(); // token -> { payload, expiresAtMs }
  }

  register(payload, { ttlMs } = {}) {
    const token = randomToken(9);
    const expiresAtMs = nowMs() + Math.max(10_000, Math.floor(Number(ttlMs ?? this.ttlMs)));
    this.byToken.set(token, { payload, expiresAtMs });
    this._prune();
    return token;
  }

  get(token) {
    const entry = this.byToken.get(token);
    if (!entry) return null;
    if (nowMs() >= entry.expiresAtMs) {
      this.byToken.delete(token);
      return null;
    }
    return entry.payload ?? null;
  }

  _prune() {
    const now = nowMs();
    for (const [token, entry] of this.byToken.entries()) {
      if (!entry || now >= entry.expiresAtMs) this.byToken.delete(token);
    }
    if (this.byToken.size <= this.maxEntries) return;
    const entries = Array.from(this.byToken.entries());
    entries.sort((a, b) => (a[1]?.expiresAtMs || 0) - (b[1]?.expiresAtMs || 0));
    const surplus = this.byToken.size - this.maxEntries;
    for (const [token] of entries.slice(0, Math.max(0, surplus))) {
      this.byToken.delete(token);
    }
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

  async editMessageText(params) {
    return await this.call("editMessageText", params);
  }

  async editMessageReplyMarkup(params) {
    return await this.call("editMessageReplyMarkup", params);
  }

  async deleteMessage(params) {
    return await this.call("deleteMessage", params);
  }

  async answerCallbackQuery(params) {
    return await this.call("answerCallbackQuery", params);
  }

  async getChatMember(params) {
    return await this.call("getChatMember", params);
  }

  async setMyCommands(commands, params) {
    return await this.call("setMyCommands", { commands, ...(params || {}) });
  }

  async deleteMyCommands(params) {
    return await this.call("deleteMyCommands", params || {});
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

  async getAutomationState() {
    return await this._httpJson("GET", "/automation/state");
  }

  async automationUserBootstrap(chatKey) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    return await this._httpJson("POST", "/automation/user/bootstrap", { chatKey });
  }

  async automationAgentList(chatKey) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    return await this._httpJson("POST", "/automation/agent/list", { chatKey });
  }

  async automationAgentResolve(chatKey) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    return await this._httpJson("POST", "/automation/agent/resolve", { chatKey });
  }

  async automationAgentCreate(chatKey, agentId) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    if (!isNonEmptyString(agentId)) throw new Error("Missing agentId");
    return await this._httpJson("POST", "/automation/agent/create", { chatKey, agentId });
  }

  async automationAgentUse(chatKey, agentId) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    if (!isNonEmptyString(agentId)) throw new Error("Missing agentId");
    return await this._httpJson("POST", "/automation/agent/use", { chatKey, agentId });
  }

  async automationAgentRename(chatKey, agentId, newName) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    if (!isNonEmptyString(agentId)) throw new Error("Missing agentId");
    if (!isNonEmptyString(newName)) throw new Error("Missing newName");
    return await this._httpJson("POST", "/automation/agent/rename", { chatKey, agentId, newName });
  }

  async automationChatBind(chatKey, agentId, actorUserId) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    if (!isNonEmptyString(agentId)) throw new Error("Missing agentId");
    if (!Number.isFinite(actorUserId) || actorUserId <= 0) throw new Error("Missing actorUserId");
    return await this._httpJson("POST", "/automation/chat/bind", { chatKey, agentId, actorUserId });
  }

  async automationNodeToken(chatKey) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    return await this._httpJson("POST", "/automation/node/token", { chatKey });
  }

  async ensureMainThread() {
    await this.initialize();
    const result = await this.rpc("argus/thread/main/ensure", {});
    const tid = result?.threadId;
    if (!isNonEmptyString(tid)) throw new Error("Invalid argus/thread/main/ensure response");
    return tid;
  }

  async setMainThread(threadId) {
    if (!isNonEmptyString(threadId)) throw new Error("Missing threadId");
    await this.initialize();
    const result = await this.rpc("argus/thread/main/set", { threadId });
    const tid = result?.threadId;
    if (!isNonEmptyString(tid)) throw new Error("Invalid argus/thread/main/set response");
    return tid;
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

  async enqueueInput({ text, threadId, target, source, telegramImages }) {
    const images = Array.isArray(telegramImages) ? telegramImages : [];
    const hasText = isNonEmptyString(text);
    if (!hasText && images.length === 0) throw new Error("Missing text");
    const params = { text: hasText ? text : "" };
    if (images.length > 0) params.telegramImages = images;
    if (isNonEmptyString(threadId)) params.threadId = threadId;
    if (isNonEmptyString(target)) params.target = target;
    if (source && typeof source === "object" && !Array.isArray(source)) {
      const channel = source.channel;
      const chatKey = source.chatKey;
      if (isNonEmptyString(channel) && isNonEmptyString(chatKey)) {
        params.source = { channel: String(channel), chatKey: String(chatKey) };
      }
    }
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
          const res = cb({ sessionId: this.sessionId, threadId, turnId, text: finalText });
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

const KNOWN_COMMANDS = new Set(["start", "menu", "help"]);
const LEGACY_COMMANDS = new Set(["new", "newmain", "where", "agents", "newagent", "useagent"]);

function parseSlashCommand(text, botUsername) {
  if (!isNonEmptyString(text)) return null;
  const trimmed = text.trim();
  if (!trimmed.startsWith("/")) return null;
  const first = trimmed.split(/\s+/, 1)[0];
  const withoutSlash = first.slice(1);
  const [cmdRaw, at] = withoutSlash.split("@", 2);
  const rawCmd = (cmdRaw || "").toLowerCase();
  if (!rawCmd) return null;
  const args = trimmed.slice(first.length).trim();
  if (at && botUsername && at.toLowerCase() !== botUsername.toLowerCase()) {
    return { cmd: null, rawCmd, args, known: false, forOtherBot: true };
  }
  const known = KNOWN_COMMANDS.has(rawCmd);
  return { cmd: known ? rawCmd : null, rawCmd, args, known, forOtherBot: false };
}

function extractReplyText(message) {
  const reply = message?.reply_to_message;
  if (!reply || typeof reply !== "object") return null;
  if (isNonEmptyString(reply.text)) return reply.text;
  if (isNonEmptyString(reply.caption)) return reply.caption;
  return null;
}

function extractTelegramImagesFromMessage(message) {
  const out = [];
  if (!message || typeof message !== "object") return out;

  const photos = Array.isArray(message.photo) ? message.photo : null;
  if (photos && photos.length > 0) {
    const best = photos[photos.length - 1];
    if (best && typeof best === "object" && isNonEmptyString(best.file_id)) {
      out.push({
        kind: "photo",
        fileId: best.file_id,
        fileUniqueId: isNonEmptyString(best.file_unique_id) ? best.file_unique_id : null,
        fileName: null,
        mimeType: null,
        fileSize: typeof best.file_size === "number" ? best.file_size : null,
      });
    }
  }

  const doc = message.document;
  if (doc && typeof doc === "object" && isNonEmptyString(doc.file_id)) {
    const mimeType = isNonEmptyString(doc.mime_type) ? doc.mime_type : null;
    if (mimeType && mimeType.startsWith("image/")) {
      out.push({
        kind: "document",
        fileId: doc.file_id,
        fileUniqueId: isNonEmptyString(doc.file_unique_id) ? doc.file_unique_id : null,
        fileName: isNonEmptyString(doc.file_name) ? doc.file_name : null,
        mimeType,
        fileSize: typeof doc.file_size === "number" ? doc.file_size : null,
      });
    }
  }

  return out;
}

function truncateTelegramMessage(text) {
  const max = 4000;
  if (text.length <= max) return text;
  const suffix = "\n…(truncated)";
  return text.slice(0, Math.max(0, max - suffix.length)) + suffix;
}

function detectLocaleFromLanguageCode(languageCode) {
  if (!isNonEmptyString(languageCode)) return null;
  const raw = String(languageCode).trim().toLowerCase();
  if (!raw) return null;
  if (raw.startsWith("zh")) return "zh";
  return "en";
}

const UI_STRINGS = {
  en: {
    menu_title: "Argus Menu",
    title_switch_agent: "Switch Agent",
    title_create_agent: "Create Agent",
    title_rename_agent: "Rename Agent",
    title_status: "Status",
    title_node_token: "Node Token",
    title_help: "Help",

    btn_switch_agent: "Switch Agent",
    btn_create_agent: "Create Agent",
    btn_rename_agent: "Rename Agent",
    btn_new_main_thread: "New Main Thread",
    btn_node_token: "Node Token",
    btn_status: "Status",
    btn_help: "Help",
    btn_close: "Close",
    btn_refresh: "Refresh",
    btn_back: "Back",
    btn_prev: "Prev",
    btn_next: "Next",
    btn_cancel: "Cancel",
    btn_reveal: "Reveal",
    btn_bind_this_chat: "Bind This Chat",
    btn_new_thread: "New Thread",

    shared_suffix: "(shared)",
    status_unbound: "UNBOUND",

    msg_not_initialized: "Not initialized. Send /start in this DM first.",
    msg_use_start_in_dm: "Please use /start in a private chat.",
    msg_unsupported_message: "Unsupported message type (text/images only for now).",
    msg_admins_only: "Admins only",
    msg_expired: "Expired. Send /menu.",
    msg_unsupported_cb: "Unsupported",
    msg_invalid_agent: "Invalid agent.",

    err_not_initialized: "Please run /start first.",
    err_forbidden: "error: forbidden.",
    err_agent_exists: "error: already exists: {name}",

    notice_initialized_created: "Initialized: main created.",
    notice_initialized_exists: "Initialized: main exists.",
    notice_canceled: "Canceled.",
    notice_unbound_hint: "UNBOUND. Use Bind This Chat in /menu.",
    notice_legacy_moved: "/{cmd} has moved to /menu.",
    notice_unknown_command: "Unknown command: /{cmd}. Use /menu.",
    notice_switched: "Switched: {agentId}",
    notice_created: "Created: {name}",
    notice_renamed: "Renamed: {old} -> {name}",
    notice_new_main_thread: "New main thread: {threadId}",
    notice_new_thread: "New thread: {threadId}",
    notice_bound: "Bound: {agentId}",

    label_current: "current",
    label_page: "page",
    label_status: "status",
    label_error: "error",

    create_prompt_prefix: "Send the new agent name (a-z0-9_-), e.g.",
    create_missing: "Missing agent name.",

    rename_prompt_prefix: "Send the new agent name (a-z0-9_-), e.g.",
    rename_missing: "Missing agent name.",

    node_warning: "WARNING: token is sensitive; rotate if leaked.",
    node_press_reveal: "Press Reveal to fetch the current session token.",

    group_bind_first: "Bind this chat/topic to an agent first.",

    bind_select_agent: "Select an agent to route this chat/topic to.",
    bind_hint_start: "Hint: DM the bot and run /start first.",

    help_line_menu: "Use {menu} to open the control panel.",
    help_line_start: "First time: run {start} in a DM to initialize your main agent.",
    help_line_private_more: "Switch Agent / Create Agent are available in the menu.",
    help_line_group_bind: "In groups/topics, bind the chat first ({bind}).",
    help_line_group_new: "Then use {newThread} to reset the conversation for this chat/topic."
  },
  zh: {
    menu_title: "Argus 菜单",
    title_switch_agent: "切换 Agent",
    title_create_agent: "创建 Agent",
    title_rename_agent: "重命名 Agent",
    title_status: "状态",
    title_node_token: "节点 Token",
    title_help: "帮助",

    btn_switch_agent: "切换 Agent",
    btn_create_agent: "创建 Agent",
    btn_rename_agent: "重命名 Agent",
    btn_new_main_thread: "新建 Main Thread",
    btn_node_token: "节点 Token",
    btn_status: "状态",
    btn_help: "帮助",
    btn_close: "关闭",
    btn_refresh: "刷新",
    btn_back: "返回",
    btn_prev: "上一页",
    btn_next: "下一页",
    btn_cancel: "取消",
    btn_reveal: "显示",
    btn_bind_this_chat: "绑定当前群聊",
    btn_new_thread: "新建 Thread",

    shared_suffix: "（共享）",
    status_unbound: "未绑定",

    msg_not_initialized: "未初始化：请先在私聊发送 /start。",
    msg_use_start_in_dm: "请在私聊中使用 /start。",
    msg_unsupported_message: "暂不支持该消息类型（目前仅支持文本/图片）。",
    msg_admins_only: "仅群管理员可操作",
    msg_expired: "已过期，请发送 /menu。",
    msg_unsupported_cb: "不支持",
    msg_invalid_agent: "无效的 agent。",

    err_not_initialized: "请先 /start 初始化。",
    err_forbidden: "error: 没有权限。",
    err_agent_exists: "error: 已存在：{name}",

    notice_initialized_created: "已初始化：已创建 main。",
    notice_initialized_exists: "已初始化：main 已存在。",
    notice_canceled: "已取消。",
    notice_unbound_hint: "未绑定：请用 /menu 里的“绑定当前群聊”。",
    notice_legacy_moved: "/{cmd} 已迁移到 /menu。",
    notice_unknown_command: "未知命令：/{cmd}。请用 /menu。",
    notice_switched: "已切换：{agentId}",
    notice_created: "已创建：{name}",
    notice_renamed: "已重命名：{old} -> {name}",
    notice_new_main_thread: "已新建 Main Thread：{threadId}",
    notice_new_thread: "已新建 Thread：{threadId}",
    notice_bound: "已绑定：{agentId}",

    label_current: "当前",
    label_page: "页",
    label_status: "状态",
    label_error: "错误",

    create_prompt_prefix: "发送新 agent 名称（a-z0-9_-），例如",
    create_missing: "缺少 agent 名称。",

    rename_prompt_prefix: "发送新 agent 名称（a-z0-9_-），例如",
    rename_missing: "缺少 agent 名称。",

    node_warning: "注意：token 很敏感；泄露请立刻轮换。",
    node_press_reveal: "点击“显示”获取当前 session 的 token。",

    group_bind_first: "请先把当前群聊/话题绑定到某个 agent。",

    bind_select_agent: "选择一个 agent，把当前 chat/topic 路由到它。",
    bind_hint_start: "提示：先私聊 bot 并发送 /start 完成初始化。",

    help_line_menu: "用 {menu} 打开控制面板。",
    help_line_start: "首次使用：先在私聊发送 {start} 完成初始化。",
    help_line_private_more: "切换/创建 agent 都在菜单里操作。",
    help_line_group_bind: "在群聊/话题里，先绑定（{bind}）。",
    help_line_group_new: "之后用 {newThread} 重新开一个 thread。"
  }
};

function uiStrings(locale) {
  const key = locale === "zh" ? "zh" : "en";
  return UI_STRINGS[key] || UI_STRINGS.en;
}

function formatTemplate(template, vars) {
  const src = String(template ?? "");
  if (!vars || typeof vars !== "object") return src;
  return src.replace(/\{([a-zA-Z0-9_]+)\}/g, (_m, k) => (k in vars ? String(vars[k]) : ""));
}

function formatGatewayErrorForUser(err, { locale } = {}) {
  const S = uiStrings(locale);
  const msg = err instanceof Error ? err.message : String(err);
  if (msg.includes("/start") || /not initialized/i.test(msg)) return S.err_not_initialized;
  if (/forbidden/i.test(msg)) return S.err_forbidden;
  const already = /agent already exists:\s*([a-z0-9_-]+)/i.exec(msg);
  if (already) return formatTemplate(S.err_agent_exists, { name: already[1] });
  return `error: ${msg}`;
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

  const defaultCommands = [
    { command: "menu", description: "Open menu" },
    { command: "help", description: "Help" }
  ];
  const privateCommands = [
    { command: "start", description: "Initialize (private chat)" },
    { command: "menu", description: "Open menu" },
    { command: "help", description: "Help" }
  ];
  const defaultCommandsZh = [
    { command: "menu", description: "打开菜单" },
    { command: "help", description: "帮助" }
  ];
  const privateCommandsZh = [
    { command: "start", description: "初始化（私聊）" },
    { command: "menu", description: "打开菜单" },
    { command: "help", description: "帮助" }
  ];
  try {
    try {
      await tg.deleteMyCommands({ scope: { type: "default" } });
      await tg.deleteMyCommands({ scope: { type: "all_private_chats" } });
      await tg.deleteMyCommands({ scope: { type: "default" }, language_code: "zh" });
      await tg.deleteMyCommands({ scope: { type: "all_private_chats" }, language_code: "zh" });
    } catch {
      // ignore; older bots may not support deleteMyCommands
    }
    await tg.setMyCommands(defaultCommands, { scope: { type: "default" } });
    await tg.setMyCommands(privateCommands, { scope: { type: "all_private_chats" } });
    try {
      await tg.setMyCommands(defaultCommandsZh, { scope: { type: "default" }, language_code: "zh" });
      await tg.setMyCommands(privateCommandsZh, { scope: { type: "all_private_chats" }, language_code: "zh" });
    } catch (e) {
      log("setMyCommands zh failed (continuing):", e instanceof Error ? e.message : String(e));
    }
    log(
      "Telegram command menu installed:",
      `default=[${defaultCommands.map((c) => `/${c.command}`).join(", ")}] private=[${privateCommands.map((c) => `/${c.command}`).join(", ")}]`
    );
  } catch (e) {
    log("setMyCommands failed (continuing):", e instanceof Error ? e.message : String(e));
  }

  const state = new StateStore(statePath);
  await state.load();
  log("State path:", statePath);

  // HTTP-only helper (does not need a WS connection).
  const argusHttp = new ArgusClient({ gatewayHttpUrl, gatewayWsUrl, token: argusToken, cwd });

  // Keep one WS per gateway sessionId to avoid reconnect churn when routing between agents.
  const clients = new Map(); // sessionId -> ArgusClient
  const lastActiveBySessionThread = new Map(); // `${sessionId}:${threadId}` -> chatKey
  const sessionThreadKey = (sessionId, threadId) => `${sessionId}:${threadId}`;

  const attachClientHooks = (client) => {
    client.onTurnCompleted = async ({ sessionId, threadId }) => {
      if (!isNonEmptyString(sessionId) || !isNonEmptyString(threadId)) return;
      const chatKey = lastActiveBySessionThread.get(sessionThreadKey(sessionId, threadId));
      if (!isNonEmptyString(chatKey)) return;
      typing.stop(chatKey);
    };
    client.onDisconnected = () => {
      const sid = client.sessionId;
      if (!isNonEmptyString(sid)) {
        typing.stopAll();
        return;
      }
      for (const [key, chatKey] of lastActiveBySessionThread.entries()) {
        if (key.startsWith(`${sid}:`) && isNonEmptyString(chatKey)) {
          typing.stop(chatKey);
        }
      }
    };
  };

  async function getClient(sessionId) {
    if (!isNonEmptyString(sessionId)) throw new Error("Missing sessionId");
    const sid = sessionId.trim();
    let client = clients.get(sid);
    if (!client) {
      client = new ArgusClient({ gatewayHttpUrl, gatewayWsUrl, token: argusToken, cwd });
      attachClientHooks(client);
      clients.set(sid, client);
    }
    await client.connectToSession(sid);
    return client;
  }

  const queue = new SerialQueue();

  function chatIdFromChatKey(chatKey) {
    if (!isNonEmptyString(chatKey)) return null;
    const chatIdRaw = chatKey.split(":", 2)[0];
    const chatId = Number(chatIdRaw);
    return Number.isFinite(chatId) ? chatId : null;
  }

  async function resolveRouteForChatKey(chatKey) {
    const res = await argusHttp.automationAgentResolve(chatKey);
    const sessionId = isNonEmptyString(res?.sessionId) ? res.sessionId : null;
    const agentId = isNonEmptyString(res?.agentId) ? res.agentId : "main";
    if (!sessionId) throw new Error("Missing sessionId");
    return { agentId, sessionId };
  }

  const callbacks = new CallbackStore();
  const pendingAgentInputByChatKey = new Map(); // chatKey -> { kind, expiresAtMs, panelChatId, panelMessageId, agentId? }
  const unboundWarnedAtByChatKey = new Map(); // chatKey -> ms
  const localeByChatKey = new Map(); // chatKey -> "zh" | "en"

  const MENU_AGENT_PAGE_SIZE = 8;
  const MENU_AUTO_DELETE_MS = 30 * 60_000;
  const UNBOUND_WARN_COOLDOWN_MS = 5 * 60_000;

  function localeForChatKey(chatKey, languageCode) {
    const detected = detectLocaleFromLanguageCode(languageCode);
    if (detected && isNonEmptyString(chatKey)) {
      localeByChatKey.set(chatKey, detected);
      return detected;
    }
    const existing = localeByChatKey.get(chatKey);
    return existing || detected || "en";
  }

  function callbackData(payload) {
    const token = callbacks.register(payload);
    return `cb:${token}`;
  }

  function cbButton(text, payload) {
    return { text, callback_data: callbackData(payload) };
  }

  function kb(rows) {
    return { inline_keyboard: rows };
  }

  function htmlCode(value) {
    return `<code>${escapeHtml(String(value ?? ""))}</code>`;
  }

  function isPrivateChatType(chatType) {
    return chatType === "private";
  }

  function isGroupChatType(chatType) {
    return chatType === "group" || chatType === "supergroup";
  }

  async function isChatAdmin(chatId, userId) {
    if (!Number.isFinite(chatId) || !Number.isFinite(userId)) return false;
    try {
      const res = await tg.getChatMember({ chat_id: chatId, user_id: userId });
      const status = typeof res?.status === "string" ? res.status : "";
      return status === "creator" || status === "administrator";
    } catch {
      return false;
    }
  }

  function deriveNodeWsUrl({ httpBase, pathName, token }) {
    try {
      const u = new URL(httpBase);
      u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
      u.pathname = String(pathName || "/nodes/ws");
      u.search = "";
      if (isNonEmptyString(token)) u.searchParams.set("token", token);
      return u.toString();
    } catch {
      return null;
    }
  }

  async function safeSendMessage(params) {
    try {
      return await tg.sendMessage(params);
    } catch (e) {
      log("sendMessage failed:", e instanceof Error ? e.message : String(e));
      return null;
    }
  }

  async function safeEditMessageText(params) {
    try {
      return await tg.editMessageText(params);
    } catch (e) {
      log("editMessageText failed:", e instanceof Error ? e.message : String(e));
      return null;
    }
  }

  async function safeDeleteMessage(params) {
    try {
      return await tg.deleteMessage(params);
    } catch {
      return null;
    }
  }

  function scheduleMenuAutoDelete({ chatId, menuMessageId, commandMessageId } = {}) {
    const cid = Number(chatId);
    const menuId = Number(menuMessageId);
    const cmdId = Number(commandMessageId);
    if (!Number.isFinite(cid) || !Number.isFinite(menuId)) return;
    setTimeout(() => {
      void queue.enqueue(async () => {
        await safeDeleteMessage({ chat_id: cid, message_id: menuId });
        if (Number.isFinite(cmdId)) {
          await safeDeleteMessage({ chat_id: cid, message_id: cmdId });
        }
      });
    }, MENU_AUTO_DELETE_MS);
  }

  async function safeAnswerCallbackQuery(id, text) {
    if (!isNonEmptyString(id)) return null;
    const params = { callback_query_id: id };
    if (isNonEmptyString(text)) params.text = text;
    try {
      return await tg.answerCallbackQuery(params);
    } catch {
      return null;
    }
  }

  function getPendingAgentInput(chatKey) {
    const entry = pendingAgentInputByChatKey.get(chatKey);
    if (!entry) return null;
    if (nowMs() >= entry.expiresAtMs) {
      pendingAgentInputByChatKey.delete(chatKey);
      return null;
    }
    return entry;
  }

  function clearPendingAgentInput(chatKey) {
    pendingAgentInputByChatKey.delete(chatKey);
  }

  function setPendingCreateAgent(chatKey, panelChatId, panelMessageId) {
    pendingAgentInputByChatKey.set(chatKey, {
      kind: "create",
      expiresAtMs: nowMs() + 10 * 60_000,
      panelChatId,
      panelMessageId
    });
  }

  function setPendingRenameAgent(chatKey, panelChatId, panelMessageId, agentId) {
    pendingAgentInputByChatKey.set(chatKey, {
      kind: "rename",
      expiresAtMs: nowMs() + 10 * 60_000,
      panelChatId,
      panelMessageId,
      agentId
    });
  }

  function formatAgentLabel(agent, { currentAgentId, locale } = {}) {
    const S = uiStrings(locale);
    const a = agent && typeof agent === "object" ? agent : null;
    const agentId = isNonEmptyString(a?.agentId) ? a.agentId : "(unknown)";
    const short = isNonEmptyString(a?.shortName) ? a.shortName : null;
    const isOwner = Boolean(a?.isOwner);
    const isCurrent = isNonEmptyString(currentAgentId) && agentId === currentAgentId;

    let label;
    if (isOwner && short) label = short;
    else label = locale === "zh" ? `${agentId}${S.shared_suffix}` : `${agentId} ${S.shared_suffix}`;
    return `${isCurrent ? "✅ " : ""}${label}`;
  }

  function normalizePage(pageRaw) {
    const n = Number(pageRaw);
    return Number.isFinite(n) && n >= 0 ? Math.floor(n) : 0;
  }

  async function renderPrivateMainMenu(chatKey, { notice, locale } = {}) {
    const S = uiStrings(locale);
    try {
      const route = await resolveRouteForChatKey(chatKey);
      const lines = [];
      lines.push(`<b>${escapeHtml(S.menu_title)}</b>`);
      if (isNonEmptyString(notice)) lines.push(`<i>${escapeHtml(notice)}</i>`);
      lines.push(`agent: ${htmlCode(route.agentId)}`);
      lines.push(`session: ${htmlCode(route.sessionId)}`);
      const ownPrefix = isNonEmptyString(chatKey) ? `u${chatKey.trim()}-` : "";
      const canRename = isNonEmptyString(ownPrefix)
        && isNonEmptyString(route?.agentId)
        && route.agentId.startsWith(ownPrefix)
        && !route.agentId.endsWith("-main");
      const rows = [
        [
          cbButton(S.btn_switch_agent, { action: "p:switch", chatKey, page: 0 }),
          cbButton(S.btn_create_agent, { action: "p:create_begin", chatKey })
        ]
      ];
      if (canRename) rows.push([cbButton(S.btn_rename_agent, { action: "p:rename_begin", chatKey })]);
      rows.push(
        [
          cbButton(S.btn_new_main_thread, { action: "p:newmain", chatKey }),
          cbButton(S.btn_node_token, { action: "p:node", chatKey })
        ],
        [cbButton(S.btn_status, { action: "p:status", chatKey }), cbButton(S.btn_help, { action: "help", chatKey })],
        [cbButton(S.btn_close, { action: "close", chatKey })]
      );
      const replyMarkup = kb(rows);
      return { text: lines.join("\n"), replyMarkup };
    } catch {
      const lines = [];
      lines.push(`<b>${escapeHtml(S.menu_title)}</b>`);
      lines.push(escapeHtml(S.msg_not_initialized));
      const replyMarkup = kb([
        [cbButton(S.btn_help, { action: "help", chatKey })],
        [cbButton(S.btn_close, { action: "close", chatKey })]
      ]);
      return { text: lines.join("\n"), replyMarkup };
    }
  }

  async function renderPrivateSwitchMenu(chatKey, pageRaw, { locale } = {}) {
    const S = uiStrings(locale);
    const page = normalizePage(pageRaw);
    try {
      const data = await argusHttp.automationAgentList(chatKey);
      const currentAgentId = isNonEmptyString(data?.currentAgentId) ? data.currentAgentId : null;
      const agents = Array.isArray(data?.agents) ? data.agents : [];

      const totalPages = Math.max(1, Math.ceil(agents.length / MENU_AGENT_PAGE_SIZE));
      const p = Math.min(page, totalPages - 1);
      const slice = agents.slice(p * MENU_AGENT_PAGE_SIZE, (p + 1) * MENU_AGENT_PAGE_SIZE);

      const lines = [];
      lines.push(`<b>${escapeHtml(S.title_switch_agent)}</b>`);
      lines.push(`${escapeHtml(S.label_current)}: ${htmlCode(currentAgentId || "(none)")}`);
      lines.push("");
      lines.push(`${escapeHtml(S.label_page)}: ${p + 1}/${totalPages}`);

      const rows = [];
      for (const a of slice) {
        const aid = isNonEmptyString(a?.agentId) ? a.agentId : null;
        if (!aid) continue;
        rows.push([
          cbButton(formatAgentLabel(a, { currentAgentId, locale }), { action: "p:use", chatKey, agentId: aid })
        ]);
      }

      const nav = [];
      if (p > 0) nav.push(cbButton(S.btn_prev, { action: "p:switch", chatKey, page: p - 1 }));
      if (p + 1 < totalPages) nav.push(cbButton(S.btn_next, { action: "p:switch", chatKey, page: p + 1 }));
      if (nav.length > 0) rows.push(nav);

      rows.push([
        cbButton(S.btn_refresh, { action: "p:switch", chatKey, page: p }),
        cbButton(S.btn_back, { action: "p:main", chatKey })
      ]);

      return { text: lines.join("\n"), replyMarkup: kb(rows) };
    } catch (e) {
      const lines = [];
      lines.push(`<b>${escapeHtml(S.title_switch_agent)}</b>`);
      lines.push(escapeHtml(formatGatewayErrorForUser(e, { locale })));
      const replyMarkup = kb([[cbButton(S.btn_back, { action: "p:main", chatKey })]]);
      return { text: lines.join("\n"), replyMarkup };
    }
  }

  async function renderPrivateCreateMenu(chatKey, { error, locale } = {}) {
    const S = uiStrings(locale);
    const lines = [];
    lines.push(`<b>${escapeHtml(S.title_create_agent)}</b>`);
    lines.push(
      `${escapeHtml(S.create_prompt_prefix)} ${htmlCode("foo")}${locale === "zh" ? "。" : "."}`
    );
    if (isNonEmptyString(error)) {
      lines.push("");
      lines.push(`<b>${escapeHtml(S.label_error)}:</b> ${escapeHtml(error)}`);
    }
    const replyMarkup = kb([[cbButton(S.btn_cancel, { action: "p:create_cancel", chatKey })]]);
    return { text: lines.join("\n"), replyMarkup };
  }

  async function renderPrivateRenameMenu(chatKey, agentId, { error, locale } = {}) {
    const S = uiStrings(locale);
    const lines = [];
    lines.push(`<b>${escapeHtml(S.title_rename_agent)}</b>`);
    if (isNonEmptyString(agentId)) lines.push(`${escapeHtml(S.label_current)}: ${htmlCode(agentId)}`);
    lines.push(
      `${escapeHtml(S.rename_prompt_prefix)} ${htmlCode("foo")}${locale === "zh" ? "。" : "."}`
    );
    if (isNonEmptyString(error)) {
      lines.push("");
      lines.push(`<b>${escapeHtml(S.label_error)}:</b> ${escapeHtml(error)}`);
    }
    const replyMarkup = kb([[cbButton(S.btn_cancel, { action: "p:rename_cancel", chatKey })]]);
    return { text: lines.join("\n"), replyMarkup };
  }

  async function renderPrivateStatusMenu(chatKey, { notice, error, locale } = {}) {
    const S = uiStrings(locale);
    const lines = [];
    lines.push(`<b>${escapeHtml(S.title_status)}</b>`);
    if (isNonEmptyString(notice)) lines.push(`<i>${escapeHtml(notice)}</i>`);
    try {
      const route = await resolveRouteForChatKey(chatKey);
      let tid = null;
      try {
        const client = await getClient(route.sessionId);
        tid = await client.ensureMainThread();
      } catch {
        tid = null;
      }
      lines.push(`agentId: ${htmlCode(route.agentId)}`);
      lines.push(`sessionId: ${htmlCode(route.sessionId)}`);
      lines.push(`chatKey: ${htmlCode(chatKey)}`);
      lines.push(`mainThreadId: ${htmlCode(tid || "(none)")}`);
    } catch (e) {
      lines.push(escapeHtml(formatGatewayErrorForUser(e, { locale })));
    }
    if (isNonEmptyString(error)) {
      lines.push("");
      lines.push(`<b>${escapeHtml(S.label_error)}:</b> ${escapeHtml(error)}`);
    }
    const replyMarkup = kb([
      [
        cbButton(S.btn_refresh, { action: "p:status", chatKey }),
        cbButton(S.btn_back, { action: "p:main", chatKey })
      ]
    ]);
    return { text: lines.join("\n"), replyMarkup };
  }

  async function renderPrivateNodeMenu(chatKey, { reveal = false, error, locale } = {}) {
    const S = uiStrings(locale);
    const lines = [];
    lines.push(`<b>${escapeHtml(S.title_node_token)}</b>`);
    lines.push(escapeHtml(S.node_warning));
    if (!reveal) {
      lines.push("");
      lines.push(escapeHtml(S.node_press_reveal));
      const replyMarkup = kb([
        [cbButton(S.btn_reveal, { action: "p:node_reveal", chatKey })],
        [cbButton(S.btn_back, { action: "p:main", chatKey })]
      ]);
      return { text: lines.join("\n"), replyMarkup };
    }

    try {
      const res = await argusHttp.automationNodeToken(chatKey);
      const sid = isNonEmptyString(res?.sessionId) ? res.sessionId : null;
      const token = isNonEmptyString(res?.token) ? res.token : null;
      const pathName = isNonEmptyString(res?.path) ? res.path : "/nodes/ws";
      const wsUrl = deriveNodeWsUrl({ httpBase: gatewayHttpUrl, pathName, token });
      lines.push(`sessionId: ${htmlCode(sid || "(none)")}`);
      lines.push(`path: ${htmlCode(pathName)}`);
      lines.push(`token:\n<pre><code>${escapeHtml(token || "(none)")}</code></pre>`);
      if (wsUrl) {
        lines.push(`wsUrl:\n<pre><code>${escapeHtml(wsUrl)}</code></pre>`);
      }
    } catch (e) {
      lines.push(escapeHtml(formatGatewayErrorForUser(e, { locale })));
    }

    if (isNonEmptyString(error)) {
      lines.push("");
      lines.push(`<b>${escapeHtml(S.label_error)}:</b> ${escapeHtml(error)}`);
    }

    const replyMarkup = kb([
      [
        cbButton(S.btn_refresh, { action: "p:node_reveal", chatKey }),
        cbButton(S.btn_back, { action: "p:main", chatKey })
      ]
    ]);
    return { text: lines.join("\n"), replyMarkup };
  }

  async function renderGroupMainMenu(chatKey, { notice, locale } = {}) {
    const S = uiStrings(locale);
    let route = null;
    try {
      route = await resolveRouteForChatKey(chatKey);
    } catch {
      route = null;
    }

    const lines = [];
    lines.push(`<b>${escapeHtml(S.menu_title)}</b>`);
    if (isNonEmptyString(notice)) lines.push(`<i>${escapeHtml(notice)}</i>`);
    lines.push(`chatKey: ${htmlCode(chatKey)}`);
    if (!route) {
      lines.push(`${escapeHtml(S.label_status)}: <b>${escapeHtml(S.status_unbound)}</b>`);
      lines.push(escapeHtml(S.group_bind_first));
      const replyMarkup = kb([
        [cbButton(S.btn_bind_this_chat, { action: "g:bind", chatKey, page: 0 })],
        [cbButton(S.btn_help, { action: "help", chatKey })],
        [cbButton(S.btn_close, { action: "close", chatKey })]
      ]);
      return { text: lines.join("\n"), replyMarkup };
    }

    const threadId = state.getThreadId(route.sessionId, chatKey);
    lines.push(`agent: ${htmlCode(route.agentId)}`);
    lines.push(`session: ${htmlCode(route.sessionId)}`);
    lines.push(`thread: ${htmlCode(threadId || "(none)")}`);

    const replyMarkup = kb([
      [
        cbButton(S.btn_new_thread, { action: "g:new", chatKey }),
        cbButton(S.btn_bind_this_chat, { action: "g:bind", chatKey, page: 0 })
      ],
      [cbButton(S.btn_status, { action: "g:status", chatKey }), cbButton(S.btn_help, { action: "help", chatKey })],
      [cbButton(S.btn_close, { action: "close", chatKey })]
    ]);
    return { text: lines.join("\n"), replyMarkup };
  }

  async function renderGroupStatusMenu(chatKey, { notice, error, locale } = {}) {
    const S = uiStrings(locale);
    const lines = [];
    lines.push(`<b>${escapeHtml(S.title_status)}</b>`);
    if (isNonEmptyString(notice)) lines.push(`<i>${escapeHtml(notice)}</i>`);
    lines.push(`chatKey: ${htmlCode(chatKey)}`);
    try {
      const route = await resolveRouteForChatKey(chatKey);
      const threadId = state.getThreadId(route.sessionId, chatKey);
      lines.push(`agentId: ${htmlCode(route.agentId)}`);
      lines.push(`sessionId: ${htmlCode(route.sessionId)}`);
      lines.push(`threadId: ${htmlCode(threadId || "(none)")}`);
    } catch (e) {
      lines.push(`${escapeHtml(S.label_status)}: <b>${escapeHtml(S.status_unbound)}</b>`);
      lines.push(escapeHtml(formatGatewayErrorForUser(e, { locale })));
    }
    if (isNonEmptyString(error)) {
      lines.push("");
      lines.push(`<b>${escapeHtml(S.label_error)}:</b> ${escapeHtml(error)}`);
    }
    const replyMarkup = kb([
      [cbButton(S.btn_refresh, { action: "g:status", chatKey }), cbButton(S.btn_back, { action: "g:main", chatKey })]
    ]);
    return { text: lines.join("\n"), replyMarkup };
  }

  async function renderGroupBindMenu(chatKey, actorUserId, pageRaw, { error, locale } = {}) {
    const S = uiStrings(locale);
    const page = normalizePage(pageRaw);
    const lines = [];
    lines.push(`<b>${escapeHtml(S.btn_bind_this_chat)}</b>`);
    lines.push(`chatKey: ${htmlCode(chatKey)}`);

    let boundAgentId = null;
    try {
      const r = await resolveRouteForChatKey(chatKey);
      boundAgentId = isNonEmptyString(r?.agentId) ? r.agentId : null;
    } catch {
      boundAgentId = null;
    }
    lines.push(`${escapeHtml(S.label_current)}: ${htmlCode(boundAgentId || "(none)")}`);
    lines.push("");
    lines.push(escapeHtml(S.bind_select_agent));

    let data;
    try {
      data = await argusHttp.automationAgentList(String(actorUserId));
    } catch (e) {
      lines.push("");
      lines.push(escapeHtml(formatGatewayErrorForUser(e, { locale })));
      lines.push(escapeHtml(S.bind_hint_start));
      const replyMarkup = kb([[cbButton(S.btn_back, { action: "g:main", chatKey })]]);
      return { text: lines.join("\n"), replyMarkup };
    }

    const agents = Array.isArray(data?.agents) ? data.agents : [];
    const totalPages = Math.max(1, Math.ceil(agents.length / MENU_AGENT_PAGE_SIZE));
    const p = Math.min(page, totalPages - 1);
    const slice = agents.slice(p * MENU_AGENT_PAGE_SIZE, (p + 1) * MENU_AGENT_PAGE_SIZE);

    lines.push(`${escapeHtml(S.label_page)}: ${p + 1}/${totalPages}`);
    if (isNonEmptyString(error)) {
      lines.push("");
      lines.push(`<b>${escapeHtml(S.label_error)}:</b> ${escapeHtml(error)}`);
    }

    const rows = [];
    for (const a of slice) {
      const aid = isNonEmptyString(a?.agentId) ? a.agentId : null;
      if (!aid) continue;
      const label = (() => {
        const base = formatAgentLabel(a, { currentAgentId: boundAgentId, locale });
        return base.replace(/^✅\s/, "✅ ");
      })();
      rows.push([cbButton(label, { action: "g:bind_to", chatKey, actorUserId, agentId: aid })]);
    }

    const nav = [];
    if (p > 0) nav.push(cbButton(S.btn_prev, { action: "g:bind", chatKey, actorUserId, page: p - 1 }));
    if (p + 1 < totalPages) nav.push(cbButton(S.btn_next, { action: "g:bind", chatKey, actorUserId, page: p + 1 }));
    if (nav.length > 0) rows.push(nav);

    rows.push([
      cbButton(S.btn_refresh, { action: "g:bind", chatKey, actorUserId, page: p }),
      cbButton(S.btn_back, { action: "g:main", chatKey })
    ]);

    return { text: lines.join("\n"), replyMarkup: kb(rows) };
  }

  async function renderHelpMenu(chatKey, { forGroup = false, locale } = {}) {
    const S = uiStrings(locale);
    const lines = [];
    lines.push(`<b>${escapeHtml(S.title_help)}</b>`);
    lines.push(`- ${formatTemplate(S.help_line_menu, { menu: htmlCode("/menu") })}`);
    if (forGroup) {
      lines.push(
        `- ${formatTemplate(S.help_line_group_bind, { bind: `<b>${escapeHtml(S.btn_bind_this_chat)}</b>` })}`
      );
      lines.push(
        `- ${formatTemplate(S.help_line_group_new, { newThread: `<b>${escapeHtml(S.btn_new_thread)}</b>` })}`
      );
    } else {
      lines.push(`- ${formatTemplate(S.help_line_start, { start: htmlCode("/start") })}`);
      lines.push(`- ${escapeHtml(S.help_line_private_more)}`);
    }
    const backAction = forGroup ? "g:main" : "p:main";
    const replyMarkup = kb([[cbButton(S.btn_back, { action: backAction, chatKey })]]);
    return { text: lines.join("\n"), replyMarkup };
  }

  async function sendMenuMessage({ target, chatKey, chatType, notice, locale } = {}) {
    if (!target || !isNonEmptyString(chatKey)) return null;
    clearPendingAgentInput(chatKey);
    const view = isPrivateChatType(chatType)
      ? await renderPrivateMainMenu(chatKey, { notice, locale })
      : await renderGroupMainMenu(chatKey, { notice, locale });
    return await safeSendMessage({
      ...target,
      text: view.text,
      parse_mode: "HTML",
      disable_web_page_preview: true,
      reply_markup: view.replyMarkup
    });
  }

  async function editMenuMessage({ chatId, messageId, view }) {
    if (!Number.isFinite(chatId) || !Number.isFinite(messageId) || !view) return null;
    return await safeEditMessageText({
      chat_id: chatId,
      message_id: messageId,
      text: view.text,
      parse_mode: "HTML",
      disable_web_page_preview: true,
      reply_markup: view.replyMarkup
    });
  }

  async function handleCallbackQuery(callbackQuery) {
    const cbId = callbackQuery?.id;
    const data = callbackQuery?.data;
    const msg = callbackQuery?.message;
    const fromId = callbackQuery?.from?.id;
    const localeHint = detectLocaleFromLanguageCode(callbackQuery?.from?.language_code) || "en";
    const S0 = uiStrings(localeHint);

    if (!isNonEmptyString(cbId) || !isNonEmptyString(data) || !msg || typeof msg !== "object") {
      await safeAnswerCallbackQuery(cbId, S0.msg_unsupported_cb);
      return;
    }

    const token = data.startsWith("cb:") ? data.slice("cb:".length) : null;
    if (!isNonEmptyString(token)) {
      await safeAnswerCallbackQuery(cbId, S0.msg_unsupported_cb);
      return;
    }

    const chatKey = chatKeyFromMessage(msg);
    const chatId = msg?.chat?.id;
    const chatType = msg?.chat?.type;
    const messageId = msg?.message_id;

    if (!isNonEmptyString(chatKey) || !Number.isFinite(chatId) || !Number.isFinite(messageId)) {
      await safeAnswerCallbackQuery(cbId, S0.msg_unsupported_cb);
      return;
    }

    const locale = localeForChatKey(chatKey, callbackQuery?.from?.language_code);
    const S = uiStrings(locale);

    const payload = callbacks.get(token);
    if (!payload || payload.chatKey !== chatKey) {
      await safeAnswerCallbackQuery(cbId, S.msg_expired);
      return;
    }

    let answered = false;
    const answerOnce = async (text) => {
      if (answered) return;
      answered = true;
      await safeAnswerCallbackQuery(cbId, text);
    };

    const action = payload.action;
    try {
      if (action === "close") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        await safeDeleteMessage({ chat_id: chatId, message_id: messageId });
        return;
      }

      if (action === "help") {
        await answerOnce();
        const view = await renderHelpMenu(chatKey, { forGroup: isGroupChatType(chatType), locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:main") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const view = await renderPrivateMainMenu(chatKey, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:switch") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const view = await renderPrivateSwitchMenu(chatKey, payload.page, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:use") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const agentId = payload.agentId;
        if (!isNonEmptyString(agentId)) {
          const view = await renderPrivateSwitchMenu(chatKey, 0, { locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const used = await argusHttp.automationAgentUse(chatKey, agentId);
        const sid = used?.agent?.sessionId;
        if (isNonEmptyString(sid)) {
          await getClient(sid);
        }
        const notice = formatTemplate(S.notice_switched, { agentId });
        const view = await renderPrivateMainMenu(chatKey, { notice, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:create_begin") {
        await answerOnce();
        setPendingCreateAgent(chatKey, chatId, messageId);
        const view = await renderPrivateCreateMenu(chatKey, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:rename_begin") {
        await answerOnce();
        let route = null;
        try {
          route = await resolveRouteForChatKey(chatKey);
        } catch (e) {
          const view = await renderPrivateMainMenu(chatKey, { notice: formatGatewayErrorForUser(e, { locale }), locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const ownPrefix = isNonEmptyString(chatKey) ? `u${chatKey.trim()}-` : "";
        const canRename = isNonEmptyString(ownPrefix)
          && isNonEmptyString(route?.agentId)
          && route.agentId.startsWith(ownPrefix)
          && !route.agentId.endsWith("-main");
        if (!canRename) {
          const view = await renderPrivateMainMenu(chatKey, { notice: S.msg_invalid_agent, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        setPendingRenameAgent(chatKey, chatId, messageId, route.agentId);
        const view = await renderPrivateRenameMenu(chatKey, route.agentId, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:create_cancel") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const view = await renderPrivateMainMenu(chatKey, { notice: S.notice_canceled, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:rename_cancel") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const view = await renderPrivateMainMenu(chatKey, { notice: S.notice_canceled, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:newmain") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const route = await resolveRouteForChatKey(chatKey);
        const client = await getClient(route.sessionId);
        const tid = await client.startThread();
        await client.setMainThread(tid);
        const notice = formatTemplate(S.notice_new_main_thread, { threadId: tid });
        const view = await renderPrivateStatusMenu(chatKey, { notice, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:status") {
        await answerOnce();
        const view = await renderPrivateStatusMenu(chatKey, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:node") {
        await answerOnce();
        const view = await renderPrivateNodeMenu(chatKey, { reveal: false, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:node_reveal") {
        await answerOnce();
        const view = await renderPrivateNodeMenu(chatKey, { reveal: true, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:main") {
        await answerOnce();
        const view = await renderGroupMainMenu(chatKey, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:status") {
        await answerOnce();
        const view = await renderGroupStatusMenu(chatKey, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:new") {
        await answerOnce();
        let route = null;
        try {
          route = await resolveRouteForChatKey(chatKey);
        } catch (e) {
          const view = await renderGroupMainMenu(chatKey, { notice: formatGatewayErrorForUser(e, { locale }), locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }

        const client = await getClient(route.sessionId);
        const tid = await client.startThread();
        await state.setThreadId(route.sessionId, chatKey, tid);
        const notice = formatTemplate(S.notice_new_thread, { threadId: tid });
        const view = await renderGroupMainMenu(chatKey, { notice, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:bind") {
        const ok = await isChatAdmin(chatId, fromId);
        if (!ok) {
          await answerOnce(S.msg_admins_only);
          return;
        }
        await answerOnce();
        const view = await renderGroupBindMenu(chatKey, fromId, payload.page, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:bind_to") {
        const agentId = payload.agentId;
        if (!isNonEmptyString(agentId)) {
          await answerOnce();
          const view = await renderGroupMainMenu(chatKey, { notice: S.msg_invalid_agent, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const ok = await isChatAdmin(chatId, fromId);
        if (!ok) {
          await answerOnce(S.msg_admins_only);
          return;
        }
        await answerOnce();
        const actorUserId = fromId;
        const bound = await argusHttp.automationChatBind(chatKey, agentId, actorUserId);
        const sid = bound?.sessionId;
        if (isNonEmptyString(sid)) {
          await getClient(sid);
        }
        const notice = formatTemplate(S.notice_bound, { agentId });
        const view = await renderGroupMainMenu(chatKey, { notice, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }
    } catch (e) {
      const msg2 = e instanceof Error ? e.message : String(e);
      const fallback = isPrivateChatType(chatType)
        ? await renderPrivateStatusMenu(chatKey, { error: msg2, locale })
        : await renderGroupStatusMenu(chatKey, { error: msg2, locale });
      await editMenuMessage({ chatId, messageId, view: fallback });
      return;
    }

    await answerOnce(S.msg_unsupported_cb);
  }

  let offset;
  if (Number.isFinite(state.state.lastUpdateId)) {
    offset = state.state.lastUpdateId + 1;
  } else {
    try {
      const backlog = await tg.getUpdates({ offset: 0, timeout: 0, allowed_updates: ["message", "callback_query"] });
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
      updates = await tg.getUpdates({ offset, timeout: 50, allowed_updates: ["message", "callback_query"] });
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

      const callbackQuery = upd?.callback_query;
      if (callbackQuery && typeof callbackQuery === "object") {
        queue.enqueue(async () => {
          await handleCallbackQuery(callbackQuery);
        });
        continue;
      }

      const message = upd?.message;
      if (!message || typeof message !== "object") continue;
      if (isServiceMessage(message)) continue;
      if (message.from?.is_bot) continue;

      const target = sendTargetFromMessage(message);
      const typingTarget = typingTargetFromMessage(message);
      const chatKey = chatKeyFromMessage(message);
      if (!target || !chatKey) continue;
      const chatType = message?.chat?.type;
      const locale = localeForChatKey(chatKey, message?.from?.language_code);

      const text = isNonEmptyString(message.text)
        ? message.text
        : isNonEmptyString(message.caption)
          ? message.caption
          : null;

      const trimmedText = isNonEmptyString(text) ? text.trim() : "";
      const slash = parseSlashCommand(trimmedText, botUsername);
      const looksSlash = trimmedText.startsWith("/");

      const messageImages = extractTelegramImagesFromMessage(message).map((img) => ({ ...img, source: "message" }));
      const replyTextRaw = extractReplyText(message);
      const replyImages = extractTelegramImagesFromMessage(message?.reply_to_message).map((img) => ({ ...img, source: "reply" }));
      const replyText = replyTextRaw || (replyImages.length > 0 ? "<media:image>" : null);

      queue.enqueue(async () => {
        const S = uiStrings(locale);
        try {
          if (slash?.forOtherBot) return;
          if (slash || looksSlash) {
            clearPendingAgentInput(chatKey);
            if (slash?.known && slash.cmd === "start") {
              if (!isPrivateChatType(chatType)) {
                await safeSendMessage({ ...target, text: S.msg_use_start_in_dm });
                return;
              }
              const boot = await argusHttp.automationUserBootstrap(chatKey);
              const currentSessionId = isNonEmptyString(boot?.currentSessionId) ? boot.currentSessionId : null;
              if (currentSessionId) await getClient(currentSessionId);
              await sendMenuMessage({
                target,
                chatKey,
                chatType,
                locale,
                notice: Boolean(boot?.createdMain) ? S.notice_initialized_created : S.notice_initialized_exists
              });
              return;
            }

            if (slash?.known && slash.cmd === "menu") {
              const sent = await sendMenuMessage({ target, chatKey, chatType, locale });
              scheduleMenuAutoDelete({
                chatId: target.chat_id,
                menuMessageId: sent?.message_id,
                commandMessageId: message?.message_id
              });
              return;
            }

            if (slash?.known && slash.cmd === "help") {
              const view = await renderHelpMenu(chatKey, { forGroup: isGroupChatType(chatType), locale });
              await safeSendMessage({
                ...target,
                text: view.text,
                parse_mode: "HTML",
                disable_web_page_preview: true,
                reply_markup: view.replyMarkup
              });
              return;
            }

            const raw =
              slash?.rawCmd ||
              trimmedText
                .slice(1)
                .split(/\s+/, 1)[0]
                .split("@", 1)[0]
                .trim()
                .toLowerCase();
            if (!isNonEmptyString(raw)) {
              await sendMenuMessage({ target, chatKey, chatType, locale });
              return;
            }
            const notice = LEGACY_COMMANDS.has(raw)
              ? formatTemplate(S.notice_legacy_moved, { cmd: raw })
              : formatTemplate(S.notice_unknown_command, { cmd: raw });
            await sendMenuMessage({ target, chatKey, chatType, notice, locale });
            return;
          }

          const pending = isPrivateChatType(chatType) ? getPendingAgentInput(chatKey) : null;
          if (pending && pending.kind === "create") {
            const name = isNonEmptyString(text) ? text.trim().split(/\s+/, 1)[0]?.trim().toLowerCase() : "";
            if (!isNonEmptyString(name)) {
              const view = await renderPrivateCreateMenu(chatKey, { error: S.create_missing, locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
              return;
            }
            try {
              const created = await argusHttp.automationAgentCreate(chatKey, name);
              await argusHttp.automationAgentUse(chatKey, name);
              const sid = created?.agent?.sessionId;
              if (isNonEmptyString(sid)) await getClient(sid);
              clearPendingAgentInput(chatKey);
              const notice = formatTemplate(S.notice_created, { name });
              const view = await renderPrivateMainMenu(chatKey, { notice, locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
            } catch (e) {
              const view = await renderPrivateCreateMenu(chatKey, { error: formatGatewayErrorForUser(e, { locale }), locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
            }
            return;
          }
          if (pending && pending.kind === "rename") {
            const name = isNonEmptyString(text) ? text.trim().split(/\s+/, 1)[0]?.trim().toLowerCase() : "";
            if (!isNonEmptyString(name)) {
              const view = await renderPrivateRenameMenu(chatKey, pending.agentId, { error: S.rename_missing, locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
              return;
            }
            try {
              await argusHttp.automationAgentRename(chatKey, pending.agentId, name);
              clearPendingAgentInput(chatKey);
              const notice = formatTemplate(S.notice_renamed, { old: pending.agentId, name });
              const view = await renderPrivateMainMenu(chatKey, { notice, locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
            } catch (e) {
              const view = await renderPrivateRenameMenu(chatKey, pending.agentId, { error: formatGatewayErrorForUser(e, { locale }), locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
            }
            return;
          }

          if (!isNonEmptyString(text) && messageImages.length === 0 && replyImages.length === 0) {
            await safeSendMessage({ ...target, text: S.msg_unsupported_message });
            return;
          }

          let route;
          try {
            route = await resolveRouteForChatKey(chatKey);
          } catch (e) {
            if (isGroupChatType(chatType)) {
              const lastWarnAt = unboundWarnedAtByChatKey.get(chatKey) || 0;
              const now = nowMs();
              if (now - lastWarnAt >= UNBOUND_WARN_COOLDOWN_MS) {
                unboundWarnedAtByChatKey.set(chatKey, now);
                await sendMenuMessage({ target, chatKey, chatType, notice: S.notice_unbound_hint, locale });
              }
              return;
            }
            await safeSendMessage({ ...target, text: formatGatewayErrorForUser(e, { locale }) });
            return;
          }

          const sessionId = route.sessionId;
          const client = await getClient(sessionId);

          const userBody = isNonEmptyString(text) ? text : "<media:image>";
          const userLines = [];
          if (replyText) {
            userLines.push("REPLY_TO:", replyText);
            if (replyImages.length > 0) userLines.push(`[reply attachments: ${replyImages.length} image(s)]`);
            userLines.push("");
            userLines.push("USER:", userBody);
          } else {
            userLines.push(userBody);
          }
          if (messageImages.length > 0) userLines.push(`[attachments: ${messageImages.length} image(s)]`);

          const userText = userLines.join("\n");

          let threadId = state.getThreadId(sessionId, chatKey);
          let enqueueTarget = null;

          if (isPrivateChatType(chatType)) {
            enqueueTarget = "main";
            threadId = null;
          } else if (isNonEmptyString(threadId)) {
            // Use mapped thread.
          } else {
            threadId = await client.startThread();
            await state.setThreadId(sessionId, chatKey, threadId);
          }

          // Show typing cue only for forwarded inputs; Telegram requires periodic refresh for long runs.
          // Note: sendChatAction accepts message_thread_id=1 (General topic), while sendMessage may not.
          typing.start(chatKey, typingTarget);

          const res = await client.enqueueInput({
            text: userText,
            threadId,
            target: enqueueTarget,
            source: { channel: "telegram", chatKey },
            telegramImages: [...replyImages, ...messageImages],
          });
          const effectiveThreadId = isNonEmptyString(res?.threadId) ? res.threadId : threadId;
          if (isNonEmptyString(effectiveThreadId)) {
            lastActiveBySessionThread.set(sessionThreadKey(sessionId, effectiveThreadId), chatKey);
          }
        } catch (e) {
          const msg = e instanceof Error ? e.message : String(e);
          await safeSendMessage({ ...target, text: `${S.label_error}: ${msg}` });
          typing.stop(chatKey);
        }
      });
    }
  }
}

await main();
