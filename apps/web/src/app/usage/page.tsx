"use client";

import React from "react";
import { RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { useAuth } from "@/components/admin-gate";
import { Badge, EmptyState, Fact, InlineError, PanelCard, Skeleton, StatCard } from "@/components/console-primitives";
import { ConsoleShell } from "@/components/console-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  type AdminOverviewResponse,
  type AdminUsageResponse,
  type AdminUserSummary,
  fetchAdminOverview,
  fetchAdminUsage,
  fetchAdminUsers,
} from "@/lib/admin";
import type { ConsoleUser } from "@/lib/auth";
import { formatCompact, formatInt, formatRelative, formatUsd, formatWhen } from "@/lib/format";
import { useGatewayWsUrlState } from "@/lib/gateway";
import { type SelfUsageResponse, fetchMyUsage } from "@/lib/self";
import { cn } from "@/lib/utils";

export default function UsagePage() {
  const { user } = useAuth();

  if (!user) return null;
  if (user.isAdmin) {
    return <AdminUsagePage />;
  }
  return <SelfUsagePage user={user} />;
}

function SelfUsagePage({ user }: { user: ConsoleUser }) {
  const [wsUrl, setWsUrl] = useGatewayWsUrlState();
  const [usage, setUsage] = React.useState<SelfUsageResponse | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [hours, setHours] = React.useState("24");
  const [limit, setLimit] = React.useState("100");

  const refresh = React.useCallback(
    async (opts?: { notify?: boolean }) => {
      if (!wsUrl.trim()) return;
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams();
        const trimmedHours = hours.trim();
        const trimmedLimit = limit.trim();
        if (trimmedHours) params.set("hours", trimmedHours);
        if (trimmedLimit) params.set("limit", trimmedLimit);
        const result = await fetchMyUsage(wsUrl, params);
        setUsage(result);
      } catch (nextError) {
        const message = (nextError as Error)?.message || String(nextError);
        setUsage(null);
        setError(message);
        if (opts?.notify) {
          toast.error(message);
        }
      } finally {
        setLoading(false);
      }
    },
    [hours, limit, wsUrl]
  );

  React.useEffect(() => {
    if (!wsUrl.trim()) return;
    const run = async () => {
      await Promise.resolve();
      await refresh({ notify: false });
    };
    void run();
  }, [refresh, wsUrl]);

  const summary = usage?.summary;
  const showSkeleton = loading && !usage && !error;

  return (
    <ConsoleShell
      title="Usage"
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <Input
            value={wsUrl}
            onChange={(event) => setWsUrl(event.target.value)}
            className="w-[min(30rem,100%)]"
            placeholder="Gateway wss://.../ws"
            spellCheck={false}
          />
          <Button type="button" variant="secondary" disabled={loading} onClick={() => void refresh({ notify: true })}>
            <RefreshCw className={cn("h-4 w-4", loading ? "animate-spin" : null)} />
            Refresh
          </Button>
        </div>
      }
    >
      {error ? <InlineError message={error} /> : null}

      <div className="grid gap-4">
        <PanelCard
          title={user.email}
          action={
            <div className="grid gap-2 md:grid-cols-[110px_110px]">
              <Input value={hours} onChange={(event) => setHours(event.target.value)} placeholder="hours" />
              <Input value={limit} onChange={(event) => setLimit(event.target.value)} placeholder="limit" />
            </div>
          }
          className="argus-data-grid"
        >
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <Fact label="Window" value={hours.trim() ? `${hours.trim()}h` : "all"} />
            <Fact label="Rows" value={formatInt(usage?.events.length)} />
            <Fact label="Last" value={formatWhen(summary?.lastAtMs)} />
            <Fact label="Status" value={error ? "error" : loading ? "loading" : "ready"} />
          </div>
        </PanelCard>

        {showSkeleton ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            {Array.from({ length: 4 }).map((_, index) => (
              <div key={index} className="argus-metric-card rounded-[20px] p-4">
                <Skeleton className="h-3 w-24 rounded-full" />
                <Skeleton className="mt-3 h-8 w-28" />
                <Skeleton className="mt-3 h-3 w-full max-w-[14rem]" />
              </div>
            ))}
          </div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <StatCard label="Tokens" value={formatCompact(summary?.totalTokens)} tone="primary" />
            <StatCard label="Requests" value={formatInt(summary?.requestCount)} />
            <StatCard
              label="Input / output"
              value={`${formatCompact(summary?.inputTokens)} / ${formatCompact(summary?.outputTokens)}`}
            />
            <StatCard label="Est. cost" value={formatUsd(summary?.estimatedCostUsd)} />
          </div>
        )}

        <PanelCard title="Recent calls" contentClassName="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <Badge tone="default">{hours.trim() ? `${hours.trim()}h` : "all time"}</Badge>
            <Badge tone="default">{formatInt(usage?.events.length)} rows</Badge>
            <Badge tone="default">{formatRelative(summary?.lastAtMs)}</Badge>
          </div>

          {showSkeleton ? (
            <div className="space-y-3">
              {Array.from({ length: 7 }).map((_, index) => (
                <div key={index} className="grid gap-3 rounded-[16px] border border-border/70 bg-background/24 px-4 py-3 md:grid-cols-[1.15fr_0.95fr_0.95fr_0.95fr_0.7fr_0.7fr]">
                  {Array.from({ length: 6 }).map((__, cellIndex) => (
                    <Skeleton key={cellIndex} className="h-4 w-full" />
                  ))}
                </div>
              ))}
            </div>
          ) : usage?.events.length ? (
            <div className="argus-table-shell rounded-[20px]">
              <table className="w-full border-collapse text-sm">
                <thead className="argus-table-head text-left text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
                  <tr>
                    <th className="px-4 py-3">When</th>
                    <th className="px-4 py-3">Agent</th>
                    <th className="px-4 py-3">Channel</th>
                    <th className="px-4 py-3">Model</th>
                    <th className="px-4 py-3 text-right">Tokens</th>
                    <th className="px-4 py-3">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {usage.events.map((event) => (
                    <tr
                      key={[
                        event.createdAtMs,
                        event.responseId || "",
                        event.sessionId || "",
                        event.agentId || "",
                        event.channelId || "",
                      ].join(":")}
                      className="border-t border-border/60"
                    >
                      <td className="px-4 py-3 text-muted-foreground">{formatWhen(event.createdAtMs)}</td>
                      <td className="px-4 py-3 font-mono text-[12.5px]">{event.agentId || "—"}</td>
                      <td className="px-4 py-3">{event.channelName || event.channelId || "—"}</td>
                      <td className="px-4 py-3 font-mono text-[12.5px]">{event.model || event.requestedModel || "—"}</td>
                      <td className="px-4 py-3 text-right font-medium">{formatInt(event.totalTokens)}</td>
                      <td className="px-4 py-3">
                        <span
                          className={cn(
                            "inline-flex items-center rounded-md border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.08em]",
                            event.error
                              ? "border-destructive/36 bg-destructive/10 text-destructive"
                              : "border-emerald-500/28 bg-emerald-500/10 text-emerald-400"
                          )}
                        >
                          {event.error ? event.error : event.status || "ok"}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title="No usage" />
          )}
        </PanelCard>
      </div>
    </ConsoleShell>
  );
}

function AdminUsagePage() {
  const [wsUrl, setWsUrl] = useGatewayWsUrlState();
  const [overview, setOverview] = React.useState<AdminOverviewResponse | null>(null);
  const [users, setUsers] = React.useState<AdminUserSummary[]>([]);
  const [usage, setUsage] = React.useState<AdminUsageResponse | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [overviewError, setOverviewError] = React.useState<string | null>(null);
  const [usersError, setUsersError] = React.useState<string | null>(null);
  const [usageError, setUsageError] = React.useState<string | null>(null);
  const [selectedUserId, setSelectedUserId] = React.useState<string>("");
  const [hours, setHours] = React.useState("24");
  const [limit, setLimit] = React.useState("100");

  const refresh = React.useCallback(async (opts?: { notify?: boolean }) => {
    if (!wsUrl.trim()) return;
    setLoading(true);
    setOverviewError(null);
    setUsersError(null);
    setUsageError(null);

    const params = new URLSearchParams();
    const trimmedUserId = selectedUserId.trim();
    const trimmedHours = hours.trim();
    const trimmedLimit = limit.trim();
    if (trimmedUserId) params.set("user_id", trimmedUserId);
    if (trimmedHours) params.set("hours", trimmedHours);
    if (trimmedLimit) params.set("limit", trimmedLimit);

    const [overviewResult, usersResult, usageResult] = await Promise.allSettled([
      fetchAdminOverview(wsUrl),
      fetchAdminUsers(wsUrl),
      fetchAdminUsage(wsUrl, params),
    ]);

    if (overviewResult.status === "fulfilled") {
      setOverview(overviewResult.value);
    } else {
      setOverview(null);
      setOverviewError((overviewResult.reason as Error)?.message || String(overviewResult.reason));
    }

    if (usersResult.status === "fulfilled") {
      setUsers(usersResult.value.users ?? []);
    } else {
      setUsers([]);
      setUsersError((usersResult.reason as Error)?.message || String(usersResult.reason));
    }

    if (usageResult.status === "fulfilled") {
      setUsage(usageResult.value);
    } else {
      const message = (usageResult.reason as Error)?.message || String(usageResult.reason);
      setUsage(null);
      setUsageError(message);
      if (opts?.notify) {
        toast.error(message);
      }
    }

    setLoading(false);
  }, [hours, limit, selectedUserId, wsUrl]);

  React.useEffect(() => {
    if (!wsUrl.trim()) return;
    const run = async () => {
      await Promise.resolve();
      await refresh({ notify: false });
    };
    void run();
  }, [refresh, wsUrl]);

  const selectedUser = React.useMemo(() => {
    const userId = Number(selectedUserId);
    if (!Number.isFinite(userId) || userId <= 0) return null;
    return users.find((item) => item.userId === userId) ?? null;
  }, [selectedUserId, users]);

  const filteredSummary = usage?.summary;
  const showUsersSkeleton = loading && !users.length && !usersError;
  const showUsageSkeleton = loading && !usage && !usageError;
  const usageUnavailable = Boolean(usageError && !loading && !usage);
  const usersUnavailable = Boolean(usersError && !loading && !users.length);

  return (
    <ConsoleShell
      title="Usage"
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <Input
            value={wsUrl}
            onChange={(event) => setWsUrl(event.target.value)}
            className="w-[min(30rem,100%)]"
            placeholder="Gateway wss://.../ws"
            spellCheck={false}
          />
          <Button type="button" variant="secondary" disabled={loading} onClick={() => void refresh({ notify: true })}>
            <RefreshCw className={cn("h-4 w-4", loading ? "animate-spin" : null)} />
            Refresh
          </Button>
        </div>
      }
    >
      <div className="grid gap-4">
        <PanelCard
          title={selectedUser ? `User ${selectedUser.userId}` : "Global"}
          action={
            <div className="grid gap-2 md:grid-cols-[minmax(0,1.3fr)_110px_110px]">
              <select
                value={selectedUserId}
                onChange={(event) => {
                  setSelectedUserId(event.target.value);
                  setUsage(null);
                  setOverviewError(null);
                  setUsersError(null);
                  setUsageError(null);
                  setLoading(true);
                }}
                className="argus-select min-w-[12rem]"
              >
                <option value="">All users</option>
                {users.map((currentUser) => (
                  <option key={currentUser.userId} value={String(currentUser.userId)}>
                    User {currentUser.userId}
                  </option>
                ))}
              </select>
              <Input value={hours} onChange={(event) => setHours(event.target.value)} placeholder="hours" />
              <Input value={limit} onChange={(event) => setLimit(event.target.value)} placeholder="limit" />
            </div>
          }
          className="argus-data-grid"
        >
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <Fact label="Selected user" value={selectedUser ? `User ${selectedUser.userId}` : "All users"} />
            <Fact label="Window" value={hours.trim() ? `${hours.trim()}h` : "all"} />
            <Fact label="Rows" value={usageUnavailable ? "unavailable" : formatInt(usage?.events.length)} />
            <Fact label="Last" value={usageUnavailable ? "unavailable" : formatWhen(filteredSummary?.lastAtMs)} />
          </div>
        </PanelCard>

        {usageUnavailable ? (
          <PanelCard title="Usage unavailable">
            <InlineError message={usageError || "Usage ledger is unavailable."} />
          </PanelCard>
        ) : showUsageSkeleton ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            {Array.from({ length: 4 }).map((_, index) => (
              <div key={index} className="argus-metric-card rounded-[20px] p-4">
                <Skeleton className="h-3 w-24 rounded-full" />
                <Skeleton className="mt-3 h-8 w-28" />
                <Skeleton className="mt-3 h-3 w-full max-w-[14rem]" />
              </div>
            ))}
          </div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <StatCard label="Filtered tokens" value={formatCompact(filteredSummary?.totalTokens)} tone="primary" />
            <StatCard label="Filtered requests" value={formatInt(filteredSummary?.requestCount)} />
            <StatCard
              label="Input / output"
              value={`${formatCompact(filteredSummary?.inputTokens)} / ${formatCompact(filteredSummary?.outputTokens)}`}
            />
            <StatCard label="Est. cost" value={formatUsd(filteredSummary?.estimatedCostUsd)} />
          </div>
        )}

        {!usageUnavailable ? (
          <div className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
            <section className="space-y-4">
              <PanelCard title="Top users">
                {showUsersSkeleton ? (
                  <div className="space-y-3">
                    {Array.from({ length: 6 }).map((_, index) => (
                      <div key={index} className="argus-row-shell rounded-[16px] px-4 py-3">
                        <Skeleton className="h-4 w-24 rounded-full" />
                        <Skeleton className="mt-3 h-3 w-40" />
                        <Skeleton className="mt-3 h-3 w-full" />
                      </div>
                    ))}
                  </div>
                ) : usersUnavailable ? (
                  <InlineError message={usersError || "Gateway user roster is unavailable."} />
                ) : users.length ? (
                  <div className="space-y-2">
                    {users.slice(0, 8).map((currentUser) => (
                      <button
                        key={currentUser.userId}
                        type="button"
                        onClick={() => {
                          setSelectedUserId(String(currentUser.userId));
                          setUsage(null);
                          setOverviewError(null);
                          setUsersError(null);
                          setUsageError(null);
                          setLoading(true);
                        }}
                        className={cn(
                          "argus-row-shell w-full rounded-[16px] px-4 py-3 text-left",
                          String(currentUser.userId) === selectedUserId
                            ? "border-primary/28 bg-primary/10"
                            : "hover:border-border hover:bg-background/36"
                        )}
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0">
                            <div className="font-medium text-foreground">User {currentUser.userId}</div>
                            <div className="mt-1 text-xs leading-5 text-muted-foreground">
                              {currentUser.currentChannel?.name || currentUser.currentChannelId || "gateway"} · {currentUser.currentModel || "model pending"}
                            </div>
                          </div>
                          <span className="rounded-md border border-border/70 px-2 py-1 text-[10px] font-medium uppercase tracking-[0.08em] text-muted-foreground">
                            {formatCompact(currentUser.usage24h.totalTokens)} tok
                          </span>
                        </div>
                        <div className="mt-3 flex flex-wrap gap-2">
                          <Badge tone="default">{currentUser.agentCount} agents</Badge>
                          <Badge tone="default">{currentUser.channelCount} channels</Badge>
                          <Badge tone="default">{formatRelative(currentUser.lastActiveMs)}</Badge>
                        </div>
                      </button>
                    ))}
                  </div>
                ) : (
                  <EmptyState title="No users" />
                )}
              </PanelCard>
            </section>

            <section className="space-y-4">
              <PanelCard title="Recent calls" contentClassName="space-y-3">
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <Badge tone="default">{selectedUser ? `user ${selectedUser.userId}` : "all users"}</Badge>
                  <Badge tone="default">{hours.trim() ? `${hours.trim()}h` : "all time"}</Badge>
                  <Badge tone="default">{formatInt(usage?.events.length)} rows</Badge>
                  {overview ? <Badge tone="default">v{overview.version}</Badge> : null}
                </div>

                {showUsageSkeleton ? (
                  <div className="space-y-3">
                    {Array.from({ length: 7 }).map((_, index) => (
                      <div key={index} className="grid gap-3 rounded-[16px] border border-border/70 bg-background/24 px-4 py-3 md:grid-cols-[1.15fr_0.7fr_0.95fr_0.95fr_0.95fr_0.7fr_0.7fr]">
                        {Array.from({ length: 7 }).map((__, cellIndex) => (
                          <Skeleton key={cellIndex} className="h-4 w-full" />
                        ))}
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="argus-table-shell rounded-[20px]">
                    <table className="w-full border-collapse text-sm">
                      <thead className="argus-table-head text-left text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
                        <tr>
                          <th className="px-4 py-3">When</th>
                          <th className="px-4 py-3">User</th>
                          <th className="px-4 py-3">Agent</th>
                          <th className="px-4 py-3">Channel</th>
                          <th className="px-4 py-3">Model</th>
                          <th className="px-4 py-3 text-right">Tokens</th>
                          <th className="px-4 py-3">Status</th>
                        </tr>
                      </thead>
                      <tbody>
                        {usage?.events.map((event) => (
                          <tr
                            key={[
                              event.createdAtMs,
                              event.responseId || "",
                              event.sessionId || "",
                              event.agentId || "",
                              event.channelId || "",
                            ].join(":")}
                            className="border-t border-border/60"
                          >
                            <td className="px-4 py-3 text-muted-foreground">{formatWhen(event.createdAtMs)}</td>
                            <td className="px-4 py-3">{event.ownerUserId || "—"}</td>
                            <td className="px-4 py-3 font-mono text-[12.5px]">{event.agentId || "—"}</td>
                            <td className="px-4 py-3">{event.channelName || event.channelId || "—"}</td>
                            <td className="px-4 py-3 font-mono text-[12.5px]">{event.model || event.requestedModel || "—"}</td>
                            <td className="px-4 py-3 text-right font-medium">{formatInt(event.totalTokens)}</td>
                            <td className="px-4 py-3">
                              <span
                                className={cn(
                                  "inline-flex items-center rounded-md border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.08em]",
                                  event.error
                                    ? "border-destructive/36 bg-destructive/10 text-destructive"
                                    : "border-emerald-500/28 bg-emerald-500/10 text-emerald-400"
                                )}
                              >
                                {event.error ? event.error : event.status || "ok"}
                              </span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {!usage?.events.length ? (
                      <div className="px-4 py-8 text-center text-sm text-muted-foreground">No usage</div>
                    ) : null}
                  </div>
                )}
              </PanelCard>
            </section>
          </div>
        ) : null}
      </div>
    </ConsoleShell>
  );
}
