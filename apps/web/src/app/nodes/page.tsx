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
    const parsed = new URL(url);
    const proto = parsed.protocol === "wss:" ? "https:" : "http:";
    return `${proto}//${parsed.host}`;
  } catch {
    return "";
  }
}

function nodeApiUrl(httpBase: string, path: string): string {
  const trimmed = path.startsWith("/") ? path.slice(1) : path;
  const suffix = trimmed ? `/${trimmed}` : "";
  return new URL(`/api/nodes${suffix}`, httpBase).toString();
}

function extractTokenFromWsUrl(url: string): string {
  try {
    const parsed = new URL(url);
    const token = parsed.searchParams.get("token");
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

function extractJobIdFromResponse(text: string): string {
  const parsed = safeParseJson(text);
  if (!parsed || typeof parsed !== "object") return "";

  const payload = (parsed as { payload?: unknown }).payload;
  if (!payload || typeof payload !== "object") return "";

  const directJobId = (payload as { jobId?: unknown }).jobId;
  if (typeof directJobId === "string" && directJobId.trim()) {
    return directJobId.trim();
  }

  const job = (payload as { job?: unknown }).job;
  if (!job || typeof job !== "object") return "";

  const nestedJobId = (job as { jobId?: unknown }).jobId;
  if (typeof nestedJobId === "string" && nestedJobId.trim()) {
    return nestedJobId.trim();
  }

  return "";
}

function splitKeys(text: string): string[] {
  return text
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
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

  const [jobId, setJobId] = React.useState<string>("");
  const [tailBytes, setTailBytes] = React.useState<string>("65536");
  const [writeData, setWriteData] = React.useState<string>("");
  const [sendKeys, setSendKeys] = React.useState<string>("up,down,enter");
  const [sendLiteral, setSendLiteral] = React.useState<string>("");
  const [pasteText, setPasteText] = React.useState<string>("");
  const [pasteBracketed, setPasteBracketed] = React.useState<boolean>(true);

  function syncFromWsUrl(nextWsUrl: string): void {
    setWsUrl(nextWsUrl);
    const derived = httpBaseFromWsUrl(nextWsUrl);
    if (derived) setHttpBase(derived);
    setToken(extractTokenFromWsUrl(nextWsUrl));
  }

  async function refreshNodes(): Promise<void> {
    setLoading(true);
    setError(null);
    try {
      const url = nodeApiUrl(httpBase, "");
      if (token) url.searchParams.set("token", token);
      const res = await fetch(url, { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as { nodes?: NodeInfo[] };
      const nextNodes = Array.isArray(data.nodes) ? data.nodes : [];
      setNodes(nextNodes);
      if (!nodePick && nextNodes.length) setNodePick(nextNodes[0].nodeId);
    } catch (event) {
      setError(String(event));
    } finally {
      setLoading(false);
    }
  }

  async function invokeRequest(nextCommand: string, nextParams: unknown): Promise<void> {
    setInvokeOut("");
    setError(null);
    setCommand(nextCommand);

    const trimmedCommand = nextCommand.trim();
    if (!trimmedCommand) {
      setError("Command is required");
      return;
    }

    const body: Record<string, unknown> = {
      node: nodePick,
      command: trimmedCommand,
      params: nextParams
    };
    const timeoutNum = Number(timeoutMs);
    if (Number.isFinite(timeoutNum)) {
      body.timeoutMs = timeoutNum;
    }

    setLoading(true);
    try {
      const url = new URL(nodeApiUrl(httpBase, "invoke"));
      if (token) url.searchParams.set("token", token);
      const res = await fetch(url.toString(), {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body)
      });
      const text = await res.text();
      setInvokeOut(text);

      const nextJobId = extractJobIdFromResponse(text);
      if (nextJobId) {
        setJobId(nextJobId);
      }

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (event) {
      setError(String(event));
    } finally {
      setLoading(false);
    }
  }

  async function invoke(): Promise<void> {
    const parsedParams = safeParseJson(paramsText);
    if (paramsText.trim() && parsedParams === null) {
      setError("Params must be valid JSON");
      return;
    }
    await invokeRequest(command, paramsText.trim() ? parsedParams : null);
  }

  async function invokeForJob(nextCommand: string, extraParams?: Record<string, unknown>): Promise<void> {
    const trimmedJobId = jobId.trim();
    if (!trimmedJobId) {
      setError("jobId is required");
      return;
    }
    await invokeRequest(nextCommand, { jobId: trimmedJobId, ...(extraParams ?? {}) });
  }

  const selected = nodes.find((node) => node.nodeId === nodePick) ?? null;

  return (
    <main className="min-h-dvh bg-background text-foreground">
      <div className="mx-auto w-full max-w-5xl px-6 py-8">
        <div className="flex flex-col gap-2">
          <h1 className="text-2xl font-semibold tracking-tight">Nodes</h1>
          <p className="text-sm text-muted-foreground">
            Minimal node management + invoke tester. The web UI talks to the gateway through{" "}
            <code className="rounded bg-muted px-1 py-0.5">/api/nodes</code>.
          </p>
        </div>

        <div className="mt-6 grid gap-4">
          <div className="rounded-2xl border border-border bg-background/50 p-4">
            <div className="grid gap-3">
              <label className="text-sm font-medium">WebSocket URL (for deriving base + token)</label>
              <Input value={wsUrl} onChange={(event) => syncFromWsUrl(event.target.value)} />

              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <div className="grid gap-2">
                  <label className="text-sm font-medium">HTTP Base</label>
                  <Input value={httpBase} onChange={(event) => setHttpBase(event.target.value)} />
                </div>
                <div className="grid gap-2">
                  <label className="text-sm font-medium">Token (optional)</label>
                  <Input value={token} onChange={(event) => setToken(event.target.value)} />
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
                  {nodes.map((node) => (
                    <button
                      key={node.nodeId}
                      type="button"
                      className={cn(
                        "flex w-full items-start justify-between gap-3 rounded-xl border border-border/60 bg-background/60 p-3 text-left",
                        "transition-colors hover:border-border hover:bg-background/70",
                        nodePick === node.nodeId ? "border-primary/40 bg-background/80" : ""
                      )}
                      onClick={() => setNodePick(node.nodeId)}
                    >
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium">
                          {node.displayName || node.nodeId}
                        </div>
                        <div className="truncate text-xs text-muted-foreground">
                          {node.nodeId}
                          {node.platform ? ` • ${node.platform}` : ""}
                          {node.version ? ` • v${node.version}` : ""}
                        </div>
                      </div>
                      <div className="shrink-0 text-xs text-muted-foreground">
                        {(node.commands || []).length} cmds
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
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="text-base font-semibold">Invoke</h2>
                <span className="text-xs text-muted-foreground">
                  Interactive tip: use <code>pty: true</code> + <code>yieldMs: 0</code>
                </span>
              </div>

              <div className="grid gap-2">
                <label className="text-sm font-medium">Node</label>
                <select
                  className={cn(
                    "h-10 w-full rounded-xl border border-input bg-background/70 px-3 text-sm",
                    "shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.06)]",
                    "outline-none focus-visible:ring-4 focus-visible:ring-ring/25"
                  )}
                  value={nodePick}
                  onChange={(event) => setNodePick(event.target.value)}
                  disabled={!nodes.length}
                >
                  {nodes.map((node) => (
                    <option key={node.nodeId} value={node.nodeId}>
                      {node.displayName || node.nodeId}
                    </option>
                  ))}
                </select>
              </div>

              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <div className="grid gap-2">
                  <label className="text-sm font-medium">Command</label>
                  <Input value={command} onChange={(event) => setCommand(event.target.value)} />
                  {selected?.commands?.length ? (
                    <div className="flex flex-wrap gap-2">
                      {selected.commands.slice(0, 12).map((item) => (
                        <button
                          key={item}
                          type="button"
                          className="rounded-lg border border-border/60 bg-background/60 px-2 py-1 text-xs text-muted-foreground hover:text-foreground"
                          onClick={() => setCommand(item)}
                        >
                          {item}
                        </button>
                      ))}
                    </div>
                  ) : null}
                </div>
                <div className="grid gap-2">
                  <label className="text-sm font-medium">Timeout (ms)</label>
                  <Input value={timeoutMs} onChange={(event) => setTimeoutMs(event.target.value)} />
                </div>
              </div>

              <div className="grid gap-2">
                <label className="text-sm font-medium">Params (JSON)</label>
                <Textarea value={paramsText} onChange={(event) => setParamsText(event.target.value)} />
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <Button type="button" onClick={invoke} disabled={loading || !nodePick || !command.trim()}>
                  {loading ? "Invoking…" : "Invoke"}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => {
                    setCommand("system.run");
                    setParamsText(
                      JSON.stringify(
                        {
                          argv: [
                            "bash",
                            "-lc",
                            "read -p 'type agree: ' answer; echo answer=$answer"
                          ],
                          pty: true,
                          yieldMs: 0
                        },
                        null,
                        2
                      )
                    );
                  }}
                  disabled={loading}
                >
                  Load interactive example
                </Button>
              </div>

              <div className="grid gap-2">
                <label className="text-sm font-medium">Result</label>
                <Textarea value={invokeOut} readOnly className="min-h-40 font-mono text-xs" />
              </div>
            </div>
          </div>

          <div className="rounded-2xl border border-border bg-background/50 p-4">
            <div className="grid gap-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <h2 className="text-base font-semibold">Interactive helpers</h2>
                  <p className="text-sm text-muted-foreground">
                    Reuse the returned <code>jobId</code> to fetch logs or send more input.
                  </p>
                </div>
              </div>

              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <div className="grid gap-2">
                  <label className="text-sm font-medium">Job ID</label>
                  <Input value={jobId} onChange={(event) => setJobId(event.target.value)} />
                </div>
                <div className="grid gap-2">
                  <label className="text-sm font-medium">Logs tail bytes</label>
                  <Input value={tailBytes} onChange={(event) => setTailBytes(event.target.value)} />
                </div>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <Button type="button" variant="outline" onClick={() => invokeForJob("process.get")} disabled={loading || !jobId.trim()}>
                  Get
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => {
                    const nextTailBytes = Number(tailBytes);
                    void invokeForJob(
                      "process.logs",
                      Number.isFinite(nextTailBytes) ? { tailBytes: nextTailBytes } : undefined
                    );
                  }}
                  disabled={loading || !jobId.trim()}
                >
                  Logs
                </Button>
                <Button type="button" variant="outline" onClick={() => invokeForJob("process.submit")} disabled={loading || !jobId.trim()}>
                  Submit
                </Button>
                <Button type="button" variant="outline" onClick={() => invokeForJob("process.kill")} disabled={loading || !jobId.trim()}>
                  Kill
                </Button>
              </div>

              <div className="grid gap-2">
                <label className="text-sm font-medium">Write data</label>
                <Textarea
                  value={writeData}
                  onChange={(event) => setWriteData(event.target.value)}
                  className="min-h-24"
                />
                <div>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => invokeForJob("process.write", { data: writeData })}
                    disabled={loading || !jobId.trim() || !writeData}
                  >
                    Write
                  </Button>
                </div>
              </div>

              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <div className="grid gap-2">
                  <label className="text-sm font-medium">Send keys (comma separated)</label>
                  <Input value={sendKeys} onChange={(event) => setSendKeys(event.target.value)} />
                </div>
                <div className="grid gap-2">
                  <label className="text-sm font-medium">Literal text</label>
                  <Input value={sendLiteral} onChange={(event) => setSendLiteral(event.target.value)} />
                </div>
              </div>

              <div>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => {
                    const keys = splitKeys(sendKeys);
                    void invokeForJob("process.send_keys", {
                      ...(keys.length ? { keys } : {}),
                      ...(sendLiteral ? { literal: sendLiteral } : {})
                    });
                  }}
                  disabled={loading || !jobId.trim() || (!sendKeys.trim() && !sendLiteral)}
                >
                  Send keys
                </Button>
              </div>

              <div className="grid gap-2">
                <label className="text-sm font-medium">Paste text</label>
                <Textarea
                  value={pasteText}
                  onChange={(event) => setPasteText(event.target.value)}
                  className="min-h-24"
                />
                <label className="flex items-center gap-2 text-sm text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={pasteBracketed}
                    onChange={(event) => setPasteBracketed(event.target.checked)}
                  />
                  Use bracketed paste markers
                </label>
                <div>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => invokeForJob("process.paste", { text: pasteText, bracketed: pasteBracketed })}
                    disabled={loading || !jobId.trim() || !pasteText}
                  >
                    Paste
                  </Button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </main>
  );
}
