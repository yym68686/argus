"use client";

import * as React from "react";

import { Badge, EmptyState, Fact, InlineError, PanelCard } from "@/components/console-primitives";
import { ConsoleShell } from "@/components/console-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { gatewayFetchJson, useGatewayWsUrlState } from "@/lib/gateway";
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

function safeParseJson(text: string): unknown | null {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function extractJobIdFromResponse(parsed: unknown): string {
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

function formatStamp(value?: number | null): string {
  if (!value || !Number.isFinite(value)) return "—";
  return new Date(value).toLocaleString();
}

export default function NodesPage() {
  const [wsUrl] = useGatewayWsUrlState();

  const [loading, setLoading] = React.useState(false);
  const [nodes, setNodes] = React.useState<NodeInfo[]>([]);
  const [inventoryError, setInventoryError] = React.useState<string | null>(null);
  const [invokeError, setInvokeError] = React.useState<string | null>(null);

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

  const refreshNodes = React.useCallback(async (): Promise<void> => {
    if (!wsUrl.trim()) return;
    setLoading(true);
    setInventoryError(null);
    try {
      const data = await gatewayFetchJson<{ nodes?: NodeInfo[] }>(wsUrl, "/api/nodes");
      const nextNodes = Array.isArray(data.nodes) ? data.nodes : [];
      setNodes(nextNodes);
      setNodePick((current) => current || nextNodes[0]?.nodeId || "");
    } catch (event) {
      setInventoryError((event as Error)?.message || String(event));
    } finally {
      setLoading(false);
    }
  }, [wsUrl]);

  React.useEffect(() => {
    if (!wsUrl.trim()) return;
    void refreshNodes();
  }, [refreshNodes, wsUrl]);

  async function invokeRequest(nextCommand: string, nextParams: unknown): Promise<void> {
    setInvokeOut("");
    setInvokeError(null);
    setCommand(nextCommand);

    const trimmedCommand = nextCommand.trim();
    if (!trimmedCommand) {
      setInvokeError("Command is required");
      return;
    }

    const body: Record<string, unknown> = {
      node: nodePick,
      command: trimmedCommand,
      params: nextParams,
    };
    const timeoutNum = Number(timeoutMs);
    if (Number.isFinite(timeoutNum)) {
      body.timeoutMs = timeoutNum;
    }

    setLoading(true);
    try {
      const result = await gatewayFetchJson<Record<string, unknown>>(wsUrl, "/api/nodes/invoke", {
        method: "POST",
        body: JSON.stringify(body),
      });
      setInvokeOut(JSON.stringify(result, null, 2));
      const nextJobId = extractJobIdFromResponse(result);
      if (nextJobId) {
        setJobId(nextJobId);
      }
      if (result.ok === false) {
        const nextError = typeof result.error === "string" && result.error.trim() ? result.error.trim() : "Node invoke request failed";
        throw new Error(nextError);
      }
    } catch (event) {
      setInvokeError((event as Error)?.message || String(event));
    } finally {
      setLoading(false);
    }
  }

  async function invoke(): Promise<void> {
    const parsedParams = safeParseJson(paramsText);
    if (paramsText.trim() && parsedParams === null) {
      setInvokeError("Params must be valid JSON");
      return;
    }
    await invokeRequest(command, paramsText.trim() ? parsedParams : null);
  }

  async function invokeForJob(nextCommand: string, extraParams?: Record<string, unknown>): Promise<void> {
    const trimmedJobId = jobId.trim();
    if (!trimmedJobId) {
      setInvokeError("jobId is required");
      return;
    }
    await invokeRequest(nextCommand, { jobId: trimmedJobId, ...(extraParams ?? {}) });
  }

  const selected = nodes.find((node) => node.nodeId === nodePick) ?? null;

  return (
    <ConsoleShell title="Nodes">
      <div className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
        <section className="space-y-4">
          <PanelCard
            eyebrow="Inventory"
            title="Connected nodes"
            subtitle={nodes.length ? `${nodes.length} node-host peers are currently visible.` : "No node-host peers are currently visible."}
          >
            {inventoryError ? <InlineError message={inventoryError} /> : null}
            {inventoryError && !nodes.length ? null : nodes.length ? (
              <div className="space-y-2">
                {nodes.map((node) => (
                  <button
                    key={node.nodeId}
                    type="button"
                    className={cn(
                      "argus-row-shell flex w-full items-start justify-between gap-3 rounded-[16px] px-4 py-3 text-left",
                      nodePick === node.nodeId ? "border-primary/28 bg-primary/10" : "hover:border-border hover:bg-background/36",
                    )}
                    onClick={() => setNodePick(node.nodeId)}
                  >
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium text-foreground">
                        {node.displayName || node.nodeId}
                      </div>
                      <div className="mt-1 truncate text-xs leading-5 text-muted-foreground">
                        {node.nodeId}
                        {node.platform ? ` · ${node.platform}` : ""}
                        {node.version ? ` · v${node.version}` : ""}
                      </div>
                    </div>
                    <div className="shrink-0 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                      {(node.commands || []).length} cmds
                    </div>
                  </button>
                ))}
              </div>
            ) : (
              <EmptyState title="No nodes" body="Start node-host on a machine and it will appear here once the gateway sees it." />
            )}
          </PanelCard>

          <PanelCard
            eyebrow="Selected peer"
            title={selected?.displayName || selected?.nodeId || "Node"}
            subtitle="Quick identity snapshot for the currently selected node."
          >
            {selected ? (
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-1">
                <Fact label="Node id" value={selected.nodeId} mono />
                <Fact label="Platform" value={selected.platform || "—"} />
                <Fact label="Version" value={selected.version || "—"} />
                <Fact label="Connected at" value={formatStamp(selected.connectedAtMs)} />
                <Fact label="Last seen" value={formatStamp(selected.lastSeenMs)} />
                <Fact label="Commands" value={String((selected.commands || []).length)} />
              </div>
            ) : inventoryError ? (
              <EmptyState title="Inventory unavailable" body="Node inventory will reappear here once the gateway node API is responding again." />
            ) : (
              <EmptyState title="No selection" body="Pick a node from the inventory to inspect metadata and target commands." />
            )}
          </PanelCard>
        </section>

        <section className="space-y-4">
          <div className="grid gap-4 xl:grid-cols-[1.1fr_0.9fr]">
            <PanelCard
              eyebrow="Invoke"
              title="Remote command"
              subtitle="Send one-off requests to the selected peer. For interactive flows, start with pty=true and yieldMs=0."
            >
              <div className="grid gap-4">
                <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_120px]">
                  <div className="grid gap-2">
                    <label className="argus-surface-label">Node</label>
                    <select
                      className="argus-select"
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
                  <div className="grid gap-2">
                    <label className="argus-surface-label">Timeout (ms)</label>
                    <Input value={timeoutMs} onChange={(event) => setTimeoutMs(event.target.value)} />
                  </div>
                </div>

                <div className="grid gap-2">
                  <label className="argus-surface-label">Command</label>
                  <Input value={command} onChange={(event) => setCommand(event.target.value)} />
                  {selected?.commands?.length ? (
                    <div className="flex flex-wrap gap-2">
                      {selected.commands.slice(0, 12).map((item) => (
                        <button
                          key={item}
                          type="button"
                          className="rounded-md border border-border/70 bg-background/28 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground hover:border-primary/28 hover:text-foreground"
                          onClick={() => setCommand(item)}
                        >
                          {item}
                        </button>
                      ))}
                    </div>
                  ) : null}
                </div>

                <div className="grid gap-2">
                  <label className="argus-surface-label">Params (JSON)</label>
                  <Textarea value={paramsText} onChange={(event) => setParamsText(event.target.value)} className="min-h-[16rem]" />
                </div>

                <div className="flex flex-wrap items-center gap-2">
                  <Button type="button" onClick={invoke} disabled={loading || !nodePick || !command.trim()}>
                    {loading ? "Invoking…" : "Invoke"}
                  </Button>
                  <Button
                    type="button"
                    variant="secondary"
                    onClick={() => {
                      setCommand("system.run");
                      setParamsText(
                        JSON.stringify(
                          {
                            argv: ["bash", "-lc", "read -p 'type agree: ' answer; echo answer=$answer"],
                            pty: true,
                            yieldMs: 0,
                          },
                          null,
                          2,
                        ),
                      );
                    }}
                    disabled={loading}
                  >
                    Load interactive example
                  </Button>
                </div>
              </div>
            </PanelCard>

            <PanelCard eyebrow="Result" title="Latest response" subtitle="Raw invoke output from the node proxy." contentClassName="space-y-3">
              {invokeError ? <InlineError message={invokeError} /> : null}
              <Textarea value={invokeOut} readOnly className="min-h-[28rem] font-mono text-xs" />
            </PanelCard>
          </div>

          <PanelCard
            eyebrow="Interactive helpers"
            title="Reuse the returned jobId"
            subtitle="Fetch logs, send more input, or terminate the remote process once a command has been started."
          >
            <div className="grid gap-4">
              <div className="grid gap-3 md:grid-cols-2">
                <div className="grid gap-2">
                  <label className="argus-surface-label">Job ID</label>
                  <Input value={jobId} onChange={(event) => setJobId(event.target.value)} />
                </div>
                <div className="grid gap-2">
                  <label className="argus-surface-label">Logs tail bytes</label>
                  <Input value={tailBytes} onChange={(event) => setTailBytes(event.target.value)} />
                </div>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <Button type="button" variant="secondary" onClick={() => invokeForJob("process.get")} disabled={loading || !jobId.trim()}>
                  Get
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => {
                    const nextTailBytes = Number(tailBytes);
                    void invokeForJob(
                      "process.logs",
                      Number.isFinite(nextTailBytes) ? { tailBytes: nextTailBytes } : undefined,
                    );
                  }}
                  disabled={loading || !jobId.trim()}
                >
                  Logs
                </Button>
                <Button type="button" variant="secondary" onClick={() => invokeForJob("process.submit")} disabled={loading || !jobId.trim()}>
                  Submit
                </Button>
                <Button type="button" variant="destructive" onClick={() => invokeForJob("process.kill")} disabled={loading || !jobId.trim()}>
                  Kill
                </Button>
              </div>

              <div className="grid gap-4 xl:grid-cols-2">
                <div className="grid gap-2">
                  <label className="argus-surface-label">Write data</label>
                  <Textarea value={writeData} onChange={(event) => setWriteData(event.target.value)} className="min-h-[7rem]" />
                  <div>
                    <Button
                      type="button"
                      variant="secondary"
                      onClick={() => invokeForJob("process.write", { data: writeData })}
                      disabled={loading || !jobId.trim() || !writeData}
                    >
                      Write
                    </Button>
                  </div>
                </div>

                <div className="grid gap-2">
                  <label className="argus-surface-label">Paste text</label>
                  <Textarea value={pasteText} onChange={(event) => setPasteText(event.target.value)} className="min-h-[7rem]" />
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
                      variant="secondary"
                      onClick={() => invokeForJob("process.paste", { text: pasteText, bracketed: pasteBracketed })}
                      disabled={loading || !jobId.trim() || !pasteText}
                    >
                      Paste
                    </Button>
                  </div>
                </div>
              </div>

              <div className="grid gap-3 md:grid-cols-2">
                <div className="grid gap-2">
                  <label className="argus-surface-label">Send keys (comma separated)</label>
                  <Input value={sendKeys} onChange={(event) => setSendKeys(event.target.value)} />
                </div>
                <div className="grid gap-2">
                  <label className="argus-surface-label">Literal text</label>
                  <Input value={sendLiteral} onChange={(event) => setSendLiteral(event.target.value)} />
                </div>
              </div>

              <div className="flex flex-wrap gap-2">
                <Badge tone="default">process.write</Badge>
                <Badge tone="default">process.send_keys</Badge>
                <Badge tone="default">process.paste</Badge>
              </div>

              <div>
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => {
                    const keys = splitKeys(sendKeys);
                    void invokeForJob("process.send_keys", {
                      ...(keys.length ? { keys } : {}),
                      ...(sendLiteral ? { literal: sendLiteral } : {}),
                    });
                  }}
                  disabled={loading || !jobId.trim() || (!sendKeys.trim() && !sendLiteral)}
                >
                  Send keys
                </Button>
              </div>
            </div>
          </PanelCard>
        </section>
      </div>
    </ConsoleShell>
  );
}
