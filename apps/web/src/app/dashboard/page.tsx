"use client";

import * as React from "react";
import { Bot, KeyRound, RefreshCw, Send, Server, WalletCards } from "lucide-react";
import { toast } from "sonner";

import { useAuth } from "@/components/admin-gate";
import { Badge, EmptyState, Fact, InlineError, PanelCard, Skeleton, StatCard } from "@/components/console-primitives";
import { ConsoleShell } from "@/components/console-shell";
import { PanelReveal } from "@/components/transitions";
import { Button } from "@/components/ui/button";
import type { AdminAgentEntry, UsageEventEntry, UsageSummary } from "@/lib/admin";
import { formatCompact, formatInt, formatRelative, formatUsd, formatWhen } from "@/lib/format";
import { useGatewayWsUrlState } from "@/lib/gateway";
import {
  fetchMyAgents,
  fetchMyDeveloperKeys,
  fetchMyTelegramLinkStatus,
  fetchMyUsage,
  createMyTelegramLink,
  type SelfAgentsResponse,
  type SelfDeveloperKeysResponse,
  type SelfTelegramLinkIssueResponse,
  type SelfTelegramLinkStatusResponse,
  type SelfUsageResponse,
} from "@/lib/self";
import { cn } from "@/lib/utils";

function agentProvisioningState(agent: Pick<AdminAgentEntry, "provisioningState">): "pending" | "ready" | "failed" {
  const normalized = String(agent.provisioningState || "").trim().toLowerCase();
  if (normalized === "pending" || normalized === "failed") return normalized;
  return "ready";
}

function agentProvisioningTone(agent: Pick<AdminAgentEntry, "provisioningState">): "warning" | "success" | "default" {
  const state = agentProvisioningState(agent);
  if (state === "pending") return "warning";
  if (state === "ready") return "success";
  return "default";
}

function agentProvisioningLabel(agent: Pick<AdminAgentEntry, "provisioningState">): string {
  const state = agentProvisioningState(agent);
  if (state === "pending") return "provisioning";
  if (state === "failed") return "failed";
  return "ready";
}

function activeKeyCount(keysState: SelfDeveloperKeysResponse | null): number {
  if (!keysState) return 0;
  return keysState.keys.filter((key) => key.active !== false && !key.revokedAtMs).length;
}

function percentage(value: number | null | undefined, total: number | null | undefined): string {
  if (!Number.isFinite(value) || !Number.isFinite(total) || Number(total) <= 0) return "0%";
  return `${Math.round((Number(value) / Number(total)) * 100)}%`;
}

function tokenBreakdown(summary: UsageSummary | null | undefined): Array<{ label: string; value: number }> {
  return [
    { label: "Input", value: summary?.inputTokens ?? 0 },
    { label: "Output", value: summary?.outputTokens ?? 0 },
    { label: "Reasoning", value: summary?.reasoningTokens ?? 0 },
  ];
}

function usageStatus(event: UsageEventEntry): { label: string; className: string } {
  if (event.error) {
    return {
      label: event.status || "error",
      className: "border-destructive/36 bg-destructive/10 text-destructive",
    };
  }
  return {
    label: event.status || "ok",
    className: "border-emerald-500/28 bg-emerald-500/10 text-emerald-400",
  };
}

function telegramBotUrlFromIssue(issue: SelfTelegramLinkIssueResponse): string | null {
  const direct = String(issue.botUrl || "").trim();
  if (direct) return direct;
  return null;
}

function telegramLinkLabel(state: SelfTelegramLinkStatusResponse | null): string {
  if (!state) return "—";
  if (!state.linked) return "Not linked";
  const profile = state.link?.profile;
  if (profile?.username) return `@${profile.username}`;
  if (profile?.displayName) return profile.displayName;
  if (state.link?.telegramUserId) return `TG ${state.link.telegramUserId}`;
  return "Linked";
}

export default function DashboardPage() {
  const { user } = useAuth();
  const [wsUrl] = useGatewayWsUrlState();
  const [agentsState, setAgentsState] = React.useState<SelfAgentsResponse | null>(null);
  const [keysState, setKeysState] = React.useState<SelfDeveloperKeysResponse | null>(null);
  const [telegramLinkState, setTelegramLinkState] = React.useState<SelfTelegramLinkStatusResponse | null>(null);
  const [telegramLinkBusy, setTelegramLinkBusy] = React.useState(false);
  const [usage24h, setUsage24h] = React.useState<SelfUsageResponse | null>(null);
  const [usageTotal, setUsageTotal] = React.useState<SelfUsageResponse | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const refresh = React.useCallback(
    async (opts?: { notify?: boolean }) => {
      if (!wsUrl.trim()) return;
      setLoading(true);
      setError(null);

      const recentParams = new URLSearchParams({ hours: "24", limit: "6" });
      const totalParams = new URLSearchParams({ hours: "0", limit: "6" });
      const [agentsResult, keysResult, telegramLinkResult, recentUsageResult, totalUsageResult] = await Promise.allSettled([
        fetchMyAgents(wsUrl),
        fetchMyDeveloperKeys(wsUrl),
        fetchMyTelegramLinkStatus(wsUrl),
        fetchMyUsage(wsUrl, recentParams),
        fetchMyUsage(wsUrl, totalParams),
      ]);

      const errors: string[] = [];

      if (agentsResult.status === "fulfilled") {
        setAgentsState(agentsResult.value);
      } else {
        errors.push((agentsResult.reason as Error)?.message || String(agentsResult.reason));
      }

      if (keysResult.status === "fulfilled") {
        setKeysState(keysResult.value);
      } else {
        errors.push((keysResult.reason as Error)?.message || String(keysResult.reason));
      }

      if (telegramLinkResult.status === "fulfilled") {
        setTelegramLinkState(telegramLinkResult.value);
      } else {
        errors.push((telegramLinkResult.reason as Error)?.message || String(telegramLinkResult.reason));
      }

      if (recentUsageResult.status === "fulfilled") {
        setUsage24h(recentUsageResult.value);
      } else {
        errors.push((recentUsageResult.reason as Error)?.message || String(recentUsageResult.reason));
      }

      if (totalUsageResult.status === "fulfilled") {
        setUsageTotal(totalUsageResult.value);
      } else {
        errors.push((totalUsageResult.reason as Error)?.message || String(totalUsageResult.reason));
      }

      const nextError = errors.length ? errors.join(" · ") : null;
      setError(nextError);
      if (opts?.notify) {
        if (nextError) toast.error(nextError);
        else toast.success("Refreshed");
      }
      setLoading(false);
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

  const handleTelegramLink = React.useCallback(async () => {
    if (!wsUrl.trim()) return;
    setTelegramLinkBusy(true);
    try {
      const issue = await createMyTelegramLink(wsUrl);
      setTelegramLinkState(issue);
      const url = telegramBotUrlFromIssue(issue);
      if (!url) {
        toast.error("Telegram bot token is not configured or invalid. Configure it in Settings first.");
        return;
      }
      const opened = window.open(url, "_blank");
      if (opened) {
        opened.opener = null;
      } else {
        window.location.assign(url);
      }
    } catch (nextError) {
      toast.error((nextError as Error)?.message || String(nextError));
    } finally {
      setTelegramLinkBusy(false);
    }
  }, [wsUrl]);

  const agents = agentsState?.agents ?? [];
  const agentCount = agentsState?.counts.agents ?? agents.length;
  const sessionCount = agentsState?.counts.sessions ?? 0;
  const keyCount = keysState?.counts.apiKeys ?? activeKeyCount(keysState);
  const activeKeys = activeKeyCount(keysState);
  const readyAgents = agents.filter((agent) => agentProvisioningState(agent) === "ready").length;
  const currentAgent = agentsState?.currentAgent ?? agents.find((agent) => agent.agentId === agentsState?.currentAgentId) ?? null;
  const totalSummary = usageTotal?.summary;
  const recentSummary = usage24h?.summary;
  const recentEvents = usageTotal?.events?.length ? usageTotal.events : usage24h?.events ?? [];
  const quota = agentsState?.limits.monthlyTokenQuota ?? keysState?.limits.monthlyTokenQuota ?? null;
  const showSkeleton = loading && !agentsState && !keysState && !usage24h && !usageTotal;

  return (
    <ConsoleShell
      title="Dashboard"
      actions={
        <div className="flex flex-wrap justify-start gap-2 xl:justify-end">
          <Button type="button" variant="secondary" onClick={() => void handleTelegramLink()} disabled={telegramLinkBusy || !wsUrl.trim()}>
            <Send className={cn("h-4 w-4", telegramLinkBusy ? "animate-pulse" : null)} />
            {telegramLinkState?.linked ? "Open Telegram" : "Connect Telegram"}
          </Button>
          <Button type="button" variant="secondary" onClick={() => void refresh({ notify: true })} disabled={loading || !wsUrl.trim()}>
            <RefreshCw className={cn("h-4 w-4", loading ? "animate-spin" : null)} />
            Refresh
          </Button>
        </div>
      }
    >
      <div className="grid gap-4">
        {error ? <InlineError message={error} /> : null}

        {showSkeleton ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            {Array.from({ length: 4 }).map((_, index) => (
              <div key={index} className="argus-metric-card rounded-[20px] p-4">
                <Skeleton className="h-3 w-24 rounded-full" />
                <Skeleton className="mt-3 h-8 w-28" />
              </div>
            ))}
          </div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <StatCard label="Agents" value={formatInt(agentCount)} tone="primary" />
            <StatCard label="24h tokens" value={formatCompact(recentSummary?.totalTokens)} />
            <StatCard label="Total tokens" value={formatCompact(totalSummary?.totalTokens)} />
            <StatCard label="Est. spend" value={formatUsd(totalSummary?.estimatedCostUsd)} />
          </div>
        )}

        <div className="grid gap-4 xl:grid-cols-[minmax(0,0.95fr)_minmax(0,1.05fr)]">
          <section className="space-y-4">
            <PanelCard title="Account">
              <div className="grid gap-3 sm:grid-cols-2">
                <Fact label="User" value={user?.email || `User ${user?.userId ?? "—"}`} />
                <Fact label="Role" value={user?.isAdmin ? "Admin" : "User"} />
                <Fact label="Telegram" value={telegramLinkLabel(telegramLinkState)} />
                <Fact label="Current agent" value={currentAgent?.shortName || currentAgent?.agentId || "—"} mono />
                <Fact label="Current model" value={currentAgent?.model || "—"} mono />
                <Fact label="Sessions" value={formatInt(sessionCount)} />
                <Fact label="API keys" value={`${formatInt(activeKeys)} active / ${formatInt(keyCount)} total`} />
              </div>
            </PanelCard>

            <PanelCard title="Capacity">
              <div className="grid gap-3 sm:grid-cols-2">
                <DashboardFact icon={<Bot className="h-4 w-4" />} label="Ready agents" value={`${formatInt(readyAgents)} / ${formatInt(agentCount)}`} />
                <DashboardFact icon={<Server className="h-4 w-4" />} label="Managed sessions" value={`${formatInt(sessionCount)} / ${formatLimit(agentsState?.limits.maxManagedSessions)}`} />
                <DashboardFact icon={<KeyRound className="h-4 w-4" />} label="Developer keys" value={`${formatInt(keyCount)} / ${formatLimit(keysState?.limits.maxApiKeys)}`} />
                <DashboardFact icon={<WalletCards className="h-4 w-4" />} label="Monthly quota" value={formatLimit(quota)} />
              </div>
            </PanelCard>
          </section>

          <section className="space-y-4">
            <PanelCard title="Token usage">
              <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_220px]">
                <div className="space-y-3">
                  {tokenBreakdown(totalSummary).map((item) => (
                    <div key={item.label} className="border-l border-border/58 px-3 py-2">
                      <div className="flex items-center justify-between gap-3">
                        <span className="argus-surface-label">{item.label}</span>
                        <span className="font-mono text-[12.5px] text-foreground">{formatInt(item.value)}</span>
                      </div>
                      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-background/40">
                        <div
                          className="h-full rounded-full bg-primary/80"
                          style={{ width: percentage(item.value, totalSummary?.totalTokens) }}
                        />
                      </div>
                    </div>
                  ))}
                </div>

                <div className="grid gap-3">
                  <Fact label="Requests" value={formatInt(totalSummary?.requestCount)} />
                  <Fact label="Errors" value={formatInt(totalSummary?.errorCount)} />
                  <Fact label="Last call" value={formatRelative(totalSummary?.lastAtMs)} />
                </div>
              </div>
            </PanelCard>

            <PanelCard title="Agents">
              {agents.length ? (
                <div className="grid gap-2">
                  {agents.slice(0, 6).map((agent) => (
                    <div key={agent.agentId} className="border-l border-border/58 px-3 py-2.5">
                      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                        <div className="min-w-0">
                          <div className="truncate font-medium text-foreground">{agent.shortName || agent.agentId}</div>
                          <div className="mt-1 truncate font-mono text-[12px] text-muted-foreground">{agent.agentId}</div>
                        </div>
                        <div className="flex shrink-0 flex-wrap gap-2">
                          {agent.agentId === agentsState?.currentAgentId ? <Badge tone="primary">current</Badge> : null}
                          <Badge tone={agentProvisioningTone(agent)}>{agentProvisioningLabel(agent)}</Badge>
                        </div>
                      </div>
                      <div className="mt-3 flex flex-wrap gap-2">
                        {agent.model ? <Badge tone="default">{agent.model}</Badge> : null}
                        {agent.sessionId ? <Badge tone="default">{agent.sessionId}</Badge> : null}
                        {agent.lastReadyAtMs ? <Badge tone="default">{formatRelative(agent.lastReadyAtMs)}</Badge> : null}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <EmptyState title="No agents" />
              )}
            </PanelCard>
          </section>
        </div>

        <PanelReveal open travel="12px">
          <PanelCard title="Recent calls">
            {recentEvents.length ? (
              <div className="argus-table-shell">
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
                    {recentEvents.map((event) => {
                      const status = usageStatus(event);
                      return (
                        <tr
                          key={[event.createdAtMs, event.responseId || "", event.sessionId || "", event.agentId || ""].join(":")}
                          className="border-t border-border/60"
                        >
                          <td className="px-4 py-3 text-muted-foreground">{formatWhen(event.createdAtMs)}</td>
                          <td className="px-4 py-3 font-mono text-[12.5px]">{event.agentId || "—"}</td>
                          <td className="px-4 py-3">{event.channelName || event.channelId || "—"}</td>
                          <td className="px-4 py-3 font-mono text-[12.5px]">{event.model || event.requestedModel || "—"}</td>
                          <td className="px-4 py-3 text-right font-medium">{formatInt(event.totalTokens)}</td>
                          <td className="px-4 py-3">
                            <span className={cn("inline-flex items-center rounded-md border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.08em]", status.className)}>
                              {status.label}
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState title="No usage" />
            )}
          </PanelCard>
        </PanelReveal>
      </div>
    </ConsoleShell>
  );
}

function formatLimit(value: number | null | undefined): string {
  if (!Number.isFinite(value) || Number(value) <= 0) return "—";
  return formatInt(value);
}

function DashboardFact({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
}) {
  return (
    <div className="flex min-w-0 items-start gap-3 border-l border-border/58 px-3 py-2">
      <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-border/70 bg-background/24 text-primary">
        {icon}
      </span>
      <span className="min-w-0">
        <span className="argus-surface-label block">{label}</span>
        <span className="mt-1 block break-words text-sm font-medium leading-6 text-foreground">{value}</span>
      </span>
    </div>
  );
}
