"use client";

import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

type NodeInfo = {
  nodeId: string;
  displayName?: string | null;
  platform?: string | null;
  version?: string | null;
  caps?: string[] | null;
  commands?: string[] | null;
  connectedAtMs?: number | null;
  lastSeenMs?: number | null;
};

function defaultWsUrl(): string {
  const preset = (process.env.NEXT_PUBLIC_ARGUS_WS_URL ?? "").trim();
  if (preset) return preset;
  if (typeof window === "undefined") return "ws://127.0.0.1:8080/ws";
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const host =
    window.location.port === "3000" ? `${window.location.hostname}:8080` : window.location.host;
  return `${proto}//${host}/ws`;
}

function httpBaseFromWsUrl(url: string): string {
  try {
    const u = new URL(url);
    const proto = u.protocol === "wss:" ? "https:" : "http:";
    return `${proto}//${u.host}`;
  } catch {
    return "";
  }
}

function extractTokenFromWsUrl(url: string): string {
  try {
    const u = new URL(url);
    const token = u.searchParams.get("token");
    return token && token.trim() ? token : "";
  } catch {
    return "";
  }
}

function safeParseJson(text: string): unknown | null {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export default function NodesPage() {
  const [wsUrl, setWsUrl] = React.useState<string>(() => defaultWsUrl());
  const [httpBase, setHttpBase] = React.useState<string>(() => httpBaseFromWsUrl(defaultWsUrl()));
  const [token, setToken] = React.useState<string>(() => extractTokenFromWsUrl(defaultWsUrl()));

  const [loading, setLoading] = React.useState(false);
  const [nodes, setNodes] = React.useState<NodeInfo[]>([]);
  const [error, setError] = React.useState<string | null>(null);

  const [nodePick, setNodePick] = React.useState<string>("");
  const [command, setCommand] = React.useState<string>("system.run");
  const [paramsText, setParamsText] = React.useState<string>(
    JSON.stringify({ argv: ["echo", "hello from node-host"] }, null, 2)
  );
  const [timeoutMs, setTimeoutMs] = React.useState<string>("30000");
  const [invokeOut, setInvokeOut] = React.useState<string>("");

  function syncFromWsUrl(nextWsUrl: string): void {
    setWsUrl(nextWsUrl);
    const derived = httpBaseFromWsUrl(nextWsUrl);
    if (derived) setHttpBase(derived);
    const t = extractTokenFromWsUrl(nextWsUrl);
    setToken(t);
  }

  async function refreshNodes(): Promise<void> {
    setLoading(true);
    setError(null);
    try {
      const url = new URL("/nodes", httpBase);
      if (token) url.searchParams.set("token", token);
      const res = await fetch(url.toString(), { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as { nodes?: NodeInfo[] };
      const next = Array.isArray(data.nodes) ? data.nodes : [];
      setNodes(next);
      if (!nodePick && next.length) setNodePick(next[0].nodeId);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  async function invoke(): Promise<void> {
    setInvokeOut("");
    setError(null);
    const params = safeParseJson(paramsText);
    if (paramsText.trim() && params === null) {
      setError("Params must be valid JSON");
      return;
    }
    const timeoutNum = Number(timeoutMs);
    const body: Record<string, unknown> = {
      node: nodePick,
      command: command.trim(),
      params: paramsText.trim() ? params : null
    };
    if (Number.isFinite(timeoutNum)) body.timeoutMs = timeoutNum;

    setLoading(true);
    try {
      const url = new URL("/nodes/invoke", httpBase);
      if (token) url.searchParams.set("token", token);
      const res = await fetch(url.toString(), {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body)
      });
      const text = await res.text();
      setInvokeOut(text);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  const selected = nodes.find((n) => n.nodeId === nodePick) ?? null;

  return (
    <main className="min-h-dvh bg-background text-foreground">
      <div className="mx-auto w-full max-w-5xl px-6 py-8">
        <div className="flex flex-col gap-2">
          <h1 className="text-2xl font-semibold tracking-tight">Nodes</h1>
          <p className="text-sm text-muted-foreground">
            Minimal node management + invoke tester. Node host connects to{" "}
            <code className="rounded bg-muted px-1 py-0.5">/nodes/ws</code>.
          </p>
        </div>

        <div className="mt-6 grid gap-4">
          <div className="rounded-2xl border border-border bg-background/50 p-4">
            <div className="grid gap-3">
              <label className="text-sm font-medium">WebSocket URL (for deriving base + token)</label>
              <Input value={wsUrl} onChange={(e) => syncFromWsUrl(e.target.value)} />

              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <div className="grid gap-2">
                  <label className="text-sm font-medium">HTTP Base</label>
                  <Input value={httpBase} onChange={(e) => setHttpBase(e.target.value)} />
                </div>
                <div className="grid gap-2">
                  <label className="text-sm font-medium">Token (optional)</label>
                  <Input value={token} onChange={(e) => setToken(e.target.value)} />
                </div>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <Button type="button" onClick={refreshNodes} disabled={loading || !httpBase}>
                  {loading ? "Loading…" : "Refresh nodes"}
                </Button>
                <a
                  href="/"
                  className={cn(
                    "text-sm text-muted-foreground underline underline-offset-4 hover:text-foreground"
                  )}
                >
                  Back to chat
                </a>
                {error ? <span className="text-sm text-destructive">{error}</span> : null}
              </div>
            </div>
          </div>

          <div className="rounded-2xl border border-border bg-background/50 p-4">
            <div className="grid gap-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="text-base font-semibold">Connected nodes</h2>
                <span className="text-sm text-muted-foreground">{nodes.length} total</span>
              </div>
              {nodes.length ? (
                <div className="grid gap-2">
                  {nodes.map((n) => (
                    <button
                      key={n.nodeId}
                      type="button"
                      className={cn(
                        "flex w-full items-start justify-between gap-3 rounded-xl border border-border/60 bg-background/60 p-3 text-left",
                        "transition-colors hover:border-border hover:bg-background/70",
                        nodePick === n.nodeId ? "border-primary/40 bg-background/80" : ""
                      )}
                      onClick={() => setNodePick(n.nodeId)}
                    >
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium">
                          {n.displayName || n.nodeId}
                        </div>
                        <div className="truncate text-xs text-muted-foreground">
                          {n.nodeId}
                          {n.platform ? ` • ${n.platform}` : ""}
                          {n.version ? ` • v${n.version}` : ""}
                        </div>
                      </div>
                      <div className="shrink-0 text-xs text-muted-foreground">
                        {(n.commands || []).length} cmds
                      </div>
                    </button>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  No nodes connected. Start node-host on your Mac and refresh.
                </p>
              )}
            </div>
          </div>

          <div className="rounded-2xl border border-border bg-background/50 p-4">
            <div className="grid gap-3">
              <h2 className="text-base font-semibold">Invoke</h2>

              <div className="grid gap-2">
                <label className="text-sm font-medium">Node</label>
                <select
                  className={cn(
                    "h-10 w-full rounded-xl border border-input bg-background/70 px-3 text-sm",
                    "shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.06)]",
                    "outline-none focus-visible:ring-4 focus-visible:ring-ring/25"
                  )}
                  value={nodePick}
                  onChange={(e) => setNodePick(e.target.value)}
                  disabled={!nodes.length}
                >
                  {nodes.map((n) => (
                    <option key={n.nodeId} value={n.nodeId}>
                      {n.displayName || n.nodeId}
                    </option>
                  ))}
                </select>
              </div>

              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <div className="grid gap-2">
                  <label className="text-sm font-medium">Command</label>
                  <Input value={command} onChange={(e) => setCommand(e.target.value)} />
                  {selected?.commands?.length ? (
                    <div className="flex flex-wrap gap-2">
                      {selected.commands.slice(0, 8).map((c) => (
                        <button
                          key={c}
                          type="button"
                          className="rounded-lg border border-border/60 bg-background/60 px-2 py-1 text-xs text-muted-foreground hover:text-foreground"
                          onClick={() => setCommand(c)}
                        >
                          {c}
                        </button>
                      ))}
                    </div>
                  ) : null}
                </div>
                <div className="grid gap-2">
                  <label className="text-sm font-medium">Timeout (ms)</label>
                  <Input value={timeoutMs} onChange={(e) => setTimeoutMs(e.target.value)} />
                </div>
              </div>

              <div className="grid gap-2">
                <label className="text-sm font-medium">Params (JSON)</label>
                <Textarea value={paramsText} onChange={(e) => setParamsText(e.target.value)} />
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <Button type="button" onClick={invoke} disabled={loading || !nodePick || !command.trim()}>
                  {loading ? "Invoking…" : "Invoke"}
                </Button>
                {error ? <span className="text-sm text-destructive">{error}</span> : null}
              </div>

              <div className="grid gap-2">
                <label className="text-sm font-medium">Result</label>
                <Textarea value={invokeOut} readOnly className="min-h-40 font-mono text-xs" />
              </div>
            </div>
          </div>
        </div>
      </div>
    </main>
  );
}

