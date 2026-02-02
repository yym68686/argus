"use client";

import React from "react";
import {
  ArrowUp,
  Archive,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Copy,
  Link as LinkIcon,
  MoreHorizontal,
  Paperclip,
  PlugZap,
  RefreshCw,
  Search,
  SquarePen,
  Trash2,
  XCircle
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { toast } from "sonner";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

interface RpcRequest {
  id?: number;
  method: string;
  params?: JsonValue;
}

interface RpcResponse {
  id: number;
  result?: JsonValue;
  error?: { code?: number; message?: string };
}

interface RpcNotification {
  method: string;
  params?: JsonValue;
}

type AnyWireMessage = Partial<RpcRequest & RpcResponse & RpcNotification>;

type ApprovalPolicy = "never" | "on-request" | "on-failure" | "untrusted";

type ToolMessageKind = "commandExecution" | "fileChange" | "mcpToolCall" | "webSearch" | "imageView";

type ToolMessageStatus = "inProgress" | "completed" | "failed" | "declined" | "unknown";

type ToolMessageMeta =
  | {
      kind: "commandExecution";
      status: ToolMessageStatus;
      command: string;
      cwd?: string;
      exitCode?: number | null;
      durationMs?: number | null;
    }
  | {
      kind: "fileChange";
      status: ToolMessageStatus;
      files: { path: string; kind: string }[];
      diffs: string;
    }
  | {
      kind: "mcpToolCall";
      status: ToolMessageStatus;
      server: string;
      tool: string;
    }
  | {
      kind: "webSearch";
      status: ToolMessageStatus;
      query: string;
    }
  | {
      kind: "imageView";
      status: ToolMessageStatus;
      path: string;
    };

type ChatMessage =
  | { id: string; role: "user"; text: string }
  | { id: string; role: "assistant"; text: string }
  | { id: string; role: "reasoning"; text: string }
  | { id: string; role: "tool"; text: string; meta: ToolMessageMeta };

interface SessionRow {
  sessionId?: string;
  containerId?: string;
  name?: string;
  status?: string;
}

interface ThreadRow {
  id: string;
  preview: string;
  modelProvider?: string;
  createdAt?: number;
  updatedAt?: number;
  path?: string;
}

interface ThreadChatState {
  messages: ChatMessage[];
  turnId: string | null;
  turnInProgress: boolean;
  hydrated: boolean;
  reasoningSummary: string;
  reasoningSummaryIndex: number | null;
}

function emptyThreadChatState(): ThreadChatState {
  return {
    messages: [],
    turnId: null,
    turnInProgress: false,
    hydrated: false,
    reasoningSummary: "",
    reasoningSummaryIndex: null
  };
}

function promptToThreadPreview(text: string): string {
  const firstLine = text.trim().split(/\r?\n/, 1)[0] ?? "";
  const normalized = firstLine.replace(/\s+/g, " ").trim();
  if (normalized.length <= 160) return normalized;
  return `${normalized.slice(0, 157)}…`;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function getString(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

function getNumber(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function toolStatusFromString(v: unknown): ToolMessageStatus {
  const raw = getString(v);
  if (!raw) return "unknown";
  const normalized = raw.replace(/_/g, "");
  if (normalized === "inProgress" || normalized === "inprogress") return "inProgress";
  if (normalized === "completed") return "completed";
  if (normalized === "failed") return "failed";
  if (normalized === "declined") return "declined";
  return "unknown";
}

function patchKindLabel(v: unknown): string {
  if (isRecord(v)) {
    const t = getString(v["type"]);
    if (t) return t;
  }
  const s = getString(v);
  return s ?? "update";
}

function stripSessionFromWsUrl(url: string): string {
  try {
    const u = new URL(url);
    u.searchParams.delete("session");
    return u.toString();
  } catch {
    return url;
  }
}

function defaultWsUrl(): string {
  const preset = (process.env.NEXT_PUBLIC_ARGUS_WS_URL ?? "").trim();
  if (preset) return preset;
  if (typeof window === "undefined") return "ws://127.0.0.1:8080/ws";
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const host =
    window.location.port === "3000" ? `${window.location.hostname}:8080` : window.location.host;
  return `${proto}//${host}/ws`;
}

function safeJsonParse(text: string): AnyWireMessage | null {
  try {
    return JSON.parse(text) as AnyWireMessage;
  } catch {
    return null;
  }
}

function safeJsonStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? "";
  } catch {
    return "";
  }
}

function extractTokenFromWsUrl(url: string): string | null {
  try {
    const u = new URL(url);
    const token = u.searchParams.get("token");
    return token && token.trim() ? token : null;
  } catch {
    return null;
  }
}

function httpBaseFromWsUrl(url: string): string | null {
  try {
    const u = new URL(url);
    const proto = u.protocol === "wss:" ? "https:" : "http:";
    return `${proto}//${u.host}`;
  } catch {
    return null;
  }
}

function withSessionInWsUrl(url: string, sessionId: string): string {
  try {
    const u = new URL(url);
    u.searchParams.set("session", sessionId);
    return u.toString();
  } catch {
    return url;
  }
}

function isNonEmptyString(v: unknown): v is string {
  return typeof v === "string" && v.trim().length > 0;
}

function nowId(prefix: string): string {
  return `${prefix}_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

function toolMessageFromThreadItem(rawItem: Record<string, unknown>): ChatMessage | null {
  const type = getString(rawItem["type"]);
  const id = getString(rawItem["id"]) ?? nowId("itm");

  if (type === "commandExecution") {
    const command = getString(rawItem["command"]) ?? "";
    const cwd = getString(rawItem["cwd"]) ?? undefined;
    let status = toolStatusFromString(rawItem["status"]);
    const aggregatedOutput = getString(rawItem["aggregatedOutput"]) ?? "";
    const exitCode = getNumber(rawItem["exitCode"]);
    const durationMs = getNumber(rawItem["durationMs"]);

    if (status === "completed" && exitCode !== null && exitCode !== 0) {
      status = "failed";
    }

    return {
      id,
      role: "tool",
      text: aggregatedOutput,
      meta: { kind: "commandExecution", status, command, cwd, exitCode, durationMs }
    };
  }

  if (type === "fileChange") {
    const status = toolStatusFromString(rawItem["status"]);
    const changesRaw = rawItem["changes"];
    const files: { path: string; kind: string }[] = [];
    const diffParts: string[] = [];

    if (Array.isArray(changesRaw)) {
      for (const rawChange of changesRaw) {
        if (!isRecord(rawChange)) continue;
        const path = getString(rawChange["path"]);
        const kind = patchKindLabel(rawChange["kind"]);
        const diff = getString(rawChange["diff"]) ?? "";
        if (path) files.push({ path, kind });
        if (diff.trim()) {
          diffParts.push(`${kind.toUpperCase()}: ${path ?? ""}\n${diff}`.trim());
        }
      }
    }

    return {
      id,
      role: "tool",
      text: "",
      meta: { kind: "fileChange", status, files, diffs: diffParts.join("\n\n") }
    };
  }

  if (type === "mcpToolCall") {
    const status = toolStatusFromString(rawItem["status"]);
    const server = getString(rawItem["server"]) ?? "";
    const tool = getString(rawItem["tool"]) ?? "";

    let text = "";
    const error = isRecord(rawItem["error"]) ? getString(rawItem["error"]["message"]) : null;
    if (error) {
      text = error;
    } else if (rawItem["result"] !== undefined && rawItem["result"] !== null) {
      text = safeJsonStringify(rawItem["result"]);
    } else if (rawItem["arguments"] !== undefined && rawItem["arguments"] !== null) {
      text = safeJsonStringify(rawItem["arguments"]);
    }

    return {
      id,
      role: "tool",
      text,
      meta: { kind: "mcpToolCall", status, server, tool }
    };
  }

  if (type === "webSearch") {
    const query = getString(rawItem["query"]) ?? "";
    return { id, role: "tool", text: "", meta: { kind: "webSearch", status: "completed", query } };
  }

  if (type === "imageView") {
    const path = getString(rawItem["path"]) ?? "";
    return { id, role: "tool", text: "", meta: { kind: "imageView", status: "completed", path } };
  }

  return null;
}

function isArchivedThreadPath(p: string | null | undefined): boolean {
  if (!p) return false;
  return p.includes("/archived_sessions/") || p.includes("\\archived_sessions\\") || p.endsWith("/archived_sessions");
}

function userInputsToText(inputs: unknown): string {
  if (!Array.isArray(inputs)) return "";
  const parts: string[] = [];
  for (const raw of inputs) {
    if (!isRecord(raw)) continue;
    const t = getString(raw["type"]);
    if (t === "text") {
      const text = getString(raw["text"]);
      if (text) parts.push(text);
      continue;
    }
    if (t === "image") {
      const url = getString(raw["url"]);
      parts.push(url ? `[image] ${url}` : "[image]");
      continue;
    }
    if (t === "localImage") {
      const path = getString(raw["path"]);
      parts.push(path ? `[local image] ${path}` : "[local image]");
      continue;
    }
  }
  return parts.join("\n").trim();
}

function turnsToChatMessages(turns: unknown): ChatMessage[] {
  if (!Array.isArray(turns)) return [];
  const messages: ChatMessage[] = [];

  for (const rawTurn of turns) {
    if (!isRecord(rawTurn)) continue;
    const items = rawTurn["items"];
    if (!Array.isArray(items)) continue;

    for (const rawItem of items) {
      if (!isRecord(rawItem)) continue;
      const type = getString(rawItem["type"]);
      const id = getString(rawItem["id"]) ?? nowId("itm");
      if (type === "userMessage") {
        const text = userInputsToText(rawItem["content"]);
        if (!text) continue;
        messages.push({ id, role: "user", text });
        continue;
      }
      if (type === "agentMessage") {
        const text = getString(rawItem["text"]) ?? "";
        if (!text.trim()) continue;
        messages.push({ id, role: "assistant", text });
        continue;
      }
      if (type === "reasoning") {
        const rawSummary = rawItem["summary"];
        if (!Array.isArray(rawSummary)) continue;
        const parts = rawSummary.map((v) => getString(v)).filter((v): v is string => isNonEmptyString(v));
        const text = parts.join("\n\n").trim();
        if (!text) continue;
        messages.push({ id, role: "reasoning", text });
        continue;
      }

      const toolMsg = toolMessageFromThreadItem(rawItem);
      if (toolMsg) {
        messages.push(toolMsg);
      }
    }
  }

  return messages;
}

export default function Page() {
  type ActivePane = "chat" | "connection";

  const [wsUrl, setWsUrl] = React.useState<string>("");
  const [cwd, setCwd] = React.useState<string>("/workspace");
  const [approvalPolicy, setApprovalPolicy] = React.useState<ApprovalPolicy>("never");

  const [activePane, setActivePane] = React.useState<ActivePane>("chat");

  const [activeSessionId, setActiveSessionId] = React.useState<string | null>(null);
  const [activeThreadId, setActiveThreadId] = React.useState<string | null>(null);
  const [currentThreadBySession, setCurrentThreadBySession] = React.useState<
    Record<string, string | null>
  >({});

  const [connBySession, setConnBySession] = React.useState<
    Record<string, { ok: boolean; text: string }>
  >({});

  const [chatByThreadKey, setChatByThreadKey] = React.useState<Record<string, ThreadChatState>>({});
  const [prompt, setPrompt] = React.useState<string>("");

  const [sessions, setSessions] = React.useState<SessionRow[] | null>(null);
  const [sessionsBusy, setSessionsBusy] = React.useState<boolean>(false);
  const [sessionsError, setSessionsError] = React.useState<string | null>(null);
  const [historyQuery, setHistoryQuery] = React.useState<string>("");
  const [expandedSessions, setExpandedSessions] = React.useState<Record<string, boolean>>({});
  const [sessionMenuOpenFor, setSessionMenuOpenFor] = React.useState<string | null>(null);
  const [threadMenuOpenFor, setThreadMenuOpenFor] = React.useState<string | null>(null);

  const [threadsBySession, setThreadsBySession] = React.useState<Record<string, ThreadRow[] | null>>(
    {}
  );
  const [threadsBusyBySession, setThreadsBusyBySession] = React.useState<Record<string, boolean>>({});
  const [threadsErrorBySession, setThreadsErrorBySession] = React.useState<
    Record<string, string | null>
  >({});

  interface SessionRuntime {
    ws: WebSocket | null;
    keyRef: { current: string };
    nextId: number;
    pending: Map<number, { resolve: (v: JsonValue) => void; reject: (e: Error) => void }>;
    initialized: boolean;
    sessionIdPromise: Promise<string>;
    resolveSessionId: (id: string) => void;
    rejectSessionId: (e: Error) => void;
  }

  const runtimesRef = React.useRef<Map<string, SessionRuntime>>(new Map());
  const connectPromisesRef = React.useRef<Map<string, Promise<void>>>(new Map());
  const autoExpandedRef = React.useRef<boolean>(false);
  const autoConnectAttemptedRef = React.useRef<boolean>(false);

  const chatScrollRef = React.useRef<HTMLDivElement | null>(null);
  const composingRef = React.useRef<boolean>(false);

  const activeThreadKey =
    activeSessionId && activeThreadId ? `${activeSessionId}:${activeThreadId}` : null;
  const activeChat = activeThreadKey ? chatByThreadKey[activeThreadKey] : null;
  const activeConnStatus = activeSessionId ? connBySession[activeSessionId] : undefined;

  const isActiveSessionBusy = React.useMemo(() => {
    if (!activeSessionId) return false;
    const prefix = `${activeSessionId}:`;
    for (const [k, v] of Object.entries(chatByThreadKey)) {
      if (!k.startsWith(prefix)) continue;
      if (v.turnInProgress) return true;
    }
    return false;
  }, [activeSessionId, chatByThreadKey]);

  const canSend = !!prompt.trim() && !isActiveSessionBusy;

  React.useEffect(() => {
    chatScrollRef.current?.scrollTo({ top: chatScrollRef.current.scrollHeight });
  }, [activeThreadKey, activeChat?.messages]);

  React.useEffect(() => {
    setWsUrl(stripSessionFromWsUrl(defaultWsUrl()));
  }, []);

  React.useEffect(() => {
    if (autoConnectAttemptedRef.current) return;
    const base = stripSessionFromWsUrl(wsUrl);
    if (!base.trim()) return;
    autoConnectAttemptedRef.current = true;

    void (async () => {
      try {
        const list = await refreshSessions({ silent: true, throwOnError: true });
        const existing = list.find((s) => isNonEmptyString(s.sessionId))?.sessionId ?? null;
        if (existing) {
          setActiveSessionId(existing);
          await ensureSessionReady(existing);
          return;
        }

        await connectNewSession();
      } catch (e) {
        const msg = (e as Error)?.message || String(e);
        setSessionsError(msg);
        toast.error(msg);
      }
    })();
  }, [wsUrl]);

  React.useEffect(() => {
    if (!activeSessionId || autoExpandedRef.current) return;
    setExpandedSessions((prev) => ({ ...prev, [activeSessionId]: true }));
    autoExpandedRef.current = true;
  }, [activeSessionId]);

  React.useEffect(() => {
    if (!sessionMenuOpenFor) return;
    const onDoc = () => setSessionMenuOpenFor(null);
    document.addEventListener("click", onDoc);
    return () => document.removeEventListener("click", onDoc);
  }, [sessionMenuOpenFor]);

  React.useEffect(() => {
    const expandedIds = Object.entries(expandedSessions).filter(([, v]) => !!v).map(([k]) => k);
    if (expandedIds.length === 0) return;
    for (const sessionId of expandedIds) {
      const busy = !!threadsBusyBySession[sessionId];
      if (busy) continue;
      const loaded = threadsBySession[sessionId];
      if (loaded && loaded.length >= 0) continue;
      void refreshThreadsForSession(sessionId, { silent: true });
    }
  }, [expandedSessions, threadsBusyBySession, threadsBySession]);

  function setConnStatus(sessionId: string, status: { ok: boolean; text: string }): void {
    setConnBySession((prev) => ({ ...prev, [sessionId]: status }));
  }

  function getOrCreateRuntime(sessionId: string): SessionRuntime {
    const existing = runtimesRef.current.get(sessionId);
    if (existing) return existing;

    let resolveSessionId: (id: string) => void = () => {};
    let rejectSessionId: (e: Error) => void = () => {};
    const sessionIdPromise = new Promise<string>((resolve, reject) => {
      resolveSessionId = resolve;
      rejectSessionId = reject;
    });

    const rt: SessionRuntime = {
      ws: null,
      keyRef: { current: sessionId },
      nextId: 1,
      pending: new Map(),
      initialized: false,
      sessionIdPromise,
      resolveSessionId,
      rejectSessionId
    };
    runtimesRef.current.set(sessionId, rt);
    rt.resolveSessionId(sessionId);
    return rt;
  }

  function closeSessionSocket(sessionId: string, reason?: string): void {
    const rt = runtimesRef.current.get(sessionId);
    const ws = rt?.ws;
    if (!ws) return;
    try {
      ws.close(1000, reason ?? "disconnect");
    } catch {
      // ignore
    }
  }

  function threadKey(sessionId: string, threadId: string): string {
    return `${sessionId}:${threadId}`;
  }

  function sendWire(sessionId: string, obj: unknown): void {
    const rt = runtimesRef.current.get(sessionId);
    const ws = rt?.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      throw new Error("WebSocket is not connected");
    }
    ws.send(JSON.stringify(obj));
  }

  function rpc(sessionId: string, method: string, params?: JsonValue): Promise<JsonValue> {
    const rt = runtimesRef.current.get(sessionId);
    if (!rt) throw new Error("Not connected");
    const id = rt.nextId++;
    const req: RpcRequest = { method, id };
    if (params !== undefined) req.params = params;

    sendWire(sessionId, req);

    return new Promise<JsonValue>((resolve, reject) => {
      rt.pending.set(id, { resolve, reject });
      window.setTimeout(() => {
        const pending = rt.pending.get(id);
        if (!pending) return;
        rt.pending.delete(id);
        reject(new Error(`Timeout waiting for response to ${method} (${id})`));
      }, 120000);
    });
  }

  async function initializeSession(sessionId: string): Promise<void> {
    const rt = runtimesRef.current.get(sessionId);
    if (!rt) throw new Error("Not connected");
    if (rt.initialized) return;
    await rpc(sessionId, "initialize", {
      clientInfo: { name: "argus_web", title: "Argus Web", version: "0.1.0" }
    });
    sendWire(sessionId, { method: "initialized" });
    rt.initialized = true;
  }

  function handleServerRequest(sessionId: string, msg: AnyWireMessage): void {
    if (typeof msg.id !== "number" || !isNonEmptyString(msg.method)) return;
    const id = msg.id;
    const method = msg.method;

    if (method === "item/commandExecution/requestApproval") {
      sendWire(sessionId, { id, result: { decision: "decline" } });
      return;
    }
    if (method === "item/fileChange/requestApproval") {
      sendWire(sessionId, { id, result: { decision: "decline" } });
      return;
    }

    sendWire(sessionId, {
      id,
      error: { code: -32601, message: `Unsupported server request: ${method}` }
    });
  }

  function setThreadTurnStarted(sessionId: string, incomingThreadId: string, incomingTurnId: string | null): void {
    const key = threadKey(sessionId, incomingThreadId);
    setChatByThreadKey((prev) => {
      const current = prev[key] ?? emptyThreadChatState();
      const nextTurnId = current.turnId ?? incomingTurnId;
      return {
        ...prev,
        [key]: {
          ...current,
          turnInProgress: true,
          turnId: nextTurnId ?? null,
          reasoningSummary: "",
          reasoningSummaryIndex: null
        }
      };
    });
  }

  function appendAssistantDelta(sessionId: string, incomingThreadId: string, incomingTurnId: string | null, delta: string): void {
    const key = threadKey(sessionId, incomingThreadId);
    setChatByThreadKey((prev) => {
      const current = prev[key] ?? emptyThreadChatState();
      const turnId = current.turnId;
      if (turnId && incomingTurnId && incomingTurnId !== turnId) return prev;

      const nextTurnId =
        current.turnInProgress && !current.turnId && incomingTurnId ? incomingTurnId : current.turnId;

      const msgs = current.messages;
      const last = msgs[msgs.length - 1];
      const newAssistant: ChatMessage = { id: nowId("a"), role: "assistant", text: delta };
      const nextMessages =
        last && last.role === "assistant"
          ? [...msgs.slice(0, -1), { ...last, text: last.text + delta }]
          : [...msgs, newAssistant];

      return {
        ...prev,
        [key]: { ...current, messages: nextMessages, turnId: nextTurnId ?? null, hydrated: true }
      };
    });
  }

  function appendReasoningSummaryDelta(
    sessionId: string,
    incomingThreadId: string,
    incomingTurnId: string | null,
    delta: string,
    summaryIndex: number | null
  ): void {
    const key = threadKey(sessionId, incomingThreadId);
    setChatByThreadKey((prev) => {
      const current = prev[key] ?? emptyThreadChatState();
      const turnId = current.turnId;
      if (turnId && incomingTurnId && incomingTurnId !== turnId) return prev;

      const nextIndex = summaryIndex ?? current.reasoningSummaryIndex;
      const needsBreak =
        current.reasoningSummary &&
        summaryIndex !== null &&
        current.reasoningSummaryIndex !== null &&
        summaryIndex !== current.reasoningSummaryIndex;

      const nextText = `${current.reasoningSummary}${needsBreak ? "\n\n" : ""}${delta}`;

      return {
        ...prev,
        [key]: { ...current, reasoningSummary: nextText, reasoningSummaryIndex: nextIndex, hydrated: true }
      };
    });
  }

  function setAssistantFullText(sessionId: string, incomingThreadId: string, incomingTurnId: string | null, fullText: string): void {
    const key = threadKey(sessionId, incomingThreadId);
    setChatByThreadKey((prev) => {
      const current = prev[key] ?? emptyThreadChatState();
      const turnId = current.turnId;
      if (turnId && incomingTurnId && incomingTurnId !== turnId) return prev;

      const nextTurnId =
        current.turnInProgress && !current.turnId && incomingTurnId ? incomingTurnId : current.turnId;

      const msgs = current.messages;
      const last = msgs[msgs.length - 1];
      const newAssistant: ChatMessage = { id: nowId("a"), role: "assistant", text: fullText };
      const nextMessages =
        last && last.role === "assistant"
          ? [...msgs.slice(0, -1), { ...last, text: fullText }]
          : [...msgs, newAssistant];

      return {
        ...prev,
        [key]: { ...current, messages: nextMessages, turnId: nextTurnId ?? null, hydrated: true }
      };
    });
  }

	  function setThreadTurnCompleted(sessionId: string, incomingThreadId: string, incomingTurnId: string | null): void {
	    const key = threadKey(sessionId, incomingThreadId);
	    setChatByThreadKey((prev) => {
	      const current = prev[key] ?? emptyThreadChatState();
	      if (!current.turnInProgress) return prev;
	      if (current.turnId && incomingTurnId && incomingTurnId !== current.turnId) return prev;
	      return {
	        ...prev,
	        [key]: { ...current, turnInProgress: false, turnId: null, reasoningSummary: "", reasoningSummaryIndex: null }
	      };
	    });
	  }

  function mergeToolMeta(prevMeta: ToolMessageMeta, nextMeta: ToolMessageMeta): ToolMessageMeta {
    if (prevMeta.kind !== nextMeta.kind) return nextMeta;

    if (prevMeta.kind === "commandExecution" && nextMeta.kind === "commandExecution") {
      return {
        kind: "commandExecution",
        status: nextMeta.status !== "unknown" ? nextMeta.status : prevMeta.status,
        command: nextMeta.command.trim() ? nextMeta.command : prevMeta.command,
        cwd: nextMeta.cwd ?? prevMeta.cwd,
        exitCode: nextMeta.exitCode ?? prevMeta.exitCode,
        durationMs: nextMeta.durationMs ?? prevMeta.durationMs
      };
    }
    if (prevMeta.kind === "fileChange" && nextMeta.kind === "fileChange") {
      return {
        kind: "fileChange",
        status: nextMeta.status !== "unknown" ? nextMeta.status : prevMeta.status,
        files: nextMeta.files.length > 0 ? nextMeta.files : prevMeta.files,
        diffs: nextMeta.diffs.trim() ? nextMeta.diffs : prevMeta.diffs
      };
    }
    if (prevMeta.kind === "mcpToolCall" && nextMeta.kind === "mcpToolCall") {
      return {
        kind: "mcpToolCall",
        status: nextMeta.status !== "unknown" ? nextMeta.status : prevMeta.status,
        server: nextMeta.server.trim() ? nextMeta.server : prevMeta.server,
        tool: nextMeta.tool.trim() ? nextMeta.tool : prevMeta.tool
      };
    }
    if (prevMeta.kind === "webSearch" && nextMeta.kind === "webSearch") {
      return {
        kind: "webSearch",
        status: nextMeta.status !== "unknown" ? nextMeta.status : prevMeta.status,
        query: nextMeta.query.trim() ? nextMeta.query : prevMeta.query
      };
    }
    if (prevMeta.kind === "imageView" && nextMeta.kind === "imageView") {
      return {
        kind: "imageView",
        status: nextMeta.status !== "unknown" ? nextMeta.status : prevMeta.status,
        path: nextMeta.path.trim() ? nextMeta.path : prevMeta.path
      };
    }
    return nextMeta;
  }

  function upsertToolFromItem(
    sessionId: string,
    incomingThreadId: string,
    incomingTurnId: string | null,
    item: Record<string, unknown>
  ): void {
    const msg = toolMessageFromThreadItem(item);
    if (!msg || msg.role !== "tool") return;

    const key = threadKey(sessionId, incomingThreadId);
    setChatByThreadKey((prev) => {
      const current = prev[key] ?? emptyThreadChatState();
      const turnId = current.turnId;
      if (turnId && incomingTurnId && incomingTurnId !== turnId) return prev;

      const nextTurnId =
        current.turnInProgress && !current.turnId && incomingTurnId ? incomingTurnId : current.turnId;

      const msgs = current.messages;
      const idx = msgs.findIndex((m) => m.id === msg.id && m.role === "tool");
      if (idx === -1) {
        return {
          ...prev,
          [key]: { ...current, messages: [...msgs, msg], hydrated: true, turnId: nextTurnId ?? null }
        };
      }

      const existing = msgs[idx];
      if (existing.role !== "tool") return prev;
      const mergedMeta = mergeToolMeta(existing.meta, msg.meta);
      const nextText = existing.text.trim() ? existing.text : msg.text;
      const nextMsg: ChatMessage = { ...existing, text: nextText, meta: mergedMeta };

      return {
        ...prev,
        [key]: {
          ...current,
          messages: [...msgs.slice(0, idx), nextMsg, ...msgs.slice(idx + 1)],
          hydrated: true,
          turnId: nextTurnId ?? null
        }
      };
    });
  }

  function appendToolDelta(
    sessionId: string,
    incomingThreadId: string,
    incomingTurnId: string | null,
    itemId: string,
    delta: string,
    stubMeta: ToolMessageMeta
  ): void {
    const key = threadKey(sessionId, incomingThreadId);
    setChatByThreadKey((prev) => {
      const current = prev[key] ?? emptyThreadChatState();
      const turnId = current.turnId;
      if (turnId && incomingTurnId && incomingTurnId !== turnId) return prev;

      const nextTurnId =
        current.turnInProgress && !current.turnId && incomingTurnId ? incomingTurnId : current.turnId;

      const msgs = current.messages;
      const idx = msgs.findIndex((m) => m.id === itemId && m.role === "tool");
      if (idx === -1) {
        const nextMsg: ChatMessage = { id: itemId, role: "tool", text: delta, meta: stubMeta };
        return {
          ...prev,
          [key]: { ...current, messages: [...msgs, nextMsg], hydrated: true, turnId: nextTurnId ?? null }
        };
      }

      const existing = msgs[idx];
      if (existing.role !== "tool") return prev;
      const mergedMeta = mergeToolMeta(existing.meta, stubMeta);
      const nextMsg: ChatMessage = { ...existing, text: existing.text + delta, meta: mergedMeta };

      return {
        ...prev,
        [key]: {
          ...current,
          messages: [...msgs.slice(0, idx), nextMsg, ...msgs.slice(idx + 1)],
          hydrated: true,
          turnId: nextTurnId ?? null
        }
      };
    });
  }

	  function handleNotification(sessionId: string, msg: AnyWireMessage): void {
	    if (!isNonEmptyString(msg.method)) return;
	    const method = msg.method;
	    const params = isRecord(msg.params) ? msg.params : {};

    if (method === "argus/session") {
      const id = params["id"];
      if (!isNonEmptyString(id)) return;
      if (id === sessionId) return;

      if (sessionId.startsWith("__pending__")) {
        const rt = runtimesRef.current.get(sessionId);
        if (!rt) return;
        runtimesRef.current.delete(sessionId);
        rt.keyRef.current = id;
        runtimesRef.current.set(id, rt);

        setConnBySession((prev) => {
          const next: Record<string, { ok: boolean; text: string }> = { ...prev };
          if (prev[sessionId]) {
            next[id] = prev[sessionId];
            delete next[sessionId];
          } else if (!next[id]) {
            next[id] = { ok: true, text: "connected" };
          }
          return next;
        });

        rt.resolveSessionId(id);
        if (!autoExpandedRef.current) {
          setExpandedSessions((prev) => ({ ...prev, [id]: true }));
          autoExpandedRef.current = true;
        }
        setActiveSessionId((prev) => prev ?? id);
      }
      return;
    }

	    if (method === "turn/started") {
	      const incomingThreadId = getString(params["threadId"]);
	      if (!isNonEmptyString(incomingThreadId)) return;
	      const turn = isRecord(params["turn"]) ? params["turn"] : {};
	      const incomingTurnId = getString(turn["id"]);
	      setThreadTurnStarted(sessionId, incomingThreadId, incomingTurnId);
	      return;
	    }

    if (method === "item/started") {
      const incomingThreadId = getString(params["threadId"]);
      if (!isNonEmptyString(incomingThreadId)) return;
      const incomingTurnId = getString(params["turnId"]);
      const item = isRecord(params["item"]) ? params["item"] : null;
      if (!item) return;
      upsertToolFromItem(sessionId, incomingThreadId, incomingTurnId, item);
      return;
    }

	    if (method === "item/agentMessage/delta") {
	      const incomingThreadId = getString(params["threadId"]);
	      if (!isNonEmptyString(incomingThreadId)) return;
	      const incomingTurnId = getString(params["turnId"]);
      const delta = getString(params["delta"]);
      if (!isNonEmptyString(delta)) return;
      appendAssistantDelta(sessionId, incomingThreadId, incomingTurnId, delta);
      return;
    }

    if (method === "item/reasoning/summaryTextDelta") {
      const incomingThreadId = getString(params["threadId"]);
      if (!isNonEmptyString(incomingThreadId)) return;
      const incomingTurnId = getString(params["turnId"]);
      const delta = getString(params["delta"]);
      if (!isNonEmptyString(delta)) return;
      const summaryIndex = getNumber(params["summaryIndex"]);
	      appendReasoningSummaryDelta(sessionId, incomingThreadId, incomingTurnId, delta, summaryIndex);
	      return;
	    }

    if (method === "item/commandExecution/outputDelta") {
      const incomingThreadId = getString(params["threadId"]);
      if (!isNonEmptyString(incomingThreadId)) return;
      const incomingTurnId = getString(params["turnId"]);
      const itemId = getString(params["itemId"]);
      const delta = getString(params["delta"]);
      if (!isNonEmptyString(itemId) || !isNonEmptyString(delta)) return;
      appendToolDelta(sessionId, incomingThreadId, incomingTurnId, itemId, delta, {
        kind: "commandExecution",
        status: "inProgress",
        command: "",
        cwd: undefined,
        exitCode: null,
        durationMs: null
      });
      return;
    }

    if (method === "item/fileChange/outputDelta") {
      const incomingThreadId = getString(params["threadId"]);
      if (!isNonEmptyString(incomingThreadId)) return;
      const incomingTurnId = getString(params["turnId"]);
      const itemId = getString(params["itemId"]);
      const delta = getString(params["delta"]);
      if (!isNonEmptyString(itemId) || !isNonEmptyString(delta)) return;
      appendToolDelta(sessionId, incomingThreadId, incomingTurnId, itemId, delta, {
        kind: "fileChange",
        status: "inProgress",
        files: [],
        diffs: ""
      });
      return;
    }

    if (method === "item/mcpToolCall/progress") {
      const incomingThreadId = getString(params["threadId"]);
      if (!isNonEmptyString(incomingThreadId)) return;
      const incomingTurnId = getString(params["turnId"]);
      const itemId = getString(params["itemId"]);
      const message = getString(params["message"]);
      if (!isNonEmptyString(itemId) || !isNonEmptyString(message)) return;
      appendToolDelta(sessionId, incomingThreadId, incomingTurnId, itemId, message + "\n", {
        kind: "mcpToolCall",
        status: "inProgress",
        server: "",
        tool: ""
      });
      return;
    }

	    if (method === "item/completed") {
	      const incomingThreadId = getString(params["threadId"]);
	      if (!isNonEmptyString(incomingThreadId)) return;
	      const incomingTurnId = getString(params["turnId"]);

	      const item = isRecord(params["item"]) ? params["item"] : {};
	      if (item["type"] === "agentMessage") {
	        const fullText = getString(item["text"]);
	        if (isNonEmptyString(fullText)) {
	          setAssistantFullText(sessionId, incomingThreadId, incomingTurnId, fullText);
	        }
          return;
	      }

        if (item["type"] === "commandExecution" || item["type"] === "fileChange" || item["type"] === "mcpToolCall") {
          upsertToolFromItem(sessionId, incomingThreadId, incomingTurnId, item);
          return;
        }
	      return;
	    }

    if (method === "turn/completed") {
      const incomingThreadId = getString(params["threadId"]);
      if (!isNonEmptyString(incomingThreadId)) return;
      const turn = isRecord(params["turn"]) ? params["turn"] : {};
      const incomingTurnId = getString(turn["id"]);
      setThreadTurnCompleted(sessionId, incomingThreadId, incomingTurnId);
      void refreshThreadsForSession(sessionId, { silent: true });
      return;
    }
  }

  function handleWireMessage(sessionId: string, text: string): void {
    const msg = safeJsonParse(text);
    if (!msg) return;

    if (typeof msg.id === "number" && isNonEmptyString(msg.method)) {
      handleServerRequest(sessionId, msg);
      return;
    }

    if (typeof msg.id === "number") {
      const rt = runtimesRef.current.get(sessionId);
      const pending = rt?.pending.get(msg.id);
      if (!pending) return;
      rt?.pending.delete(msg.id);
      const error = (msg as RpcResponse).error;
      if (error) pending.reject(new Error(error.message || "RPC error"));
      else pending.resolve((msg as RpcResponse).result ?? null);
      return;
    }

    if (isNonEmptyString(msg.method)) {
      handleNotification(sessionId, msg);
    }
  }

  async function connectSession(sessionId: string, targetUrl: string): Promise<void> {
    const existingPromise = connectPromisesRef.current.get(sessionId);
    if (existingPromise) return existingPromise;

    const promise = (async () => {
      const rt = getOrCreateRuntime(sessionId);
      rt.keyRef.current = sessionId;
      rt.initialized = false;
      rt.nextId = 1;
      rt.pending.clear();

      const prev = rt.ws;
      if (prev && (prev.readyState === WebSocket.OPEN || prev.readyState === WebSocket.CONNECTING)) {
        try {
          prev.close(1000, "Reconnecting");
        } catch {
          // ignore
        }
      }

      setConnStatus(sessionId, { ok: false, text: "connecting…" });

      let ws: WebSocket;
      try {
        ws = new WebSocket(targetUrl);
      } catch (e) {
        setConnStatus(sessionId, { ok: false, text: "error" });
        throw e instanceof Error ? e : new Error(String(e));
      }

      rt.ws = ws;

      await new Promise<void>((resolve, reject) => {
        const onOpen = () => resolve();
        const onError = () => reject(new Error("WebSocket error"));
        ws.addEventListener("open", onOpen, { once: true });
        ws.addEventListener("error", onError, { once: true });
      });

      setConnStatus(rt.keyRef.current, { ok: true, text: "connected" });
      void refreshSessions({ silent: true });

      ws.addEventListener("close", (ev) => {
        const key = rt.keyRef.current;
        const code = ev.code;
        const reason = ev.reason;
        const msg = code ? `disconnected (${code}${reason ? `: ${reason}` : ""})` : "disconnected";
        setConnStatus(key, { ok: false, text: msg });
        rt.initialized = false;
        rt.ws = null;

        if (rt.pending.size) {
          const err = new Error("WebSocket connection closed");
          for (const [, p] of rt.pending) p.reject(err);
          rt.pending.clear();
        }
      });

      ws.addEventListener("error", () => {
        const key = rt.keyRef.current;
        setConnStatus(key, { ok: false, text: "error" });
      });

      ws.addEventListener("message", (ev) => {
        if (typeof ev.data !== "string") return;
        handleWireMessage(rt.keyRef.current, ev.data);
      });
    })();

    connectPromisesRef.current.set(sessionId, promise);
    try {
      await promise;
    } finally {
      connectPromisesRef.current.delete(sessionId);
    }
  }

  async function ensureSessionReady(sessionId: string): Promise<void> {
    const base = stripSessionFromWsUrl(wsUrl);
    if (!base.trim()) throw new Error("WebSocket URL is required");
    const rt = getOrCreateRuntime(sessionId);
    const ws = rt.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      await connectSession(sessionId, withSessionInWsUrl(base, sessionId));
    }
    await initializeSession(sessionId);
  }

  async function connectNewSession(): Promise<string> {
    const base = stripSessionFromWsUrl(wsUrl);
    if (!base.trim()) throw new Error("WebSocket URL is required");
    const pendingId = `__pending__${Date.now()}_${Math.random().toString(16).slice(2)}`;

    let resolveSessionId: (id: string) => void = () => {};
    let rejectSessionId: (e: Error) => void = () => {};
    const sessionIdPromise = new Promise<string>((resolve, reject) => {
      resolveSessionId = resolve;
      rejectSessionId = reject;
    });

    const rt: SessionRuntime = {
      ws: null,
      keyRef: { current: pendingId },
      nextId: 1,
      pending: new Map(),
      initialized: false,
      sessionIdPromise,
      resolveSessionId,
      rejectSessionId
    };

    runtimesRef.current.set(pendingId, rt);

    await connectSession(pendingId, base);
    setConnStatus(pendingId, { ok: true, text: "connected" });

    const realSessionId = await Promise.race<string>([
      rt.sessionIdPromise,
      new Promise<string>((_, reject) =>
        window.setTimeout(() => reject(new Error("Timed out waiting for session id")), 15000)
      )
    ]);

    await ensureSessionReady(realSessionId);
    setExpandedSessions((prev) => ({ ...prev, [realSessionId]: true }));
    setActiveSessionId(realSessionId);
    await startThreadInSession(realSessionId);

    return realSessionId;
  }

  async function startThreadInSession(sessionId: string): Promise<string> {
    await ensureSessionReady(sessionId);
    const result = await rpc(sessionId, "thread/start", { cwd, approvalPolicy, sandbox: "danger-full-access" });
    const tid = (result as { thread?: { id?: string } })?.thread?.id;
    if (!isNonEmptyString(tid)) throw new Error("Invalid thread/start response");

    setCurrentThreadBySession((prev) => ({ ...prev, [sessionId]: tid }));
    setActiveSessionId(sessionId);
    setActiveThreadId(tid);
    setExpandedSessions((prev) => ({ ...prev, [sessionId]: true }));

    const key = threadKey(sessionId, tid);
    setChatByThreadKey((prev) => ({ ...prev, [key]: emptyThreadChatState() }));
    return tid;
  }

  async function resumeThreadInSession(sessionId: string, targetThreadId: string): Promise<string> {
    await ensureSessionReady(sessionId);
    const result = await rpc(sessionId, "thread/resume", { threadId: targetThreadId });
    const tid = (result as { thread?: { id?: string } })?.thread?.id;
    if (!isNonEmptyString(tid)) throw new Error("Invalid thread/resume response");

    setCurrentThreadBySession((prev) => ({ ...prev, [sessionId]: tid }));
    setActiveSessionId(sessionId);
    setActiveThreadId(tid);

    const key = threadKey(sessionId, tid);
    setChatByThreadKey((prev) => (prev[key] ? prev : { ...prev, [key]: emptyThreadChatState() }));
    return tid;
  }

  async function loadThreadHistory(sessionId: string, threadId: string): Promise<void> {
    await ensureSessionReady(sessionId);

    const result = await rpc(sessionId, "thread/read", { threadId, includeTurns: true });
    const obj = isRecord(result) ? result : null;
    const thread = obj && isRecord(obj["thread"]) ? obj["thread"] : null;
    const turns = thread ? thread["turns"] : null;
    const messages = turnsToChatMessages(turns);

    const key = threadKey(sessionId, threadId);
    setChatByThreadKey((prev) => {
      const current = prev[key] ?? emptyThreadChatState();
      if (current.hydrated) return prev;
      if (current.turnInProgress) return prev;
      if (current.messages.length > 0) return { ...prev, [key]: { ...current, hydrated: true } };
      return { ...prev, [key]: { ...current, messages, hydrated: true } };
    });
  }

  async function disconnectSession(sessionId: string): Promise<void> {
    closeSessionSocket(sessionId, "disconnect");
  }

  async function connectOrCreateForUi(): Promise<void> {
    const base = stripSessionFromWsUrl(wsUrl);
    if (!base.trim()) throw new Error("WebSocket URL is required");

    if (activeSessionId) {
      await ensureSessionReady(activeSessionId);
      const current = currentThreadBySession[activeSessionId] ?? null;
      if (!current) {
        await startThreadInSession(activeSessionId);
      }
      return;
    }

    const list = sessions ?? (await refreshSessions({ silent: true, throwOnError: true }));
    const existing = list.find((s) => isNonEmptyString(s.sessionId))?.sessionId ?? null;
    if (existing) {
      setActiveSessionId(existing);
      await ensureSessionReady(existing);
      return;
    }

    await connectNewSession();
  }

	  async function sendTurn(): Promise<void> {
	    const text = prompt.trim();
	    if (!text) return;
	    if (isActiveSessionBusy) return;

	    try {
	      let sessionId = activeSessionId;
	      if (!isNonEmptyString(sessionId)) {
	        const list = sessions ?? (await refreshSessions({ silent: true, throwOnError: true }));
	        const existing = list.find((s) => isNonEmptyString(s.sessionId))?.sessionId ?? null;
	        if (existing) {
	          sessionId = existing;
	          setActiveSessionId(existing);
	          await ensureSessionReady(existing);
	        } else {
	          sessionId = await connectNewSession();
	        }
	      } else {
	        await ensureSessionReady(sessionId);
	      }

	      let threadId: string | null = null;
	      if (sessionId === activeSessionId) threadId = activeThreadId;
	      if (!isNonEmptyString(threadId)) {
	        const fromMap = currentThreadBySession[sessionId] ?? null;
	        if (isNonEmptyString(fromMap)) threadId = fromMap;
	      }
	      if (!isNonEmptyString(threadId)) {
	        threadId = await startThreadInSession(sessionId);
	      }

	      setActivePane("chat");
	      setActiveSessionId(sessionId);
	      setActiveThreadId(threadId);

	      const key = threadKey(sessionId, threadId);
	      const userMsg: ChatMessage = { id: nowId("u"), role: "user", text };
	      const preview = promptToThreadPreview(text);

	      setThreadsBySession((prev) => {
	        const existing = prev[sessionId];
	        if (!existing || !Array.isArray(existing)) {
	          return { ...prev, [sessionId]: [{ id: threadId, preview, updatedAt: Date.now() }] };
	        }
	        const idx = existing.findIndex((t) => t.id === threadId);
	        if (idx === -1) {
	          return { ...prev, [sessionId]: [{ id: threadId, preview }, ...existing] };
	        }
	        const row = existing[idx];
	        const updated: ThreadRow = {
	          ...row,
	          preview: row.preview.trim() ? row.preview : preview,
	          updatedAt: Date.now()
	        };
	        return { ...prev, [sessionId]: [updated, ...existing.slice(0, idx), ...existing.slice(idx + 1)] };
	      });

	      setChatByThreadKey((prev) => {
	        const current = prev[key] ?? emptyThreadChatState();
	        return {
	          ...prev,
	          [key]: {
	            ...current,
	            messages: [...current.messages, userMsg],
	            turnInProgress: true,
	            turnId: null,
	            reasoningSummary: "",
	            reasoningSummaryIndex: null,
	            hydrated: true
	          }
	        };
	      });
	      setPrompt("");

		      try {
		        const result = await rpc(sessionId, "turn/start", {
		          threadId,
		          input: [{ type: "text", text }],
		          cwd,
		          approvalPolicy,
		          sandboxPolicy: { type: "dangerFullAccess" }
		        });
		        const turnId = (result as { turn?: { id?: string } })?.turn?.id;
		        if (isNonEmptyString(turnId)) {
		          setChatByThreadKey((prev) => {
		            const current = prev[key] ?? { ...emptyThreadChatState(), turnInProgress: true };
	            return { ...prev, [key]: { ...current, turnId } };
	          });
	        }
	      } catch (e) {
	        setChatByThreadKey((prev) => {
	          const current = prev[key] ?? { ...emptyThreadChatState(), turnInProgress: true };
	          return { ...prev, [key]: { ...current, turnInProgress: false, turnId: null } };
	        });
	        throw e;
	      }
	    } catch (e) {
	      toast.error((e as Error)?.message || String(e));
	    }
	  }

  async function refreshSessions(opts?: { silent?: boolean; throwOnError?: boolean }): Promise<SessionRow[]> {
    setSessionsBusy(true);
    setSessionsError(null);
    try {
      const token = extractTokenFromWsUrl(wsUrl);
      const base = httpBaseFromWsUrl(wsUrl) ?? "";
      const headers: Record<string, string> = {};
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const resp = await fetch(`${base}/sessions`, { headers });
      if (!resp.ok) throw new Error(`Failed to list sessions: ${resp.status}`);
      const body = (await resp.json()) as { sessions?: SessionRow[] };
      const list = body.sessions ?? [];
      setSessions(list);
      return list;
    } catch (e) {
      const msg = (e as Error)?.message || String(e);
      setSessionsError(msg);
      if (!opts?.silent) toast.error(msg);
      if (opts?.throwOnError) throw e instanceof Error ? e : new Error(msg);
      return [];
    } finally {
      setSessionsBusy(false);
    }
  }

  async function deleteSession(id: string): Promise<void> {
    setSessionsBusy(true);
    try {
      closeSessionSocket(id, "deleting session");
      if (activeSessionId === id) {
        setActiveThreadId(null);
        setActiveSessionId(null);
      }
      setExpandedSessions((prev) => {
        if (!prev[id]) return prev;
        const next = { ...prev };
        delete next[id];
        return next;
      });
      setThreadsBySession((prev) => {
        if (prev[id] === undefined) return prev;
        const next = { ...prev };
        delete next[id];
        return next;
      });
      setThreadsBusyBySession((prev) => {
        if (prev[id] === undefined) return prev;
        const next = { ...prev };
        delete next[id];
        return next;
      });
      setThreadsErrorBySession((prev) => {
        if (prev[id] === undefined) return prev;
        const next = { ...prev };
        delete next[id];
        return next;
      });

      const token = extractTokenFromWsUrl(wsUrl);
      const base = httpBaseFromWsUrl(wsUrl) ?? "";
      const headers: Record<string, string> = {};
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const resp = await fetch(`${base}/sessions/${encodeURIComponent(id)}`, {
        method: "DELETE",
        headers
      });
      if (!resp.ok) throw new Error(`Failed to delete session: ${resp.status}`);
      toast.success("Session deleted");
      await refreshSessions();
    } catch (e) {
      toast.error((e as Error)?.message || String(e));
    } finally {
      setSessionsBusy(false);
    }
  }

  async function refreshThreadsForSession(sessionId: string, opts?: { silent?: boolean }): Promise<void> {
    if (!isNonEmptyString(sessionId)) return;
    setThreadsBusyBySession((prev) => ({ ...prev, [sessionId]: true }));
    setThreadsErrorBySession((prev) => ({ ...prev, [sessionId]: null }));

    try {
      await ensureSessionReady(sessionId);
      const result = await rpc(sessionId, "thread/list", {
        limit: 50,
        sortKey: "updated_at",
        archived: false
      });

      const obj = isRecord(result) ? result : null;
      const data = obj && Array.isArray(obj["data"]) ? obj["data"] : [];

      const rows: ThreadRow[] = [];
      for (const raw of data) {
        if (!isRecord(raw)) continue;
        const id = getString(raw["id"]);
        if (!isNonEmptyString(id)) continue;
        const path = getString(raw["path"]);
        if (isArchivedThreadPath(path)) continue;
        rows.push({
          id,
          preview: getString(raw["preview"]) ?? "",
          modelProvider: getString(raw["modelProvider"]) ?? undefined,
          createdAt: getNumber(raw["createdAt"]) ?? undefined,
          updatedAt: getNumber(raw["updatedAt"]) ?? undefined,
          path: path ?? undefined
        });
      }

      setThreadsBySession((prev) => {
        const existing = prev[sessionId];
        const currentThread = currentThreadBySession[sessionId] ?? null;

        const existingById = new Map<string, ThreadRow>();
        if (Array.isArray(existing)) {
          for (const row of existing) existingById.set(row.id, row);
        }

        const merged = rows.map((row) => {
          const prevRow = existingById.get(row.id);
          if (!prevRow) return row;
          return {
            ...row,
            preview: row.preview.trim() ? row.preview : prevRow.preview,
            modelProvider: row.modelProvider ?? prevRow.modelProvider,
            createdAt: row.createdAt ?? prevRow.createdAt,
            updatedAt: row.updatedAt ?? prevRow.updatedAt,
            path: row.path ?? prevRow.path
          };
        });

        if (isNonEmptyString(currentThread) && !merged.some((t) => t.id === currentThread)) {
          const keep = existingById.get(currentThread);
          if (keep) return { ...prev, [sessionId]: [keep, ...merged] };
        }

        return { ...prev, [sessionId]: merged };
      });
    } catch (e) {
      const msg = (e as Error)?.message || String(e);
      setThreadsErrorBySession((prev) => ({ ...prev, [sessionId]: msg }));
      if (!opts?.silent) toast.error(msg);
    } finally {
      setThreadsBusyBySession((prev) => ({ ...prev, [sessionId]: false }));
    }
  }

  async function openThreadInSession(targetSessionId: string, targetThreadId: string): Promise<void> {
    if (!isNonEmptyString(targetSessionId) || !isNonEmptyString(targetThreadId)) return;
    setActivePane("chat");
    setExpandedSessions((prev) => ({ ...prev, [targetSessionId]: true }));
    setActiveSessionId(targetSessionId);
    try {
      const tid = await resumeThreadInSession(targetSessionId, targetThreadId);
      void loadThreadHistory(targetSessionId, tid).catch((e) => {
        toast.error((e as Error)?.message || String(e));
      });
    } catch {
      await startThreadInSession(targetSessionId);
    }
  }

  function copyText(text: string): void {
    void navigator.clipboard.writeText(text).then(
      () => toast.success("Copied"),
      () => toast.error("Copy failed")
    );
  }

  async function archiveThread(sessionId: string, threadId: string, rolloutPath?: string): Promise<void> {
    if (!isNonEmptyString(sessionId) || !isNonEmptyString(threadId)) return;
    try {
      await ensureSessionReady(sessionId);
      // Be liberal in what we send: some app-server builds use `threadId`, others `thread_id`.
      await rpc(sessionId, "thread/archive", { threadId, thread_id: threadId });

      const checkArchived = async (): Promise<boolean> => {
        try {
          const read = await rpc(sessionId, "thread/read", { threadId, includeTurns: false });
          const obj = isRecord(read) ? read : null;
          const thread = obj && isRecord(obj["thread"]) ? obj["thread"] : null;
          const path = thread ? getString(thread["path"]) : null;
          // If `thread/read` resolves and the path is not under archived_sessions,
          // the rollout is still in sessions => not archived.
          return isArchivedThreadPath(path);
        } catch {
          // `thread/read` can't find archived threads in many builds; treat "not readable" as archived.
          return true;
        }
      };

      let archived = await checkArchived();
      if (!archived) {
        if (!isNonEmptyString(rolloutPath)) {
          throw new Error("Archive did not take effect (thread still readable); missing rollout path for fallback.");
        }
        // Legacy fallback for builds where `thread/archive` is a no-op.
        await rpc(sessionId, "archiveConversation", {
          conversationId: threadId,
          conversation_id: threadId,
          rolloutPath,
          rollout_path: rolloutPath
        });
        archived = await checkArchived();
        if (!archived) {
          throw new Error("Archive did not take effect (thread is still listed as active).");
        }
      }

      setThreadsBySession((prev) => {
        const list = prev[sessionId];
        if (!list) return prev;
        const next = list.filter((t) => t.id !== threadId);
        return { ...prev, [sessionId]: next };
      });

      const key = threadKey(sessionId, threadId);
      setChatByThreadKey((prev) => {
        if (!prev[key]) return prev;
        const next = { ...prev };
        delete next[key];
        return next;
      });

      setCurrentThreadBySession((prev) => {
        if (prev[sessionId] !== threadId) return prev;
        return { ...prev, [sessionId]: null };
      });

      if (activeSessionId === sessionId && activeThreadId === threadId) {
        setActiveThreadId(null);
      }

      toast.success("Thread archived");
      void refreshThreadsForSession(sessionId, { silent: true });
    } catch (e) {
      toast.error((e as Error)?.message || String(e));
    }
  }

  const sessionsLoaded = sessions !== null;
  const sessionsList = sessions ?? [];
  const sessionsQuery = historyQuery.trim().toLowerCase();
  const sessionsFiltered = sessionsQuery
    ? sessionsList.filter((s) => {
        const sid = (s.sessionId ?? "").toLowerCase();
        const name = (s.name ?? "").toLowerCase();
        const status = (s.status ?? "").toLowerCase();
        const cid = (s.containerId ?? "").toLowerCase();
        return sid.includes(sessionsQuery) || name.includes(sessionsQuery) || status.includes(sessionsQuery) || cid.includes(sessionsQuery);
      })
    : sessionsList;

  return (
    <div className="relative min-h-dvh">
      <div className="pointer-events-none fixed inset-0 z-0 overflow-hidden">
        <div className="absolute inset-0 argus-landing-canvas" />
        <div className="absolute inset-0 argus-landing-grid-64 opacity-70" />
        <div className="absolute inset-0 argus-landing-noise" />
        <div className="absolute -left-[30%] -top-[25%] h-[70vh] w-[70vw] argus-landing-blob argus-landing-blob-a" />
        <div className="absolute -right-[30%] -top-[20%] h-[70vh] w-[70vw] argus-landing-blob argus-landing-blob-b" />
        <div className="absolute -bottom-[30%] -right-[10%] h-[80vh] w-[80vw] argus-landing-blob argus-landing-blob-c" />
      </div>

      <main className="relative z-[1] h-dvh w-full overflow-hidden">
        <div className="grid h-dvh w-full grid-cols-[320px_1fr]">
          <aside
            className={cn(
              "flex h-dvh flex-col overflow-hidden border-r border-border bg-card/80 backdrop-blur-xl",
              "shadow-[0_0_0_1px_oklch(var(--border)/0.55),0_14px_40px_oklch(0%_0_0/0.35)]"
            )}
          >
            <div className="flex items-end gap-3 border-b border-border/60 bg-background/70 p-4 backdrop-blur-md">
              <div
                className={cn(
                  "flex h-11 w-11 items-center justify-center rounded-2xl border border-border",
                  "bg-background/70 backdrop-blur-md",
                  "shadow-[0_0_0_1px_oklch(var(--border)/0.55),0_14px_40px_oklch(0%_0_0/0.35)]"
                )}
              >
                <PlugZap className="h-5 w-5 text-primary" />
              </div>
              <div className="min-w-0">
                <div className="truncate font-logo text-base tracking-wide text-foreground">Argus</div>
                <div className="truncate text-sm text-muted-foreground">App-server WebSocket bridge</div>
              </div>
            </div>

            <div className="border-b border-border/60 bg-background/70 p-4 backdrop-blur-md">
              <div className="relative">
                <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  className="pl-9"
                  value={historyQuery}
                  onChange={(e) => setHistoryQuery(e.target.value)}
                  placeholder="Search…"
                  spellCheck={false}
                />
              </div>
            </div>

            <div
              className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden p-2"
              onClick={() => {
                setSessionMenuOpenFor(null);
                setThreadMenuOpenFor(null);
              }}
            >
              <SidebarNavItem
                icon={<LinkIcon className="h-4 w-4" />}
                label="Connection"
                active={activePane === "connection"}
                onClick={() => setActivePane("connection")}
              />

              <div className="mt-1 flex items-center gap-2 rounded-xl border border-transparent px-2 py-2 transition-colors hover:border-border/60 hover:bg-background/50">
                <div className="flex h-9 w-9 items-center justify-center rounded-xl border border-border/60 bg-background/60 text-muted-foreground">
                  <PlugZap className="h-4 w-4" />
                </div>
                <div className="min-w-0 flex-1 truncate text-sm text-muted-foreground">Sessions</div>
                <Button
                  type="button"
                  size="sm"
                  onClick={() =>
                    void (async () => {
                      setActivePane("chat");
                      await connectNewSession();
                    })().catch((e) => toast.error((e as Error)?.message || String(e)))
                  }
                  disabled={!wsUrl.trim()}
                >
                  New
                </Button>
              </div>

              {sessions === null ? (
                <div className="mt-2 rounded-xl border border-border/60 bg-background/60 px-3 py-2 text-sm text-muted-foreground">
                  {sessionsError ? (
                    <>
                      Failed to load sessions: <code className="font-mono break-words">{sessionsError}</code>
                    </>
                  ) : (
                    <span className="inline-flex items-center gap-2">
                      <RefreshCw className="h-4 w-4 animate-spin" />
                      Connecting…
                    </span>
                  )}
                </div>
              ) : sessionsFiltered.length === 0 ? (
                <div className="mt-2 rounded-xl border border-border/60 bg-background/60 px-3 py-2 text-sm text-muted-foreground">
                  No sessions.
                </div>
              ) : (
                <div className="mt-2 flex flex-col gap-1">
                  {sessionsFiltered.map((s) => {
                    const sid = s.sessionId ?? "";
                    const isCurrent = sid && sid === activeSessionId;
                    const isExpanded = sid ? !!expandedSessions[sid] : false;
                    const canToggle = !!sid;
                    const isMenuOpen = sid && sessionMenuOpenFor === sid;
                    const conn = sid ? connBySession[sid] : undefined;
                    const connText = conn?.text ?? "disconnected";
                    const connOk = !!conn?.ok;
                    const threads = sid ? threadsBySession[sid] : null;
                    const threadsBusy = sid ? !!threadsBusyBySession[sid] : false;
                    const threadsError = sid ? threadsErrorBySession[sid] : null;

	                    return (
	                      <React.Fragment key={sid || s.containerId || Math.random().toString(16)}>
	                        <div
	                          className={cn(
	                            "group flex items-center gap-2 rounded-xl border border-transparent",
	                            "transition-colors",
	                            "focus-within:ring-4 focus-within:ring-ring/25",
	                            isCurrent ? "border-primary/25 bg-primary/10" : "hover:border-border/60 hover:bg-background/50"
	                          )}
	                        >
	                          <button
	                            type="button"
	                            className={cn(
	                              "flex min-w-0 flex-1 items-center gap-2 px-2 py-2 text-left",
	                              "focus-visible:outline-none",
	                              canToggle ? "cursor-pointer" : "cursor-default"
	                            )}
                            onClick={() => {
                              if (!sid) return;
                              setActivePane("chat");
                              const nextExpanded = !expandedSessions[sid];
                              setExpandedSessions((prev) => ({ ...prev, [sid]: nextExpanded }));
                              setActiveSessionId(sid);
                              if (nextExpanded && !threadsBusy && threads == null) {
                                void refreshThreadsForSession(sid, { silent: true });
                              }
                            }}
                          >
                            {sid ? (
                              isExpanded ? (
                                <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
                              ) : (
                                <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
                              )
                            ) : (
                              <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground opacity-40" />
                            )}
                            <span
                              className={cn(
                                "h-1.5 w-1.5 shrink-0 rounded-full",
                                connOk ? "bg-success" : connText === "connecting…" ? "bg-warning" : "bg-border"
                              )}
                            />
                            <code className="min-w-0 flex-1 truncate font-mono text-xs text-foreground">
                              {sid || "-"}
                            </code>
                            {isCurrent ? <span className="text-xs font-medium text-primary">current</span> : null}
                          </button>

	                          <div className="relative shrink-0 pr-1">
	                            <Button
	                              type="button"
	                              size="sm"
	                              variant="ghost"
	                              disabled={!sid}
	                              className={cn(
	                                "h-8 w-8 rounded-xl border border-transparent p-0",
	                                "text-muted-foreground transition-colors hover:border-border/60 hover:bg-background/60 hover:text-foreground group-hover:text-foreground",
	                                isMenuOpen ? "border-border/60 bg-background/60 text-foreground" : null
	                              )}
	                              onClick={(e) => {
	                                e.stopPropagation();
	                                if (!sid) return;
	                                setSessionMenuOpenFor((prev) => (prev === sid ? null : sid));
	                              }}
	                              aria-label="Session menu"
	                            >
	                              <MoreHorizontal className="h-4 w-4" />
	                            </Button>
                            {sid && isMenuOpen ? (
                              <div
                                className={cn(
                                  "absolute right-0 top-full z-50 mt-2 w-48 overflow-hidden rounded-2xl border border-border/60",
                                  "bg-background/90 backdrop-blur-xl",
                                  "shadow-[0_0_0_1px_oklch(var(--border)/0.55),0_20px_60px_oklch(0%_0_0/0.45)]"
                                )}
                                onClick={(e) => e.stopPropagation()}
                              >
                                <button
                                  type="button"
                                  className={cn(
                                    "flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-foreground",
                                    "transition-colors hover:bg-background/60"
                                  )}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setSessionMenuOpenFor(null);
                                    copyText(sid);
                                  }}
                                >
                                  <Copy className="h-4 w-4 text-primary" />
                                  Copy session id
                                </button>
                                <button
                                  type="button"
                                  className={cn(
                                    "flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-foreground",
                                    "transition-colors hover:bg-background/60"
                                  )}
                                  disabled={!wsUrl.trim()}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setSessionMenuOpenFor(null);
                                    setActivePane("chat");
                                    void startThreadInSession(sid);
                                  }}
                                >
                                  <PlugZap className="h-4 w-4 text-primary" />
                                  New thread
                                </button>
                                <button
                                  type="button"
                                  className={cn(
                                    "flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-destructive",
                                    "transition-colors hover:bg-destructive/10"
                                  )}
                                  disabled={sessionsBusy}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setSessionMenuOpenFor(null);
                                    void deleteSession(sid);
                                  }}
                                >
                                  <Trash2 className="h-4 w-4" />
                                  Delete session
                                </button>
                              </div>
                            ) : null}
                          </div>
                        </div>

                        {sid && isExpanded ? (
                          <div className="mb-2 mt-1">
                            {threadsBusy && threads == null ? (
                              <div className="flex items-center gap-2 rounded-xl px-2 py-1.5 text-sm text-muted-foreground">
                                <RefreshCw className="h-4 w-4 animate-spin" />
                                Loading threads…
                              </div>
                            ) : threadsError ? (
                              <div className="break-words rounded-xl border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                                {threadsError}
                              </div>
                            ) : !threads ? (
                              <div className="rounded-xl px-2 py-1.5 text-sm text-muted-foreground">
                                Loading threads…
                              </div>
                            ) : threads.length === 0 ? (
                              <div className="rounded-xl px-2 py-1.5 text-sm text-muted-foreground">
                                No threads.
                              </div>
                            ) : (
                              <div className="flex flex-col gap-1">
	                                {threads.map((t) => {
	                                  const isCurrentThread = t.id === (currentThreadBySession[sid] ?? null);
	                                  const label = t.preview?.trim() ? t.preview.trim() : "(no preview)";
	                                  const threadKeyId = `${sid}:${t.id}`;
	                                  const isThreadMenuOpen = threadMenuOpenFor === threadKeyId;
	                                  return (
		                                    <div
		                                      key={t.id}
		                                      className={cn(
		                                        "group flex items-center gap-2 rounded-xl border border-transparent",
		                                        "transition-colors",
		                                        "focus-within:border-primary/25 focus-within:bg-primary/10 focus-within:text-foreground",
		                                        isCurrentThread
		                                          ? "border-primary/25 bg-primary/10 text-foreground"
		                                          : "text-foreground/90 hover:border-primary/25 hover:bg-primary/10 hover:text-foreground"
		                                      )}
		                                    >
	                                      <button
	                                        type="button"
	                                        title={label}
	                                        className={cn(
	                                          "min-w-0 flex-1 cursor-pointer px-2 py-2 text-left",
	                                          "focus-visible:outline-none"
	                                        )}
	                                        onClick={() => {
	                                          setThreadMenuOpenFor(null);
	                                          void openThreadInSession(sid, t.id);
	                                        }}
	                                      >
	                                        <span className="flex min-w-0 items-center gap-2">
	                                          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-muted-foreground/60" />
	                                          <span className="min-w-0 flex-1 truncate text-sm">{label}</span>
	                                        </span>
	                                      </button>
	
	                                      <div className="relative shrink-0 pr-1">
	                                        <Button
	                                          type="button"
	                                          variant="ghost"
	                                          size="sm"
	                                          className={cn(
	                                            "h-8 w-8 rounded-xl border border-transparent p-0",
	                                            "text-muted-foreground transition-colors hover:border-border/60 hover:bg-background/60 hover:text-foreground group-hover:text-foreground",
	                                            isThreadMenuOpen ? "border-border/60 bg-background/60 text-foreground" : ""
	                                          )}
	                                          onClick={(e) => {
	                                            e.stopPropagation();
	                                            setSessionMenuOpenFor(null);
	                                            setThreadMenuOpenFor((prev) => (prev === threadKeyId ? null : threadKeyId));
                                          }}
                                          aria-label="Thread menu"
                                        >
                                          <MoreHorizontal className="h-4 w-4" />
                                        </Button>

                                        {isThreadMenuOpen ? (
                                          <div
                                            className={cn(
                                              "absolute right-0 top-full z-50 mt-2 w-48 overflow-hidden rounded-2xl border border-border/60",
                                              "bg-background/90 backdrop-blur-xl",
                                              "shadow-[0_0_0_1px_oklch(var(--border)/0.55),0_20px_60px_oklch(0%_0_0/0.45)]"
                                            )}
                                            onClick={(e) => e.stopPropagation()}
                                          >
                                            <button
                                              type="button"
                                              className={cn(
                                                "flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-foreground",
                                                "transition-colors hover:bg-background/60"
                                              )}
                                              onClick={(e) => {
                                                e.stopPropagation();
                                                setThreadMenuOpenFor(null);
                                                copyText(t.id);
                                              }}
                                            >
                                              <Copy className="h-4 w-4 text-primary" />
                                              Copy thread id
                                            </button>
                                            <button
                                              type="button"
                                              className={cn(
                                                "flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-foreground",
                                                "transition-colors hover:bg-background/60"
                                              )}
                                              onClick={(e) => {
                                                e.stopPropagation();
                                                setThreadMenuOpenFor(null);
                                                void archiveThread(sid, t.id, t.path);
                                              }}
                                            >
                                              <Archive className="h-4 w-4 text-primary" />
                                              Archive thread
                                            </button>
                                          </div>
                                        ) : null}
                                      </div>
                                    </div>
                                  );
                                })}
                              </div>
                            )}
                          </div>
                        ) : null}
                      </React.Fragment>
                    );
                  })}
                </div>
              )}
            </div>
          </aside>

          <section className="relative flex min-h-0 min-w-0 flex-col overflow-hidden bg-card/80 backdrop-blur-xl">
            {activePane === "connection" ? (
              <>
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 bg-background/70 p-4 backdrop-blur-md">
                  <div className="flex items-center gap-2 text-sm font-medium text-foreground">
                    <LinkIcon className="h-4 w-4 text-primary" />
                    <span>Connection</span>
                  </div>
                  <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
                    <div
                      className={cn(
                        "inline-flex items-center gap-2 rounded-full border px-3 py-1",
                        "bg-background/70 backdrop-blur-md",
                        activeConnStatus?.ok
                          ? "border-success/40 text-success"
                          : "border-border/60 text-muted-foreground"
                      )}
                    >
                      {activeConnStatus?.ok ? (
                        <CheckCircle2 className="h-4 w-4" />
                      ) : (
                        <XCircle className="h-4 w-4" />
                      )}
                      <span className="font-mono">{activeConnStatus?.text ?? "disconnected"}</span>
                    </div>
                    <IdPill label="session" value={activeSessionId} />
                  </div>
                </div>

                <div className="flex-1 overflow-auto p-4 scrollbar-hide md:p-5">
                  <div className="mx-auto grid w-full max-w-2xl gap-4">
                    <div
                      className={cn(
                        "rounded-2xl border border-border/60 bg-background/60 p-4",
                        "shadow-[0_0_0_1px_oklch(var(--border)/0.55),0_14px_40px_oklch(0%_0_0/0.35)]"
                      )}
                    >
                      <div className="grid gap-4">
                        <div className="grid gap-1.5">
                          <FieldLabel icon={<LinkIcon className="h-4 w-4" />} label="WebSocket URL" />
                          <Input value={wsUrl} onChange={(e) => setWsUrl(e.target.value)} spellCheck={false} />
                        </div>

                        <div className="grid gap-3 sm:grid-cols-2">
                          <div className="grid gap-1.5">
                            <FieldLabel label="CWD" />
                            <Input value={cwd} onChange={(e) => setCwd(e.target.value)} spellCheck={false} />
                          </div>
                          <div className="grid gap-1.5">
                            <FieldLabel label="Approval" />
                            <select
                              className={cn(
                                "h-10 w-full rounded-xl border border-input bg-background/70 px-3 text-sm text-foreground outline-none",
                                "focus-visible:ring-4 focus-visible:ring-ring/25"
                              )}
                              value={approvalPolicy}
                              onChange={(e) => setApprovalPolicy(e.target.value as ApprovalPolicy)}
                            >
                              <option value="never">never</option>
                              <option value="on-request">on-request</option>
                              <option value="on-failure">on-failure</option>
                              <option value="untrusted">untrusted</option>
                            </select>
                          </div>
                        </div>

	                        <div className="flex flex-wrap items-center gap-2">
	                          <Button
	                            type="button"
	                            variant={activeConnStatus?.ok ? "destructive" : "default"}
	                            onClick={() =>
	                              void (async () => {
	                                if (activeConnStatus?.ok && activeSessionId) {
	                                  await disconnectSession(activeSessionId);
	                                  return;
	                                }
	                                await connectOrCreateForUi();
	                              })().catch((e) => toast.error((e as Error)?.message || String(e)))
	                            }
	                            disabled={!wsUrl.trim() || sessionsBusy}
	                          >
	                            {activeConnStatus?.ok ? "Disconnect" : "Connect"}
	                          </Button>
	                        </div>
	                      </div>
                    </div>
                  </div>
                </div>
              </>
            ) : (
              <>
                <div
                  className={cn(
                    "pointer-events-none absolute inset-x-0 top-0 z-10",
                    "h-16 md:h-20",
                    "bg-background/75 backdrop-blur-xl",
                    "[mask-image:linear-gradient(to_bottom,black_0%,black_35%,transparent_100%)]",
                    "[-webkit-mask-image:linear-gradient(to_bottom,black_0%,black_35%,transparent_100%)]"
                  )}
                />

                <div className="absolute right-4 top-4 z-20">
                  <Button
                    type="button"
                    variant="ghost"
                    className={cn(
                      "h-10 w-10 rounded-full border border-border/60 bg-background/50 p-0 text-foreground/85",
                      "shadow-[0_0_0_1px_oklch(var(--border)/0.55),0_14px_40px_oklch(0%_0_0/0.25)]",
                      "hover:bg-background/70 hover:text-foreground"
                    )}
                    onClick={() =>
                      void (async () => {
                        if (!activeSessionId) {
                          setActivePane("connection");
                          toast.error("No session connected");
                          return;
                        }
                        await startThreadInSession(activeSessionId);
                      })().catch((e) => toast.error((e as Error)?.message || String(e)))
                    }
                    aria-label="New conversation"
                    title="New conversation"
                  >
                    <SquarePen className="h-4 w-4" />
                  </Button>
                </div>

	                <div className="relative flex-1 min-h-0">
	                  <div
	                    ref={chatScrollRef}
	                    className="h-full overflow-auto p-4 pt-12 pb-44 scrollbar-hide md:p-5 md:pt-14 md:pb-48"
	                  >
	                    <div className="mx-auto grid w-full max-w-3xl grid-cols-1 gap-3">
	                      {(activeChat?.messages ?? [])
	                        .filter((m) => m.role !== "assistant" || m.text.trim().length > 0)
	                        .map((m) => <Bubble key={m.id} message={m} />)}
	                      {activeChat?.turnInProgress ? (
	                        <TurnProgress summary={activeChat.reasoningSummary} />
	                      ) : null}
	                    </div>
	                  </div>
	
	                  <div className="pointer-events-none absolute inset-x-0 bottom-0 z-10 px-4 pb-5 md:px-5">
	                    <div className="relative mx-auto w-full max-w-3xl">
			                      <div className="pointer-events-auto flex items-center gap-3 rounded-full bg-background/55 px-3 py-2 backdrop-blur-xl">
		                        <button
		                          type="button"
		                          className={cn(
		                            "flex h-10 w-10 items-center justify-center rounded-full bg-background/50 text-muted-foreground",
		                            "transition-colors hover:bg-background/70 hover:text-foreground",
		                            "focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-ring/25",
		                            "disabled:opacity-50"
		                          )}
	                          disabled={!canSend}
	                          aria-label="Attach"
	                        >
	                          <Paperclip className="h-4 w-4" />
	                        </button>
	
	                        <textarea
	                          value={prompt}
	                          onChange={(e) => setPrompt(e.target.value)}
	                          onCompositionStart={() => {
	                            composingRef.current = true;
	                          }}
	                          onCompositionEnd={() => {
	                            composingRef.current = false;
	                          }}
	                          placeholder="Message Argus…"
	                          disabled={isActiveSessionBusy}
	                          rows={1}
	                          className={cn(
	                            "min-h-10 flex-1 resize-none bg-transparent px-2 py-2 text-sm text-foreground outline-none",
	                            "placeholder:text-muted-foreground/70"
	                          )}
	                          onKeyDown={(e) => {
	                            if (e.key === "Enter" && !e.shiftKey) {
	                              // Avoid sending while using IME (e.g. Chinese/Japanese input). Enter should commit composition.
	                              const native = e.nativeEvent as KeyboardEvent;
	                              const keyCode = (native as unknown as { keyCode?: number }).keyCode;
	                              if (composingRef.current || native.isComposing || keyCode === 229) return;
	                              e.preventDefault();
	                              void sendTurn();
	                            }
	                          }}
	                        />
	
		                        <Button
		                          type="button"
		                          variant="ghost"
		                          onClick={() => void sendTurn()}
		                          disabled={!canSend}
		                          className={cn(
		                            "h-10 w-10 rounded-full p-0",
		                            "bg-background/50 text-muted-foreground",
		                            "hover:bg-background/70 hover:text-foreground"
		                          )}
		                          aria-label="Send"
		                        >
		                          <ArrowUp className="h-4 w-4" />
		                        </Button>
	                      </div>
	                    </div>
	                  </div>
	                </div>
	              </>
	            )}
	          </section>
	        </div>
      </main>
    </div>
  );
}

interface SidebarNavItemProps {
  icon: React.ReactNode;
  label: string;
  active?: boolean;
  onClick?: () => void;
}

function SidebarNavItem({ icon, label, active = false, onClick }: SidebarNavItemProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex w-full items-center gap-3 rounded-xl border px-2 py-2 text-left",
        "transition-colors hover:bg-background/50",
        active ? "border-primary/25 bg-primary/10" : "border-transparent bg-transparent hover:border-border/60"
      )}
    >
      <span
        className={cn(
          "flex h-9 w-9 items-center justify-center rounded-xl border border-border/60 bg-background/60",
          active ? "text-primary" : "text-muted-foreground"
        )}
      >
        {icon}
      </span>
      <span className={cn("min-w-0 flex-1 truncate text-sm", active ? "text-foreground" : "text-muted-foreground")}>
        {label}
      </span>
    </button>
  );
}

function FieldLabel({ label, icon }: { label: string; icon?: React.ReactNode }) {
  return (
    <div className="mb-1.5 inline-flex items-center gap-2 text-xs text-muted-foreground">
      {icon ? <span className="text-primary">{icon}</span> : null}
      <span>{label}</span>
    </div>
  );
}

function IdPill({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="inline-flex items-center gap-2 rounded-full border border-border/60 bg-background/70 px-3 py-1 backdrop-blur-md">
      <span className="text-xs text-muted-foreground">{label}:</span>
      <code className="font-mono text-xs text-foreground">{value ?? "-"}</code>
    </div>
  );
}

const markdownComponents = {
  p: ({ children }: any) => <p className="my-2 whitespace-pre-wrap">{children}</p>,
  a: ({ href, children }: any) => (
    <a href={href} target="_blank" rel="noreferrer" className="underline underline-offset-4 hover:text-foreground">
      {children}
    </a>
  ),
  ul: ({ children }: any) => <ul className="my-2 list-disc space-y-1 pl-5">{children}</ul>,
  ol: ({ children }: any) => <ol className="my-2 list-decimal space-y-1 pl-5">{children}</ol>,
  li: ({ children }: any) => <li className="min-w-0">{children}</li>,
  blockquote: ({ children }: any) => (
    <blockquote className="my-3 border-l-2 border-border/70 pl-4 text-foreground/85">{children}</blockquote>
  ),
  h1: ({ children }: any) => <h1 className="my-3 text-lg font-semibold text-foreground">{children}</h1>,
  h2: ({ children }: any) => <h2 className="my-3 text-base font-semibold text-foreground">{children}</h2>,
  h3: ({ children }: any) => <h3 className="my-3 text-sm font-semibold text-foreground">{children}</h3>,
  hr: () => <hr className="my-4 border-border/70" />,
  code: ({ className, children, ...props }: any) => {
    const isBlock = typeof className === "string" && className.includes("language-");
    if (!isBlock) {
      return (
        <code
          className="rounded-md border border-border/60 bg-background/50 px-1.5 py-0.5 font-mono text-[0.85em] text-foreground"
          {...props}
        >
          {children}
        </code>
      );
    }
    return (
      <code className={cn("font-mono text-[0.85em] text-foreground", className)} {...props}>
        {children}
      </code>
    );
  },
  pre: ({ children }: any) => (
    <pre className="my-3 overflow-x-auto rounded-2xl border border-border/60 bg-background/55 p-4 font-mono text-[0.85em] leading-relaxed text-foreground shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.06)]">
      {children}
    </pre>
  ),
  table: ({ children }: any) => (
    <div className="my-3 overflow-x-auto rounded-2xl border border-border/60 bg-background/55">
      <table className="w-full border-collapse text-sm">{children}</table>
    </div>
  ),
  thead: ({ children }: any) => <thead className="bg-background/40">{children}</thead>,
  th: ({ children }: any) => (
    <th className="border-b border-border/60 px-3 py-2 text-left font-medium text-foreground">{children}</th>
  ),
  td: ({ children }: any) => <td className="border-b border-border/60 px-3 py-2 align-top">{children}</td>
};

function TurnProgress({ summary }: { summary: string }) {
  const hasSummary = summary.trim().length > 0;
  return (
    <div className="w-full">
      <div className="max-w-[72ch]">
        <div className="inline-flex items-center gap-2 text-xs text-muted-foreground">
          <RefreshCw className="h-3.5 w-3.5 animate-spin text-primary" />
          <span>正在生成…</span>
        </div>
        {hasSummary ? (
          <details className="group mt-2" open>
            <summary
              className={cn(
                "flex cursor-pointer list-none items-center gap-2 text-xs text-muted-foreground",
                "select-none [&::-webkit-details-marker]:hidden"
              )}
            >
              <ChevronRight className="h-3.5 w-3.5 transition-transform group-open:rotate-90" />
              <span>思考摘要</span>
            </summary>
            <div className="mt-2 break-words text-sm leading-relaxed text-foreground/70">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                {summary}
              </ReactMarkdown>
            </div>
          </details>
        ) : null}
      </div>
    </div>
  );
}

function ToolStatusBadge({ status }: { status: ToolMessageStatus }) {
  const label =
    status === "inProgress" ? "running" : status === "completed" ? "done" : status === "failed" ? "failed" : status;
  const className =
    status === "inProgress"
      ? "border-primary/25 bg-primary/10 text-primary"
      : status === "completed"
        ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-400"
        : status === "failed"
          ? "border-destructive/45 bg-destructive/15 text-destructive"
          : status === "declined"
            ? "border-border/60 bg-background/40 text-muted-foreground"
            : "border-border/60 bg-background/40 text-muted-foreground";

  return (
    <span className={cn("inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium", className)}>
      {status === "inProgress" ? <RefreshCw className="h-3 w-3 animate-spin" /> : null}
      {status === "completed" ? <CheckCircle2 className="h-3 w-3" /> : null}
      {status === "failed" ? <XCircle className="h-3 w-3" /> : null}
      <span>{label}</span>
    </span>
  );
}

function Bubble({ message }: { message: ChatMessage }) {
  if (message.role === "user") {
    return (
      <div className="flex w-full justify-end">
        <div
          className={cn(
            "w-fit max-w-[80%] rounded-2xl border border-primary/25 bg-primary/10 px-4 py-3",
            "shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.06)]"
          )}
        >
          <div className="whitespace-pre-wrap break-words text-sm leading-relaxed text-foreground">{message.text}</div>
        </div>
      </div>
    );
  }

  if (message.role === "assistant") {
    return (
      <div className="w-full">
        <div className="max-w-[72ch] break-words text-sm leading-relaxed text-foreground/90">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
            {message.text}
          </ReactMarkdown>
        </div>
      </div>
    );
  }

  if (message.role === "reasoning") {
    return (
      <div className="w-full">
        <details className="group max-w-[72ch] rounded-2xl border border-border/60 bg-background/40 px-4 py-3">
          <summary
            className={cn(
              "flex cursor-pointer list-none items-center gap-2 text-xs font-medium text-muted-foreground",
              "select-none [&::-webkit-details-marker]:hidden"
            )}
          >
            <ChevronRight className="h-3.5 w-3.5 transition-transform group-open:rotate-90" />
            <span>思考摘要</span>
          </summary>
          <div className="mt-2 break-words text-sm leading-relaxed text-foreground/70">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
              {message.text}
            </ReactMarkdown>
          </div>
        </details>
      </div>
    );
  }

  const meta = message.meta;
  const label =
    meta.kind === "commandExecution"
      ? "bash"
      : meta.kind === "fileChange"
        ? "patch"
        : meta.kind === "mcpToolCall"
          ? "tool"
          : meta.kind === "webSearch"
            ? "search"
            : meta.kind === "imageView"
              ? "image"
              : "tool";

  const commandLine =
    meta.kind === "commandExecution" && meta.command.trim().length > 0 ? `$ ${meta.command.trim()}` : "";

  const preBody =
    meta.kind === "commandExecution"
      ? `${commandLine}${commandLine && message.text.trim() ? "\n" : ""}${message.text}`.trimEnd()
      : message.text.trimEnd();

  return (
    <div className="w-full">
      <div className="max-w-[72ch]">
        <div className="relative overflow-hidden rounded-2xl border border-border/60 bg-background/55 shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.06)]">
          <div className="pointer-events-none absolute left-4 top-3 text-xs font-medium text-muted-foreground">
            {label}
          </div>
          <div className="absolute right-4 top-3">
            <ToolStatusBadge status={meta.status} />
          </div>

          <div className="px-4 pb-4 pt-10">
            {meta.kind === "mcpToolCall" && (meta.server.trim() || meta.tool.trim()) ? (
              <div className="text-xs text-muted-foreground">
                <span className="font-mono">{meta.server}</span>
                {meta.server && meta.tool ? <span> · </span> : null}
                <span className="font-mono">{meta.tool}</span>
              </div>
            ) : null}

            {meta.kind === "fileChange" && meta.files.length ? (
              <div className="text-xs text-muted-foreground">
                {meta.files.slice(0, 3).map((f) => f.path).join(", ")}
                {meta.files.length > 3 ? ` +${meta.files.length - 3}` : ""}
              </div>
            ) : null}

            {preBody.trim() ? (
              <pre
                className={cn(
                  "max-h-64 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-foreground",
                  meta.kind === "mcpToolCall" || (meta.kind === "fileChange" && meta.files.length) ? "mt-2" : "mt-0"
                )}
              >
                {preBody}
              </pre>
            ) : null}
          </div>

          {meta.kind === "fileChange" && meta.diffs.trim() ? (
            <details className="border-t border-border/60 px-4 py-3">
              <summary className="cursor-pointer select-none text-xs text-muted-foreground hover:text-foreground">
                View diffs
              </summary>
              <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-foreground">
                {meta.diffs}
              </pre>
            </details>
          ) : null}
        </div>
      </div>
    </div>
  );
}
