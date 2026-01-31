"use client";

import React from "react";
import {
  Archive,
  CheckCircle2,
  Copy,
  Link as LinkIcon,
  MessageSquare,
  PlugZap,
  RefreshCw,
  Search,
  Trash2,
  XCircle
} from "lucide-react";
import { toast } from "sonner";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

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
type SandboxMode = "workspace-write" | "read-only" | "danger-full-access";

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
}

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

function stripSessionFromWsUrl(url: string): string {
  try {
    const u = new URL(url);
    u.searchParams.delete("session");
    return u.toString();
  } catch {
    return url;
  }
}

function wsUrlHasSession(url: string): boolean {
  try {
    return new URL(url).searchParams.has("session");
  } catch {
    return false;
  }
}

function defaultWsUrl(): string {
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

function formatEpochSecondsOrMs(ts: number | undefined): string | null {
  if (ts === undefined) return null;
  const ms = ts < 100_000_000_000 ? ts * 1000 : ts;
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleString();
}

const STORAGE = {
  lastThreadId: "argus_webchat_last_thread_id",
  resumeLast: "argus_webchat_resume_last"
} as const;

function storageGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function storageSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // ignore
  }
}

function storageRemove(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {
    // ignore
  }
}

export default function Page() {
  const [mounted, setMounted] = React.useState<boolean>(false);
  const [wsUrl, setWsUrl] = React.useState<string>("");
  const [cwd, setCwd] = React.useState<string>("/workspace");
  const [approvalPolicy, setApprovalPolicy] = React.useState<ApprovalPolicy>("never");
  const [sandboxMode, setSandboxMode] = React.useState<SandboxMode>("workspace-write");
  const [resumeLast, setResumeLast] = React.useState<boolean>(true);
  const [savedThreadIdState, setSavedThreadIdState] = React.useState<string | null>(null);

  const [connStatus, setConnStatus] = React.useState<{ ok: boolean; text: string }>({
    ok: false,
    text: "disconnected"
  });

  const [sessionId, setSessionId] = React.useState<string | null>(null);
  const [threadId, setThreadId] = React.useState<string | null>(null);
  const [turnId, setTurnId] = React.useState<string | null>(null);
  const [turnInProgress, setTurnInProgress] = React.useState<boolean>(false);

  const [messages, setMessages] = React.useState<ChatMessage[]>([]);
  const [prompt, setPrompt] = React.useState<string>("");

  const [sessions, setSessions] = React.useState<SessionRow[] | null>(null);
  const [sessionsBusy, setSessionsBusy] = React.useState<boolean>(false);
  const [sessionsSearch, setSessionsSearch] = React.useState<string>("");
  const [sessionAttachId, setSessionAttachId] = React.useState<string>("");

  const [threads, setThreads] = React.useState<ThreadRow[] | null>(null);
  const [threadsBusy, setThreadsBusy] = React.useState<boolean>(false);
  const [threadsSearch, setThreadsSearch] = React.useState<string>("");
  const [threadsArchived, setThreadsArchived] = React.useState<boolean>(false);
  const [threadsNextCursor, setThreadsNextCursor] = React.useState<string | null>(null);

  const wsRef = React.useRef<WebSocket | null>(null);
  const nextIdRef = React.useRef<number>(1);
  const pendingRef = React.useRef<
    Map<number, { resolve: (v: JsonValue) => void; reject: (e: Error) => void }>
  >(new Map());
  const initializedRef = React.useRef<boolean>(false);

  const threadIdRef = React.useRef<string | null>(null);
  const turnIdRef = React.useRef<string | null>(null);
  const turnInProgressRef = React.useRef<boolean>(false);

  const chatScrollRef = React.useRef<HTMLDivElement | null>(null);

  const canSend = connStatus.ok && !!threadId && !turnInProgress;

  React.useEffect(() => {
    if (!mounted) return;
    storageSet(STORAGE.resumeLast, resumeLast ? "1" : "0");
  }, [mounted, resumeLast]);

  React.useEffect(() => {
    chatScrollRef.current?.scrollTo({ top: chatScrollRef.current.scrollHeight });
  }, [messages]);

  React.useEffect(() => {
    setMounted(true);
    setWsUrl(defaultWsUrl());

    const rawResume = storageGet(STORAGE.resumeLast);
    if (rawResume !== null) setResumeLast(rawResume === "1");

    const rawThreadId = storageGet(STORAGE.lastThreadId);
    if (rawThreadId && rawThreadId.trim()) setSavedThreadIdState(rawThreadId.trim());
  }, []);

  function saveThreadId(id: string): void {
    storageSet(STORAGE.lastThreadId, id);
    setSavedThreadIdState(id);
  }

  function clearSavedThreadId(): void {
    storageRemove(STORAGE.lastThreadId);
    setSavedThreadIdState(null);
    toast.message("Saved thread cleared");
  }

  function resetThreadUi(): void {
    threadIdRef.current = null;
    turnIdRef.current = null;
    turnInProgressRef.current = false;
    setThreadId(null);
    setTurnId(null);
    setTurnInProgress(false);
    setMessages([]);
  }

  function sendWire(obj: unknown): void {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      throw new Error("WebSocket is not connected");
    }
    ws.send(JSON.stringify(obj));
  }

  function rpc(method: string, params?: JsonValue): Promise<JsonValue> {
    const id = nextIdRef.current++;
    const req: RpcRequest = { method, id };
    if (params !== undefined) req.params = params;

    sendWire(req);

    return new Promise<JsonValue>((resolve, reject) => {
      pendingRef.current.set(id, { resolve, reject });
      window.setTimeout(() => {
        if (!pendingRef.current.has(id)) return;
        pendingRef.current.delete(id);
        reject(new Error(`Timeout waiting for response to ${method} (${id})`));
      }, 120000);
    });
  }

  async function initialize(): Promise<void> {
    if (initializedRef.current) return;
    await rpc("initialize", {
      clientInfo: { name: "argus_web", title: "Argus Web", version: "0.1.0" }
    });
    sendWire({ method: "initialized" });
    initializedRef.current = true;
  }

  async function startThread(): Promise<void> {
    resetThreadUi();
    const result = await rpc("thread/start", {
      cwd,
      approvalPolicy,
      sandbox: sandboxMode
    });
    const tid = (result as { thread?: { id?: string } })?.thread?.id;
    if (!isNonEmptyString(tid)) throw new Error("Invalid thread/start response");
    threadIdRef.current = tid;
    setThreadId(tid);
    saveThreadId(tid);
  }

  async function resumeThread(id: string): Promise<void> {
    resetThreadUi();
    const result = await rpc("thread/resume", { threadId: id });
    const tid = (result as { thread?: { id?: string } })?.thread?.id;
    if (!isNonEmptyString(tid)) throw new Error("Invalid thread/resume response");
    threadIdRef.current = tid;
    setThreadId(tid);
    saveThreadId(tid);
  }

  function handleServerRequest(msg: AnyWireMessage): void {
    if (typeof msg.id !== "number" || !isNonEmptyString(msg.method)) return;
    const id = msg.id;
    const method = msg.method;

    if (method === "item/commandExecution/requestApproval") {
      sendWire({ id, result: { decision: "decline" } });
      return;
    }
    if (method === "item/fileChange/requestApproval") {
      sendWire({ id, result: { decision: "decline" } });
      return;
    }

    sendWire({
      id,
      error: { code: -32601, message: `Unsupported server request: ${method}` }
    });
  }

  function handleNotification(msg: AnyWireMessage): void {
    if (!isNonEmptyString(msg.method)) return;
    const method = msg.method;
    const params = isRecord(msg.params) ? msg.params : {};

    if (method === "argus/session") {
      const id = params["id"];
      if (isNonEmptyString(id)) setSessionId(id);
      return;
    }

    if (method === "turn/started") {
      const incomingThreadId = params["threadId"];
      const currentThreadId = threadIdRef.current;
      if (!currentThreadId || incomingThreadId !== currentThreadId) return;

      const turn = isRecord(params["turn"]) ? params["turn"] : {};
      const incomingTurnId = getString(turn["id"]);
      if (turnInProgressRef.current && !turnIdRef.current && isNonEmptyString(incomingTurnId)) {
        turnIdRef.current = incomingTurnId;
        setTurnId(incomingTurnId);
      }
      return;
    }

    if (method === "item/agentMessage/delta") {
      const incomingThreadId = params["threadId"];
      const currentThreadId = threadIdRef.current;
      if (!currentThreadId || incomingThreadId !== currentThreadId) return;

      const incomingTurnId = getString(params["turnId"]);
      if (turnInProgressRef.current && !turnIdRef.current && isNonEmptyString(incomingTurnId)) {
        turnIdRef.current = incomingTurnId;
        setTurnId(incomingTurnId);
      }
      if (turnIdRef.current && incomingTurnId && incomingTurnId !== turnIdRef.current) return;

      const delta = params["delta"];
      if (!isNonEmptyString(delta)) return;

      setMessages((prev) => {
        if (prev.length === 0) return prev;
        const last = prev[prev.length - 1];
        if (last.role !== "assistant") return prev;
        return [...prev.slice(0, -1), { ...last, text: last.text + delta }];
      });
      return;
    }

    if (method === "item/completed") {
      const incomingThreadId = params["threadId"];
      const currentThreadId = threadIdRef.current;
      if (!currentThreadId || incomingThreadId !== currentThreadId) return;

      const incomingTurnId = getString(params["turnId"]);
      if (turnInProgressRef.current && !turnIdRef.current && isNonEmptyString(incomingTurnId)) {
        turnIdRef.current = incomingTurnId;
        setTurnId(incomingTurnId);
      }
      if (turnIdRef.current && incomingTurnId && incomingTurnId !== turnIdRef.current) return;

      const item = (params["item"] ?? {}) as Record<string, unknown>;
      if (item["type"] === "agentMessage") {
        const fullText = item["text"];
        if (isNonEmptyString(fullText)) {
          setMessages((prev) => {
            if (prev.length === 0) return prev;
            const last = prev[prev.length - 1];
            if (last.role !== "assistant") return prev;
            return [...prev.slice(0, -1), { ...last, text: fullText }];
          });
        }
      }
      return;
    }

    if (method === "turn/completed") {
      const incomingThreadId = params["threadId"];
      const currentThreadId = threadIdRef.current;
      if (!currentThreadId || incomingThreadId !== currentThreadId) return;

      if (!turnInProgressRef.current) return;
      const turn = isRecord(params["turn"]) ? params["turn"] : {};
      const incomingTurnId = getString(turn["id"]);
      if (turnIdRef.current && incomingTurnId && incomingTurnId !== turnIdRef.current) return;

      turnInProgressRef.current = false;
      turnIdRef.current = null;
      setTurnInProgress(false);
      setTurnId(null);
      return;
    }
  }

  function handleWireMessage(text: string): void {
    const msg = safeJsonParse(text);
    if (!msg) return;

    if (typeof msg.id === "number" && isNonEmptyString(msg.method)) {
      handleServerRequest(msg);
      return;
    }

    if (typeof msg.id === "number") {
      const pending = pendingRef.current.get(msg.id);
      if (!pending) return;
      pendingRef.current.delete(msg.id);
      const error = (msg as RpcResponse).error;
      if (error) pending.reject(new Error(error.message || "RPC error"));
      else pending.resolve((msg as RpcResponse).result ?? null);
      return;
    }

    if (isNonEmptyString(msg.method)) {
      handleNotification(msg);
    }
  }

  async function connect(customUrl?: string): Promise<void> {
    const targetUrl = customUrl ?? wsUrl;

    const prev = wsRef.current;
    if (
      prev &&
      (prev.readyState === WebSocket.OPEN || prev.readyState === WebSocket.CONNECTING)
    ) {
      try {
        prev.close(1000, "Switching connection");
      } catch {
        // ignore
      }
    }

    if (pendingRef.current.size) {
      const err = new Error("WebSocket connection was replaced");
      for (const [, p] of pendingRef.current) p.reject(err);
      pendingRef.current.clear();
    }

    setConnStatus({ ok: false, text: "connecting…" });
    setSessionId(null);
    resetThreadUi();
    initializedRef.current = false;

    let ws: WebSocket;
    try {
      ws = new WebSocket(targetUrl);
    } catch (e) {
      setConnStatus({ ok: false, text: "error" });
      toast.error((e as Error)?.message || String(e));
      return;
    }

    wsRef.current = ws;

    ws.addEventListener("open", async () => {
      if (wsRef.current !== ws) return;
      setConnStatus({ ok: true, text: "connected" });
      try {
        await initialize();
        const saved = savedThreadIdState;
        if (resumeLast && saved) {
          try {
            await resumeThread(saved);
          } catch {
            await startThread();
          }
        } else {
          await startThread();
        }
      } catch (e) {
        toast.error((e as Error)?.message || String(e));
      }
    });

    ws.addEventListener("close", (ev) => {
      if (wsRef.current !== ws) return;
      const code = ev.code;
      const reason = ev.reason;
      const msg = code ? `disconnected (${code}${reason ? `: ${reason}` : ""})` : "disconnected";
      setConnStatus({ ok: false, text: msg });
      initializedRef.current = false;
      setSessionId(null);
      resetThreadUi();
      wsRef.current = null;

      if (pendingRef.current.size) {
        const err = new Error("WebSocket connection closed");
        for (const [, p] of pendingRef.current) p.reject(err);
        pendingRef.current.clear();
      }
    });

    ws.addEventListener("error", () => {
      if (wsRef.current !== ws) return;
      setConnStatus({ ok: false, text: "error" });
    });

    ws.addEventListener("message", (ev) => {
      if (wsRef.current !== ws) return;
      if (typeof ev.data !== "string") return;
      handleWireMessage(ev.data);
    });
  }

  function disconnect(): void {
    const ws = wsRef.current;
    if (!ws) return;
    try {
      ws.close();
    } catch {
      // ignore
    }
  }

  async function sendTurn(): Promise<void> {
    const text = prompt.trim();
    if (!text || !threadId || turnInProgress) return;

    setMessages((prev) => [
      ...prev,
      { id: nowId("u"), role: "user", text },
      { id: nowId("a"), role: "assistant", text: "" }
    ]);
    setPrompt("");
    turnInProgressRef.current = true;
    turnIdRef.current = null;
    setTurnInProgress(true);
    setTurnId(null);

    try {
      const result = await rpc("turn/start", {
        threadId,
        input: [{ type: "text", text }],
        cwd,
        approvalPolicy,
        sandboxPolicy: { type: "externalSandbox", networkAccess: "enabled" }
      });
      const tid = (result as { turn?: { id?: string } })?.turn?.id;
      if (isNonEmptyString(tid)) {
        turnIdRef.current = tid;
        setTurnId(tid);
      }
    } catch (e) {
      turnInProgressRef.current = false;
      turnIdRef.current = null;
      setTurnInProgress(false);
      setTurnId(null);
      toast.error((e as Error)?.message || String(e));
    }
  }

  async function refreshSessions(): Promise<void> {
    setSessionsBusy(true);
    try {
      const token = extractTokenFromWsUrl(wsUrl);
      const base = httpBaseFromWsUrl(wsUrl) ?? "";
      const headers: Record<string, string> = {};
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const resp = await fetch(`${base}/sessions`, { headers });
      if (!resp.ok) throw new Error(`Failed to list sessions: ${resp.status}`);
      const body = (await resp.json()) as { sessions?: SessionRow[] };
      setSessions(body.sessions ?? []);
    } catch (e) {
      toast.error((e as Error)?.message || String(e));
    } finally {
      setSessionsBusy(false);
    }
  }

  async function deleteSession(id: string): Promise<void> {
    setSessionsBusy(true);
    try {
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

  async function refreshThreads(opts?: {
    cursor?: string;
    append?: boolean;
    archived?: boolean;
  }): Promise<void> {
    setThreadsBusy(true);
    try {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        throw new Error("Not connected");
      }

      const params: Record<string, JsonValue> = {
        limit: 50,
        sortKey: "updated_at",
        archived: opts?.archived ?? threadsArchived
      };
      if (opts?.cursor) params.cursor = opts.cursor;

      const result = await rpc("thread/list", params);
      const obj = isRecord(result) ? result : null;
      const data = obj && Array.isArray(obj["data"]) ? obj["data"] : [];
      const nextCursor = obj ? getString(obj["nextCursor"]) : null;

      const rows: ThreadRow[] = [];
      for (const raw of data) {
        if (!isRecord(raw)) continue;
        const id = getString(raw["id"]);
        if (!isNonEmptyString(id)) continue;
        rows.push({
          id,
          preview: getString(raw["preview"]) ?? "",
          modelProvider: getString(raw["modelProvider"]) ?? undefined,
          createdAt: getNumber(raw["createdAt"]) ?? undefined,
          updatedAt: getNumber(raw["updatedAt"]) ?? undefined
        });
      }

      setThreadsNextCursor(nextCursor);
      if (opts?.append) setThreads((prev) => [...(prev ?? []), ...rows]);
      else setThreads(rows);
    } catch (e) {
      toast.error((e as Error)?.message || String(e));
    } finally {
      setThreadsBusy(false);
    }
  }

  async function archiveThread(id: string): Promise<void> {
    try {
      await rpc("thread/archive", { threadId: id });
      toast.success("Thread archived");
      await refreshThreads();
    } catch (e) {
      toast.error((e as Error)?.message || String(e));
    }
  }

  async function unarchiveThread(id: string): Promise<void> {
    try {
      await rpc("thread/unarchive", { threadId: id });
      toast.success("Thread unarchived");
      await refreshThreads();
    } catch (e) {
      toast.error((e as Error)?.message || String(e));
    }
  }

  function copyText(text: string): void {
    void navigator.clipboard.writeText(text).then(
      () => toast.success("Copied"),
      () => toast.error("Copy failed")
    );
  }

  const sessionsLoaded = sessions !== null;
  const sessionsList = sessions ?? [];
  const sessionsQuery = sessionsSearch.trim().toLowerCase();
  const sessionsFiltered = sessionsQuery
    ? sessionsList.filter((s) => {
        const sid = (s.sessionId ?? "").toLowerCase();
        const name = (s.name ?? "").toLowerCase();
        const status = (s.status ?? "").toLowerCase();
        const cid = (s.containerId ?? "").toLowerCase();
        return sid.includes(sessionsQuery) || name.includes(sessionsQuery) || status.includes(sessionsQuery) || cid.includes(sessionsQuery);
      })
    : sessionsList;

  const threadsLoaded = threads !== null;
  const threadsList = threads ?? [];
  const threadsQuery = threadsSearch.trim().toLowerCase();
  const threadsFiltered = threadsQuery
    ? threadsList.filter((t) => {
        const preview = (t.preview ?? "").toLowerCase();
        const provider = (t.modelProvider ?? "").toLowerCase();
        return t.id.toLowerCase().includes(threadsQuery) || preview.includes(threadsQuery) || provider.includes(threadsQuery);
      })
    : threadsList;

  return (
    <div className="relative">
      <div className="pointer-events-none fixed inset-0 z-0 overflow-hidden">
        <div className="absolute inset-0 argus-landing-canvas" />
        <div className="absolute inset-0 argus-landing-grid-64 opacity-70" />
        <div className="absolute inset-0 argus-landing-noise" />
        <div className="absolute -left-[30%] -top-[25%] h-[70vh] w-[70vw] argus-landing-blob argus-landing-blob-a" />
        <div className="absolute -right-[30%] -top-[20%] h-[70vh] w-[70vw] argus-landing-blob argus-landing-blob-b" />
        <div className="absolute -bottom-[30%] -right-[10%] h-[80vh] w-[80vw] argus-landing-blob argus-landing-blob-c" />
      </div>

      <main className="relative z-[1] mx-auto grid max-w-6xl gap-4 px-4 py-6 md:px-6 md:py-10">
        <header className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
          <div className="flex items-end gap-3">
            <div
              className={cn(
                "flex h-11 w-11 items-center justify-center rounded-2xl border border-border",
                "bg-background/70 backdrop-blur-md",
                "shadow-[0_0_0_1px_oklch(var(--border)/0.55),0_14px_40px_oklch(0%_0_0/0.35)]"
              )}
            >
              <PlugZap className="h-5 w-5 text-primary" />
            </div>
            <div>
              <div className="font-logo text-base tracking-wide text-foreground">Argus</div>
              <div className="text-sm text-muted-foreground">App-server WebSocket bridge</div>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <div
              className={cn(
                "inline-flex items-center gap-2 rounded-full border px-3 py-1",
                "bg-background/70 backdrop-blur-md",
                connStatus.ok ? "border-success/40 text-success" : "border-destructive/50 text-destructive"
              )}
            >
              {connStatus.ok ? <CheckCircle2 className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
              <span className="font-mono">{connStatus.text}</span>
            </div>
            <IdPill label="session" value={sessionId} />
            <IdPill label="thread" value={threadId} />
            <IdPill label="turn" value={turnId} />
          </div>
        </header>

        <section
          className={cn(
            "rounded-2xl border border-border bg-card/80 backdrop-blur-xl",
            "shadow-[0_0_0_1px_oklch(var(--border)/0.55),0_14px_40px_oklch(0%_0_0/0.35)]"
          )}
        >
          <div className="grid gap-4 border-b border-border/60 bg-background/70 p-4 backdrop-blur-md md:p-5">
            <div className="grid gap-3 md:grid-cols-12">
              <div className="md:col-span-6">
                <FieldLabel icon={<LinkIcon className="h-4 w-4" />} label="WebSocket URL" />
                <Input value={wsUrl} onChange={(e) => setWsUrl(e.target.value)} spellCheck={false} />
              </div>
              <div className="md:col-span-3">
                <FieldLabel label="CWD" />
                <Input value={cwd} onChange={(e) => setCwd(e.target.value)} spellCheck={false} />
              </div>
              <div className="md:col-span-3">
                <FieldLabel label="Approval / Sandbox" />
                <div className="grid grid-cols-2 gap-2">
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
                  <select
                    className={cn(
                      "h-10 w-full rounded-xl border border-input bg-background/70 px-3 text-sm text-foreground outline-none",
                      "focus-visible:ring-4 focus-visible:ring-ring/25"
                    )}
                    value={sandboxMode}
                    onChange={(e) => setSandboxMode(e.target.value as SandboxMode)}
                  >
                    <option value="workspace-write">workspace-write</option>
                    <option value="read-only">read-only</option>
                    <option value="danger-full-access">danger-full-access</option>
                  </select>
                </div>
              </div>
            </div>

            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-center gap-3">
                <label className="inline-flex cursor-pointer items-center gap-2 text-sm text-muted-foreground">
                  <input
                    className="h-4 w-4 accent-primary"
                    type="checkbox"
                    checked={resumeLast}
                    onChange={(e) => setResumeLast(e.target.checked)}
                  />
                  Resume last thread
                </label>
                <div className="inline-flex items-center gap-2 text-sm text-muted-foreground">
                  <span className="font-mono opacity-70">saved:</span>
                  <code className="rounded-lg border border-border/60 bg-background/60 px-2 py-1 font-mono text-xs">
                    {mounted ? savedThreadIdState ?? "-" : "-"}
                  </code>
                  <Button
                    variant="ghost"
                    size="sm"
                    type="button"
                    onClick={() => clearSavedThreadId()}
                    disabled={!savedThreadIdState}
                  >
                    Clear
                  </Button>
                </div>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <Button
                  type="button"
                  onClick={() => void connect()}
                  disabled={connStatus.ok}
                >
                  Connect
                </Button>
                <Button type="button" variant="secondary" onClick={() => void startThread()} disabled={!connStatus.ok}>
                  New thread
                </Button>
                <Button type="button" variant="destructive" onClick={() => disconnect()} disabled={!connStatus.ok}>
                  Disconnect
                </Button>
              </div>
            </div>

            <div className="grid gap-3 md:grid-cols-2">
              <div className="grid gap-3 rounded-2xl border border-border/60 bg-background/60 p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="inline-flex items-center gap-2 text-sm font-medium text-foreground">
                    <PlugZap className="h-4 w-4 text-primary" />
                    <span>Sessions</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      onClick={() => void refreshSessions()}
                      disabled={sessionsBusy}
                    >
                      <RefreshCw className="h-4 w-4" />
                      Refresh
                    </Button>
                  </div>
                </div>
                <div className="grid gap-2 md:grid-cols-12">
                  <div className="md:col-span-6">
                    <div className="relative">
                      <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                      <Input
                        className="pl-9"
                        value={sessionsSearch}
                        onChange={(e) => setSessionsSearch(e.target.value)}
                        placeholder="Search session id / name / status…"
                        disabled={!sessionsLoaded}
                        spellCheck={false}
                      />
                    </div>
                  </div>
                  <div className="md:col-span-6">
                    <div className="flex flex-wrap items-center gap-2">
                      <Input
                        value={sessionAttachId}
                        onChange={(e) => setSessionAttachId(e.target.value)}
                        placeholder="Attach by session id…"
                        spellCheck={false}
                      />
                      <Button
                        type="button"
                        variant="secondary"
                        onClick={() => {
                          const sid = sessionAttachId.trim();
                          if (!sid) return;
                          const next = withSessionInWsUrl(wsUrl, sid);
                          setWsUrl(next);
                          void connect(next);
                        }}
                        disabled={!sessionAttachId.trim()}
                      >
                        Attach
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        onClick={() => {
                          const next = stripSessionFromWsUrl(wsUrl);
                          setWsUrl(next);
                          void connect(next);
                        }}
                        disabled={!wsUrlHasSession(wsUrl)}
                      >
                        Detach
                      </Button>
                    </div>
                  </div>
                </div>
                {sessions === null ? (
                  <div className="text-sm text-muted-foreground">
                    Optional: list/delete runtime containers via <code className="font-mono">/sessions</code>.
                  </div>
                ) : sessionsFiltered.length === 0 ? (
                  <div className="text-sm text-muted-foreground">No sessions.</div>
                ) : (
                  <div className="grid gap-2">
                    {sessionsFiltered.map((s) => {
                      const sid = s.sessionId ?? "";
                      const disabled = sessionsBusy || !sid;
                      const isCurrent = sid && sid === sessionId;
                      return (
                        <div
                          key={sid || s.containerId || Math.random().toString(16)}
                          className={cn(
                            "flex flex-col gap-2 rounded-2xl border border-border/60 bg-card/60 p-3 md:flex-row md:items-center md:justify-between",
                            isCurrent ? "border-primary/35 bg-primary/5" : null
                          )}
                        >
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-2">
                              <code className="rounded-lg border border-border/60 bg-background/60 px-2 py-1 font-mono text-xs">
                                {sid || "-"}
                              </code>
                              {isCurrent ? (
                                <span className="text-xs font-medium text-primary">current</span>
                              ) : null}
                              {s.status ? <span className="text-xs text-muted-foreground">{s.status}</span> : null}
                            </div>
                            <div className="mt-1 truncate text-xs text-muted-foreground">
                              {s.name ?? ""} {s.containerId ? `· ${s.containerId.slice(0, 12)}` : ""}
                            </div>
                          </div>
                          <div className="flex flex-wrap items-center gap-2">
                            <Button
                              type="button"
                              size="sm"
                              variant="secondary"
                              disabled={disabled}
                              onClick={() => {
                                const next = withSessionInWsUrl(wsUrl, sid);
                                setWsUrl(next);
                                void connect(next);
                              }}
                            >
                              Attach
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              variant="ghost"
                              disabled={disabled}
                              onClick={() => sid && copyText(sid)}
                            >
                              <Copy className="h-4 w-4" />
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              variant="destructive"
                              disabled={disabled}
                              onClick={() => sid && void deleteSession(sid)}
                            >
                              <Trash2 className="h-4 w-4" />
                            </Button>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>

              <div className="grid gap-3 rounded-2xl border border-border/60 bg-background/60 p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="inline-flex items-center gap-2 text-sm font-medium text-foreground">
                    <MessageSquare className="h-4 w-4 text-primary" />
                    <span>Threads</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      onClick={() => void refreshThreads()}
                      disabled={threadsBusy || !connStatus.ok}
                    >
                      <RefreshCw className="h-4 w-4" />
                      Refresh
                    </Button>
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      onClick={() => {
                        if (!threadsNextCursor) return;
                        void refreshThreads({ cursor: threadsNextCursor, append: true });
                      }}
                      disabled={threadsBusy || !connStatus.ok || !threadsNextCursor}
                    >
                      Load more
                    </Button>
                  </div>
                </div>

                <div className="grid gap-2 md:grid-cols-12">
                  <div className="md:col-span-6">
                    <div className="relative">
                      <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                      <Input
                        className="pl-9"
                        value={threadsSearch}
                        onChange={(e) => setThreadsSearch(e.target.value)}
                        placeholder="Search thread id / preview / provider…"
                        disabled={!threadsLoaded}
                        spellCheck={false}
                      />
                    </div>
                  </div>
                  <div className="md:col-span-6">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <label className="inline-flex cursor-pointer items-center gap-2 text-sm text-muted-foreground">
                        <input
                          className="h-4 w-4 accent-primary"
                          type="checkbox"
                          checked={threadsArchived}
                          onChange={(e) => {
                            const next = e.target.checked;
                            setThreadsArchived(next);
                            if (connStatus.ok) void refreshThreads({ archived: next });
                          }}
                        />
                        Archived
                      </label>
                      <div className="text-xs text-muted-foreground">
                        {threadsLoaded ? `${threadsFiltered.length} loaded` : "not loaded"}
                      </div>
                    </div>
                  </div>
                </div>

                {!connStatus.ok ? (
                  <div className="text-sm text-muted-foreground">Connect to manage threads.</div>
                ) : !threadsLoaded ? (
                  <div className="text-sm text-muted-foreground">
                    Click <code className="font-mono">Refresh</code> to list threads (requires app-server support for{" "}
                    <code className="font-mono">thread/list</code>).
                  </div>
                ) : threadsFiltered.length === 0 ? (
                  <div className="text-sm text-muted-foreground">No threads.</div>
                ) : (
                  <div className="grid gap-2">
                    {threadsFiltered.map((t) => {
                      const isCurrent = t.id === threadId;
                      const updated =
                        formatEpochSecondsOrMs(t.updatedAt) ?? formatEpochSecondsOrMs(t.createdAt);
                      const disabled = threadsBusy || !connStatus.ok;
                      return (
                        <div
                          key={t.id}
                          className={cn(
                            "flex flex-col gap-2 rounded-2xl border border-border/60 bg-card/60 p-3 md:flex-row md:items-center md:justify-between",
                            isCurrent ? "border-primary/35 bg-primary/5" : null
                          )}
                        >
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-2">
                              <code className="rounded-lg border border-border/60 bg-background/60 px-2 py-1 font-mono text-xs">
                                {t.id}
                              </code>
                              {isCurrent ? (
                                <span className="text-xs font-medium text-primary">current</span>
                              ) : null}
                              {t.modelProvider ? (
                                <span className="text-xs text-muted-foreground">{t.modelProvider}</span>
                              ) : null}
                              {updated ? <span className="text-xs text-muted-foreground">{updated}</span> : null}
                            </div>
                            <div className="mt-1 truncate text-xs text-muted-foreground">
                              {t.preview || "(no preview)"}
                            </div>
                          </div>
                          <div className="flex flex-wrap items-center gap-2">
                            <Button
                              type="button"
                              size="sm"
                              variant="secondary"
                              disabled={disabled}
                              onClick={() => void resumeThread(t.id)}
                            >
                              Resume
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              variant="ghost"
                              disabled={disabled}
                              onClick={() => copyText(t.id)}
                            >
                              <Copy className="h-4 w-4" />
                            </Button>
                            {threadsArchived ? (
                              <Button
                                type="button"
                                size="sm"
                                variant="secondary"
                                disabled={disabled}
                                onClick={() => void unarchiveThread(t.id)}
                              >
                                <Archive className="h-4 w-4" />
                                Unarchive
                              </Button>
                            ) : (
                              <Button
                                type="button"
                                size="sm"
                                variant="secondary"
                                disabled={disabled}
                                onClick={() => void archiveThread(t.id)}
                              >
                                <Archive className="h-4 w-4" />
                                Archive
                              </Button>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
          </div>

          <div ref={chatScrollRef} className="grid max-h-[66vh] gap-3 overflow-auto p-4 scrollbar-hide md:p-5">
            {messages.length === 0 ? (
              <div className="rounded-2xl border border-border/60 bg-background/60 p-4 text-sm text-muted-foreground">
                Connect, then start sending messages. The UI enforces sequential turns (waits for{" "}
                <code className="font-mono">turn/completed</code>).
              </div>
            ) : (
              messages.map((m) => <Bubble key={m.id} role={m.role} text={m.text} />)
            )}
          </div>

          <div className="border-t border-border/60 bg-background/70 p-4 backdrop-blur-md md:p-5">
            <div className="grid gap-3 md:grid-cols-[1fr_auto] md:items-end">
              <div className="grid gap-2">
                <Textarea
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  placeholder="Type a prompt…"
                  disabled={!canSend}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      void sendTurn();
                    }
                  }}
                />
                <div className="text-xs text-muted-foreground">
                  Enter to send, Shift+Enter for newline.
                </div>
              </div>
              <div className="flex items-center justify-end gap-2">
                <Button type="button" onClick={() => void sendTurn()} disabled={!canSend}>
                  Send
                </Button>
              </div>
            </div>
          </div>
        </section>
      </main>
    </div>
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

function Bubble({ role, text }: { role: "user" | "assistant"; text: string }) {
  return (
    <div
      className={cn(
        "w-fit max-w-[92%] rounded-2xl border p-3 shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.06)]",
        role === "user"
          ? "ml-auto border-primary/25 bg-primary/10"
          : "mr-auto border-border/60 bg-card/70"
      )}
    >
      <div className="mb-2 text-xs font-semibold text-muted-foreground">{role}</div>
      <pre className="whitespace-pre-wrap break-words rounded-xl border border-border/60 bg-background/60 p-3 font-mono text-sm leading-relaxed text-foreground">
        {text}
      </pre>
    </div>
  );
}
