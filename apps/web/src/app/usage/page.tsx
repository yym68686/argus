"use client";

import React from "react";
import { RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { Badge, EmptyState, Fact, InlineError, PanelCard, StatCard } from "@/components/console-primitives";
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

  return (
    <ConsoleShell
      title="Usage"
      subtitle="Track Requests API consumption by user, agent, channel, and time window. This view reads the gateway-side usage ledger."
      actions={
        <div className="flex flex-col gap-2 md:items-end">
          <div className="flex flex-wrap items-center gap-2">
            <Input
              value={wsUrl}
              onChange={(event) => setWsUrl(event.target.value)}
              className="w-[320px]"
              placeholder="Gateway wss://.../ws"
              spellCheck={false}
            />
            <Button type="button" variant="secondary" disabled={loading} onClick={() => void refresh()}>
              <RefreshCw className={cn("mr-2 h-4 w-4", loading && "animate-spin")} />
              Refresh
            </Button>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <select
              value={selectedUserId}
              onChange={(event) => setSelectedUserId(event.target.value)}
              className={cn(
                "h-10 min-w-[180px] rounded-xl border border-border/60 bg-background/60 px-3 text-sm text-foreground outline-none",
                "focus:border-primary/40 focus:ring-4 focus:ring-ring/20",
              )}
            >
              <option value="">All users</option>
              {users.map((user) => (
                <option key={user.userId} value={String(user.userId)}>
                  User {user.userId}
                </option>
              ))}
            </select>
            <Input value={hours} onChange={(event) => setHours(event.target.value)} className="w-[90px]" placeholder="hours" />
            <Input value={limit} onChange={(event) => setLimit(event.target.value)} className="w-[90px]" placeholder="limit" />
          </div>
        </div>
      }
    >
      {error ? <InlineError message={error} /> : null}

      <div className="grid gap-6 xl:grid-cols-[340px_1fr]">
        <section className="space-y-6">
          <PanelCard
            eyebrow="Fleet snapshot"
            title="Fleet"
            subtitle={overview ? `Version ${overview.version}` : "Gateway-wide activity across all tracked users."}
          >
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-1">
              <StatCard
                label="24h tokens"
                value={formatCompact(overview?.usage24h.totalTokens)}
                tone="primary"
                hint="Total traffic recorded by the gateway ledger over the last rolling 24 hours."
              />
              <StatCard label="24h requests" value={formatInt(overview?.usage24h.requestCount)} hint="Successful and failed Responses calls." />
              <StatCard label="Tracked users" value={formatInt(overview?.totals.userCount)} hint="Users with observable traffic or managed state." />
              <StatCard label="Live sessions" value={formatInt(overview?.totals.sessionCount)} hint="Currently listed runtime sessions." />
            </div>
          </PanelCard>

          <PanelCard eyebrow="Operators" title="Top users" subtitle="Sorted by 24h token usage from the same ledger.">
            <div className="space-y-2">
              {users.slice(0, 8).map((user) => (
                <button
                  key={user.userId}
                  type="button"
                  onClick={() => setSelectedUserId(String(user.userId))}
                    className={cn(
                    "w-full rounded-[22px] border px-4 py-4 text-left transition-colors",
                    String(user.userId) === selectedUserId
                      ? "border-primary/25 bg-primary/10"
                      : "border-border/60 bg-background/30 hover:border-border hover:bg-background/55"
                  )}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="font-medium text-foreground">User {user.userId}</div>
                      <div className="mt-1 text-xs text-muted-foreground">
                        {user.currentChannel?.name || user.currentChannelId || "gateway"} · {user.currentModel || "model pending"}
                      </div>
                    </div>
                    <span className="rounded-full border border-border/60 px-2 py-0.5 text-[11px] text-muted-foreground">
                      {formatCompact(user.usage24h.totalTokens)} tok
                    </span>
                  </div>
                  <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-muted-foreground">
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
          </PanelCard>
        </section>

        <section className="space-y-6">
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <StatCard
              label="Filtered tokens"
              value={formatCompact(filteredSummary?.totalTokens)}
              tone="primary"
              hint={selectedUser ? `Scoped to user ${selectedUser.userId}` : "Current global filter window"}
              className="md:col-span-2 xl:col-span-1"
            />
            <StatCard label="Filtered requests" value={formatInt(filteredSummary?.requestCount)} hint="Rows that matched the filter window." />
            <StatCard
              label="Input / output"
              value={`${formatCompact(filteredSummary?.inputTokens)} / ${formatCompact(filteredSummary?.outputTokens)}`}
              hint="Input first, output second."
            />
            <StatCard label="Est. cost" value={formatUsd(filteredSummary?.estimatedCostUsd)} hint="Derived from recorded upstream usage." />
          </div>

          <PanelCard
            eyebrow="Current lens"
            title={selectedUser ? `User ${selectedUser.userId}` : "Current filter"}
            subtitle={selectedUser ? "The panels below are scoped to the selected user and time range." : "Use the filter bar to narrow the usage ledger."}
            className="argus-data-grid"
          >
            <div className="grid gap-4 lg:grid-cols-2">
              <Fact label="Time window" value={hours.trim() ? `Last ${hours.trim()} hours` : "Full history"} />
              <Fact label="Rows loaded" value={formatInt(usage?.events.length)} />
              <Fact label="Last request" value={formatWhen(filteredSummary?.lastAtMs)} />
              <Fact label="Last active" value={selectedUser ? formatRelative(selectedUser.lastActiveMs) : formatRelative(filteredSummary?.lastAtMs)} />
            </div>
          </PanelCard>

          <PanelCard
            eyebrow="Ledger"
            title="Recent calls"
            subtitle="Newest usage events first, as observed by the gateway proxy."
            contentClassName="space-y-4"
          >
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <Badge tone="default">{selectedUser ? `user ${selectedUser.userId}` : "all users"}</Badge>
              <Badge tone="default">{hours.trim() ? `${hours.trim()}h window` : "full history"}</Badge>
              <Badge tone="default">{formatInt(usage?.events.length)} rows</Badge>
            </div>

            <div className="argus-table-shell rounded-[24px]">
              <table className="w-full border-collapse text-sm">
                <thead className="argus-table-head text-left text-xs uppercase tracking-[0.22em] text-muted-foreground">
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
                      <td className="px-4 py-3 font-mono text-[13px]">{event.agentId || "—"}</td>
                      <td className="px-4 py-3">{event.channelName || event.channelId || "—"}</td>
                      <td className="px-4 py-3 font-mono text-[13px]">{event.model || event.requestedModel || "—"}</td>
                      <td className="px-4 py-3 text-right font-medium">{formatInt(event.totalTokens)}</td>
                      <td className="px-4 py-3">
                        <span
                          className={cn(
                            "rounded-full border px-2 py-1 text-[11px]",
                            event.error
                              ? "border-destructive/40 bg-destructive/10 text-destructive"
                              : "border-emerald-500/30 bg-emerald-500/10 text-emerald-400"
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
                <div className="px-4 py-8 text-center text-sm text-muted-foreground">No usage records match the current filter.</div>
              ) : null}
            </div>
          </PanelCard>
        </section>
      </div>
    </ConsoleShell>
  );
}
