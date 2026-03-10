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

function isObjectRecord(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function normalizeMessagePhase(value) {
  if (!isNonEmptyString(value)) return null;
  return value.trim().toLowerCase();
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

function createTurnTextEntry() {
  return { delta: "", fullText: null, completedCommentaryTexts: [] };
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
    this.onTurnStarted = null;
    this.onAgentMessageCompleted = null;
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

  async automationAgentSetModel(chatKey, agentId, model) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    if (!isNonEmptyString(agentId)) throw new Error("Missing agentId");
    if (!isNonEmptyString(model)) throw new Error("Missing model");
    return await this._httpJson("POST", "/automation/agent/model/set", { chatKey, agentId, model });
  }

  async automationAgentDelete(chatKey, agentId) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    if (!isNonEmptyString(agentId)) throw new Error("Missing agentId");
    return await this._httpJson("POST", "/automation/agent/delete", { chatKey, agentId });
  }

  async automationChannelList(chatKey) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    return await this._httpJson("POST", "/automation/channel/list", { chatKey });
  }

  async automationChannelSelect(chatKey, channelId) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    if (!isNonEmptyString(channelId)) throw new Error("Missing channelId");
    return await this._httpJson("POST", "/automation/channel/select", { chatKey, channelId });
  }

  async automationChannelCreate(chatKey, name, baseUrl, apiKey) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    if (!isNonEmptyString(name)) throw new Error("Missing name");
    if (!isNonEmptyString(baseUrl)) throw new Error("Missing baseUrl");
    if (!isNonEmptyString(apiKey)) throw new Error("Missing apiKey");
    return await this._httpJson("POST", "/automation/channel/create", { chatKey, name, baseUrl, apiKey });
  }

  async automationChannelRename(chatKey, channelId, newName) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    if (!isNonEmptyString(channelId)) throw new Error("Missing channelId");
    if (!isNonEmptyString(newName)) throw new Error("Missing newName");
    return await this._httpJson("POST", "/automation/channel/rename", { chatKey, channelId, newName });
  }

  async automationChannelDelete(chatKey, channelId) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    if (!isNonEmptyString(channelId)) throw new Error("Missing channelId");
    return await this._httpJson("POST", "/automation/channel/delete", { chatKey, channelId });
  }

  async automationChannelKeySet(chatKey, channelId, apiKey) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    if (!isNonEmptyString(channelId)) throw new Error("Missing channelId");
    if (!isNonEmptyString(apiKey)) throw new Error("Missing apiKey");
    return await this._httpJson("POST", "/automation/channel/key/set", { chatKey, channelId, apiKey });
  }

  async automationChannelKeyClear(chatKey, channelId) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    if (!isNonEmptyString(channelId)) throw new Error("Missing channelId");
    return await this._httpJson("POST", "/automation/channel/key/clear", { chatKey, channelId });
  }

  async automationChatBind(chatKey, agentId, actorUserId) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    if (!isNonEmptyString(agentId)) throw new Error("Missing agentId");
    if (!Number.isFinite(actorUserId) || actorUserId <= 0) throw new Error("Missing actorUserId");
    return await this._httpJson("POST", "/automation/chat/bind", { chatKey, agentId, actorUserId });
  }

  async automationNodeList(chatKey) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    return await this._httpJson("POST", "/automation/node/list", { chatKey });
  }

  async automationNodePause(chatKey, nodeId) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    if (!isNonEmptyString(nodeId)) throw new Error("Missing nodeId");
    return await this._httpJson("POST", "/automation/node/pause", { chatKey, nodeId });
  }

  async automationNodeResume(chatKey, nodeId) {
    if (!isNonEmptyString(chatKey)) throw new Error("Missing chatKey");
    if (!isNonEmptyString(nodeId)) throw new Error("Missing nodeId");
    return await this._httpJson("POST", "/automation/node/resume", { chatKey, nodeId });
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

  async startThread(model) {
    const params = { cwd: this.cwd, approvalPolicy: "never", sandbox: "danger-full-access" };
    if (AVAILABLE_AGENT_MODELS.includes(model)) {
      params.model = model;
    }
    const result = await this.rpc("thread/start", params);
    const tid = result?.thread?.id;
    if (!isNonEmptyString(tid)) throw new Error("Invalid thread/start response");
    return tid;
  }

  async resumeThread(threadId) {
    await this.rpc("thread/resume", { threadId });
  }

  async enqueueInput({ text, threadId, target, source, telegramAttachments }) {
    const attachments = Array.isArray(telegramAttachments) ? telegramAttachments : [];
    const hasText = isNonEmptyString(text);
    if (!hasText && attachments.length === 0) throw new Error("Missing text");
    const params = { text: hasText ? text : "" };
    if (attachments.length > 0) params.telegramAttachments = attachments;
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

  async cancelTurn({ threadId, target } = {}) {
    const params = {};
    if (isNonEmptyString(threadId)) params.threadId = threadId;
    if (isNonEmptyString(target)) params.target = target;
    return await this.rpc("argus/turn/cancel", params, { timeoutMs: 15000 });
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
    const source = params.source && typeof params.source === "object" ? params.source : null;
    const sourceChatKey = isNonEmptyString(params.sourceChatKey)
      ? params.sourceChatKey
      : isNonEmptyString(source?.chatKey)
        ? source.chatKey
        : null;
    const key = threadId && turnId ? `${threadId}:${turnId}` : null;

    if (msg.method === "item/agentMessage/delta") {
      if (!key) return;
      const delta = params.delta;
      if (!isNonEmptyString(delta)) return;
      const existing = this.turnsByKey.get(key) || createTurnTextEntry();
      existing.delta += delta;
      this.turnsByKey.set(key, existing);
      return;
    }

    if (msg.method === "item/completed") {
      if (!key) return;
      const item = params.item && typeof params.item === "object" ? params.item : null;
      if (!item || item.type !== "agentMessage") return;
      if (!isNonEmptyString(item.text)) return;
      const existing = this.turnsByKey.get(key) || createTurnTextEntry();
      const phase = normalizeMessagePhase(item.phase);
      if (phase === "commentary") {
        existing.completedCommentaryTexts.push(item.text);
      } else {
        existing.fullText = item.text;
      }
      this.turnsByKey.set(key, existing);

      const cb = this.onAgentMessageCompleted;
      if (typeof cb === "function") {
        try {
          const res = cb({
            sessionId: this.sessionId,
            threadId,
            turnId,
            sourceChatKey,
            itemId: isNonEmptyString(item.id) ? item.id : null,
            text: item.text,
            phase
          });
          if (res && typeof res.then === "function") res.catch(() => {});
        } catch {
          // ignore
        }
      }
      return;
    }

    if (msg.method === "turn/started") {
      const cb = this.onTurnStarted;
      if (typeof cb === "function") {
        try {
          const res = cb({ sessionId: this.sessionId, threadId, turnId, sourceChatKey });
          if (res && typeof res.then === "function") res.catch(() => {});
        } catch {
          // ignore
        }
      }
      return;
    }

    if (msg.method === "turn/completed") {
      if (!key) return;
      const existing = this.turnsByKey.get(key) || createTurnTextEntry();
      this.turnsByKey.delete(key);
      let finalText = isNonEmptyString(existing.fullText) ? existing.fullText : existing.delta;
      if (!isNonEmptyString(existing.fullText) && isNonEmptyString(finalText)) {
        const commentaryPrefix = Array.isArray(existing.completedCommentaryTexts)
          ? existing.completedCommentaryTexts.filter(isNonEmptyString).join("")
          : "";
        if (isNonEmptyString(commentaryPrefix) && finalText.startsWith(commentaryPrefix)) {
          finalText = finalText.slice(commentaryPrefix.length);
        }
      }
      const cb = this.onTurnCompleted;
      if (typeof cb === "function") {
        try {
          const res = cb({ sessionId: this.sessionId, threadId, turnId, sourceChatKey, text: finalText });
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

function typingTargetFromChatKey(chatKey) {
  if (!isNonEmptyString(chatKey)) return null;
  const [chatIdRaw, topicRaw] = chatKey.split(":", 2);
  const chatId = Number(chatIdRaw);
  if (!Number.isFinite(chatId)) return null;
  const out = { chat_id: chatId };
  const topicId = Number(topicRaw);
  if (Number.isFinite(topicId)) out.message_thread_id = Math.trunc(topicId);
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
    // Forum/topic lifecycle messages are service events too; Telegram emits these
    // when topics are created/edited/closed/reopened or when the General topic changes.
    "forum_topic_created",
    "forum_topic_edited",
    "forum_topic_closed",
    "forum_topic_reopened",
    "general_forum_topic_hidden",
    "general_forum_topic_unhidden",
    "pinned_message",
    "invoice",
    "successful_payment"
  ];
  return serviceKeys.some((k) => k in message);
}

const KNOWN_COMMANDS = new Set(["start", "menu", "help", "cancel"]);
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

function appendTelegramAttachment(out, kind, file, { fileName = null, mimeType = null } = {}) {
  if (!Array.isArray(out)) return;
  if (!file || typeof file !== "object" || !isNonEmptyString(file.file_id)) return;
  out.push({
    kind,
    fileId: file.file_id,
    fileUniqueId: isNonEmptyString(file.file_unique_id) ? file.file_unique_id : null,
    fileName: isNonEmptyString(fileName) ? fileName : isNonEmptyString(file.file_name) ? file.file_name : null,
    mimeType: isNonEmptyString(mimeType) ? mimeType : isNonEmptyString(file.mime_type) ? file.mime_type : null,
    fileSize: typeof file.file_size === "number" ? file.file_size : null,
  });
}

function extractTelegramAttachmentsFromMessage(message) {
  const out = [];
  if (!message || typeof message !== "object") return out;

  const photos = Array.isArray(message.photo) ? message.photo : null;
  if (photos && photos.length > 0) {
    const best = photos[photos.length - 1];
    appendTelegramAttachment(out, "photo", best);
  }

  const doc = message.document;
  appendTelegramAttachment(out, "document", doc);
  appendTelegramAttachment(out, "audio", message.audio);
  appendTelegramAttachment(out, "video", message.video);
  appendTelegramAttachment(out, "voice", message.voice);
  appendTelegramAttachment(out, "animation", message.animation);
  appendTelegramAttachment(out, "video_note", message.video_note);

  return out;
}

function attachmentPlaceholder(attachments) {
  if (!Array.isArray(attachments) || attachments.length === 0) return "<attachment>";
  if (attachments.length === 1) {
    const item = attachments[0] && typeof attachments[0] === "object" ? attachments[0] : null;
    const fileName = isNonEmptyString(item?.fileName) ? item.fileName : null;
    if (fileName) return `<attachment:${fileName}>`;
    const kind = isNonEmptyString(item?.kind) ? item.kind : "attachment";
    return kind === "photo" ? "<photo>" : `<${kind}>`;
  }
  return `<attachments:${attachments.length}>`;
}

function workspaceAttachmentRef(pathValue) {
  if (!isNonEmptyString(pathValue)) return null;
  let value = pathValue.trim().replace(/\\/g, "/");
  if (value.startsWith("./")) return value;
  value = value.replace(/^\/+/, "");
  return value ? `./${value}` : null;
}

function formatStagedAttachmentsNotice(result, S) {
  const staged = Array.isArray(result?.stagedAttachments) ? result.stagedAttachments : [];
  const pending = Array.isArray(result?.attachments) ? result.attachments : staged;
  const stagedCountRaw = Number(result?.stagedAttachmentCount);
  const pendingCountRaw = Number(result?.pendingAttachmentCount);
  const stagedCount = Number.isFinite(stagedCountRaw) ? Math.max(0, Math.floor(stagedCountRaw)) : staged.length;
  const pendingCount = Number.isFinite(pendingCountRaw) ? Math.max(0, Math.floor(pendingCountRaw)) : pending.length;
  const preview = staged.length > 0 ? staged : pending;

  const lines = [formatTemplate(S.notice_attachments_staged, { stagedCount, pendingCount })];
  for (const item of preview.slice(0, 5)) {
    const ref = workspaceAttachmentRef(item?.path);
    if (ref) lines.push(`- ${ref}`);
  }
  const remaining = Math.max(0, preview.length - Math.min(preview.length, 5));
  if (remaining > 0) {
    lines.push(formatTemplate(S.notice_attachments_staged_more, { count: remaining }));
  }
  return lines.join("\n");
}

const TELEGRAM_MAX_MESSAGE_CHARS = 4000;

function findTelegramSplitBoundary(text) {
  if (!isNonEmptyString(text)) return 0;
  const min = Math.max(1, Math.floor(text.length * 0.6));
  const candidates = [
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
  ];
  for (const needle of candidates) {
    const idx = text.lastIndexOf(needle);
    if (idx >= min) return idx + needle.length;
  }
  return 0;
}

function splitTelegramMessage(text) {
  const raw = String(text ?? "");
  if (!raw) return [];
  if (raw.length <= TELEGRAM_MAX_MESSAGE_CHARS) return [raw];

  const out = [];
  let start = 0;
  while (start < raw.length) {
    let end = Math.min(start + TELEGRAM_MAX_MESSAGE_CHARS, raw.length);
    if (end < raw.length) {
      const boundary = findTelegramSplitBoundary(raw.slice(start, end));
      if (boundary > 0) end = start + boundary;
    }
    if (end <= start) end = Math.min(start + TELEGRAM_MAX_MESSAGE_CHARS, raw.length);
    out.push(raw.slice(start, end));
    start = end;
  }
  return out.filter((chunk) => chunk.length > 0);
}

function detectLocaleFromLanguageCode(languageCode) {
  if (!isNonEmptyString(languageCode)) return null;
  const raw = String(languageCode).trim().toLowerCase();
  if (!raw) return null;
  if (raw.startsWith("zh")) return "zh";
  return "en";
}

const DEFAULT_AGENT_MODEL = "gpt-5.4";
const AVAILABLE_AGENT_MODELS = ["gpt-5.2", "gpt-5.4"];

function normalizeAgentModel(model) {
  return AVAILABLE_AGENT_MODELS.includes(model) ? model : DEFAULT_AGENT_MODEL;
}

function normalizeAvailableModels(models) {
  if (!Array.isArray(models)) return [...AVAILABLE_AGENT_MODELS];
  const out = models.filter((model) => AVAILABLE_AGENT_MODELS.includes(model));
  return out.length > 0 ? out : [...AVAILABLE_AGENT_MODELS];
}

  const UI_STRINGS = {
  en: {
    menu_title: "Argus Menu",
    title_switch_agent: "Switch Agent",
    title_switch_model: "Switch Model",
    title_create_agent: "Create Agent",
    title_rename_agent: "Rename Agent",
    title_delete_agent: "Delete Agent",
    title_status: "Status",
    title_node_token: "Node Management",
    title_help: "Help",

    btn_switch_agent: "Switch Agent",
    btn_switch_model: "Switch Model",
    btn_create_agent: "Create Agent",
    btn_rename_agent: "Rename Agent",
    btn_delete_agent: "Delete Agent",
    btn_delete_confirm: "Delete",
    btn_new_main_thread: "New Conversation",
    btn_node_token: "Node Management",
    btn_pause_node: "Pause",
    btn_resume_node: "Resume",
    btn_show_node_token: "Show Token",
    btn_status: "Status",
    btn_help: "Help",
    btn_close: "Close",
    btn_refresh: "Refresh",
    btn_back: "Back",
    btn_prev: "Prev",
    btn_next: "Next",
    btn_cancel: "Cancel",
    btn_reveal: "Reveal",
    btn_bind_this_chat: "Bind This Group",
    btn_new_thread: "New Conversation",

    shared_suffix: "(shared)",
    status_unbound: "UNBOUND",

    msg_not_initialized: "No agent yet. Press Create Agent to create main.",
    msg_use_start_in_dm: "Please use /start in a private chat.",
    msg_unsupported_message: "Unsupported message type (text/files only for now).",
    msg_admins_only: "Admins only",
    msg_expired: "Expired. Send /menu.",
    msg_unsupported_cb: "Unsupported",
    msg_invalid_agent: "Invalid agent.",
    msg_invalid_model: "Invalid model.",
    msg_group_owner_only: "This action is only available for the owner of the group's current agent.",

    err_not_initialized: "No agent yet. Open /menu and press Create Agent.",
    err_forbidden: "error: forbidden.",
    err_agent_exists: "error: already exists: {name}",

    notice_initialized_created: "Initialized: main created.",
    notice_initialized_exists: "Initialized: main exists.",
    notice_canceled: "Canceled.",
    notice_cancel_requested: "Stopping the current turn…",
    notice_stop_already_requested: "Stop already requested.",
    notice_no_active_user_turn: "No active user turn to stop.",
    notice_non_user_turn: "Current run isn't a user turn.",
    notice_heartbeat_not_cancelable: "Heartbeat turns can't be stopped from chat.",
    notice_turn_not_ready: "The current turn is still starting; try again in a moment.",
    notice_unbound_hint: "UNBOUND. Use Bind This Group in /menu.",
    notice_legacy_moved: "/{cmd} has moved to /menu.",
    notice_unknown_command: "Unknown command: /{cmd}. Use /menu.",
    notice_switched: "Switched: {agentId}",
    notice_created: "Created: {name}",
    notice_renamed: "Renamed: {old} -> {name}",
    notice_deleted: "Deleted: {agentId}",
    notice_model_switched: "Model: {model}",
    notice_new_main_thread: "New conversation: {threadId}",
    notice_new_thread: "New group conversation: {threadId}",
    notice_bound: "Bound: {agentId}",
    notice_node_paused: "Paused: {nodeId}",
    notice_node_resumed: "Resumed: {nodeId}",
    notice_attachments_staged: "Saved {stagedCount} attachment(s) to the workspace. Pending for your next message: {pendingCount}.",
    notice_attachments_staged_more: "... and {count} more.",

    label_current: "current",
    label_model: "model",
    label_page: "page",
    label_status: "status",
    label_error: "error",
    label_node: "node",

    create_prompt_prefix: "Send the new agent name (a-z0-9_-), e.g.",
    create_missing: "Missing agent name.",

    rename_prompt_prefix: "Send the new agent name (a-z0-9_-), e.g.",
    rename_missing: "Missing agent name.",

    delete_warning: "This will remove the agent from the list and delete its runtime container. The workspace will be archived.",

    node_warning: "WARNING: token is sensitive; rotate if leaked.",
    node_pause_hint: "Paused nodes stay connected, but new non-process commands are blocked.",
    node_default_note: "The default node stays connected and cannot be paused.",
    node_connected_label: "Connected nodes",
    node_none_connected: "No nodes are currently connected for this session.",
    node_token_label: "Connection token",
    node_status_active: "active",
    node_status_paused: "paused",
    node_status_default: "default",
    node_status_not_connected: "not connected",
    node_press_reveal: "Press Show Token to fetch the current session token.",

    group_bind_first: "Bind this group to an agent first. All topics will use it by default.",

    bind_select_agent: "Select an agent for this topic/group. If the group has no default binding yet, this becomes the default for all topics.",
    bind_hint_start: "Hint: DM the bot and create an agent (via /menu).",

    help_line_menu: "Use {menu} to open the control panel.",
    help_line_start: "First time: run {start} in a DM to initialize your main agent.",
    help_line_cancel: "Use {cancel} to stop the current user turn.",
    help_line_private_more: "Switch Agent / Create Agent are available in the menu.",
    help_line_group_bind: "In groups/topics, bind a default agent once ({bind}); topics can later switch independently.",
    help_line_group_new: "Then use {newThread} to reset the current agent's conversation.",

    title_api_channels: "API Channels",
    title_channel_detail: "API Channel",
    title_create_channel: "Add Channel",
    title_rename_channel: "Rename Channel",
    title_delete_channel: "Delete Channel",
    title_set_channel_key: "Set API Key",

    btn_api_channels: "API Channels",
    btn_add_channel: "Add Channel",
    btn_select_channel: "Use This Channel",
    btn_rename_channel: "Rename Channel",
    btn_delete_channel: "Delete Channel",
    btn_set_channel_key: "Set API Key",
    btn_clear_channel_key: "Clear API Key",
    btn_open_00pro: "Open 0-0.pro",

    msg_invalid_channel: "Invalid channel.",
    msg_invalid_base_url: "Invalid base URL.",

    err_channel_exists: "error: already exists: {name}",

    notice_channel_switched: "Channel: {name}",
    notice_channel_created: "Channel created: {name}",
    notice_channel_renamed: "Channel renamed: {old} -> {name}",
    notice_channel_deleted: "Channel deleted: {name}",
    notice_channel_key_saved: "API key saved: {name}",
    notice_channel_key_cleared: "API key cleared: {name}",

    label_channel: "channel",
    label_base_url: "base_url",
    label_api_key: "api_key",
    label_scope: "scope",
    value_ready: "ready",
    value_not_ready: "not ready",
    value_set: "set",
    value_unset: "unset",

    channel_scope_all_agents: "Switching here affects all your agents/containers.",
    channel_gateway_hint: "Uses the gateway's shared OPENAI-compatible upstream.",
    channel_promo_hint: "Set your own 0-0.pro API key, then switch to it.",
    channel_delete_warning: "This deletes the saved channel. If it is current, all your agents switch to the fallback channel.",
    channel_create_name_prompt: "Send the new channel name (a-z0-9._-), e.g.",
    channel_create_name_missing: "Missing channel name.",
    channel_create_base_url_prompt: "Send the base URL, e.g.",
    channel_create_base_url_missing: "Missing base URL.",
    channel_create_key_prompt: "Send the API key for {name}.",
    channel_create_key_missing: "Missing API key.",
    channel_rename_prompt_prefix: "Send the new channel name (a-z0-9._-), e.g.",
    channel_rename_missing: "Missing channel name.",
    channel_key_prompt_prefix: "Send the API key for {name}.",
    channel_key_missing: "Missing API key."
  },
  zh: {
    menu_title: "Argus 菜单",
    title_switch_agent: "切换 Agent",
    title_switch_model: "切换模型",
    title_create_agent: "创建 Agent",
    title_rename_agent: "重命名 Agent",
    title_delete_agent: "删除 Agent",
    title_status: "状态",
    title_node_token: "节点管理",
    title_help: "帮助",

    btn_switch_agent: "切换 Agent",
    btn_switch_model: "切换模型",
    btn_create_agent: "创建 Agent",
    btn_rename_agent: "重命名 Agent",
    btn_delete_agent: "删除 Agent",
    btn_delete_confirm: "删除",
    btn_new_main_thread: "新建对话",
    btn_node_token: "节点管理",
    btn_pause_node: "暂停",
    btn_resume_node: "恢复",
    btn_show_node_token: "显示 Token",
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
    btn_new_thread: "新建对话",

    shared_suffix: "（共享）",
    status_unbound: "未绑定",

    msg_not_initialized: "尚未创建 agent：点击“创建 Agent”即可创建 main。",
    msg_use_start_in_dm: "请在私聊中使用 /start。",
    msg_unsupported_message: "暂不支持该消息类型（目前仅支持文本/文件）。",
    msg_admins_only: "仅群管理员可操作",
    msg_expired: "已过期，请发送 /menu。",
    msg_unsupported_cb: "不支持",
    msg_invalid_agent: "无效的 agent。",
    msg_invalid_model: "无效的模型。",
    msg_group_owner_only: "只有当前群组 Agent 的拥有者才能执行这个操作。",

    err_not_initialized: "尚未创建 agent：请发送 /menu 并点击“创建 Agent”。",
    err_forbidden: "error: 没有权限。",
    err_agent_exists: "error: 已存在：{name}",

    notice_initialized_created: "已初始化：已创建 main。",
    notice_initialized_exists: "已初始化：main 已存在。",
    notice_canceled: "已取消。",
    notice_cancel_requested: "正在停止当前任务……",
    notice_stop_already_requested: "已经请求停止了。",
    notice_no_active_user_turn: "当前没有可停止的用户任务。",
    notice_non_user_turn: "当前运行的不是用户任务。",
    notice_heartbeat_not_cancelable: "heartbeat 任务不能从聊天里停止。",
    notice_turn_not_ready: "当前任务还在启动中，请稍后再试。",
    notice_unbound_hint: "未绑定：请用 /menu 里的“绑定当前群聊”。绑定后所有 topic 默认共用这个 agent。",
    notice_legacy_moved: "/{cmd} 已迁移到 /menu。",
    notice_unknown_command: "未知命令：/{cmd}。请用 /menu。",
    notice_switched: "已切换：{agentId}",
    notice_created: "已创建：{name}",
    notice_renamed: "已重命名：{old} -> {name}",
    notice_deleted: "已删除：{agentId}",
    notice_model_switched: "已切换模型：{model}",
    notice_new_main_thread: "已新建对话：{threadId}",
    notice_new_thread: "已新建群组对话：{threadId}",
    notice_bound: "已绑定：{agentId}",
    notice_node_paused: "已暂停：{nodeId}",
    notice_node_resumed: "已恢复：{nodeId}",
    notice_attachments_staged: "已将 {stagedCount} 个附件保存到工作区；下条消息会自动带上当前待处理的 {pendingCount} 个附件。",
    notice_attachments_staged_more: "……其余 {count} 个未展开。",

    label_current: "当前",
    label_model: "模型",
    label_page: "页",
    label_status: "状态",
    label_error: "错误",
    label_node: "节点",

    create_prompt_prefix: "发送新 agent 名称（a-z0-9_-），例如",
    create_missing: "缺少 agent 名称。",

    rename_prompt_prefix: "发送新 agent 名称（a-z0-9_-），例如",
    rename_missing: "缺少 agent 名称。",

    delete_warning: "将从列表中移除该 agent，并删除其 runtime 容器；workspace 会被归档。",

    node_warning: "注意：token 很敏感；泄露请立刻轮换。",
    node_pause_hint: "暂停后节点仍保持连接，但新的非 process.* 命令会被阻止。",
    node_default_note: "默认节点会保持连接，且不可暂停。",
    node_connected_label: "已连接节点",
    node_none_connected: "当前 session 下还没有已连接节点。",
    node_token_label: "连接 Token",
    node_status_active: "活跃",
    node_status_paused: "已暂停",
    node_status_default: "默认",
    node_status_not_connected: "未连接",
    node_press_reveal: "点击“显示 Token”获取当前 session 的 token。",

    group_bind_first: "请先把当前群聊绑定到某个 agent；绑定后所有 topic 默认共用它。",

    bind_select_agent: "为当前话题/群选择 agent。若当前群还没有默认绑定，这次选择会成为所有 topic 的默认 agent。",
    bind_hint_start: "提示：先私聊 bot，用 /menu 创建一个 agent。",

    help_line_menu: "用 {menu} 打开控制面板。",
    help_line_start: "首次使用：先在私聊发送 {start} 完成初始化。",
    help_line_cancel: "需要打断当前用户任务时，发送 {cancel}。",
    help_line_private_more: "切换/创建 agent 都在菜单里操作。",
    help_line_group_bind: "在群聊/话题里，先给整个群绑定一个默认 agent（{bind}）；之后各个 topic 也可以单独切换。",
    help_line_group_new: "之后用 {newThread} 重置当前 agent 的对话。",

    title_api_channels: "API 渠道",
    title_channel_detail: "API 渠道",
    title_create_channel: "添加渠道",
    title_rename_channel: "重命名渠道",
    title_delete_channel: "删除渠道",
    title_set_channel_key: "设置 API Key",

    btn_api_channels: "API 渠道",
    btn_add_channel: "添加渠道",
    btn_select_channel: "切换到此渠道",
    btn_rename_channel: "重命名渠道",
    btn_delete_channel: "删除渠道",
    btn_set_channel_key: "设置 API Key",
    btn_clear_channel_key: "清空 API Key",
    btn_open_00pro: "打开 0-0.pro",

    msg_invalid_channel: "无效的渠道。",
    msg_invalid_base_url: "无效的 Base URL。",

    err_channel_exists: "error: 已存在：{name}",

    notice_channel_switched: "已切换渠道：{name}",
    notice_channel_created: "已添加渠道：{name}",
    notice_channel_renamed: "已重命名渠道：{old} -> {name}",
    notice_channel_deleted: "已删除渠道：{name}",
    notice_channel_key_saved: "已保存 API Key：{name}",
    notice_channel_key_cleared: "已清空 API Key：{name}",

    label_channel: "渠道",
    label_base_url: "Base URL",
    label_api_key: "API Key",
    label_scope: "作用范围",
    value_ready: "可用",
    value_not_ready: "未就绪",
    value_set: "已设置",
    value_unset: "未设置",

    channel_scope_all_agents: "这里的切换会影响你的所有 agent / 容器。",
    channel_gateway_hint: "使用 gateway 自己配置的 OpenAI-compatible 上游。",
    channel_promo_hint: "先设置你自己的 0-0.pro API Key，再切换到它。",
    channel_delete_warning: "这会删除已保存的渠道；如果它当前正在使用，你的所有 agent 会切回回退渠道。",
    channel_create_name_prompt: "发送新渠道名称（a-z0-9._-），例如",
    channel_create_name_missing: "缺少渠道名称。",
    channel_create_base_url_prompt: "发送 Base URL，例如",
    channel_create_base_url_missing: "缺少 Base URL。",
    channel_create_key_prompt: "发送 {name} 的 API Key。",
    channel_create_key_missing: "缺少 API Key。",
    channel_rename_prompt_prefix: "发送新的渠道名称（a-z0-9._-），例如",
    channel_rename_missing: "缺少渠道名称。",
    channel_key_prompt_prefix: "发送 {name} 的 API Key。",
    channel_key_missing: "缺少 API Key。"
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
  if (/invalid model/i.test(msg)) return S.msg_invalid_model;
  if (/invalid channel|unknown channel|missing 'channelid'/i.test(msg)) return S.msg_invalid_channel;
  if (/invalid baseurl/i.test(msg)) return S.msg_invalid_base_url;
  const already = /agent already exists:\s*([a-z0-9_-]+)/i.exec(msg);
  if (already) return formatTemplate(S.err_agent_exists, { name: already[1] });
  const channelAlready = /channel already exists:\s*([a-z0-9._-]+)/i.exec(msg);
  if (channelAlready) return formatTemplate(S.err_channel_exists, { name: channelAlready[1] });
  return `error: ${msg}`;
}

function formatCancelTurnResultForUser(result, { locale } = {}) {
  const S = uiStrings(locale);
  const payload = isObjectRecord(result) ? result : {};
  if (payload.cancelRequested === true) {
    return payload.alreadyRequested === true ? S.notice_stop_already_requested : S.notice_cancel_requested;
  }
  const reason = isNonEmptyString(payload.reason) ? payload.reason : "";
  const activeKind = isNonEmptyString(payload.activeKind) ? payload.activeKind.toLowerCase() : "";
  if (reason === "TURN_NOT_READY") return S.notice_turn_not_ready;
  if (reason === "NON_USER_TURN") {
    if (activeKind === "heartbeat") return S.notice_heartbeat_not_cancelable;
    return S.notice_non_user_turn;
  }
  return S.notice_no_active_user_turn;
}

const TELEGRAM_HTML_PARSE_ERR_RE = /can't parse entities|parse entities|find end of the entity/i;
const TELEGRAM_MESSAGE_TOO_LONG_RE = /message is too long/i;
const TELEGRAM_TOPIC_UNAVAILABLE_RE = /TOPIC_CLOSED|topic is closed|message thread not found/i;

function isIgnorableTelegramSendError(err) {
  const msg = err instanceof Error ? err.message : String(err);
  return TELEGRAM_TOPIC_UNAVAILABLE_RE.test(msg);
}

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
  constructor(tg, { intervalSeconds = 4.5, ttlMs = 0 } = {}) {
    this.tg = tg;
    this.intervalMs = Math.max(1000, Math.floor(Number(intervalSeconds) * 1000));
    const ttlNumber = Number(ttlMs);
    this.ttlMs = Number.isFinite(ttlNumber) && ttlNumber > 0 ? Math.max(10_000, Math.floor(ttlNumber)) : null;
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

    const expiresAtMs = this.ttlMs ? Date.now() + this.ttlMs : null;
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
      if (current.expiresAtMs && Date.now() >= current.expiresAtMs) {
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
  const cwd = process.env.ARGUS_CWD || "/workspace";
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
    { command: "cancel", description: "Stop current turn" },
    { command: "help", description: "Help" }
  ];
  const privateCommands = [
    { command: "start", description: "Initialize (private chat)" },
    { command: "menu", description: "Open menu" },
    { command: "cancel", description: "Stop current turn" },
    { command: "help", description: "Help" }
  ];
  const defaultCommandsZh = [
    { command: "menu", description: "打开菜单" },
    { command: "cancel", description: "停止当前任务" },
    { command: "help", description: "帮助" }
  ];
  const privateCommandsZh = [
    { command: "start", description: "初始化（私聊）" },
    { command: "menu", description: "打开菜单" },
    { command: "cancel", description: "停止当前任务" },
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
  const pendingTurnTargetsBySessionThread = new Map(); // `${sessionId}:${threadId}` -> [chatKey]
  const turnTargetBySessionThreadTurn = new Map(); // `${sessionId}:${threadId}:${turnId}` -> chatKey
  const sessionThreadKey = (sessionId, threadId) => `${sessionId}:${threadId}`;
  const sessionThreadTurnKey = (sessionId, threadId, turnId) => `${sessionId}:${threadId}:${turnId}`;

  function queuePendingTurnTarget(sessionId, threadId, chatKey) {
    if (!isNonEmptyString(sessionId) || !isNonEmptyString(threadId) || !isNonEmptyString(chatKey)) return;
    const key = sessionThreadKey(sessionId, threadId);
    const queue0 = pendingTurnTargetsBySessionThread.get(key);
    if (Array.isArray(queue0)) {
      queue0.push(chatKey);
      return;
    }
    pendingTurnTargetsBySessionThread.set(key, [chatKey]);
  }

  function shiftPendingTurnTarget(sessionId, threadId) {
    const key = sessionThreadKey(sessionId, threadId);
    const queue0 = pendingTurnTargetsBySessionThread.get(key);
    if (!Array.isArray(queue0) || queue0.length === 0) return null;
    const chatKey = queue0.shift();
    if (queue0.length === 0) pendingTurnTargetsBySessionThread.delete(key);
    return isNonEmptyString(chatKey) ? chatKey : null;
  }

  function removePendingTurnTarget(sessionId, threadId, chatKey) {
    if (!isNonEmptyString(sessionId) || !isNonEmptyString(threadId) || !isNonEmptyString(chatKey)) return;
    const key = sessionThreadKey(sessionId, threadId);
    const queue0 = pendingTurnTargetsBySessionThread.get(key);
    if (!Array.isArray(queue0) || queue0.length === 0) return;
    const idx = queue0.findIndex((item) => item === chatKey);
    if (idx === -1) return;
    queue0.splice(idx, 1);
    if (queue0.length === 0) pendingTurnTargetsBySessionThread.delete(key);
  }

  function clearPendingTurnTargets(sessionId, threadId) {
    if (!isNonEmptyString(sessionId) || !isNonEmptyString(threadId)) return;
    pendingTurnTargetsBySessionThread.delete(sessionThreadKey(sessionId, threadId));
  }

  function rememberTurnTarget(sessionId, threadId, turnId, chatKey) {
    if (!isNonEmptyString(sessionId) || !isNonEmptyString(threadId) || !isNonEmptyString(turnId) || !isNonEmptyString(chatKey)) return;
    turnTargetBySessionThreadTurn.set(sessionThreadTurnKey(sessionId, threadId, turnId), chatKey);
  }

  function resolveExactTurnChatKey(sessionId, threadId, turnId) {
    if (!isNonEmptyString(sessionId) || !isNonEmptyString(threadId) || !isNonEmptyString(turnId)) return null;
    const exact = turnTargetBySessionThreadTurn.get(sessionThreadTurnKey(sessionId, threadId, turnId));
    return isNonEmptyString(exact) ? exact : null;
  }

  function resolveTurnChatKey(sessionId, threadId, turnId) {
    const exact = resolveExactTurnChatKey(sessionId, threadId, turnId);
    if (isNonEmptyString(exact)) return exact;
    return lastActiveBySessionThread.get(sessionThreadKey(sessionId, threadId)) || null;
  }

  function clearSessionRoutingState(sessionId) {
    if (!isNonEmptyString(sessionId)) return;
    const prefix = `${sessionId}:`;
    for (const [key, chatKey] of lastActiveBySessionThread.entries()) {
      if (key.startsWith(prefix) && isNonEmptyString(chatKey)) {
        typing.stop(chatKey);
        lastActiveBySessionThread.delete(key);
      }
    }
    for (const key of pendingTurnTargetsBySessionThread.keys()) {
      if (key.startsWith(prefix)) pendingTurnTargetsBySessionThread.delete(key);
    }
    for (const key of turnTargetBySessionThreadTurn.keys()) {
      if (key.startsWith(prefix)) turnTargetBySessionThreadTurn.delete(key);
    }
  }

  const attachClientHooks = (client) => {
    client.onTurnStarted = async ({ sessionId, threadId, turnId, sourceChatKey }) => {
      if (!isNonEmptyString(sessionId) || !isNonEmptyString(threadId)) return;
      if (isNonEmptyString(sourceChatKey)) {
        removePendingTurnTarget(sessionId, threadId, sourceChatKey);
      }
      let chatKey = isNonEmptyString(sourceChatKey) ? sourceChatKey : resolveExactTurnChatKey(sessionId, threadId, turnId);
      if (!isNonEmptyString(chatKey)) {
        chatKey = shiftPendingTurnTarget(sessionId, threadId);
        if (isNonEmptyString(chatKey) && isNonEmptyString(turnId)) {
          rememberTurnTarget(sessionId, threadId, turnId, chatKey);
        }
      }
      if (!isNonEmptyString(chatKey)) {
        chatKey = lastActiveBySessionThread.get(sessionThreadKey(sessionId, threadId)) || null;
      }
      if (!isNonEmptyString(chatKey)) return;
      if (isNonEmptyString(turnId)) {
        rememberTurnTarget(sessionId, threadId, turnId, chatKey);
      }
      lastActiveBySessionThread.set(sessionThreadKey(sessionId, threadId), chatKey);
      const target = typingTargetFromChatKey(chatKey);
      if (!target) return;
      typing.start(chatKey, target);
    };
    client.onAgentMessageCompleted = async ({ sessionId, threadId, turnId, sourceChatKey, text, phase }) => {
      if (phase !== "commentary") return;
      if (!isNonEmptyString(sessionId) || !isNonEmptyString(threadId) || !isNonEmptyString(text)) return;
      const chatKey = isNonEmptyString(sourceChatKey) ? sourceChatKey : resolveTurnChatKey(sessionId, threadId, turnId);
      if (!isNonEmptyString(chatKey)) return;

      await queue.enqueue(async () => {
        const sendTarget = sendTargetFromChatKey(chatKey);
        if (sendTarget) {
          await sendAssistantMessage(sendTarget, text);
        }

        const typingTarget = typingTargetFromChatKey(chatKey);
        if (typingTarget) {
          typing.start(chatKey, typingTarget);
        }
      });
    };
    client.onTurnCompleted = async ({ sessionId, threadId, turnId, sourceChatKey }) => {
      if (!isNonEmptyString(sessionId) || !isNonEmptyString(threadId)) return;
      const chatKey = isNonEmptyString(sourceChatKey) ? sourceChatKey : resolveTurnChatKey(sessionId, threadId, turnId);
      if (!isNonEmptyString(chatKey)) return;
      typing.stop(chatKey);
      if (isNonEmptyString(turnId)) {
        turnTargetBySessionThreadTurn.delete(sessionThreadTurnKey(sessionId, threadId, turnId));
      }
    };
    client.onDisconnected = () => {
      const sid = client.sessionId;
      if (!isNonEmptyString(sid)) {
        typing.stopAll();
        return;
      }
      clearSessionRoutingState(sid);
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

  async function ensureSessionMainThread(sessionId) {
    const client = await getClient(sessionId);
    return await client.ensureMainThread();
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
    const model = normalizeAgentModel(res?.model);
    const availableModels = normalizeAvailableModels(res?.availableModels);
    if (!sessionId) throw new Error("Missing sessionId");
    return { agentId, sessionId, model, availableModels };
  }

  async function resolveChannelStateForChatKey(chatKey) {
    const res = await argusHttp.automationChannelList(chatKey);
    const channels = Array.isArray(res?.channels) ? res.channels : [];
    const currentChannelId = isNonEmptyString(res?.currentChannelId) ? res.currentChannelId : null;
    const currentChannel = (res?.currentChannel && typeof res.currentChannel === "object")
      ? res.currentChannel
      : (currentChannelId ? channels.find((channel) => isNonEmptyString(channel?.channelId) && channel.channelId === currentChannelId) || null : null);
    return { channels, currentChannelId, currentChannel };
  }

  function findChannelById(channels, channelId) {
    if (!Array.isArray(channels) || !isNonEmptyString(channelId)) return null;
    return channels.find((channel) => channel && typeof channel === "object" && channel.channelId === channelId) || null;
  }

  const callbacks = new CallbackStore();
  const pendingAgentInputByChatKey = new Map(); // chatKey -> panel input state
  const unboundWarnedAtByChatKey = new Map(); // chatKey -> ms
  const localeByChatKey = new Map(); // chatKey -> "zh" | "en"

  const MENU_AGENT_PAGE_SIZE = 8;
  const MENU_CHANNEL_PAGE_SIZE = 8;
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

  function urlButton(text, url) {
    return { text, url };
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
      if (isIgnorableTelegramSendError(e)) return null;
      log("sendMessage failed:", e instanceof Error ? e.message : String(e));
      return null;
    }
  }

  async function sendAssistantMessage(target, text) {
    if (!target || !isNonEmptyString(text)) return null;
    const chunks = splitTelegramMessage(text.trim());
    let lastResult = null;

    try {
      for (const chunk of chunks) {
        const html = markdownToTelegramHtml(chunk);
        if (isNonEmptyString(html)) {
          try {
            lastResult = await tg.sendMessage({ ...target, text: html, parse_mode: "HTML" });
            continue;
          } catch (e) {
            if (isIgnorableTelegramSendError(e)) return lastResult;
            const msg = e instanceof Error ? e.message : String(e);
            if (!TELEGRAM_HTML_PARSE_ERR_RE.test(msg) && !TELEGRAM_MESSAGE_TOO_LONG_RE.test(msg)) {
              throw e;
            }
          }
        }
        try {
          lastResult = await tg.sendMessage({ ...target, text: chunk });
        } catch (e) {
          if (isIgnorableTelegramSendError(e)) return lastResult;
          throw e;
        }
      }
      return lastResult;
    } catch (e) {
      if (isIgnorableTelegramSendError(e)) return lastResult;
      log("assistant sendMessage failed:", e instanceof Error ? e.message : String(e));
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

  function pendingInputKey(chatKey, actorUserId) {
    if (!isNonEmptyString(chatKey)) return null;
    const uid = Number(actorUserId);
    if (Number.isFinite(uid) && uid > 0) return `${chatKey}::u:${Math.trunc(uid)}`;
    return chatKey;
  }

  function privateChatKeyForUserId(userId) {
    const uid = Number(userId);
    if (!Number.isFinite(uid) || uid <= 0) return null;
    return String(Math.trunc(uid));
  }

  function ownerUserIdFromAgentId(agentId) {
    const match = /^u(\d+)-/.exec(String(agentId || "").trim());
    if (!match) return null;
    const uid = Number(match[1]);
    return Number.isFinite(uid) && uid > 0 ? Math.trunc(uid) : null;
  }

  function canActorManageAgent(agentId, actorUserId) {
    const ownerUserId = ownerUserIdFromAgentId(agentId);
    const uid = Number(actorUserId);
    return Number.isFinite(uid) && uid > 0 && ownerUserId === Math.trunc(uid);
  }

  function getPendingAgentInput(chatKey, actorUserId) {
    const key = pendingInputKey(chatKey, actorUserId);
    if (!isNonEmptyString(key)) return null;
    const entry = pendingAgentInputByChatKey.get(key);
    if (!entry) return null;
    if (nowMs() >= entry.expiresAtMs) {
      pendingAgentInputByChatKey.delete(key);
      return null;
    }
    return entry;
  }

  function clearPendingAgentInput(chatKey, actorUserId) {
    const key = pendingInputKey(chatKey, actorUserId);
    if (!isNonEmptyString(key)) return;
    pendingAgentInputByChatKey.delete(key);
  }

  function setPendingInput(chatKey, entry, actorUserId) {
    const key = pendingInputKey(chatKey, actorUserId);
    if (!isNonEmptyString(key)) return;
    pendingAgentInputByChatKey.set(key, {
      expiresAtMs: nowMs() + 10 * 60_000,
      ...entry
    });
  }

  function setPendingCreateAgent(chatKey, panelChatId, panelMessageId, actorUserId) {
    setPendingInput(chatKey, {
      kind: "create",
      panelChatId,
      panelMessageId
    }, actorUserId);
  }

  function setPendingRenameAgent(chatKey, panelChatId, panelMessageId, agentId, actorUserId) {
    setPendingInput(chatKey, {
      kind: "rename",
      panelChatId,
      panelMessageId,
      agentId
    }, actorUserId);
  }

  function setPendingCreateChannelName(chatKey, panelChatId, panelMessageId, actorUserId) {
    setPendingInput(chatKey, {
      kind: "channel_create_name",
      panelChatId,
      panelMessageId
    }, actorUserId);
  }

  function setPendingCreateChannelBaseUrl(chatKey, panelChatId, panelMessageId, channelName, actorUserId) {
    setPendingInput(chatKey, {
      kind: "channel_create_base_url",
      panelChatId,
      panelMessageId,
      channelName
    }, actorUserId);
  }

  function setPendingCreateChannelApiKey(chatKey, panelChatId, panelMessageId, channelName, baseUrl, actorUserId) {
    setPendingInput(chatKey, {
      kind: "channel_create_api_key",
      panelChatId,
      panelMessageId,
      channelName,
      baseUrl
    }, actorUserId);
  }

  function setPendingRenameChannel(chatKey, panelChatId, panelMessageId, channelId, page = 0, actorUserId) {
    setPendingInput(chatKey, {
      kind: "channel_rename",
      panelChatId,
      panelMessageId,
      channelId,
      page
    }, actorUserId);
  }

  function setPendingChannelKey(chatKey, panelChatId, panelMessageId, channelId, page = 0, actorUserId) {
    setPendingInput(chatKey, {
      kind: "channel_key",
      panelChatId,
      panelMessageId,
      channelId,
      page
    }, actorUserId);
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

  function channelDisplayName(channel) {
    const c = channel && typeof channel === "object" ? channel : null;
    if (isNonEmptyString(c?.name)) return c.name;
    if (isNonEmptyString(c?.channelId)) return c.channelId;
    return "(unknown)";
  }

  function channelStatusText(channel, { locale } = {}) {
    const S = uiStrings(locale);
    return channel?.ready ? S.value_ready : S.value_not_ready;
  }

  function channelApiKeyText(channel, { locale } = {}) {
    const S = uiStrings(locale);
    if (isNonEmptyString(channel?.apiKeyMasked)) return channel.apiKeyMasked;
    return channel?.hasApiKey ? S.value_set : S.value_unset;
  }

  function formatChannelLabel(channel, { currentChannelId } = {}) {
    const c = channel && typeof channel === "object" ? channel : null;
    const channelId = isNonEmptyString(c?.channelId) ? c.channelId : "(unknown)";
    const name = channelDisplayName(c);
    const isCurrent = isNonEmptyString(currentChannelId) && channelId === currentChannelId;
    const ready = Boolean(c?.ready);
    const prefix = isCurrent ? "✅ " : ready ? "• " : "⚠️ ";
    return `${prefix}${name}`;
  }

  function nodeDisplayName(node) {
    const n = node && typeof node === "object" ? node : null;
    if (isNonEmptyString(n?.displayName)) return n.displayName;
    if (isNonEmptyString(n?.nodeId)) return n.nodeId;
    return "(unknown)";
  }

  function nodeStatusText(node, { locale } = {}) {
    const S = uiStrings(locale);
    const labels = [];
    if (node?.isDefault) labels.push(S.node_status_default);
    labels.push(node?.paused ? S.node_status_paused : S.node_status_active);
    return labels.join(" • ");
  }

  function trimMenuLabel(text, maxLen = 20) {
    const value = String(text || "").trim();
    if (value.length <= maxLen) return value;
    return `${value.slice(0, Math.max(1, maxLen - 1))}…`;
  }

  function normalizePage(pageRaw) {
    const n = Number(pageRaw);
    return Number.isFinite(n) && n >= 0 ? Math.floor(n) : 0;
  }

  function buildTwoColumnMenuRows(primaryButtons, secondaryButtons = [], trailingButton = null) {
    const primary = Array.isArray(primaryButtons) ? primaryButtons.filter(Boolean) : [];
    const secondary = Array.isArray(secondaryButtons) ? secondaryButtons.filter(Boolean) : [];
    const rows = [];

    while (primary.length > 0) {
      const row = [primary.shift()];
      if (primary.length > 0) row.push(primary.shift());
      else if (secondary.length > 0) row.push(secondary.shift());
      rows.push(row);
    }

    while (secondary.length > 1) {
      rows.push([secondary.shift(), secondary.shift()]);
    }

    if (secondary.length === 1) {
      if (trailingButton) {
        rows.push([secondary.shift(), trailingButton]);
        trailingButton = null;
      } else {
        rows.push([secondary.shift()]);
      }
    }

    if (trailingButton) rows.push([trailingButton]);
    return rows;
  }

  const PRIVATE_TO_GROUP_ACTION = new Map([
    ["p:main", "g:main"],
    ["p:create_cancel", "g:create_cancel"],
    ["p:channels", "g:channels"],
    ["p:channel_view", "g:channel_view"],
    ["p:channel_select", "g:channel_select"],
    ["p:channel_create_begin", "g:channel_create_begin"],
    ["p:channel_create_cancel", "g:channel_create_cancel"],
    ["p:channel_rename_begin", "g:channel_rename_begin"],
    ["p:channel_rename_cancel", "g:channel_rename_cancel"],
    ["p:channel_delete_begin", "g:channel_delete_begin"],
    ["p:channel_delete_cancel", "g:channel_delete_cancel"],
    ["p:channel_delete_confirm", "g:channel_delete_confirm"],
    ["p:channel_key_begin", "g:channel_key_begin"],
    ["p:channel_key_cancel", "g:channel_key_cancel"],
    ["p:channel_key_clear", "g:channel_key_clear"],
    ["p:rename_cancel", "g:rename_cancel"],
    ["p:delete_cancel", "g:delete_cancel"],
    ["p:delete_confirm", "g:delete_confirm"],
    ["p:node", "g:node"],
    ["p:node_reveal", "g:node_reveal"],
    ["p:node_pause", "g:node_pause"],
    ["p:node_resume", "g:node_resume"]
  ]);

  function remapViewCallbacks(view, transformPayload) {
    if (!view || typeof view !== "object") return view;
    const replyMarkup = view.replyMarkup;
    const keyboard = replyMarkup?.inline_keyboard;
    if (!Array.isArray(keyboard)) return view;

    const remappedKeyboard = keyboard.map((row) => {
      if (!Array.isArray(row)) return row;
      return row.map((button) => {
        if (!button || typeof button !== "object" || !isNonEmptyString(button.callback_data)) return button;
        const token = button.callback_data.startsWith("cb:") ? button.callback_data.slice("cb:".length) : null;
        if (!isNonEmptyString(token)) return button;
        const payload = callbacks.get(token);
        if (!payload || typeof payload !== "object") return button;
        const nextPayload = transformPayload(payload);
        if (!nextPayload || typeof nextPayload !== "object") return button;
        return {
          ...button,
          callback_data: callbackData(nextPayload)
        };
      });
    });

    return {
      ...view,
      replyMarkup: {
        ...replyMarkup,
        inline_keyboard: remappedKeyboard
      }
    };
  }

  function remapPrivateViewToGroup(view, chatKey) {
    return remapViewCallbacks(view, (payload) => {
      const action = isNonEmptyString(payload?.action) ? payload.action : null;
      const nextAction = action ? PRIVATE_TO_GROUP_ACTION.get(action) : null;
      if (!isNonEmptyString(nextAction)) return null;
      return {
        ...payload,
        action: nextAction,
        chatKey
      };
    });
  }

  function renderGroupOwnerOnlyView(chatKey, { error, locale } = {}) {
    const S = uiStrings(locale);
    const lines = [];
    lines.push(`<b>${escapeHtml(S.menu_title)}</b>`);
    lines.push(`chatKey: ${htmlCode(chatKey)}`);
    if (isNonEmptyString(error)) lines.push(escapeHtml(error));
    const replyMarkup = kb([[cbButton(S.btn_back, { action: "g:main", chatKey })]]);
    return { text: lines.join("\n"), replyMarkup };
  }

  async function resolveGroupActorContext(chatKey, actorUserId) {
    const route = await resolveRouteForChatKey(chatKey);
    const actorChatKey = privateChatKeyForUserId(actorUserId);
    const ownsCurrentAgent = isNonEmptyString(route?.agentId) && canActorManageAgent(route.agentId, actorUserId);
    let currentChannel = null;
    if (ownsCurrentAgent && isNonEmptyString(actorChatKey)) {
      try {
        const channelState = await resolveChannelStateForChatKey(actorChatKey);
        currentChannel = channelState?.currentChannel || null;
      } catch {
        currentChannel = null;
      }
    }
    return {
      actorChatKey,
      canRenameCurrentAgent: ownsCurrentAgent && isNonEmptyString(route?.agentId) && !route.agentId.endsWith("-main"),
      ownsCurrentAgent,
      currentChannel,
      route
    };
  }

  async function renderPrivateMainMenu(chatKey, { notice, locale } = {}) {
    const S = uiStrings(locale);
    let currentChannel = null;
    try {
      const channelState = await resolveChannelStateForChatKey(chatKey);
      currentChannel = channelState?.currentChannel || null;
    } catch {
      currentChannel = null;
    }
    try {
      const route = await resolveRouteForChatKey(chatKey);
      const lines = [];
      lines.push(`<b>${escapeHtml(S.menu_title)}</b>`);
      if (isNonEmptyString(notice)) lines.push(`<i>${escapeHtml(notice)}</i>`);
      lines.push(`agent: ${htmlCode(route.agentId)}`);
      lines.push(`${escapeHtml(S.label_model)}: ${htmlCode(route.model)}`);
      if (currentChannel) lines.push(`${escapeHtml(S.label_channel)}: ${htmlCode(channelDisplayName(currentChannel))}`);
      lines.push(`session: ${htmlCode(route.sessionId)}`);
      const ownPrefix = isNonEmptyString(chatKey) ? `u${chatKey.trim()}-` : "";
      const canDelete = isNonEmptyString(ownPrefix)
        && isNonEmptyString(route?.agentId)
        && route.agentId.startsWith(ownPrefix);
      const canRename = canDelete && !route.agentId.endsWith("-main");
      const agentButtons = [
        cbButton(S.btn_switch_agent, { action: "p:switch", chatKey, page: 0 }),
        cbButton(S.btn_create_agent, { action: "p:create_begin", chatKey })
      ];
      if (canRename) {
        agentButtons.push(
          cbButton(S.btn_rename_agent, { action: "p:rename_begin", chatKey }),
          cbButton(S.btn_delete_agent, { action: "p:delete_begin", chatKey })
        );
      } else if (canDelete) {
        agentButtons.push(cbButton(S.btn_delete_agent, { action: "p:delete_begin", chatKey }));
      }

      const rows = buildTwoColumnMenuRows(
        agentButtons,
        [
          cbButton(S.btn_api_channels, { action: "p:channels", chatKey, page: 0 }),
          cbButton(S.btn_switch_model, { action: "p:model", chatKey }),
          cbButton(S.btn_new_main_thread, { action: "p:newmain", chatKey }),
          cbButton(S.btn_node_token, { action: "p:node", chatKey }),
          cbButton(S.btn_status, { action: "p:status", chatKey }),
          cbButton(S.btn_help, { action: "help", chatKey })
        ],
        cbButton(S.btn_close, { action: "close", chatKey })
      );
      const replyMarkup = kb(rows);
      return { text: lines.join("\n"), replyMarkup };
    } catch {
      const lines = [];
      lines.push(`<b>${escapeHtml(S.menu_title)}</b>`);
      if (isNonEmptyString(notice)) lines.push(`<i>${escapeHtml(notice)}</i>`);
      if (currentChannel) lines.push(`${escapeHtml(S.label_channel)}: ${htmlCode(channelDisplayName(currentChannel))}`);
      lines.push(escapeHtml(S.msg_not_initialized));
      const replyMarkup = kb(buildTwoColumnMenuRows(
        [
          cbButton(S.btn_create_agent, { action: "p:create_begin", chatKey }),
          cbButton(S.btn_api_channels, { action: "p:channels", chatKey, page: 0 })
        ],
        [cbButton(S.btn_help, { action: "help", chatKey })],
        cbButton(S.btn_close, { action: "close", chatKey })
      ));
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

  async function renderPrivateModelMenu(chatKey, { error, locale } = {}) {
    const S = uiStrings(locale);
    try {
      const data = await argusHttp.automationAgentList(chatKey);
      const currentAgentId = isNonEmptyString(data?.currentAgentId) ? data.currentAgentId : null;
      const agents = Array.isArray(data?.agents) ? data.agents : [];
      const availableModels = normalizeAvailableModels(data?.availableModels);
      const currentAgent = agents.find((agent) => isNonEmptyString(agent?.agentId) && agent.agentId === currentAgentId) || null;
      const currentModel = normalizeAgentModel(currentAgent?.model);

      const lines = [];
      lines.push(`<b>${escapeHtml(S.title_switch_model)}</b>`);
      lines.push(`${escapeHtml(S.label_current)}: ${htmlCode(currentAgentId || "(none)")}`);
      lines.push(`${escapeHtml(S.label_model)}: ${htmlCode(currentModel)}`);
      if (isNonEmptyString(error)) {
        lines.push("");
        lines.push(`<b>${escapeHtml(S.label_error)}:</b> ${escapeHtml(error)}`);
      }

      const rows = availableModels.map((model) => [
        cbButton(`${model === currentModel ? "✅ " : ""}${model}`, { action: "p:model_set", chatKey, agentId: currentAgentId, model })
      ]);
      rows.push([
        cbButton(S.btn_refresh, { action: "p:model", chatKey }),
        cbButton(S.btn_back, { action: "p:main", chatKey })
      ]);

      return { text: lines.join("\n"), replyMarkup: kb(rows) };
    } catch (e) {
      const lines = [];
      lines.push(`<b>${escapeHtml(S.title_switch_model)}</b>`);
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

  async function renderPrivateDeleteMenu(chatKey, agentId, { error, locale } = {}) {
    const S = uiStrings(locale);
    const lines = [];
    lines.push(`<b>${escapeHtml(S.title_delete_agent)}</b>`);
    if (isNonEmptyString(agentId)) lines.push(`${escapeHtml(S.label_current)}: ${htmlCode(agentId)}`);
    lines.push(escapeHtml(S.delete_warning));
    if (isNonEmptyString(error)) {
      lines.push("");
      lines.push(`<b>${escapeHtml(S.label_error)}:</b> ${escapeHtml(error)}`);
    }
    const replyMarkup = kb([
      [cbButton(S.btn_delete_confirm, { action: "p:delete_confirm", chatKey, agentId })],
      [cbButton(S.btn_cancel, { action: "p:delete_cancel", chatKey })]
    ]);
    return { text: lines.join("\n"), replyMarkup };
  }

  async function renderPrivateChannelListMenu(chatKey, pageRaw, { notice, error, locale } = {}) {
    const S = uiStrings(locale);
    const page = normalizePage(pageRaw);
    try {
      const { channels, currentChannelId, currentChannel } = await resolveChannelStateForChatKey(chatKey);
      const totalPages = Math.max(1, Math.ceil(channels.length / MENU_CHANNEL_PAGE_SIZE));
      const p = Math.min(page, totalPages - 1);
      const slice = channels.slice(p * MENU_CHANNEL_PAGE_SIZE, (p + 1) * MENU_CHANNEL_PAGE_SIZE);

      const lines = [];
      lines.push(`<b>${escapeHtml(S.title_api_channels)}</b>`);
      if (isNonEmptyString(notice)) lines.push(`<i>${escapeHtml(notice)}</i>`);
      lines.push(`${escapeHtml(S.label_current)}: ${htmlCode(channelDisplayName(currentChannel || { channelId: currentChannelId || "gateway" }))}`);
      lines.push(escapeHtml(S.channel_scope_all_agents));
      if (isNonEmptyString(error)) {
        lines.push("");
        lines.push(`<b>${escapeHtml(S.label_error)}:</b> ${escapeHtml(error)}`);
      }

      const rows = [];
      for (const channel of slice) {
        rows.push([
          cbButton(formatChannelLabel(channel, { currentChannelId }), {
            action: "p:channel_view",
            chatKey,
            channelId: channel?.channelId,
            page: p
          })
        ]);
      }

      const nav = [];
      if (p > 0) nav.push(cbButton(S.btn_prev, { action: "p:channels", chatKey, page: p - 1 }));
      if (p + 1 < totalPages) nav.push(cbButton(S.btn_next, { action: "p:channels", chatKey, page: p + 1 }));
      if (nav.length > 0) rows.push(nav);

      rows.push([
        cbButton(S.btn_add_channel, { action: "p:channel_create_begin", chatKey }),
        cbButton(S.btn_refresh, { action: "p:channels", chatKey, page: p })
      ]);
      rows.push([cbButton(S.btn_back, { action: "p:main", chatKey })]);
      return { text: lines.join("\n"), replyMarkup: kb(rows) };
    } catch (e) {
      const lines = [];
      lines.push(`<b>${escapeHtml(S.title_api_channels)}</b>`);
      lines.push(escapeHtml(formatGatewayErrorForUser(e, { locale })));
      return { text: lines.join("\n"), replyMarkup: kb([[cbButton(S.btn_back, { action: "p:main", chatKey })]]) };
    }
  }

  async function renderPrivateChannelDetailMenu(chatKey, channelId, pageRaw, { notice, error, locale } = {}) {
    const S = uiStrings(locale);
    const page = normalizePage(pageRaw);
    try {
      const { channels } = await resolveChannelStateForChatKey(chatKey);
      const channel = findChannelById(channels, channelId);
      if (!channel) throw new Error(S.msg_invalid_channel);

      const lines = [];
      lines.push(`<b>${escapeHtml(S.title_channel_detail)}</b>`);
      if (isNonEmptyString(notice)) lines.push(`<i>${escapeHtml(notice)}</i>`);
      lines.push(`${escapeHtml(S.label_channel)}: ${htmlCode(channelDisplayName(channel))}`);
      lines.push(`${escapeHtml(S.label_status)}: ${htmlCode(channelStatusText(channel, { locale }))}`);
      lines.push(`${escapeHtml(S.label_base_url)}: ${htmlCode(channel?.baseUrl || "(none)")}`);
      lines.push(`${escapeHtml(S.label_api_key)}: ${htmlCode(channelApiKeyText(channel, { locale }))}`);
      lines.push(`${escapeHtml(S.label_scope)}: ${escapeHtml(S.channel_scope_all_agents)}`);
      if (!channel?.ready && isNonEmptyString(channel?.reason)) {
        lines.push(`${escapeHtml(S.label_error)}: ${escapeHtml(channel.reason)}`);
      }
      if (channel?.channelId === "gateway") {
        lines.push(escapeHtml(S.channel_gateway_hint));
      } else if (channel?.channelId === "0-0.pro") {
        lines.push(escapeHtml(S.channel_promo_hint));
      }
      if (isNonEmptyString(error)) {
        lines.push("");
        lines.push(`<b>${escapeHtml(S.label_error)}:</b> ${escapeHtml(error)}`);
      }

      const rows = [];
      if (!channel?.selected && channel?.ready) {
        rows.push([cbButton(S.btn_select_channel, { action: "p:channel_select", chatKey, channelId: channel.channelId, page })]);
      }
      if (channel?.canSetKey) {
        const keyRow = [cbButton(S.btn_set_channel_key, { action: "p:channel_key_begin", chatKey, channelId: channel.channelId, page })];
        if (channel?.canClearKey) keyRow.push(cbButton(S.btn_clear_channel_key, { action: "p:channel_key_clear", chatKey, channelId: channel.channelId, page }));
        rows.push(keyRow);
      }
      if (channel?.canRename || channel?.canDelete) {
        const manageRow = [];
        if (channel?.canRename) manageRow.push(cbButton(S.btn_rename_channel, { action: "p:channel_rename_begin", chatKey, channelId: channel.channelId, page }));
        if (channel?.canDelete) manageRow.push(cbButton(S.btn_delete_channel, { action: "p:channel_delete_begin", chatKey, channelId: channel.channelId, page }));
        if (manageRow.length > 0) rows.push(manageRow);
      }
      if (isNonEmptyString(channel?.websiteUrl)) {
        rows.push([urlButton(S.btn_open_00pro, channel.websiteUrl)]);
      }
      rows.push([
        cbButton(S.btn_refresh, { action: "p:channel_view", chatKey, channelId: channel.channelId, page }),
        cbButton(S.btn_back, { action: "p:channels", chatKey, page })
      ]);
      return { text: lines.join("\n"), replyMarkup: kb(rows) };
    } catch (e) {
      return await renderPrivateChannelListMenu(chatKey, page, { error: formatGatewayErrorForUser(e, { locale }), locale });
    }
  }

  async function renderPrivateChannelCreateNameMenu(chatKey, { error, locale } = {}) {
    const S = uiStrings(locale);
    const lines = [];
    lines.push(`<b>${escapeHtml(S.title_create_channel)}</b>`);
    lines.push(`${escapeHtml(S.channel_create_name_prompt)} ${htmlCode("foo")}${locale === "zh" ? "。" : "."}`);
    if (isNonEmptyString(error)) {
      lines.push("");
      lines.push(`<b>${escapeHtml(S.label_error)}:</b> ${escapeHtml(error)}`);
    }
    return { text: lines.join("\n"), replyMarkup: kb([[cbButton(S.btn_cancel, { action: "p:channel_create_cancel", chatKey })]]) };
  }

  async function renderPrivateChannelCreateBaseUrlMenu(chatKey, channelName, { error, locale } = {}) {
    const S = uiStrings(locale);
    const lines = [];
    lines.push(`<b>${escapeHtml(S.title_create_channel)}</b>`);
    if (isNonEmptyString(channelName)) lines.push(`${escapeHtml(S.label_channel)}: ${htmlCode(channelName)}`);
    lines.push(`${escapeHtml(S.channel_create_base_url_prompt)} ${htmlCode("https://api.example.com/v1")}${locale === "zh" ? "。" : "."}`);
    if (isNonEmptyString(error)) {
      lines.push("");
      lines.push(`<b>${escapeHtml(S.label_error)}:</b> ${escapeHtml(error)}`);
    }
    return { text: lines.join("\n"), replyMarkup: kb([[cbButton(S.btn_cancel, { action: "p:channel_create_cancel", chatKey })]]) };
  }

  async function renderPrivateChannelCreateKeyMenu(chatKey, channelName, baseUrl, { error, locale } = {}) {
    const S = uiStrings(locale);
    const lines = [];
    lines.push(`<b>${escapeHtml(S.title_create_channel)}</b>`);
    if (isNonEmptyString(channelName)) lines.push(`${escapeHtml(S.label_channel)}: ${htmlCode(channelName)}`);
    if (isNonEmptyString(baseUrl)) lines.push(`${escapeHtml(S.label_base_url)}: ${htmlCode(baseUrl)}`);
    lines.push(escapeHtml(formatTemplate(S.channel_create_key_prompt, { name: channelName || "channel" })));
    if (isNonEmptyString(error)) {
      lines.push("");
      lines.push(`<b>${escapeHtml(S.label_error)}:</b> ${escapeHtml(error)}`);
    }
    return { text: lines.join("\n"), replyMarkup: kb([[cbButton(S.btn_cancel, { action: "p:channel_create_cancel", chatKey })]]) };
  }

  async function renderPrivateChannelRenameMenu(chatKey, channelId, pageRaw, { error, locale } = {}) {
    const S = uiStrings(locale);
    const page = normalizePage(pageRaw);
    const lines = [];
    lines.push(`<b>${escapeHtml(S.title_rename_channel)}</b>`);
    try {
      const { channels } = await resolveChannelStateForChatKey(chatKey);
      const channel = findChannelById(channels, channelId);
      if (channel) lines.push(`${escapeHtml(S.label_current)}: ${htmlCode(channelDisplayName(channel))}`);
    } catch {
      if (isNonEmptyString(channelId)) lines.push(`${escapeHtml(S.label_current)}: ${htmlCode(channelId)}`);
    }
    lines.push(`${escapeHtml(S.channel_rename_prompt_prefix)} ${htmlCode("foo")}${locale === "zh" ? "。" : "."}`);
    if (isNonEmptyString(error)) {
      lines.push("");
      lines.push(`<b>${escapeHtml(S.label_error)}:</b> ${escapeHtml(error)}`);
    }
    return { text: lines.join("\n"), replyMarkup: kb([[cbButton(S.btn_cancel, { action: "p:channel_rename_cancel", chatKey, channelId, page })]]) };
  }

  async function renderPrivateChannelDeleteMenu(chatKey, channelId, pageRaw, { error, locale } = {}) {
    const S = uiStrings(locale);
    const page = normalizePage(pageRaw);
    const lines = [];
    lines.push(`<b>${escapeHtml(S.title_delete_channel)}</b>`);
    try {
      const { channels } = await resolveChannelStateForChatKey(chatKey);
      const channel = findChannelById(channels, channelId);
      if (channel) lines.push(`${escapeHtml(S.label_current)}: ${htmlCode(channelDisplayName(channel))}`);
    } catch {
      if (isNonEmptyString(channelId)) lines.push(`${escapeHtml(S.label_current)}: ${htmlCode(channelId)}`);
    }
    lines.push(escapeHtml(S.channel_delete_warning));
    if (isNonEmptyString(error)) {
      lines.push("");
      lines.push(`<b>${escapeHtml(S.label_error)}:</b> ${escapeHtml(error)}`);
    }
    return {
      text: lines.join("\n"),
      replyMarkup: kb([
        [cbButton(S.btn_delete_confirm, { action: "p:channel_delete_confirm", chatKey, channelId, page })],
        [cbButton(S.btn_cancel, { action: "p:channel_delete_cancel", chatKey, channelId, page })]
      ])
    };
  }

  async function renderPrivateChannelKeyMenu(chatKey, channelId, pageRaw, { error, locale } = {}) {
    const S = uiStrings(locale);
    const page = normalizePage(pageRaw);
    const lines = [];
    lines.push(`<b>${escapeHtml(S.title_set_channel_key)}</b>`);
    try {
      const { channels } = await resolveChannelStateForChatKey(chatKey);
      const channel = findChannelById(channels, channelId);
      if (channel) lines.push(`${escapeHtml(S.label_channel)}: ${htmlCode(channelDisplayName(channel))}`);
      lines.push(escapeHtml(formatTemplate(S.channel_key_prompt_prefix, { name: channelDisplayName(channel) })));
    } catch {
      if (isNonEmptyString(channelId)) lines.push(`${escapeHtml(S.label_channel)}: ${htmlCode(channelId)}`);
      lines.push(escapeHtml(formatTemplate(S.channel_key_prompt_prefix, { name: channelId || "channel" })));
    }
    if (isNonEmptyString(error)) {
      lines.push("");
      lines.push(`<b>${escapeHtml(S.label_error)}:</b> ${escapeHtml(error)}`);
    }
    return { text: lines.join("\n"), replyMarkup: kb([[cbButton(S.btn_cancel, { action: "p:channel_key_cancel", chatKey, channelId, page })]]) };
  }

  async function renderPrivateStatusMenu(chatKey, { notice, error, locale } = {}) {
    const S = uiStrings(locale);
    const lines = [];
    lines.push(`<b>${escapeHtml(S.title_status)}</b>`);
    if (isNonEmptyString(notice)) lines.push(`<i>${escapeHtml(notice)}</i>`);
    try {
      const route = await resolveRouteForChatKey(chatKey);
      let tid = null;
      let currentChannel = null;
      try {
        const client = await getClient(route.sessionId);
        tid = await client.ensureMainThread();
      } catch {
        tid = null;
      }
      try {
        const channelState = await resolveChannelStateForChatKey(chatKey);
        currentChannel = channelState?.currentChannel || null;
      } catch {
        currentChannel = null;
      }
      lines.push(`agentId: ${htmlCode(route.agentId)}`);
      lines.push(`${escapeHtml(S.label_model)}: ${htmlCode(route.model)}`);
      if (currentChannel) lines.push(`${escapeHtml(S.label_channel)}: ${htmlCode(channelDisplayName(currentChannel))}`);
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

  async function renderPrivateNodeMenu(chatKey, { reveal = false, notice, error, locale } = {}) {
    const S = uiStrings(locale);
    const lines = [];
    const rows = [];
    lines.push(`<b>${escapeHtml(S.title_node_token)}</b>`);
    if (isNonEmptyString(notice)) lines.push(`<i>${escapeHtml(notice)}</i>`);

    try {
      const res = await argusHttp.automationNodeList(chatKey);
      const sid = isNonEmptyString(res?.sessionId) ? res.sessionId : null;
      const defaultNodeId = isNonEmptyString(res?.defaultNodeId) ? res.defaultNodeId : null;
      const defaultNodeConnected = Boolean(res?.defaultNodeConnected);
      const nodes = Array.isArray(res?.nodes) ? res.nodes : [];

      lines.push(`sessionId: ${htmlCode(sid || "(none)")}`);
      lines.push(`${escapeHtml(S.label_current)} ${escapeHtml(S.label_node)}: ${htmlCode(defaultNodeId || "(none)")}`);
      lines.push(`${escapeHtml(S.label_status)}: ${escapeHtml(defaultNodeConnected ? S.node_status_active : S.node_status_not_connected)}`);
      lines.push(escapeHtml(S.node_default_note));
      lines.push(escapeHtml(S.node_pause_hint));
      lines.push("");
      lines.push(`<b>${escapeHtml(S.node_connected_label)}</b>`);

      if (nodes.length) {
        for (const [index, node] of nodes.entries()) {
          const extra = [];
          if (isNonEmptyString(node?.platform)) extra.push(String(node.platform));
          if (isNonEmptyString(node?.version)) extra.push(`v${node.version}`);
          if (Array.isArray(node?.commands) && node.commands.length) extra.push(`${node.commands.length} cmds`);
          lines.push(`${index + 1}. ${escapeHtml(nodeDisplayName(node))}`);
          lines.push(`${escapeHtml(S.label_node)}: ${htmlCode(node?.nodeId || "(unknown)")}`);
          lines.push(`${escapeHtml(S.label_status)}: ${escapeHtml(nodeStatusText(node, { locale }))}${extra.length ? ` • ${escapeHtml(extra.join(" • "))}` : ""}`);
        }
      } else {
        lines.push(escapeHtml(S.node_none_connected));
      }

      for (const node of nodes) {
        if (!node?.canPause || !isNonEmptyString(node?.nodeId)) continue;
        const label = trimMenuLabel(nodeDisplayName(node));
        rows.push([
          cbButton(
            node?.paused ? `${S.btn_resume_node} · ${label}` : `${S.btn_pause_node} · ${label}`,
            { action: node?.paused ? "p:node_resume" : "p:node_pause", chatKey, nodeId: node.nodeId, reveal }
          )
        ]);
      }

      if (reveal) {
        lines.push("");
        lines.push(`<b>${escapeHtml(S.node_token_label)}</b>`);
        lines.push(escapeHtml(S.node_warning));
        try {
          const tokenRes = await argusHttp.automationNodeToken(chatKey);
          const token = isNonEmptyString(tokenRes?.token) ? tokenRes.token : null;
          const pathName = isNonEmptyString(tokenRes?.path) ? tokenRes.path : "/nodes/ws";
          const wsUrl = deriveNodeWsUrl({ httpBase: gatewayHttpUrl, pathName, token });
          lines.push(`path: ${htmlCode(pathName)}`);
          lines.push(`token:
<pre><code>${escapeHtml(token || "(none)")}</code></pre>`);
          if (wsUrl) {
            lines.push(`wsUrl:
<pre><code>${escapeHtml(wsUrl)}</code></pre>`);
          }
        } catch (e) {
          lines.push(escapeHtml(formatGatewayErrorForUser(e, { locale })));
        }
      } else {
        lines.push("");
        lines.push(escapeHtml(S.node_press_reveal));
      }
    } catch (e) {
      lines.push(escapeHtml(formatGatewayErrorForUser(e, { locale })));
    }

    if (isNonEmptyString(error)) {
      lines.push("");
      lines.push(`<b>${escapeHtml(S.label_error)}:</b> ${escapeHtml(error)}`);
    }

    rows.push([cbButton(S.btn_refresh, { action: reveal ? "p:node_reveal" : "p:node", chatKey })]);
    if (!reveal) rows.push([cbButton(S.btn_show_node_token, { action: "p:node_reveal", chatKey })]);
    rows.push([cbButton(S.btn_back, { action: "p:main", chatKey })]);

    return { text: lines.join("\n"), replyMarkup: kb(rows) };
  }

  async function renderGroupMainMenu(chatKey, { notice, locale, actorUserId } = {}) {
    const S = uiStrings(locale);
    let route = null;
    let mainThreadId = null;
    let ownsCurrentAgent = false;
    let canRenameCurrentAgent = false;
    let canDeleteCurrentAgent = false;
    let currentChannel = null;
    try {
      const ctx = await resolveGroupActorContext(chatKey, actorUserId);
      route = ctx.route;
      ownsCurrentAgent = Boolean(ctx.ownsCurrentAgent);
      canRenameCurrentAgent = Boolean(ctx.canRenameCurrentAgent);
      canDeleteCurrentAgent = Boolean(ctx.ownsCurrentAgent);
      currentChannel = ctx.currentChannel || null;
      if (isNonEmptyString(route?.sessionId)) {
        mainThreadId = await ensureSessionMainThread(route.sessionId);
      }
    } catch {
      route = null;
      mainThreadId = null;
      ownsCurrentAgent = false;
      canRenameCurrentAgent = false;
      canDeleteCurrentAgent = false;
      currentChannel = null;
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

    lines.push(`agent: ${htmlCode(route.agentId)}`);
    lines.push(`${escapeHtml(S.label_model)}: ${htmlCode(route.model)}`);
    if (currentChannel) lines.push(`${escapeHtml(S.label_channel)}: ${htmlCode(channelDisplayName(currentChannel))}`);
    lines.push(`session: ${htmlCode(route.sessionId)}`);
    lines.push(`mainThread: ${htmlCode(mainThreadId || "(none)")}`);

    const primaryButtons = [
      cbButton(S.btn_switch_agent, { action: "g:switch", chatKey, page: 0 }),
      cbButton(S.btn_create_agent, { action: "g:create_begin", chatKey })
    ];
    if (canRenameCurrentAgent) {
      primaryButtons.push(cbButton(S.btn_rename_agent, { action: "g:rename_begin", chatKey }));
    }
    if (canDeleteCurrentAgent) {
      primaryButtons.push(cbButton(S.btn_delete_agent, { action: "g:delete_begin", chatKey }));
    }

    const secondaryButtons = [];
    secondaryButtons.push(
      cbButton(S.btn_api_channels, { action: "g:channels", chatKey, page: 0 }),
      cbButton(S.btn_switch_model, { action: "g:model", chatKey }),
      cbButton(S.btn_new_main_thread, { action: "g:newmain", chatKey }),
      cbButton(S.btn_node_token, { action: "g:node", chatKey }),
      cbButton(S.btn_status, { action: "g:status", chatKey }),
      cbButton(S.btn_help, { action: "help", chatKey })
    );

    const replyMarkup = kb(buildTwoColumnMenuRows(
      primaryButtons,
      secondaryButtons,
      cbButton(S.btn_close, { action: "close", chatKey })
    ));
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
      const mainThreadId = await ensureSessionMainThread(route.sessionId);
      lines.push(`agentId: ${htmlCode(route.agentId)}`);
      lines.push(`${escapeHtml(S.label_model)}: ${htmlCode(route.model)}`);
      lines.push(`sessionId: ${htmlCode(route.sessionId)}`);
      lines.push(`mainThreadId: ${htmlCode(mainThreadId || "(none)")}`);
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
    lines.push(`<b>${escapeHtml(S.btn_switch_agent)}</b>`);
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

  async function renderGroupModelMenu(chatKey, actorUserId, { error, locale } = {}) {
    const S = uiStrings(locale);
    try {
      const ctx = await resolveGroupActorContext(chatKey, actorUserId);
      if (!ctx.ownsCurrentAgent || !isNonEmptyString(ctx.actorChatKey)) {
        return renderGroupOwnerOnlyView(chatKey, { error: S.msg_group_owner_only, locale });
      }
      const data = await argusHttp.automationAgentList(ctx.actorChatKey);
      const availableModels = normalizeAvailableModels(data?.availableModels);

      const lines = [];
      lines.push(`<b>${escapeHtml(S.title_switch_model)}</b>`);
      lines.push(`${escapeHtml(S.label_current)}: ${htmlCode(ctx.route.agentId || "(none)")}`);
      lines.push(`${escapeHtml(S.label_model)}: ${htmlCode(ctx.route.model || DEFAULT_AGENT_MODEL)}`);
      if (isNonEmptyString(error)) {
        lines.push("");
        lines.push(`<b>${escapeHtml(S.label_error)}:</b> ${escapeHtml(error)}`);
      }

      const rows = availableModels.map((model) => [
        cbButton(`${model === ctx.route.model ? "✅ " : ""}${model}`, { action: "g:model_set", chatKey, model })
      ]);
      rows.push([
        cbButton(S.btn_refresh, { action: "g:model", chatKey }),
        cbButton(S.btn_back, { action: "g:main", chatKey })
      ]);
      return { text: lines.join("\n"), replyMarkup: kb(rows) };
    } catch (e) {
      const lines = [];
      lines.push(`<b>${escapeHtml(S.title_switch_model)}</b>`);
      lines.push(escapeHtml(formatGatewayErrorForUser(e, { locale })));
      return { text: lines.join("\n"), replyMarkup: kb([[cbButton(S.btn_back, { action: "g:main", chatKey })]]) };
    }
  }

  async function renderGroupCreateMenu(chatKey, actorUserId, { error, locale } = {}) {
    const actorChatKey = privateChatKeyForUserId(actorUserId) || chatKey;
    const view = await renderPrivateCreateMenu(actorChatKey, { error, locale });
    return remapPrivateViewToGroup(view, chatKey);
  }

  async function renderGroupRenameMenu(chatKey, actorUserId, agentId, { error, locale } = {}) {
    try {
      const ctx = await resolveGroupActorContext(chatKey, actorUserId);
      if (!ctx.canRenameCurrentAgent || !isNonEmptyString(ctx.actorChatKey) || ctx.route.agentId !== agentId) {
        return renderGroupOwnerOnlyView(chatKey, { error: uiStrings(locale).msg_group_owner_only, locale });
      }
      const view = await renderPrivateRenameMenu(ctx.actorChatKey, agentId, { error, locale });
      return remapPrivateViewToGroup(view, chatKey);
    } catch (e) {
      return renderGroupOwnerOnlyView(chatKey, { error: formatGatewayErrorForUser(e, { locale }), locale });
    }
  }

  async function renderGroupDeleteMenu(chatKey, actorUserId, agentId, { error, locale } = {}) {
    try {
      const ctx = await resolveGroupActorContext(chatKey, actorUserId);
      if (!ctx.ownsCurrentAgent || !isNonEmptyString(ctx.actorChatKey) || ctx.route.agentId !== agentId) {
        return renderGroupOwnerOnlyView(chatKey, { error: uiStrings(locale).msg_group_owner_only, locale });
      }
      const view = await renderPrivateDeleteMenu(ctx.actorChatKey, agentId, { error, locale });
      return remapPrivateViewToGroup(view, chatKey);
    } catch (e) {
      return renderGroupOwnerOnlyView(chatKey, { error: formatGatewayErrorForUser(e, { locale }), locale });
    }
  }

  async function renderGroupNodeMenu(chatKey, actorUserId, { reveal = false, notice, error, locale } = {}) {
    try {
      const ctx = await resolveGroupActorContext(chatKey, actorUserId);
      if (!ctx.ownsCurrentAgent) {
        return renderGroupOwnerOnlyView(chatKey, { error: uiStrings(locale).msg_group_owner_only, locale });
      }
      const view = await renderPrivateNodeMenu(chatKey, { reveal, notice, error, locale });
      return remapPrivateViewToGroup(view, chatKey);
    } catch (e) {
      return renderGroupOwnerOnlyView(chatKey, { error: formatGatewayErrorForUser(e, { locale }), locale });
    }
  }

  async function renderGroupChannelListMenu(chatKey, actorUserId, pageRaw, opts = {}) {
    try {
      const ctx = await resolveGroupActorContext(chatKey, actorUserId);
      if (!ctx.ownsCurrentAgent || !isNonEmptyString(ctx.actorChatKey)) {
        return renderGroupOwnerOnlyView(chatKey, { error: uiStrings(opts.locale).msg_group_owner_only, locale: opts.locale });
      }
      const view = await renderPrivateChannelListMenu(ctx.actorChatKey, pageRaw, opts);
      return remapPrivateViewToGroup(view, chatKey);
    } catch (e) {
      return renderGroupOwnerOnlyView(chatKey, { error: formatGatewayErrorForUser(e, { locale: opts.locale }), locale: opts.locale });
    }
  }

  async function renderGroupChannelDetailMenu(chatKey, actorUserId, channelId, pageRaw, opts = {}) {
    try {
      const ctx = await resolveGroupActorContext(chatKey, actorUserId);
      if (!ctx.ownsCurrentAgent || !isNonEmptyString(ctx.actorChatKey)) {
        return renderGroupOwnerOnlyView(chatKey, { error: uiStrings(opts.locale).msg_group_owner_only, locale: opts.locale });
      }
      const view = await renderPrivateChannelDetailMenu(ctx.actorChatKey, channelId, pageRaw, opts);
      return remapPrivateViewToGroup(view, chatKey);
    } catch (e) {
      return renderGroupOwnerOnlyView(chatKey, { error: formatGatewayErrorForUser(e, { locale: opts.locale }), locale: opts.locale });
    }
  }

  async function renderGroupChannelCreateNameMenu(chatKey, actorUserId, opts = {}) {
    try {
      const ctx = await resolveGroupActorContext(chatKey, actorUserId);
      if (!ctx.ownsCurrentAgent || !isNonEmptyString(ctx.actorChatKey)) {
        return renderGroupOwnerOnlyView(chatKey, { error: uiStrings(opts.locale).msg_group_owner_only, locale: opts.locale });
      }
      const view = await renderPrivateChannelCreateNameMenu(ctx.actorChatKey, opts);
      return remapPrivateViewToGroup(view, chatKey);
    } catch (e) {
      return renderGroupOwnerOnlyView(chatKey, { error: formatGatewayErrorForUser(e, { locale: opts.locale }), locale: opts.locale });
    }
  }

  async function renderGroupChannelCreateBaseUrlMenu(chatKey, actorUserId, channelName, opts = {}) {
    try {
      const ctx = await resolveGroupActorContext(chatKey, actorUserId);
      if (!ctx.ownsCurrentAgent || !isNonEmptyString(ctx.actorChatKey)) {
        return renderGroupOwnerOnlyView(chatKey, { error: uiStrings(opts.locale).msg_group_owner_only, locale: opts.locale });
      }
      const view = await renderPrivateChannelCreateBaseUrlMenu(ctx.actorChatKey, channelName, opts);
      return remapPrivateViewToGroup(view, chatKey);
    } catch (e) {
      return renderGroupOwnerOnlyView(chatKey, { error: formatGatewayErrorForUser(e, { locale: opts.locale }), locale: opts.locale });
    }
  }

  async function renderGroupChannelCreateKeyMenu(chatKey, actorUserId, channelName, baseUrl, opts = {}) {
    try {
      const ctx = await resolveGroupActorContext(chatKey, actorUserId);
      if (!ctx.ownsCurrentAgent || !isNonEmptyString(ctx.actorChatKey)) {
        return renderGroupOwnerOnlyView(chatKey, { error: uiStrings(opts.locale).msg_group_owner_only, locale: opts.locale });
      }
      const view = await renderPrivateChannelCreateKeyMenu(ctx.actorChatKey, channelName, baseUrl, opts);
      return remapPrivateViewToGroup(view, chatKey);
    } catch (e) {
      return renderGroupOwnerOnlyView(chatKey, { error: formatGatewayErrorForUser(e, { locale: opts.locale }), locale: opts.locale });
    }
  }

  async function renderGroupChannelRenameMenu(chatKey, actorUserId, channelId, pageRaw, opts = {}) {
    try {
      const ctx = await resolveGroupActorContext(chatKey, actorUserId);
      if (!ctx.ownsCurrentAgent || !isNonEmptyString(ctx.actorChatKey)) {
        return renderGroupOwnerOnlyView(chatKey, { error: uiStrings(opts.locale).msg_group_owner_only, locale: opts.locale });
      }
      const view = await renderPrivateChannelRenameMenu(ctx.actorChatKey, channelId, pageRaw, opts);
      return remapPrivateViewToGroup(view, chatKey);
    } catch (e) {
      return renderGroupOwnerOnlyView(chatKey, { error: formatGatewayErrorForUser(e, { locale: opts.locale }), locale: opts.locale });
    }
  }

  async function renderGroupChannelDeleteMenu(chatKey, actorUserId, channelId, pageRaw, opts = {}) {
    try {
      const ctx = await resolveGroupActorContext(chatKey, actorUserId);
      if (!ctx.ownsCurrentAgent || !isNonEmptyString(ctx.actorChatKey)) {
        return renderGroupOwnerOnlyView(chatKey, { error: uiStrings(opts.locale).msg_group_owner_only, locale: opts.locale });
      }
      const view = await renderPrivateChannelDeleteMenu(ctx.actorChatKey, channelId, pageRaw, opts);
      return remapPrivateViewToGroup(view, chatKey);
    } catch (e) {
      return renderGroupOwnerOnlyView(chatKey, { error: formatGatewayErrorForUser(e, { locale: opts.locale }), locale: opts.locale });
    }
  }

  async function renderGroupChannelKeyMenu(chatKey, actorUserId, channelId, pageRaw, opts = {}) {
    try {
      const ctx = await resolveGroupActorContext(chatKey, actorUserId);
      if (!ctx.ownsCurrentAgent || !isNonEmptyString(ctx.actorChatKey)) {
        return renderGroupOwnerOnlyView(chatKey, { error: uiStrings(opts.locale).msg_group_owner_only, locale: opts.locale });
      }
      const view = await renderPrivateChannelKeyMenu(ctx.actorChatKey, channelId, pageRaw, opts);
      return remapPrivateViewToGroup(view, chatKey);
    } catch (e) {
      return renderGroupOwnerOnlyView(chatKey, { error: formatGatewayErrorForUser(e, { locale: opts.locale }), locale: opts.locale });
    }
  }

  async function renderHelpMenu(chatKey, { forGroup = false, locale } = {}) {
    const S = uiStrings(locale);
    const lines = [];
    lines.push(`<b>${escapeHtml(S.title_help)}</b>`);
    lines.push(`- ${formatTemplate(S.help_line_menu, { menu: htmlCode("/menu") })}`);
    lines.push(`- ${formatTemplate(S.help_line_cancel, { cancel: htmlCode("/cancel") })}`);
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

  async function sendMenuMessage({ target, chatKey, chatType, notice, locale, actorUserId } = {}) {
    if (!target || !isNonEmptyString(chatKey)) return null;
    clearPendingAgentInput(chatKey, isGroupChatType(chatType) ? actorUserId : undefined);
    const view = isPrivateChatType(chatType)
      ? await renderPrivateMainMenu(chatKey, { notice, locale })
      : await renderGroupMainMenu(chatKey, { notice, locale, actorUserId });
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
    const actorUserId = Number.isFinite(fromId) ? Math.trunc(fromId) : null;
    const clearPendingForActor = () => clearPendingAgentInput(chatKey, isGroupChatType(chatType) ? actorUserId : undefined);

    if (!isNonEmptyString(chatKey) || !Number.isFinite(chatId) || !Number.isFinite(messageId)) {
      await safeAnswerCallbackQuery(cbId, S0.msg_unsupported_cb);
      return;
    }

    const locale = localeForChatKey(chatKey, callbackQuery?.from?.language_code);
    const S = uiStrings(locale);
    const getOwnedGroupActorContext = async ({ requireRenameable = false } = {}) => {
      try {
        const ctx = await resolveGroupActorContext(chatKey, actorUserId);
        if (!ctx.ownsCurrentAgent || !isNonEmptyString(ctx.actorChatKey) || (requireRenameable && !ctx.canRenameCurrentAgent)) {
          return { ok: false, error: S.msg_group_owner_only, ctx: null };
        }
        return { ok: true, error: null, ctx };
      } catch (e) {
        return { ok: false, error: formatGatewayErrorForUser(e, { locale }), ctx: null };
      }
    };

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
        clearPendingForActor();
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

      if (action === "p:model") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const view = await renderPrivateModelMenu(chatKey, { locale });
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

      if (action === "p:model_set") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const agentId = payload.agentId;
        const model = payload.model;
        if (!isNonEmptyString(agentId) || !isNonEmptyString(model)) {
          const view = await renderPrivateModelMenu(chatKey, { error: S.msg_invalid_model, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        await argusHttp.automationAgentSetModel(chatKey, agentId, model);
        const notice = formatTemplate(S.notice_model_switched, { model });
        const view = await renderPrivateMainMenu(chatKey, { notice, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:channels") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const view = await renderPrivateChannelListMenu(chatKey, payload.page, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:channel_view") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderPrivateChannelListMenu(chatKey, payload.page, { error: S.msg_invalid_channel, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const view = await renderPrivateChannelDetailMenu(chatKey, channelId, payload.page, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:channel_select") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderPrivateChannelListMenu(chatKey, payload.page, { error: S.msg_invalid_channel, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        try {
          const selected = await argusHttp.automationChannelSelect(chatKey, channelId);
          const currentChannel = selected?.currentChannel && typeof selected.currentChannel === "object"
            ? selected.currentChannel
            : { channelId };
          const notice = formatTemplate(S.notice_channel_switched, { name: channelDisplayName(currentChannel) });
          const view = await renderPrivateMainMenu(chatKey, { notice, locale });
          await editMenuMessage({ chatId, messageId, view });
        } catch (e) {
          const view = await renderPrivateChannelDetailMenu(chatKey, channelId, payload.page, { error: formatGatewayErrorForUser(e, { locale }), locale });
          await editMenuMessage({ chatId, messageId, view });
        }
        return;
      }

      if (action === "p:channel_create_begin") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        setPendingCreateChannelName(chatKey, chatId, messageId);
        const view = await renderPrivateChannelCreateNameMenu(chatKey, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:channel_create_cancel") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const view = await renderPrivateChannelListMenu(chatKey, 0, { notice: S.notice_canceled, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:channel_rename_begin") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderPrivateChannelListMenu(chatKey, payload.page, { error: S.msg_invalid_channel, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        setPendingRenameChannel(chatKey, chatId, messageId, channelId, payload.page);
        const view = await renderPrivateChannelRenameMenu(chatKey, channelId, payload.page, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:channel_rename_cancel") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderPrivateChannelListMenu(chatKey, payload.page, { notice: S.notice_canceled, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const view = await renderPrivateChannelDetailMenu(chatKey, channelId, payload.page, { notice: S.notice_canceled, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:channel_delete_begin") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderPrivateChannelListMenu(chatKey, payload.page, { error: S.msg_invalid_channel, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const view = await renderPrivateChannelDeleteMenu(chatKey, channelId, payload.page, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:channel_delete_cancel") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderPrivateChannelListMenu(chatKey, payload.page, { notice: S.notice_canceled, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const view = await renderPrivateChannelDetailMenu(chatKey, channelId, payload.page, { notice: S.notice_canceled, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:channel_delete_confirm") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderPrivateChannelListMenu(chatKey, payload.page, { error: S.msg_invalid_channel, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        try {
          const result = await argusHttp.automationChannelDelete(chatKey, channelId);
          const deletedName = isNonEmptyString(result?.deletedName) ? result.deletedName : channelId;
          const notice = formatTemplate(S.notice_channel_deleted, { name: deletedName });
          const view = await renderPrivateChannelListMenu(chatKey, payload.page, { notice, locale });
          await editMenuMessage({ chatId, messageId, view });
        } catch (e) {
          const view = await renderPrivateChannelDeleteMenu(chatKey, channelId, payload.page, { error: formatGatewayErrorForUser(e, { locale }), locale });
          await editMenuMessage({ chatId, messageId, view });
        }
        return;
      }

      if (action === "p:channel_key_begin") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderPrivateChannelListMenu(chatKey, payload.page, { error: S.msg_invalid_channel, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        setPendingChannelKey(chatKey, chatId, messageId, channelId, payload.page);
        const view = await renderPrivateChannelKeyMenu(chatKey, channelId, payload.page, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:channel_key_cancel") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderPrivateChannelListMenu(chatKey, payload.page, { notice: S.notice_canceled, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const view = await renderPrivateChannelDetailMenu(chatKey, channelId, payload.page, { notice: S.notice_canceled, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:channel_key_clear") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderPrivateChannelListMenu(chatKey, payload.page, { error: S.msg_invalid_channel, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        try {
          const result = await argusHttp.automationChannelKeyClear(chatKey, channelId);
          const channel = result?.channel && typeof result.channel === "object" ? result.channel : { channelId };
          const notice = formatTemplate(S.notice_channel_key_cleared, { name: channelDisplayName(channel) });
          const view = await renderPrivateChannelDetailMenu(chatKey, channelId, payload.page, { notice, locale });
          await editMenuMessage({ chatId, messageId, view });
        } catch (e) {
          const view = await renderPrivateChannelDetailMenu(chatKey, channelId, payload.page, { error: formatGatewayErrorForUser(e, { locale }), locale });
          await editMenuMessage({ chatId, messageId, view });
        }
        return;
      }

      if (action === "p:create_begin") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        try {
          await resolveRouteForChatKey(chatKey);
        } catch (e) {
          const errMsg = formatGatewayErrorForUser(e, { locale });
          if (errMsg !== S.err_not_initialized) {
            const view = await renderPrivateMainMenu(chatKey, { notice: errMsg, locale });
            await editMenuMessage({ chatId, messageId, view });
            return;
          }
          try {
            const boot = await argusHttp.automationUserBootstrap(chatKey);
            const currentSessionId = isNonEmptyString(boot?.currentSessionId) ? boot.currentSessionId : null;
            if (currentSessionId) await getClient(currentSessionId);
            const notice = Boolean(boot?.createdMain) ? S.notice_initialized_created : S.notice_initialized_exists;
            const view = await renderPrivateMainMenu(chatKey, { notice, locale });
            await editMenuMessage({ chatId, messageId, view });
            return;
          } catch (e2) {
            const view = await renderPrivateMainMenu(chatKey, { notice: formatGatewayErrorForUser(e2, { locale }), locale });
            await editMenuMessage({ chatId, messageId, view });
            return;
          }
        }
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

      if (action === "p:delete_begin") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        let route = null;
        try {
          route = await resolveRouteForChatKey(chatKey);
        } catch (e) {
          const view = await renderPrivateMainMenu(chatKey, { notice: formatGatewayErrorForUser(e, { locale }), locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const ownPrefix = isNonEmptyString(chatKey) ? `u${chatKey.trim()}-` : "";
        const canDelete = isNonEmptyString(ownPrefix)
          && isNonEmptyString(route?.agentId)
          && route.agentId.startsWith(ownPrefix);
        if (!canDelete) {
          const view = await renderPrivateMainMenu(chatKey, { notice: S.msg_invalid_agent, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const view = await renderPrivateDeleteMenu(chatKey, route.agentId, { locale });
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

      if (action === "p:delete_cancel") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const view = await renderPrivateMainMenu(chatKey, { notice: S.notice_canceled, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:delete_confirm") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const agentId = payload.agentId;
        if (!isNonEmptyString(agentId)) {
          const view = await renderPrivateMainMenu(chatKey, { notice: S.msg_invalid_agent, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        try {
          await argusHttp.automationAgentDelete(chatKey, agentId);
        } catch (e) {
          const view = await renderPrivateDeleteMenu(chatKey, agentId, { error: formatGatewayErrorForUser(e, { locale }), locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const notice = formatTemplate(S.notice_deleted, { agentId });
        const view = await renderPrivateMainMenu(chatKey, { notice, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "p:newmain") {
        await answerOnce();
        clearPendingAgentInput(chatKey);
        const route = await resolveRouteForChatKey(chatKey);
        const client = await getClient(route.sessionId);
        const tid = await client.startThread(route.model);
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

      if (action === "p:node_pause" || action === "p:node_resume") {
        await answerOnce();
        const nodeId = payload.nodeId;
        const reveal = Boolean(payload.reveal);
        if (!isNonEmptyString(nodeId)) {
          const view = await renderPrivateNodeMenu(chatKey, { reveal, error: S.msg_unsupported_cb, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        try {
          if (action === "p:node_pause") {
            await argusHttp.automationNodePause(chatKey, nodeId);
          } else {
            await argusHttp.automationNodeResume(chatKey, nodeId);
          }
        } catch (e) {
          const view = await renderPrivateNodeMenu(chatKey, {
            reveal,
            error: formatGatewayErrorForUser(e, { locale }),
            locale
          });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const notice = formatTemplate(
          action === "p:node_pause" ? S.notice_node_paused : S.notice_node_resumed,
          { nodeId }
        );
        const view = await renderPrivateNodeMenu(chatKey, { reveal, notice, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:main") {
        await answerOnce();
        const view = await renderGroupMainMenu(chatKey, { locale, actorUserId });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:status") {
        await answerOnce();
        const view = await renderGroupStatusMenu(chatKey, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:create_begin") {
        const ok = await isChatAdmin(chatId, fromId);
        if (!ok) {
          await answerOnce(S.msg_admins_only);
          return;
        }
        await answerOnce();
        clearPendingForActor();
        const actorPrivateChatKey = privateChatKeyForUserId(actorUserId);
        if (!isNonEmptyString(actorPrivateChatKey)) {
          const view = await renderGroupMainMenu(chatKey, { notice: S.msg_group_owner_only, locale, actorUserId });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        try {
          const boot = await argusHttp.automationUserBootstrap(actorPrivateChatKey);
          const currentSessionId = isNonEmptyString(boot?.currentSessionId) ? boot.currentSessionId : null;
          if (currentSessionId) await getClient(currentSessionId);
        } catch (e) {
          const view = await renderGroupMainMenu(chatKey, { notice: formatGatewayErrorForUser(e, { locale }), locale, actorUserId });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        setPendingCreateAgent(chatKey, chatId, messageId, actorUserId);
        const view = await renderGroupCreateMenu(chatKey, actorUserId, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:create_cancel") {
        await answerOnce();
        clearPendingForActor();
        const view = await renderGroupMainMenu(chatKey, { notice: S.notice_canceled, locale, actorUserId });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:model") {
        await answerOnce();
        const view = await renderGroupModelMenu(chatKey, actorUserId, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:model_set") {
        await answerOnce();
        const model = payload.model;
        if (!isNonEmptyString(model) || !AVAILABLE_AGENT_MODELS.includes(model)) {
          const view = await renderGroupModelMenu(chatKey, actorUserId, { error: S.msg_invalid_model, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const owned = await getOwnedGroupActorContext();
        if (!owned.ok) {
          const view = await renderGroupModelMenu(chatKey, actorUserId, { error: owned.error, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        try {
          await argusHttp.automationAgentSetModel(owned.ctx.actorChatKey, owned.ctx.route.agentId, model);
        } catch (e) {
          const view = await renderGroupModelMenu(chatKey, actorUserId, { error: formatGatewayErrorForUser(e, { locale }), locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const notice = formatTemplate(S.notice_model_switched, { model });
        const view = await renderGroupMainMenu(chatKey, { notice, locale, actorUserId });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:rename_begin") {
        await answerOnce();
        clearPendingForActor();
        const owned = await getOwnedGroupActorContext({ requireRenameable: true });
        if (!owned.ok) {
          const view = await renderGroupMainMenu(chatKey, { notice: owned.error, locale, actorUserId });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        setPendingRenameAgent(chatKey, chatId, messageId, owned.ctx.route.agentId, actorUserId);
        const view = await renderGroupRenameMenu(chatKey, actorUserId, owned.ctx.route.agentId, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:rename_cancel") {
        await answerOnce();
        clearPendingForActor();
        const view = await renderGroupMainMenu(chatKey, { notice: S.notice_canceled, locale, actorUserId });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:delete_begin") {
        const ok = await isChatAdmin(chatId, fromId);
        if (!ok) {
          await answerOnce(S.msg_admins_only);
          return;
        }
        await answerOnce();
        clearPendingForActor();
        const owned = await getOwnedGroupActorContext();
        if (!owned.ok) {
          const view = await renderGroupMainMenu(chatKey, { notice: owned.error, locale, actorUserId });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const view = await renderGroupDeleteMenu(chatKey, actorUserId, owned.ctx.route.agentId, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:delete_cancel") {
        await answerOnce();
        clearPendingForActor();
        const view = await renderGroupMainMenu(chatKey, { notice: S.notice_canceled, locale, actorUserId });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:delete_confirm") {
        const ok = await isChatAdmin(chatId, fromId);
        if (!ok) {
          await answerOnce(S.msg_admins_only);
          return;
        }
        await answerOnce();
        clearPendingForActor();
        const agentId = payload.agentId;
        if (!isNonEmptyString(agentId)) {
          const view = await renderGroupMainMenu(chatKey, { notice: S.msg_invalid_agent, locale, actorUserId });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const owned = await getOwnedGroupActorContext();
        if (!owned.ok || owned.ctx.route.agentId !== agentId) {
          const view = await renderGroupDeleteMenu(chatKey, actorUserId, agentId, { error: owned.ok ? S.msg_invalid_agent : owned.error, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        try {
          await argusHttp.automationAgentDelete(owned.ctx.actorChatKey, agentId);
        } catch (e) {
          const view = await renderGroupDeleteMenu(chatKey, actorUserId, agentId, { error: formatGatewayErrorForUser(e, { locale }), locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const notice = formatTemplate(S.notice_deleted, { agentId });
        const view = await renderGroupMainMenu(chatKey, { notice, locale, actorUserId });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:channels") {
        await answerOnce();
        clearPendingForActor();
        const view = await renderGroupChannelListMenu(chatKey, actorUserId, payload.page, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:channel_view") {
        await answerOnce();
        clearPendingForActor();
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderGroupChannelListMenu(chatKey, actorUserId, payload.page, { error: S.msg_invalid_channel, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const view = await renderGroupChannelDetailMenu(chatKey, actorUserId, channelId, payload.page, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:channel_select") {
        await answerOnce();
        clearPendingForActor();
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderGroupChannelListMenu(chatKey, actorUserId, payload.page, { error: S.msg_invalid_channel, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const owned = await getOwnedGroupActorContext();
        if (!owned.ok) {
          const view = await renderGroupChannelDetailMenu(chatKey, actorUserId, channelId, payload.page, { error: owned.error, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        try {
          const result = await argusHttp.automationChannelSelect(owned.ctx.actorChatKey, channelId);
          const channel = result?.currentChannel && typeof result.currentChannel === "object" ? result.currentChannel : { channelId };
          const notice = formatTemplate(S.notice_channel_switched, { name: channelDisplayName(channel) });
          const view = await renderGroupMainMenu(chatKey, { notice, locale, actorUserId });
          await editMenuMessage({ chatId, messageId, view });
        } catch (e) {
          const view = await renderGroupChannelDetailMenu(chatKey, actorUserId, channelId, payload.page, { error: formatGatewayErrorForUser(e, { locale }), locale });
          await editMenuMessage({ chatId, messageId, view });
        }
        return;
      }

      if (action === "g:channel_create_begin") {
        await answerOnce();
        clearPendingForActor();
        const owned = await getOwnedGroupActorContext();
        if (!owned.ok) {
          const view = await renderGroupChannelListMenu(chatKey, actorUserId, 0, { error: owned.error, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        setPendingCreateChannelName(chatKey, chatId, messageId, actorUserId);
        const view = await renderGroupChannelCreateNameMenu(chatKey, actorUserId, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:channel_create_cancel") {
        await answerOnce();
        clearPendingForActor();
        const view = await renderGroupChannelListMenu(chatKey, actorUserId, 0, { notice: S.notice_canceled, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:channel_rename_begin") {
        await answerOnce();
        clearPendingForActor();
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderGroupChannelListMenu(chatKey, actorUserId, payload.page, { error: S.msg_invalid_channel, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const owned = await getOwnedGroupActorContext();
        if (!owned.ok) {
          const view = await renderGroupChannelDetailMenu(chatKey, actorUserId, channelId, payload.page, { error: owned.error, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        setPendingRenameChannel(chatKey, chatId, messageId, channelId, payload.page, actorUserId);
        const view = await renderGroupChannelRenameMenu(chatKey, actorUserId, channelId, payload.page, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:channel_rename_cancel") {
        await answerOnce();
        clearPendingForActor();
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderGroupChannelListMenu(chatKey, actorUserId, payload.page, { notice: S.notice_canceled, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const view = await renderGroupChannelDetailMenu(chatKey, actorUserId, channelId, payload.page, { notice: S.notice_canceled, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:channel_delete_begin") {
        await answerOnce();
        clearPendingForActor();
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderGroupChannelListMenu(chatKey, actorUserId, payload.page, { error: S.msg_invalid_channel, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const view = await renderGroupChannelDeleteMenu(chatKey, actorUserId, channelId, payload.page, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:channel_delete_cancel") {
        await answerOnce();
        clearPendingForActor();
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderGroupChannelListMenu(chatKey, actorUserId, payload.page, { notice: S.notice_canceled, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const view = await renderGroupChannelDetailMenu(chatKey, actorUserId, channelId, payload.page, { notice: S.notice_canceled, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:channel_delete_confirm") {
        await answerOnce();
        clearPendingForActor();
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderGroupChannelListMenu(chatKey, actorUserId, payload.page, { error: S.msg_invalid_channel, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const owned = await getOwnedGroupActorContext();
        if (!owned.ok) {
          const view = await renderGroupChannelDeleteMenu(chatKey, actorUserId, channelId, payload.page, { error: owned.error, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        try {
          const result = await argusHttp.automationChannelDelete(owned.ctx.actorChatKey, channelId);
          const deletedName = isNonEmptyString(result?.deletedName) ? result.deletedName : channelId;
          const notice = formatTemplate(S.notice_channel_deleted, { name: deletedName });
          const view = await renderGroupChannelListMenu(chatKey, actorUserId, payload.page, { notice, locale });
          await editMenuMessage({ chatId, messageId, view });
        } catch (e) {
          const view = await renderGroupChannelDeleteMenu(chatKey, actorUserId, channelId, payload.page, { error: formatGatewayErrorForUser(e, { locale }), locale });
          await editMenuMessage({ chatId, messageId, view });
        }
        return;
      }

      if (action === "g:channel_key_begin") {
        await answerOnce();
        clearPendingForActor();
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderGroupChannelListMenu(chatKey, actorUserId, payload.page, { error: S.msg_invalid_channel, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const owned = await getOwnedGroupActorContext();
        if (!owned.ok) {
          const view = await renderGroupChannelDetailMenu(chatKey, actorUserId, channelId, payload.page, { error: owned.error, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        setPendingChannelKey(chatKey, chatId, messageId, channelId, payload.page, actorUserId);
        const view = await renderGroupChannelKeyMenu(chatKey, actorUserId, channelId, payload.page, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:channel_key_cancel") {
        await answerOnce();
        clearPendingForActor();
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderGroupChannelListMenu(chatKey, actorUserId, payload.page, { notice: S.notice_canceled, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const view = await renderGroupChannelDetailMenu(chatKey, actorUserId, channelId, payload.page, { notice: S.notice_canceled, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:channel_key_clear") {
        await answerOnce();
        clearPendingForActor();
        const channelId = payload.channelId;
        if (!isNonEmptyString(channelId)) {
          const view = await renderGroupChannelListMenu(chatKey, actorUserId, payload.page, { error: S.msg_invalid_channel, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const owned = await getOwnedGroupActorContext();
        if (!owned.ok) {
          const view = await renderGroupChannelDetailMenu(chatKey, actorUserId, channelId, payload.page, { error: owned.error, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        try {
          const result = await argusHttp.automationChannelKeyClear(owned.ctx.actorChatKey, channelId);
          const channel = result?.channel && typeof result.channel === "object" ? result.channel : { channelId };
          const notice = formatTemplate(S.notice_channel_key_cleared, { name: channelDisplayName(channel) });
          const view = await renderGroupChannelDetailMenu(chatKey, actorUserId, channelId, payload.page, { notice, locale });
          await editMenuMessage({ chatId, messageId, view });
        } catch (e) {
          const view = await renderGroupChannelDetailMenu(chatKey, actorUserId, channelId, payload.page, { error: formatGatewayErrorForUser(e, { locale }), locale });
          await editMenuMessage({ chatId, messageId, view });
        }
        return;
      }

      if (action === "g:new" || action === "g:newmain") {
        await answerOnce();
        let route = null;
        try {
          route = await resolveRouteForChatKey(chatKey);
        } catch (e) {
          const view = await renderGroupMainMenu(chatKey, { notice: formatGatewayErrorForUser(e, { locale }), locale, actorUserId });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }

        const client = await getClient(route.sessionId);
        const tid = await client.startThread(route.model);
        await client.setMainThread(tid);
        const notice = formatTemplate(S.notice_new_thread, { threadId: tid });
        const view = await renderGroupMainMenu(chatKey, { notice, locale, actorUserId });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:node") {
        await answerOnce();
        const view = await renderGroupNodeMenu(chatKey, actorUserId, { reveal: false, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:node_reveal") {
        await answerOnce();
        const view = await renderGroupNodeMenu(chatKey, actorUserId, { reveal: true, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:node_pause" || action === "g:node_resume") {
        await answerOnce();
        const nodeId = payload.nodeId;
        const reveal = Boolean(payload.reveal);
        if (!isNonEmptyString(nodeId)) {
          const view = await renderGroupNodeMenu(chatKey, actorUserId, { reveal, error: S.msg_unsupported_cb, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const owned = await getOwnedGroupActorContext();
        if (!owned.ok) {
          const view = await renderGroupNodeMenu(chatKey, actorUserId, { reveal, error: owned.error, locale });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        try {
          if (action === "g:node_pause") {
            await argusHttp.automationNodePause(chatKey, nodeId);
          } else {
            await argusHttp.automationNodeResume(chatKey, nodeId);
          }
        } catch (e) {
          const view = await renderGroupNodeMenu(chatKey, actorUserId, {
            reveal,
            error: formatGatewayErrorForUser(e, { locale }),
            locale
          });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const notice = formatTemplate(
          action === "g:node_pause" ? S.notice_node_paused : S.notice_node_resumed,
          { nodeId }
        );
        const view = await renderGroupNodeMenu(chatKey, actorUserId, { reveal, notice, locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:switch" || action === "g:bind") {
        const ok = await isChatAdmin(chatId, fromId);
        if (!ok) {
          await answerOnce(S.msg_admins_only);
          return;
        }
        await answerOnce();
        const view = await renderGroupBindMenu(chatKey, actorUserId, payload.page, { locale });
        await editMenuMessage({ chatId, messageId, view });
        return;
      }

      if (action === "g:bind_to") {
        const agentId = payload.agentId;
        if (!isNonEmptyString(agentId)) {
          await answerOnce();
          const view = await renderGroupMainMenu(chatKey, { notice: S.msg_invalid_agent, locale, actorUserId });
          await editMenuMessage({ chatId, messageId, view });
          return;
        }
        const ok = await isChatAdmin(chatId, fromId);
        if (!ok) {
          await answerOnce(S.msg_admins_only);
          return;
        }
        await answerOnce();
        const bound = await argusHttp.automationChatBind(chatKey, agentId, actorUserId);
        const sid = bound?.sessionId;
        if (isNonEmptyString(sid)) {
          await getClient(sid);
        }
        const notice = formatTemplate(S.notice_bound, { agentId });
        const view = await renderGroupMainMenu(chatKey, { notice, locale, actorUserId });
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

      const messageAttachments = extractTelegramAttachmentsFromMessage(message).map((attachment) => ({ ...attachment, source: "message" }));
      const replyTextRaw = extractReplyText(message);
      const replyAttachments = extractTelegramAttachmentsFromMessage(message?.reply_to_message).map((attachment) => ({ ...attachment, source: "reply" }));
      const replyText = replyTextRaw || (replyAttachments.length > 0 ? attachmentPlaceholder(replyAttachments) : null);

      queue.enqueue(async () => {
        const S = uiStrings(locale);
        const actorUserId = Number.isFinite(message?.from?.id) ? Math.trunc(message.from.id) : null;
        const actorChatKey = privateChatKeyForUserId(actorUserId);
        const pendingActorUserId = isGroupChatType(chatType) ? actorUserId : undefined;
        const clearPendingForActor = () => clearPendingAgentInput(chatKey, pendingActorUserId);
        const renderMainMenuForActor = async ({ notice, locale: localeOverride } = {}) => (
          isPrivateChatType(chatType)
            ? await renderPrivateMainMenu(chatKey, { notice, locale: localeOverride })
            : await renderGroupMainMenu(chatKey, { notice, locale: localeOverride, actorUserId })
        );
        const renderCreateMenuForActor = async (opts = {}) => (
          isPrivateChatType(chatType)
            ? await renderPrivateCreateMenu(chatKey, opts)
            : await renderGroupCreateMenu(chatKey, actorUserId, opts)
        );
        const renderRenameMenuForActor = async (agentId, opts = {}) => (
          isPrivateChatType(chatType)
            ? await renderPrivateRenameMenu(chatKey, agentId, opts)
            : await renderGroupRenameMenu(chatKey, actorUserId, agentId, opts)
        );
        const renderChannelListMenuForActor = async (pageRaw, opts = {}) => (
          isPrivateChatType(chatType)
            ? await renderPrivateChannelListMenu(chatKey, pageRaw, opts)
            : await renderGroupChannelListMenu(chatKey, actorUserId, pageRaw, opts)
        );
        const renderChannelDetailMenuForActor = async (channelId, pageRaw, opts = {}) => (
          isPrivateChatType(chatType)
            ? await renderPrivateChannelDetailMenu(chatKey, channelId, pageRaw, opts)
            : await renderGroupChannelDetailMenu(chatKey, actorUserId, channelId, pageRaw, opts)
        );
        const renderChannelCreateNameMenuForActor = async (opts = {}) => (
          isPrivateChatType(chatType)
            ? await renderPrivateChannelCreateNameMenu(chatKey, opts)
            : await renderGroupChannelCreateNameMenu(chatKey, actorUserId, opts)
        );
        const renderChannelCreateBaseUrlMenuForActor = async (channelName, opts = {}) => (
          isPrivateChatType(chatType)
            ? await renderPrivateChannelCreateBaseUrlMenu(chatKey, channelName, opts)
            : await renderGroupChannelCreateBaseUrlMenu(chatKey, actorUserId, channelName, opts)
        );
        const renderChannelCreateKeyMenuForActor = async (channelName, baseUrl, opts = {}) => (
          isPrivateChatType(chatType)
            ? await renderPrivateChannelCreateKeyMenu(chatKey, channelName, baseUrl, opts)
            : await renderGroupChannelCreateKeyMenu(chatKey, actorUserId, channelName, baseUrl, opts)
        );
        const renderChannelRenameMenuForActor = async (channelId, pageRaw, opts = {}) => (
          isPrivateChatType(chatType)
            ? await renderPrivateChannelRenameMenu(chatKey, channelId, pageRaw, opts)
            : await renderGroupChannelRenameMenu(chatKey, actorUserId, channelId, pageRaw, opts)
        );
        const renderChannelKeyMenuForActor = async (channelId, pageRaw, opts = {}) => (
          isPrivateChatType(chatType)
            ? await renderPrivateChannelKeyMenu(chatKey, channelId, pageRaw, opts)
            : await renderGroupChannelKeyMenu(chatKey, actorUserId, channelId, pageRaw, opts)
        );
        const getOwnedGroupActorContext = async ({ requireRenameable = false } = {}) => {
          if (!isGroupChatType(chatType)) {
            return { ok: true, error: null, mutationChatKey: chatKey, ctx: null };
          }
          try {
            const ctx = await resolveGroupActorContext(chatKey, actorUserId);
            if (!ctx.ownsCurrentAgent || !isNonEmptyString(ctx.actorChatKey) || (requireRenameable && !ctx.canRenameCurrentAgent)) {
              return { ok: false, error: S.msg_group_owner_only, mutationChatKey: null, ctx: null };
            }
            return { ok: true, error: null, mutationChatKey: ctx.actorChatKey, ctx };
          } catch (e) {
            return { ok: false, error: formatGatewayErrorForUser(e, { locale }), mutationChatKey: null, ctx: null };
          }
        };
        try {
          if (slash?.forOtherBot) return;
          if (slash || looksSlash) {
            const pendingBeforeSlash = getPendingAgentInput(chatKey, pendingActorUserId);
            const hadPendingBeforeSlash = !!pendingBeforeSlash;
            clearPendingForActor();
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
                actorUserId: message?.from?.id,
                notice: Boolean(boot?.createdMain) ? S.notice_initialized_created : S.notice_initialized_exists
              });
              return;
            }

            if (slash?.known && slash.cmd === "menu") {
              const sent = await sendMenuMessage({ target, chatKey, chatType, locale, actorUserId: message?.from?.id });
              scheduleMenuAutoDelete({
                chatId: target.chat_id,
                menuMessageId: sent?.message_id,
                commandMessageId: message?.message_id
              });
              return;
            }

            if (slash?.known && slash.cmd === "cancel") {
              let notice = hadPendingBeforeSlash ? S.notice_canceled : S.notice_no_active_user_turn;
              try {
                const route = await resolveRouteForChatKey(chatKey);
                const sessionId = route.sessionId;
                const client = await getClient(sessionId);
                let cancelThreadId = null;
                let cancelTarget = null;

                if (isPrivateChatType(chatType) || isGroupChatType(chatType)) {
                  cancelTarget = "main";
                } else if (!isNonEmptyString(cancelThreadId)) {
                  await safeSendMessage({ ...target, text: hadPendingBeforeSlash ? S.notice_canceled : S.notice_no_active_user_turn });
                  return;
                }

                const res = await client.cancelTurn({ threadId: cancelThreadId, target: cancelTarget });
                notice = formatCancelTurnResultForUser(res, { locale });
                if (
                  hadPendingBeforeSlash &&
                  isObjectRecord(res) &&
                  res.cancelRequested !== true &&
                  res.reason === "NO_ACTIVE_USER_TURN"
                ) {
                  notice = S.notice_canceled;
                }
                if (isObjectRecord(res) && res.cancelRequested === true) {
                  typing.stop(chatKey);
                }
              } catch (e) {
                const rawMsg = e instanceof Error ? e.message : String(e);
                const msg2 = formatGatewayErrorForUser(e, { locale });
                if (hadPendingBeforeSlash) {
                  notice = S.notice_canceled;
                } else if (
                  msg2 === S.err_not_initialized ||
                  /no agent bound|unknown session|agent has no sessionid/i.test(rawMsg)
                ) {
                  notice = S.notice_no_active_user_turn;
                } else {
                  notice = msg2;
                }
              }
              await safeSendMessage({ ...target, text: notice });
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
              await sendMenuMessage({ target, chatKey, chatType, locale, actorUserId: message?.from?.id });
              return;
            }
            const notice = LEGACY_COMMANDS.has(raw)
              ? formatTemplate(S.notice_legacy_moved, { cmd: raw })
              : formatTemplate(S.notice_unknown_command, { cmd: raw });
            await sendMenuMessage({ target, chatKey, chatType, notice, locale, actorUserId: message?.from?.id });
            return;
          }

          const pending = getPendingAgentInput(chatKey, pendingActorUserId);
          if (pending && pending.kind === "create") {
            const name = isNonEmptyString(text) ? text.trim().split(/\s+/, 1)[0]?.trim().toLowerCase() : "";
            if (!isNonEmptyString(name)) {
              const view = await renderCreateMenuForActor({ error: S.create_missing, locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
              return;
            }
            try {
              let mutationChatKey = chatKey;
              let notice = formatTemplate(S.notice_created, { name });
              if (isGroupChatType(chatType)) {
                if (!Number.isFinite(target.chat_id) || !Number.isFinite(actorUserId)) {
                  clearPendingForActor();
                  const view = await renderMainMenuForActor({ notice: S.msg_admins_only, locale });
                  await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
                  return;
                }
                const ok = await isChatAdmin(target.chat_id, actorUserId);
                if (!ok) {
                  clearPendingForActor();
                  const view = await renderMainMenuForActor({ notice: S.msg_admins_only, locale });
                  await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
                  return;
                }
                if (!isNonEmptyString(actorChatKey)) {
                  clearPendingForActor();
                  const view = await renderMainMenuForActor({ notice: S.msg_group_owner_only, locale });
                  await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
                  return;
                }
                mutationChatKey = actorChatKey;
              }
              const created = await argusHttp.automationAgentCreate(mutationChatKey, name);
              const createdAgentId = isNonEmptyString(created?.agent?.agentId) ? created.agent.agentId : name;
              await argusHttp.automationAgentUse(mutationChatKey, createdAgentId);
              if (isGroupChatType(chatType)) {
                await argusHttp.automationChatBind(chatKey, createdAgentId, actorUserId);
                notice = formatTemplate(S.notice_bound, { agentId: createdAgentId });
              }
              const sid = created?.agent?.sessionId;
              if (isNonEmptyString(sid)) await getClient(sid);
              clearPendingForActor();
              const view = await renderMainMenuForActor({ notice, locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
            } catch (e) {
              const view = await renderCreateMenuForActor({ error: formatGatewayErrorForUser(e, { locale }), locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
            }
            return;
          }
          if (pending && pending.kind === "rename") {
            const name = isNonEmptyString(text) ? text.trim().split(/\s+/, 1)[0]?.trim().toLowerCase() : "";
            if (!isNonEmptyString(name)) {
              const view = await renderRenameMenuForActor(pending.agentId, { error: S.rename_missing, locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
              return;
            }
            try {
              const owned = await getOwnedGroupActorContext({ requireRenameable: true });
              if (!owned.ok) {
                clearPendingForActor();
                const view = await renderMainMenuForActor({ notice: owned.error, locale });
                await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
                return;
              }
              if (owned.ctx && owned.ctx.route.agentId !== pending.agentId) {
                clearPendingForActor();
                const view = await renderMainMenuForActor({ notice: S.msg_invalid_agent, locale });
                await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
                return;
              }
              await argusHttp.automationAgentRename(owned.mutationChatKey, pending.agentId, name);
              clearPendingForActor();
              const notice = formatTemplate(S.notice_renamed, { old: pending.agentId, name });
              const view = await renderMainMenuForActor({ notice, locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
            } catch (e) {
              const view = await renderRenameMenuForActor(pending.agentId, { error: formatGatewayErrorForUser(e, { locale }), locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
            }
            return;
          }
          if (pending && pending.kind === "channel_create_name") {
            const channelName = isNonEmptyString(text) ? text.trim().split(/\s+/, 1)[0]?.trim().toLowerCase() : "";
            if (!isNonEmptyString(channelName)) {
              const view = await renderChannelCreateNameMenuForActor({ error: S.channel_create_name_missing, locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
              return;
            }
            const owned = await getOwnedGroupActorContext();
            if (!owned.ok) {
              clearPendingForActor();
              const view = await renderMainMenuForActor({ notice: owned.error, locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
              return;
            }
            setPendingCreateChannelBaseUrl(chatKey, pending.panelChatId, pending.panelMessageId, channelName, pendingActorUserId);
            const view = await renderChannelCreateBaseUrlMenuForActor(channelName, { locale });
            await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
            return;
          }
          if (pending && pending.kind === "channel_create_base_url") {
            const baseUrl = isNonEmptyString(text) ? text.trim() : "";
            if (!isNonEmptyString(baseUrl)) {
              const view = await renderChannelCreateBaseUrlMenuForActor(pending.channelName, { error: S.channel_create_base_url_missing, locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
              return;
            }
            const owned = await getOwnedGroupActorContext();
            if (!owned.ok) {
              clearPendingForActor();
              const view = await renderMainMenuForActor({ notice: owned.error, locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
              return;
            }
            setPendingCreateChannelApiKey(chatKey, pending.panelChatId, pending.panelMessageId, pending.channelName, baseUrl, pendingActorUserId);
            const view = await renderChannelCreateKeyMenuForActor(pending.channelName, baseUrl, { locale });
            await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
            return;
          }
          if (pending && pending.kind === "channel_create_api_key") {
            const apiKey = isNonEmptyString(text) ? text.trim() : "";
            if (!isNonEmptyString(apiKey)) {
              const view = await renderChannelCreateKeyMenuForActor(pending.channelName, pending.baseUrl, { error: S.channel_create_key_missing, locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
              return;
            }
            try {
              const owned = await getOwnedGroupActorContext();
              if (!owned.ok) {
                clearPendingForActor();
                const view = await renderMainMenuForActor({ notice: owned.error, locale });
                await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
                return;
              }
              const created = await argusHttp.automationChannelCreate(owned.mutationChatKey, pending.channelName, pending.baseUrl, apiKey);
              const createdChannel = created?.channel && typeof created.channel === "object" ? created.channel : null;
              clearPendingForActor();
              const notice = formatTemplate(S.notice_channel_created, { name: channelDisplayName(createdChannel || { name: pending.channelName, channelId: pending.channelName }) });
              if (isNonEmptyString(createdChannel?.channelId)) {
                const view = await renderChannelDetailMenuForActor(createdChannel.channelId, 0, { notice, locale });
                await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
              } else {
                const view = await renderChannelListMenuForActor(0, { notice, locale });
                await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
              }
            } catch (e) {
              const view = await renderChannelCreateKeyMenuForActor(pending.channelName, pending.baseUrl, { error: formatGatewayErrorForUser(e, { locale }), locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
            }
            return;
          }
          if (pending && pending.kind === "channel_rename") {
            const name = isNonEmptyString(text) ? text.trim().split(/\s+/, 1)[0]?.trim().toLowerCase() : "";
            if (!isNonEmptyString(name)) {
              const view = await renderChannelRenameMenuForActor(pending.channelId, pending.page, { error: S.channel_rename_missing, locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
              return;
            }
            try {
              const owned = await getOwnedGroupActorContext();
              if (!owned.ok) {
                clearPendingForActor();
                const view = await renderMainMenuForActor({ notice: owned.error, locale });
                await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
                return;
              }
              let oldName = pending.channelId;
              try {
                const channelState = await resolveChannelStateForChatKey(owned.mutationChatKey);
                const current = findChannelById(channelState?.channels, pending.channelId);
                if (current) oldName = channelDisplayName(current);
              } catch {}
              await argusHttp.automationChannelRename(owned.mutationChatKey, pending.channelId, name);
              clearPendingForActor();
              const notice = formatTemplate(S.notice_channel_renamed, { old: oldName, name });
              const view = await renderChannelDetailMenuForActor(pending.channelId, pending.page, { notice, locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
            } catch (e) {
              const view = await renderChannelRenameMenuForActor(pending.channelId, pending.page, { error: formatGatewayErrorForUser(e, { locale }), locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
            }
            return;
          }
          if (pending && pending.kind === "channel_key") {
            const apiKey = isNonEmptyString(text) ? text.trim() : "";
            if (!isNonEmptyString(apiKey)) {
              const view = await renderChannelKeyMenuForActor(pending.channelId, pending.page, { error: S.channel_key_missing, locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
              return;
            }
            try {
              const owned = await getOwnedGroupActorContext();
              if (!owned.ok) {
                clearPendingForActor();
                const view = await renderMainMenuForActor({ notice: owned.error, locale });
                await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
                return;
              }
              const updated = await argusHttp.automationChannelKeySet(owned.mutationChatKey, pending.channelId, apiKey);
              const channel = updated?.channel && typeof updated.channel === "object" ? updated.channel : { channelId: pending.channelId };
              clearPendingForActor();
              const notice = formatTemplate(S.notice_channel_key_saved, { name: channelDisplayName(channel) });
              const view = await renderChannelDetailMenuForActor(pending.channelId, pending.page, { notice, locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
            } catch (e) {
              const view = await renderChannelKeyMenuForActor(pending.channelId, pending.page, { error: formatGatewayErrorForUser(e, { locale }), locale });
              await editMenuMessage({ chatId: pending.panelChatId, messageId: pending.panelMessageId, view });
            }
            return;
          }

          if (!isNonEmptyString(text) && messageAttachments.length === 0 && replyAttachments.length === 0) {
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
                await sendMenuMessage({ target, chatKey, chatType, notice: S.notice_unbound_hint, locale, actorUserId: message?.from?.id });
              }
              return;
            }
            const msg2 = formatGatewayErrorForUser(e, { locale });
            if (msg2 === S.err_not_initialized) {
              await sendMenuMessage({ target, chatKey, chatType, locale, actorUserId: message?.from?.id });
              return;
            }
            await safeSendMessage({ ...target, text: msg2 });
            return;
          }

          const sessionId = route.sessionId;
          const client = await getClient(sessionId);
          const hasUserText = isNonEmptyString(text);
          let userText = "";
          if (hasUserText) {
            const userLines = [];
            if (replyText) {
              userLines.push("REPLY_TO:", replyText);
              if (replyAttachments.length > 0) userLines.push(`[reply attachments: ${replyAttachments.length} attachment(s)]`);
              userLines.push("");
              userLines.push("USER:", text);
            } else {
              userLines.push(text);
            }
            if (messageAttachments.length > 0) userLines.push(`[attachments: ${messageAttachments.length} attachment(s)]`);
            userText = userLines.join("\n");
          }

          let threadId = null;
          let enqueueTarget = null;
          const useSharedMainThread = isGroupChatType(chatType);

          if (isPrivateChatType(chatType)) {
            enqueueTarget = "main";
          } else if (useSharedMainThread) {
            enqueueTarget = "main";
            threadId = await ensureSessionMainThread(sessionId);
          }

          if (hasUserText && !useSharedMainThread) {
            typing.start(chatKey, typingTarget);
          }

          const shouldTrackTurnTarget = useSharedMainThread && isNonEmptyString(threadId);
          if (shouldTrackTurnTarget) queuePendingTurnTarget(sessionId, threadId, chatKey);

          let res;
          try {
            res = await client.enqueueInput({
              text: hasUserText ? userText : "",
              threadId,
              target: enqueueTarget,
              source: { channel: "telegram", chatKey },
              telegramAttachments: [...replyAttachments, ...messageAttachments],
            });
          } catch (e) {
            if (shouldTrackTurnTarget) removePendingTurnTarget(sessionId, threadId, chatKey);
            throw e;
          }

          if (res?.staged === true && res?.started !== true && res?.queued !== true) {
            if (shouldTrackTurnTarget) removePendingTurnTarget(sessionId, threadId, chatKey);
            await safeSendMessage({ ...target, text: formatStagedAttachmentsNotice(res, S) });
            return;
          }

          const effectiveThreadId = isNonEmptyString(res?.threadId) ? res.threadId : threadId;
          if (!useSharedMainThread && isNonEmptyString(effectiveThreadId)) {
            lastActiveBySessionThread.set(sessionThreadKey(sessionId, effectiveThreadId), chatKey);
          }

          if (shouldTrackTurnTarget && res?.started === true && isNonEmptyString(res?.turnId) && isNonEmptyString(effectiveThreadId)) {
            // An immediately started turn implies the lane was idle, so any older
            // pending topic targets on this thread are stale leftovers.
            clearPendingTurnTargets(sessionId, threadId);
            if (effectiveThreadId !== threadId) clearPendingTurnTargets(sessionId, effectiveThreadId);
            rememberTurnTarget(sessionId, effectiveThreadId, res.turnId, chatKey);
          } else if (shouldTrackTurnTarget && isNonEmptyString(res?.turnId) && isNonEmptyString(effectiveThreadId)) {
            rememberTurnTarget(sessionId, effectiveThreadId, res.turnId, chatKey);
            removePendingTurnTarget(sessionId, threadId, chatKey);
            if (effectiveThreadId !== threadId) removePendingTurnTarget(sessionId, effectiveThreadId, chatKey);
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
