"use client";

import * as React from "react";
import { RefreshCw, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { useAuth } from "@/components/admin-gate";
import { Badge, EmptyState, InlineError, PanelCard, Skeleton, StatCard } from "@/components/console-primitives";
import { ConsoleShell } from "@/components/console-shell";
import { Button } from "@/components/ui/button";
import { type AdminSessionRow, deleteAdminSession, fetchAdminSessions } from "@/lib/admin";
import { formatInt } from "@/lib/format";
import { useGatewayWsUrlState } from "@/lib/gateway";

function normalizedSessionStatus(status?: string | null): string {
  const value = String(status || "").trim().toLowerCase();
  return value || "unknown";
}

function sessionStatusTone(status?: string | null): "success" | "warning" | "default" {
  const normalized = normalizedSessionStatus(status);
  if (["running", "ready", "connected", "online"].includes(normalized)) return "success";
  if (["pending", "starting", "creating", "restarting"].includes(normalized)) return "warning";
  return "default";
}

function sessionDisplayName(session: AdminSessionRow): string {
  return String(session.name || "").trim() || String(session.sessionId || "").trim() || String(session.containerId || "").trim() || "session";
}

function sessionValue(value?: string | number | null): string {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "string" && value.trim()) return value.trim();
  return "—";
}

export default function SessionFleetPage() {
  const { user } = useAuth();
  const [wsUrl] = useGatewayWsUrlState();
  const [sessions, setSessions] = React.useState<AdminSessionRow[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [deletingId, setDeletingId] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const refresh = React.useCallback(
    async (opts?: { notify?: boolean }) => {
      if (!wsUrl.trim()) return;
      setLoading(true);
      setError(null);
      try {
        const result = await fetchAdminSessions(wsUrl);
        setSessions(Array.isArray(result.sessions) ? result.sessions : []);
        if (opts?.notify) {
          toast.success("Refreshed");
        }
      } catch (nextError) {
        const message = (nextError as Error)?.message || String(nextError);
        setSessions([]);
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
      if (typeof window !== "undefined" && !window.confirm(`Delete ${label}?`)) {
        return;
      }
      setDeletingId(sessionId);
      setError(null);
      try {
        await deleteAdminSession(wsUrl, sessionId);
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
    [refresh, wsUrl],
  );

  const runningCount = React.useMemo(
    () => sessions.filter((session) => normalizedSessionStatus(session.status) === "running").length,
    [sessions],
  );
  const containerCount = React.useMemo(
    () => new Set(sessions.map((session) => String(session.containerId || "").trim()).filter(Boolean)).size,
    [sessions],
  );
  const ownerCount = React.useMemo(
    () =>
      new Set(
        sessions
          .map((session) => (typeof session.ownerUserId === "number" && session.ownerUserId > 0 ? session.ownerUserId : null))
          .filter((value): value is number => value !== null),
      ).size,
    [sessions],
  );
  const showSkeleton = loading && !sessions.length && !error;

  if (!user || !user.isAdmin) return null;

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
      <div className="grid gap-4">
        {error ? <InlineError message={error} /> : null}

        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <StatCard label="Sessions" value={formatInt(sessions.length)} tone="primary" />
          <StatCard label="Running" value={formatInt(runningCount)} />
          <StatCard label="Containers" value={formatInt(containerCount)} />
          <StatCard label="Owners" value={formatInt(ownerCount)} />
        </div>

        <PanelCard title="All sessions" contentClassName="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <Badge tone="default">{formatInt(sessions.length)} total</Badge>
            <Badge tone="default">{formatInt(runningCount)} running</Badge>
          </div>

          {showSkeleton ? (
            <div className="space-y-3">
              {Array.from({ length: 6 }).map((_, index) => (
                <div
                  key={index}
                  className="grid gap-3 rounded-[16px] border border-border/70 bg-background/24 px-4 py-3 md:grid-cols-[1.5fr_0.7fr_1fr_1fr_0.8fr_0.7fr]"
                >
                  {Array.from({ length: 6 }).map((__, cellIndex) => (
                    <Skeleton key={cellIndex} className="h-4 w-full" />
                  ))}
                </div>
              ))}
            </div>
          ) : sessions.length ? (
            <div className="argus-table-shell rounded-[20px]">
              <table className="w-full border-collapse text-sm">
                <thead className="argus-table-head text-left text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
                  <tr>
                    <th className="px-4 py-3">Session</th>
                    <th className="px-4 py-3">Owner</th>
                    <th className="px-4 py-3">Agent</th>
                    <th className="px-4 py-3">Container</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3 text-right">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {sessions.map((session) => {
                    const sessionId = String(session.sessionId || "").trim();
                    const label = sessionDisplayName(session);
                    const deleting = deletingId === sessionId;
                    return (
                      <tr key={sessionId || `${label}:${session.containerId || ""}`} className="border-t border-border/60">
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
                        <td className="px-4 py-3 font-mono text-[12.5px]">{sessionValue(session.ownerUserId)}</td>
                        <td className="px-4 py-3 font-mono text-[12.5px]">{sessionValue(session.agentId)}</td>
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
