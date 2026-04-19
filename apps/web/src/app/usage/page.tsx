"use client";

import React from "react";
import { RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { Badge, EmptyState, Fact, InlineError, PanelCard, Skeleton, StatCard } from "@/components/console-primitives";
import { ConsoleShell } from "@/components/console-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { type AdminOverviewResponse, type AdminUsageResponse, type AdminUserSummary, fetchAdminOverview, fetchAdminUsage, fetchAdminUsers } from "@/lib/admin";
import { formatCompact, formatInt, formatRelative, formatUsd, formatWhen } from "@/lib/format";
import { useGatewayWsUrlState } from "@/lib/gateway";
import { cn } from "@/lib/utils";

export default function UsagePage() {
  const [wsUrl, setWsUrl] = useGatewayWsUrlState();
  const [overview, setOverview] = React.useState<AdminOverviewResponse | null>(null);
  const [users, setUsers] = React.useState<AdminUserSummary[]>([]);
  const [usage, setUsage] = React.useState<AdminUsageResponse | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [selectedUserId, setSelectedUserId] = React.useState<string>("");
  const [hours, setHours] = React.useState("24");
  const [limit, setLimit] = React.useState("100");

  const refresh = React.useCallback(async () => {
    if (!wsUrl.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      const trimmedUserId = selectedUserId.trim();
      const trimmedHours = hours.trim();
      const trimmedLimit = limit.trim();
      if (trimmedUserId) params.set("user_id", trimmedUserId);
      if (trimmedHours) params.set("hours", trimmedHours);
      if (trimmedLimit) params.set("limit", trimmedLimit);

      const [nextOverview, nextUsers, nextUsage] = await Promise.all([
        fetchAdminOverview(wsUrl),
        fetchAdminUsers(wsUrl),
        fetchAdminUsage(wsUrl, params),
      ]);

      setOverview(nextOverview);
      setUsers(nextUsers.users ?? []);
      setUsage(nextUsage);
    } catch (err) {
      const message = (err as Error)?.message || String(err);
      setError(message);
      toast.error(message);
    } finally {
      setLoading(false);
    }
  }, [hours, limit, selectedUserId, wsUrl]);

  React.useEffect(() => {
    if (!wsUrl.trim()) return;
    const run = async () => {
      await Promise.resolve();
      await refresh();
    };
    void run();
  }, [refresh, wsUrl]);

  const selectedUser = React.useMemo(() => {
    const userId = Number(selectedUserId);
    if (!Number.isFinite(userId) || userId <= 0) return null;
    return users.find((item) => item.userId === userId) ?? null;
  }, [selectedUserId, users]);

  const filteredSummary = usage?.summary;
  const showFleetSkeleton = loading && !overview;
  const showUsageSkeleton = loading && !usage;

  return (
    <ConsoleShell
      title="Usage"
      subtitle="Read the gateway-side usage ledger by user, channel, model, and time window without leaving the operator console."
      actions={
        <div className="flex flex-col gap-2 xl:items-end">
          <div className="argus-surface-label">Gateway endpoint</div>
          <div className="flex flex-wrap items-center gap-2">
            <Input
              value={wsUrl}
              onChange={(event) => setWsUrl(event.target.value)}
              className="w-[min(30rem,100%)]"
              placeholder="Gateway wss://.../ws"
              spellCheck={false}
            />
            <Button type="button" variant="secondary" disabled={loading} onClick={() => void refresh()}>
              <RefreshCw className={cn("h-4 w-4", loading ? "animate-spin" : null)} />
              Refresh
            </Button>
          </div>
        </div>
      }
    >
      {error ? <InlineError message={error} /> : null}

      <div className="grid gap-4">
        <PanelCard
          eyebrow="Filter lens"
          title={selectedUser ? `User ${selectedUser.userId}` : "Global usage lens"}
          subtitle="Keep filters attached to the ledger they affect: choose the user, time window, and row limit here."
          action={
            <div className="grid gap-2 md:grid-cols-[minmax(0,1.3fr)_110px_110px]">
              <select
                value={selectedUserId}
                onChange={(event) => {
                  setSelectedUserId(event.target.value);
                  setUsage(null);
                  setError(null);
                  setLoading(true);
                }}
                className="argus-select min-w-[12rem]"
              >
                <option value="">All users</option>
                {users.map((user) => (
                  <option key={user.userId} value={String(user.userId)}>
                    User {user.userId}
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
            <Fact label="Time window" value={hours.trim() ? `Last ${hours.trim()} hours` : "Full history"} />
            <Fact label="Rows loaded" value={formatInt(usage?.events.length)} />
            <Fact label="Last request" value={formatWhen(filteredSummary?.lastAtMs)} />
          </div>
        </PanelCard>

        {showUsageSkeleton ? (
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
            <StatCard
              label="Filtered tokens"
              value={formatCompact(filteredSummary?.totalTokens)}
              tone="primary"
              hint={selectedUser ? `Scoped to user ${selectedUser.userId}` : "Current global filter window"}
            />
            <StatCard label="Filtered requests" value={formatInt(filteredSummary?.requestCount)} hint="Rows that matched the current lens." />
            <StatCard
              label="Input / output"
              value={`${formatCompact(filteredSummary?.inputTokens)} / ${formatCompact(filteredSummary?.outputTokens)}`}
              hint="Input first, output second."
            />
            <StatCard label="Est. cost" value={formatUsd(filteredSummary?.estimatedCostUsd)} hint="Derived from recorded upstream usage." />
          </div>
        )}

        <div className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
          <section className="space-y-4">
            <PanelCard
              eyebrow="Operators"
              title="Top users"
              subtitle={
                overview
                  ? `Version ${overview.version} · sorted by rolling 24h token usage from the same gateway ledger.`
                  : "Sorted by rolling 24h token usage from the same gateway ledger."
              }
            >
              {showFleetSkeleton ? (
                <div className="space-y-3">
                  {Array.from({ length: 6 }).map((_, index) => (
                    <div key={index} className="argus-row-shell rounded-[16px] px-4 py-3">
                      <Skeleton className="h-4 w-24 rounded-full" />
                      <Skeleton className="mt-3 h-3 w-40" />
                      <Skeleton className="mt-3 h-3 w-full" />
                    </div>
                  ))}
                </div>
              ) : (
                <div className="space-y-2">
                  {users.slice(0, 8).map((user) => (
                    <button
                      key={user.userId}
                      type="button"
                      onClick={() => {
                        setSelectedUserId(String(user.userId));
                        setUsage(null);
                        setError(null);
                        setLoading(true);
                      }}
                      className={cn(
                        "argus-row-shell w-full rounded-[16px] px-4 py-3 text-left",
                        String(user.userId) === selectedUserId ? "border-primary/28 bg-primary/10" : "hover:border-border hover:bg-background/36",
                      )}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="font-medium text-foreground">User {user.userId}</div>
                          <div className="mt-1 text-xs leading-5 text-muted-foreground">
                            {user.currentChannel?.name || user.currentChannelId || "gateway"} · {user.currentModel || "model pending"}
                          </div>
                        </div>
                        <span className="rounded-md border border-border/70 px-2 py-1 text-[10px] font-medium uppercase tracking-[0.08em] text-muted-foreground">
                          {formatCompact(user.usage24h.totalTokens)} tok
                        </span>
                      </div>
                      <div className="mt-3 flex flex-wrap gap-2">
                        <Badge tone="default">{user.agentCount} agents</Badge>
                        <Badge tone="default">{user.channelCount} channels</Badge>
                        <Badge tone="default">{formatRelative(user.lastActiveMs)}</Badge>
                      </div>
                    </button>
                  ))}
                  {!users.length ? (
                    <EmptyState title="No users yet" body="Once Telegram or web activity starts flowing, usage will aggregate here." />
                  ) : null}
                </div>
              )}
            </PanelCard>
          </section>

          <section className="space-y-4">
            <PanelCard
              eyebrow="Ledger"
              title="Recent calls"
              subtitle="Newest usage events first, as observed by the gateway proxy."
              contentClassName="space-y-3"
            >
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <Badge tone="default">{selectedUser ? `user ${selectedUser.userId}` : "all users"}</Badge>
                <Badge tone="default">{hours.trim() ? `${hours.trim()}h window` : "full history"}</Badge>
                <Badge tone="default">{formatInt(usage?.events.length)} rows</Badge>
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
                                  : "border-emerald-500/28 bg-emerald-500/10 text-emerald-400",
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
                    <div className="px-4 py-8 text-center text-sm text-muted-foreground">
                      No usage records match the current filter.
                    </div>
                  ) : null}
                </div>
              )}
            </PanelCard>
          </section>
        </div>
      </div>
    </ConsoleShell>
  );
}
