"use client";

import * as React from "react";
import { RefreshCw, Trash2, UserPlus, Users, X } from "lucide-react";
import { toast } from "sonner";

import { useAuth } from "@/components/admin-gate";
import { useConfirmDialog } from "@/components/confirm-dialog";
import { Badge, EmptyState, InlineError, PanelCard, Skeleton, StatCard } from "@/components/console-primitives";
import { ConsoleShell } from "@/components/console-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { AdminAgentEntry, AdminSessionRow } from "@/lib/admin";
import { formatInt } from "@/lib/format";
import { useGatewayWsUrlState } from "@/lib/gateway";
import {
  deleteMySession,
  fetchMyAgents,
  fetchMySessions,
  shareMyAgent,
  type SelfAgentsResponse,
  type SelfSharedUser,
  unshareMyAgent,
} from "@/lib/self";

function normalizedSessionStatus(status?: string | null): string {
  const value = String(status || "").trim().toLowerCase();
  return value || "unknown";
}

function sessionStatusTone(status?: string | null): "success" | "warning" | "default" {
  const normalized = normalizedSessionStatus(status);
  if (["running", "ready", "connected", "online"].includes(normalized)) return "success";
  if (["pending", "starting", "creating", "restarting", "failed", "error"].includes(normalized)) return "warning";
  return "default";
}

function sessionDisplayName(session: AdminSessionRow): string {
  return String(session.name || "").trim() || String(session.sessionId || "").trim() || String(session.containerId || "").trim() || "session";
}

function sessionValue(value?: string | number | null): string {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "string" && value.trim()) return value.trim();
  return "-";
}

function sharedUserLabel(user?: SelfSharedUser | null, fallbackUserId?: number): string {
  const email = String(user?.email || "").trim();
  if (email) return email;
  const username = String(user?.username || "").trim();
  if (username) return username.startsWith("@") ? username : `@${username}`;
  return typeof fallbackUserId === "number" && Number.isFinite(fallbackUserId) ? `User ${fallbackUserId}` : "User";
}

function agentShareUserIds(agent?: AdminAgentEntry | null): number[] {
  if (!agent || !Array.isArray(agent.allowedUserIds)) return [];
  const seen = new Set<number>();
  for (const rawUserId of agent.allowedUserIds) {
    const userId = Number(rawUserId);
    if (!Number.isFinite(userId) || userId <= 0 || seen.has(userId)) continue;
    seen.add(userId);
  }
  return Array.from(seen).sort((a, b) => a - b);
}

export default function SessionsPage() {
  const { user } = useAuth();
  const { confirm, confirmDialog } = useConfirmDialog();
  const [wsUrl] = useGatewayWsUrlState();
  const [sessions, setSessions] = React.useState<AdminSessionRow[]>([]);
  const [agentsState, setAgentsState] = React.useState<SelfAgentsResponse | null>(null);
  const [shareDraftByAgentId, setShareDraftByAgentId] = React.useState<Record<string, string>>({});
  const [loading, setLoading] = React.useState(false);
  const [deletingId, setDeletingId] = React.useState<string | null>(null);
  const [sharingAgentId, setSharingAgentId] = React.useState<string | null>(null);
  const [unsharingKey, setUnsharingKey] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const refresh = React.useCallback(
    async (opts?: { notify?: boolean }) => {
      if (!wsUrl.trim()) return;
      setLoading(true);
      setError(null);
      try {
        const [sessionsResult, agentsResult] = await Promise.allSettled([
          fetchMySessions(wsUrl),
          fetchMyAgents(wsUrl, { bootstrap: false }),
        ]);
        if (sessionsResult.status === "rejected") {
          throw sessionsResult.reason;
        }
        setSessions(Array.isArray(sessionsResult.value.sessions) ? sessionsResult.value.sessions : []);
        let shareLoadFailed = false;
        if (agentsResult.status === "fulfilled") {
          setAgentsState(agentsResult.value);
        } else {
          shareLoadFailed = true;
          const shareMessage = (agentsResult.reason as Error)?.message || String(agentsResult.reason);
          setAgentsState(null);
          setError(`Sharing data unavailable: ${shareMessage}`);
          if (opts?.notify) {
            toast.error(shareMessage);
          }
        }
        if (opts?.notify && !shareLoadFailed) {
          toast.success("Refreshed");
        }
      } catch (nextError) {
        const message = (nextError as Error)?.message || String(nextError);
        setSessions([]);
        setAgentsState(null);
        setError(message);
        if (opts?.notify) {
          toast.error(message);
        }
      } finally {
        setLoading(false);
      }
    },
    [wsUrl],
  );

  React.useEffect(() => {
    if (!wsUrl.trim()) return;
    const run = async () => {
      await Promise.resolve();
      await refresh({ notify: false });
    };
    void run();
  }, [refresh, wsUrl]);

  const handleDelete = React.useCallback(
    async (session: AdminSessionRow) => {
      const sessionId = String(session.sessionId || "").trim();
      if (!sessionId || !wsUrl.trim()) return;
      const label = sessionDisplayName(session);
      const confirmed = await confirm({
        title: "Delete session?",
        body: `Delete ${label}? This removes the runtime session from your account.`,
        confirmLabel: "Delete session",
        tone: "destructive",
      });
      if (!confirmed) return;
      setDeletingId(sessionId);
      setError(null);
      try {
        await deleteMySession(wsUrl, sessionId);
        setSessions((current) => current.filter((item) => String(item.sessionId || "").trim() !== sessionId));
        toast.success("Session deleted");
        await refresh({ notify: false });
      } catch (nextError) {
        const message = (nextError as Error)?.message || String(nextError);
        setError(message);
        toast.error(message);
      } finally {
        setDeletingId(null);
      }
    },
    [confirm, refresh, wsUrl],
  );

  const handleShare = React.useCallback(
    async (agentId: string) => {
      const account = String(shareDraftByAgentId[agentId] || "").trim();
      if (!agentId || !wsUrl.trim()) return;
      if (!account) {
        toast.error("Account is required");
        return;
      }
      setSharingAgentId(agentId);
      setError(null);
      try {
        const result = await shareMyAgent(wsUrl, agentId, { account });
        setAgentsState(result);
        setShareDraftByAgentId((current) => {
          const next = { ...current };
          delete next[agentId];
          return next;
        });
        toast.success(`Shared with ${sharedUserLabel(result.sharedUser)}`);
      } catch (nextError) {
        const message = (nextError as Error)?.message || String(nextError);
        setError(message);
        toast.error(message);
      } finally {
        setSharingAgentId(null);
      }
    },
    [shareDraftByAgentId, wsUrl],
  );

  const handleUnshare = React.useCallback(
    async (agent: AdminAgentEntry, targetUserId: number) => {
      if (!agent.agentId || !wsUrl.trim()) return;
      const sharedUser = agentsState?.sharedUsersById?.[String(targetUserId)] ?? null;
      const confirmed = await confirm({
        title: "Remove share?",
        body: `Stop sharing ${agent.shortName || agent.agentId} with ${sharedUserLabel(sharedUser, targetUserId)}?`,
        confirmLabel: "Remove share",
        tone: "destructive",
      });
      if (!confirmed) return;
      const key = `${agent.agentId}:${targetUserId}`;
      setUnsharingKey(key);
      setError(null);
      try {
        const result = await unshareMyAgent(wsUrl, agent.agentId, targetUserId);
        setAgentsState(result);
        toast.success("Share removed");
      } catch (nextError) {
        const message = (nextError as Error)?.message || String(nextError);
        setError(message);
        toast.error(message);
      } finally {
        setUnsharingKey(null);
      }
    },
    [agentsState?.sharedUsersById, confirm, wsUrl],
  );

  const agentsById = React.useMemo(() => {
    const next = new Map<string, AdminAgentEntry>();
    for (const agent of agentsState?.agents ?? []) {
      if (agent.agentId) next.set(agent.agentId, agent);
    }
    return next;
  }, [agentsState?.agents]);

  const sharedUsersById = agentsState?.sharedUsersById ?? {};

  const runningCount = React.useMemo(
    () => sessions.filter((session) => normalizedSessionStatus(session.status) === "running").length,
    [sessions],
  );
  const containerCount = React.useMemo(
    () => new Set(sessions.map((session) => String(session.containerId || "").trim()).filter(Boolean)).size,
    [sessions],
  );
  const agentCount = React.useMemo(
    () => new Set(sessions.map((session) => String(session.agentId || "").trim()).filter(Boolean)).size,
    [sessions],
  );
  const providerCount = React.useMemo(
    () => new Set(sessions.map((session) => String(session.provider || "").trim()).filter(Boolean)).size,
    [sessions],
  );
  const showSkeleton = loading && !sessions.length && !error;

  if (!user) return null;

  return (
    <ConsoleShell
      title="Sessions"
      actions={
        <Button type="button" variant="secondary" onClick={() => void refresh({ notify: true })} disabled={loading}>
          <RefreshCw className="h-4 w-4" />
          Refresh
        </Button>
      }
    >
      {confirmDialog}
      <div className="grid gap-4">
        {error ? <InlineError message={error} /> : null}

        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <StatCard label="Sessions" value={formatInt(sessions.length)} tone="primary" />
          <StatCard label="Running" value={formatInt(runningCount)} />
          <StatCard label="Agents" value={formatInt(agentCount)} />
          <StatCard label="Providers" value={formatInt(providerCount)} />
        </div>

        <PanelCard title="Your sessions" contentClassName="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <Badge tone="default">{formatInt(sessions.length)} total</Badge>
            <Badge tone="default">{formatInt(runningCount)} running</Badge>
            <Badge tone="default">{formatInt(containerCount)} containers</Badge>
          </div>

          {showSkeleton ? (
            <div className="space-y-3">
              {Array.from({ length: 5 }).map((_, index) => (
                <div
                  key={index}
                  className="grid gap-3 border-t border-border/54 px-4 py-3 first:border-t-0 md:grid-cols-[1.4fr_1fr_1.35fr_1fr_0.8fr_0.7fr]"
                >
                  {Array.from({ length: 6 }).map((__, cellIndex) => (
                    <Skeleton key={cellIndex} className="h-4 w-full" />
                  ))}
                </div>
              ))}
            </div>
          ) : sessions.length ? (
            <div className="argus-table-shell">
              <table className="w-full border-collapse text-sm">
                <thead className="argus-table-head text-left text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
                  <tr>
                    <th className="px-4 py-3">Session</th>
                    <th className="px-4 py-3">Agent</th>
                    <th className="px-4 py-3">Sharing</th>
                    <th className="px-4 py-3">Container</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3 text-right">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {sessions.map((session, index) => {
                    const sessionId = String(session.sessionId || "").trim();
                    const label = sessionDisplayName(session);
                    const deleting = deletingId === sessionId;
                    const agentId = String(session.agentId || "").trim();
                    const agent = agentId ? agentsById.get(agentId) ?? null : null;
                    const shareUserIds = agentShareUserIds(agent);
                    const canShare = Boolean(agent?.isOwner && agent.agentId);
                    return (
                      <tr key={sessionId || `${label}:${session.containerId || ""}:${index}`} className="border-t border-border/60">
                        <td className="px-4 py-3">
                          <div className="min-w-0">
                            <div className="truncate font-medium text-foreground">{label}</div>
                            {sessionId && label !== sessionId ? (
                              <div className="mt-1 truncate font-mono text-[12.5px] text-muted-foreground">
                                {sessionId}
                              </div>
                            ) : null}
                            {session.provider ? (
                              <div className="mt-2">
                                <Badge tone="default">{session.provider}</Badge>
                              </div>
                            ) : null}
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <div className="min-w-0">
                            <div className="truncate font-medium text-foreground">{agent?.shortName || sessionValue(session.agentId)}</div>
                            {agentId ? (
                              <div className="mt-1 truncate font-mono text-[12.5px] text-muted-foreground">{agentId}</div>
                            ) : null}
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          {canShare && agent ? (
                            <div className="grid min-w-[260px] gap-2">
                              <div className="flex flex-wrap gap-1.5">
                                {shareUserIds.length ? (
                                  shareUserIds.map((targetUserId) => {
                                    const sharedUser = sharedUsersById[String(targetUserId)] ?? null;
                                    const key = `${agent.agentId}:${targetUserId}`;
                                    const removing = unsharingKey === key;
                                    return (
                                      <span
                                        key={targetUserId}
                                        className="inline-flex max-w-full items-center gap-1 rounded-[999px] border border-border/70 bg-background/30 px-2 py-1 text-xs text-foreground"
                                      >
                                        <Users className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                                        <span className="max-w-[11rem] truncate">{sharedUserLabel(sharedUser, targetUserId)}</span>
                                        <button
                                          type="button"
                                          className="rounded-full p-0.5 text-muted-foreground transition hover:bg-destructive/12 hover:text-destructive"
                                          disabled={removing || loading || sharingAgentId === agent.agentId}
                                          onClick={() => void handleUnshare(agent, targetUserId)}
                                          aria-label={`Remove ${sharedUserLabel(sharedUser, targetUserId)}`}
                                        >
                                          <X className="h-3.5 w-3.5" />
                                        </button>
                                      </span>
                                    );
                                  })
                                ) : (
                                  <span className="text-xs text-muted-foreground">No shares</span>
                                )}
                              </div>
                              <div className="flex gap-2">
                                <Input
                                  className="h-8 text-xs"
                                  value={shareDraftByAgentId[agent.agentId] ?? ""}
                                  onChange={(event) =>
                                    setShareDraftByAgentId((current) => ({
                                      ...current,
                                      [agent.agentId]: event.target.value,
                                    }))
                                  }
                                  onKeyDown={(event) => {
                                    if (event.key === "Enter") {
                                      event.preventDefault();
                                      void handleShare(agent.agentId);
                                    }
                                  }}
                                  placeholder="email or username"
                                />
                                <Button
                                  type="button"
                                  size="sm"
                                  variant="secondary"
                                  disabled={loading || sharingAgentId === agent.agentId || !String(shareDraftByAgentId[agent.agentId] || "").trim()}
                                  onClick={() => void handleShare(agent.agentId)}
                                >
                                  <UserPlus className="h-4 w-4" />
                                  Share
                                </Button>
                              </div>
                            </div>
                          ) : agent ? (
                            <Badge tone="default">{agent.isOwner ? "not owned" : "shared"}</Badge>
                          ) : (
                            <span className="text-xs text-muted-foreground">-</span>
                          )}
                        </td>
                        <td className="px-4 py-3 font-mono text-[12.5px]">{sessionValue(session.containerId)}</td>
                        <td className="px-4 py-3">
                          <Badge tone={sessionStatusTone(session.status)}>{normalizedSessionStatus(session.status)}</Badge>
                        </td>
                        <td className="px-4 py-3 text-right">
                          <Button
                            type="button"
                            size="sm"
                            variant="destructive"
                            disabled={!sessionId || deleting}
                            onClick={() => void handleDelete(session)}
                          >
                            <Trash2 className="h-4 w-4" />
                            {deleting ? "Deleting" : "Delete"}
                          </Button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title="No sessions" />
          )}
        </PanelCard>
      </div>
    </ConsoleShell>
  );
}
