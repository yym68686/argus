"use client";

import * as React from "react";
import { Check, Copy, Link2, Pencil, Plus, RefreshCw, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { useAuth } from "@/components/admin-gate";
import { Badge, EmptyState, Fact, InlineError, PanelCard } from "@/components/console-primitives";
import { ConsoleShell } from "@/components/console-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { formatWhen } from "@/lib/format";
import { useGatewayWsUrlState } from "@/lib/gateway";
import {
  activateMyAgent,
  createMyAgent,
  deleteMyAgent,
  fetchMyAgentConnection,
  fetchMyAgents,
  renameMyAgent,
  setMyAgentModel,
  type SelfAgentConnectionResponse,
  type SelfAgentsResponse,
} from "@/lib/self";
import { cn } from "@/lib/utils";

function agentBadgeTone(agent: { isDefault?: boolean; agentId?: string | null }, currentAgentId?: string | null): "primary" | "success" | "default" {
  if (agent.agentId && currentAgentId && agent.agentId === currentAgentId) return "primary";
  if (agent.isDefault) return "success";
  return "default";
}

export default function AgentsPage() {
  const { user } = useAuth();
  const [wsUrl] = useGatewayWsUrlState();
  const [agentsState, setAgentsState] = React.useState<SelfAgentsResponse | null>(null);
  const [connection, setConnection] = React.useState<SelfAgentConnectionResponse | null>(null);
  const [selectedAgentId, setSelectedAgentId] = React.useState("");
  const [createName, setCreateName] = React.useState("");
  const [renameName, setRenameName] = React.useState("");
  const [modelDraft, setModelDraft] = React.useState("");
  const [loading, setLoading] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const agents = agentsState?.agents ?? [];

  const selectedAgent = (() => {
    if (selectedAgentId) {
      const direct = agents.find((agent) => agent.agentId === selectedAgentId);
      if (direct) return direct;
    }
    if (agentsState?.currentAgentId) {
      const current = agents.find((agent) => agent.agentId === agentsState.currentAgentId);
      if (current) return current;
    }
    return agents[0] ?? null;
  })();

  const applyAgents = React.useCallback((nextState: SelfAgentsResponse, preferredAgentId?: string | null) => {
    const candidates = [
      preferredAgentId,
      selectedAgentId,
      nextState.currentAgentId,
      nextState.agent?.agentId,
      nextState.agents[0]?.agentId,
    ];
    let nextSelectedAgentId = "";
    for (const candidate of candidates) {
      if (!candidate) continue;
      if (nextState.agents.some((agent) => agent.agentId === candidate)) {
        nextSelectedAgentId = candidate;
        break;
      }
    }
    const nextSelectedAgent = nextState.agents.find((agent) => agent.agentId === nextSelectedAgentId) ?? null;
    setAgentsState(nextState);
    setSelectedAgentId(nextSelectedAgentId);
    setRenameName(nextSelectedAgent?.shortName ?? "");
    setModelDraft(nextSelectedAgent?.model ?? "");
  }, [selectedAgentId]);

  const copyText = React.useCallback((text: string, label: string) => {
    if (!text.trim()) return;
    void navigator.clipboard.writeText(text).then(
      () => toast.success(`${label} copied`),
      () => toast.error("Copy failed")
    );
  }, []);

  const refresh = React.useCallback(async (opts?: { notify?: boolean }) => {
    if (!wsUrl.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const result = await fetchMyAgents(wsUrl);
      applyAgents(result);
      if (opts?.notify) {
        toast.success("Refreshed");
      }
    } catch (nextError) {
      const message = (nextError as Error)?.message || String(nextError);
      setError(message);
      if (opts?.notify) {
        toast.error(message);
      }
    } finally {
      setLoading(false);
    }
  }, [applyAgents, wsUrl]);

  React.useEffect(() => {
    if (!wsUrl.trim()) return;
    const run = async () => {
      await Promise.resolve();
      await refresh({ notify: false });
    };
    void run();
  }, [refresh, wsUrl]);

  const createAgent = React.useCallback(async () => {
    if (!wsUrl.trim()) return;
    if (!createName.trim()) {
      toast.error("Agent name is required");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const result = await createMyAgent(wsUrl, { name: createName });
      applyAgents(result, result.agent?.agentId ?? null);
      setCreateName("");
      toast.success("Agent created");
    } catch (nextError) {
      const message = (nextError as Error)?.message || String(nextError);
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  }, [applyAgents, createName, wsUrl]);

  const activateAgent = React.useCallback(async () => {
    if (!selectedAgent || !wsUrl.trim()) return;
    if (selectedAgent.agentId === agentsState?.currentAgentId) return;
    setSaving(true);
    setError(null);
    try {
      const result = await activateMyAgent(wsUrl, selectedAgent.agentId);
      applyAgents(result, selectedAgent.agentId);
      toast.success("Current agent updated");
    } catch (nextError) {
      const message = (nextError as Error)?.message || String(nextError);
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  }, [agentsState?.currentAgentId, applyAgents, selectedAgent, wsUrl]);

  const saveRename = React.useCallback(async () => {
    if (!selectedAgent || !wsUrl.trim()) return;
    if (!renameName.trim()) {
      toast.error("Agent name is required");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const result = await renameMyAgent(wsUrl, selectedAgent.agentId, { name: renameName });
      applyAgents(result, result.agent?.agentId ?? selectedAgent.agentId);
      toast.success("Agent renamed");
    } catch (nextError) {
      const message = (nextError as Error)?.message || String(nextError);
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  }, [applyAgents, renameName, selectedAgent, wsUrl]);

  const saveModel = React.useCallback(async () => {
    if (!selectedAgent || !wsUrl.trim()) return;
    if (!modelDraft.trim()) {
      toast.error("Model is required");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const result = await setMyAgentModel(wsUrl, selectedAgent.agentId, { model: modelDraft });
      applyAgents(result, selectedAgent.agentId);
      toast.success("Model updated");
    } catch (nextError) {
      const message = (nextError as Error)?.message || String(nextError);
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  }, [applyAgents, modelDraft, selectedAgent, wsUrl]);

  const removeAgent = React.useCallback(async () => {
    if (!selectedAgent || !wsUrl.trim()) return;
    if (!window.confirm(`Delete ${selectedAgent.shortName || selectedAgent.agentId}?`)) return;
    setSaving(true);
    setError(null);
    try {
      const result = await deleteMyAgent(wsUrl, selectedAgent.agentId);
      applyAgents(result, result.currentAgentId ?? null);
      setConnection((current) => (current?.agentId === selectedAgent.agentId ? null : current));
      toast.success("Agent deleted");
    } catch (nextError) {
      const message = (nextError as Error)?.message || String(nextError);
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  }, [applyAgents, selectedAgent, wsUrl]);

  const loadConnection = React.useCallback(async () => {
    if (!selectedAgent || !wsUrl.trim()) return;
    setSaving(true);
    setError(null);
    try {
      const result = await fetchMyAgentConnection(wsUrl, selectedAgent.agentId);
      setConnection(result);
      toast.success("Connection info generated");
    } catch (nextError) {
      const message = (nextError as Error)?.message || String(nextError);
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  }, [selectedAgent, wsUrl]);

  if (!user) return null;

  return (
    <ConsoleShell
      title="Agents"
      actions={
        <Button type="button" variant="secondary" disabled={loading} onClick={() => void refresh({ notify: true })}>
          <RefreshCw className={cn("h-4 w-4", loading ? "animate-spin" : null)} />
          Refresh
        </Button>
      }
    >
      {error ? <InlineError message={error} /> : null}

      <div className="grid gap-4">
        <PanelCard title={user.email} className="argus-data-grid">
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
            <Fact label="Current" value={agentsState?.currentAgent?.shortName || agentsState?.currentAgentId || "—"} />
            <Fact label="Session" value={agentsState?.currentSessionId || "—"} mono />
            <Fact label="Agents" value={String(agentsState?.counts.agents ?? agents.length)} />
            <Fact label="Sessions" value={String(agentsState?.counts.sessions ?? 0)} />
            <Fact label="Limit" value={String(agentsState?.limits.maxAgents ?? "—")} />
          </div>
        </PanelCard>

        <div className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
          <section className="space-y-4">
            <PanelCard title="Agents">
              {agents.length ? (
                <div className="space-y-2">
                  {agents.map((agent) => {
                    const active = selectedAgent?.agentId === agent.agentId;
                    const current = agentsState?.currentAgentId === agent.agentId;
                    return (
                      <button
                        key={agent.agentId}
                        type="button"
                        onClick={() => {
                          setSelectedAgentId(agent.agentId);
                          setRenameName(agent.shortName ?? "");
                          setModelDraft(agent.model ?? "");
                        }}
                        className={cn(
                          "argus-row-shell w-full rounded-[16px] px-4 py-3 text-left",
                          active ? "border-primary/28 bg-primary/10" : "hover:border-border hover:bg-background/36"
                        )}
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0">
                            <div className="truncate font-medium text-foreground">{agent.shortName || agent.agentId}</div>
                            <div className="mt-1 truncate text-xs text-muted-foreground">{agent.agentId}</div>
                          </div>
                          <Badge tone={agentBadgeTone(agent, agentsState?.currentAgentId)}>{current ? "current" : agent.isDefault ? "main" : "agent"}</Badge>
                        </div>
                        <div className="mt-3 flex flex-wrap gap-2">
                          {agent.sessionId ? <Badge tone="default">{agent.sessionId}</Badge> : null}
                          {agent.model ? <Badge tone="default">{agent.model}</Badge> : null}
                        </div>
                      </button>
                    );
                  })}
                </div>
              ) : (
                <EmptyState title="No agents" />
              )}
            </PanelCard>

            <PanelCard title="New agent">
              <div className="grid gap-3">
                <Input value={createName} onChange={(event) => setCreateName(event.target.value)} placeholder="agent name" />
                <Button type="button" disabled={loading || saving} onClick={() => void createAgent()}>
                  <Plus className="h-4 w-4" />
                  Create
                </Button>
              </div>
            </PanelCard>
          </section>

          <section className="space-y-4">
            <PanelCard
              title={selectedAgent?.shortName || selectedAgent?.agentId || "Agent"}
              action={
                selectedAgent ? (
                  <div className="flex flex-wrap items-center gap-2">
                    {selectedAgent.agentId !== agentsState?.currentAgentId ? (
                      <Button type="button" size="sm" disabled={loading || saving} onClick={() => void activateAgent()}>
                        <Check className="h-4 w-4" />
                        Use
                      </Button>
                    ) : null}
                    <Button type="button" size="sm" variant="secondary" disabled={loading || saving} onClick={() => void loadConnection()}>
                      <Link2 className="h-4 w-4" />
                      Connection
                    </Button>
                    {!selectedAgent.isDefault ? (
                      <Button type="button" size="sm" variant="destructive" disabled={loading || saving} onClick={() => void removeAgent()}>
                        <Trash2 className="h-4 w-4" />
                        Delete
                      </Button>
                    ) : null}
                  </div>
                ) : null
              }
            >
              {selectedAgent ? (
                <div className="grid gap-4">
                  {!selectedAgent.isDefault ? (
                    <div className="grid gap-2 md:grid-cols-[minmax(0,1fr)_auto]">
                      <Input value={renameName} onChange={(event) => setRenameName(event.target.value)} placeholder="agent name" />
                      <Button type="button" variant="secondary" disabled={loading || saving || !renameName.trim()} onClick={() => void saveRename()}>
                        <Pencil className="h-4 w-4" />
                        Rename
                      </Button>
                    </div>
                  ) : null}

                  <div className="grid gap-2 md:grid-cols-[minmax(0,1fr)_auto]">
                    <Input value={modelDraft} onChange={(event) => setModelDraft(event.target.value)} placeholder="model" />
                    <Button type="button" variant="secondary" disabled={loading || saving || !modelDraft.trim()} onClick={() => void saveModel()}>
                      Save model
                    </Button>
                  </div>

                  {agentsState?.availableModels?.length ? (
                    <div className="flex flex-wrap gap-2">
                      {agentsState.availableModels.map((model) => (
                        <Badge key={model} tone={model === selectedAgent.model ? "primary" : "default"}>
                          {model}
                        </Badge>
                      ))}
                    </div>
                  ) : null}

                  <div className="grid gap-3 md:grid-cols-2">
                    <Fact label="ID" value={selectedAgent.agentId} mono />
                    <Fact label="Session" value={selectedAgent.sessionId || "—"} mono />
                    <Fact label="Workspace" value={selectedAgent.workspaceHostPath || "—"} mono />
                    <Fact label="Created" value={formatWhen(selectedAgent.createdAtMs)} />
                  </div>
                </div>
              ) : (
                <EmptyState title="No selection" />
              )}
            </PanelCard>

            <PanelCard title="Connection info">
              {selectedAgent && connection && connection.agentId === selectedAgent.agentId ? (
                <div className="grid gap-4">
                  <div className="grid gap-3 md:grid-cols-2">
                    <Fact label="Gateway" value={connection.gatewayBaseUrl || "—"} mono />
                    <Fact label="Provider" value={connection.provider || "—"} />
                    <Fact label="Session" value={connection.sessionId} mono />
                    <Fact label="Model" value={connection.model || "—"} mono />
                  </div>

                  <ConnectionRow label="WebSocket" value={connection.ws.url} onCopy={() => copyText(connection.ws.url, "WebSocket URL")} />
                  {connection.mcp?.token ? (
                    <ConnectionRow label="MCP token" value={connection.mcp.token} onCopy={() => copyText(connection.mcp?.token || "", "MCP token")} />
                  ) : null}
                  {connection.node?.token ? (
                    <ConnectionRow label="Node token" value={connection.node.token} onCopy={() => copyText(connection.node?.token || "", "Node token")} />
                  ) : null}
                  {connection.openai?.token ? (
                    <ConnectionRow label="OpenAI token" value={connection.openai.token} onCopy={() => copyText(connection.openai?.token || "", "OpenAI token")} />
                  ) : null}
                  {connection.openai?.url ? (
                    <ConnectionRow label="Responses URL" value={connection.openai.url} onCopy={() => copyText(connection.openai?.url || "", "Responses URL")} />
                  ) : null}
                </div>
              ) : (
                <div className="grid gap-3">
                  <div className="rounded-[16px] border border-border/70 bg-background/24 px-4 py-3 text-sm text-muted-foreground">
                    Generate session-scoped connection details for the selected agent.
                  </div>
                  <Button type="button" variant="secondary" disabled={!selectedAgent || loading || saving} onClick={() => void loadConnection()}>
                    <Link2 className="h-4 w-4" />
                    Generate
                  </Button>
                </div>
              )}
            </PanelCard>
          </section>
        </div>
      </div>
    </ConsoleShell>
  );
}

function ConnectionRow({ label, value, onCopy }: { label: string; value: string; onCopy: () => void }) {
  return (
    <div className="grid gap-2 md:grid-cols-[9rem_minmax(0,1fr)_auto] md:items-center">
      <div className="text-xs font-medium uppercase tracking-[0.12em] text-muted-foreground">{label}</div>
      <code className="rounded-[14px] border border-border/70 bg-background/26 px-3 py-2 text-xs text-foreground">{value}</code>
      <Button type="button" size="sm" variant="secondary" onClick={onCopy}>
        <Copy className="h-4 w-4" />
        Copy
      </Button>
    </div>
  );
}
