"use client";

import React from "react";
import { CheckCircle2, RefreshCw, XCircle } from "lucide-react";
import { toast } from "sonner";

import { EmptyState, Fact, InlineError, PanelCard, Skeleton, StatCard } from "@/components/console-primitives";
import { ConsoleShell } from "@/components/console-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { type AdminOverviewResponse, fetchAdminOverview } from "@/lib/admin";
import { formatInt } from "@/lib/format";
import { gatewayFetchJson, useGatewayWsUrlState } from "@/lib/gateway";
import { cn } from "@/lib/utils";

interface GatewayHealth {
  ok?: boolean;
}

export default function SettingsPage() {
  const [wsUrl, setWsUrl] = useGatewayWsUrlState();
  const [overview, setOverview] = React.useState<AdminOverviewResponse | null>(null);
  const [health, setHealth] = React.useState<GatewayHealth | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const showSkeleton = loading && !overview && !health;

  const refresh = React.useCallback(async (opts?: { notify?: boolean }) => {
    if (!wsUrl.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const [nextOverview, nextHealth] = await Promise.all([
        fetchAdminOverview(wsUrl),
        gatewayFetchJson<GatewayHealth>(wsUrl, "/healthz"),
      ]);
      setOverview(nextOverview);
      setHealth(nextHealth);
    } catch (err) {
      const message = (err as Error)?.message || String(err);
      setError(message);
      if (opts?.notify) {
        toast.error(message);
      }
    } finally {
      setLoading(false);
    }
  }, [wsUrl]);

  React.useEffect(() => {
    if (!wsUrl.trim()) return;
    const run = async () => {
      await Promise.resolve();
      await refresh({ notify: false });
    };
    void run();
  }, [refresh, wsUrl]);

  return (
    <ConsoleShell
      title="Settings"
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
            <StatCard
              label="Gateway health"
              value={health?.ok ? "Healthy" : "Unknown"}
              tone={health?.ok ? "primary" : "default"}
            />
            <StatCard label="Gateway version" value={overview?.version || "unknown"} />
            <StatCard label="Tracked users" value={formatInt(overview?.totals.userCount)} />
            <StatCard label="Live sessions" value={formatInt(overview?.totals.sessionCount)} />
          </div>
        )}

        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
          <section className="space-y-4">
            <PanelCard title="Connection" className="argus-data-grid">
              {showSkeleton ? (
                <div className="grid gap-3 lg:grid-cols-2">
                  {Array.from({ length: 6 }).map((_, index) => (
                    <div
                      key={index}
                      className="rounded-[16px] border border-border/70 bg-background/24 px-3.5 py-3"
                    >
                      <Skeleton className="h-3 w-24 rounded-full" />
                      <Skeleton className="mt-3 h-5 w-full" />
                    </div>
                  ))}
                </div>
              ) : (
                <div className="grid gap-3 lg:grid-cols-2">
                  <Fact label="WebSocket" value={wsUrl || "—"} mono />
                  <Fact label="Version" value={overview?.version || "unknown"} />
                  <Fact label="Health" value={health?.ok ? "ok" : "unknown"} />
                  <Fact label="Auth" value="token" />
                  <Fact label="Ledger" value="/openai/v1/responses" mono />
                  <Fact label="Origin" value="same host" />
                </div>
              )}
            </PanelCard>
          </section>

          <section className="space-y-4">
            <PanelCard title="Checks">
              <div className="space-y-3">
                <ContractRow label="Gateway health" ok={Boolean(health?.ok)} detail="GET /healthz" />
                <ContractRow label="Admin overview" ok={Boolean(overview?.ok)} detail="GET /admin/overview" />
                <ContractRow
                  label="Usage ledger"
                  ok={Boolean(overview?.usageTotal)}
                  detail="GET /admin/usage"
                />
              </div>
            </PanelCard>

            <PanelCard title="Deploy">
              {showSkeleton ? (
                <div className="grid gap-3">
                  {Array.from({ length: 4 }).map((_, index) => (
                    <div
                      key={index}
                      className="rounded-[16px] border border-border/70 bg-background/24 px-3.5 py-3"
                    >
                      <Skeleton className="h-3 w-24 rounded-full" />
                      <Skeleton className="mt-3 h-5 w-32" />
                    </div>
                  ))}
                </div>
              ) : overview ? (
                <div className="grid gap-3">
                  <Fact label="Tracked agents" value={formatInt(overview.totals.agentCount)} />
                  <Fact label="Channels" value={formatInt(overview.totals.channelCount)} />
                  <Fact label="24h requests" value={formatInt(overview.usage24h.requestCount)} />
                  <Fact label="24h tokens" value={formatInt(overview.usage24h.totalTokens)} />
                </div>
              ) : (
                <EmptyState title="No overview" />
              )}
            </PanelCard>
          </section>
        </div>
      </div>
    </ConsoleShell>
  );
}

function ContractRow({ label, ok, detail }: { label: string; ok: boolean; detail: string }) {
  return (
    <div className="argus-row-shell flex items-center justify-between gap-3 rounded-[16px] px-4 py-3">
      <div className="flex min-w-0 items-center gap-3">
        <div
          className={cn(
            "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border",
            ok
              ? "border-emerald-500/28 bg-emerald-500/10 text-emerald-400"
              : "border-border/70 bg-background/40 text-muted-foreground"
          )}
        >
          {ok ? <CheckCircle2 className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
        </div>
        <div className="font-medium text-foreground">{label}</div>
      </div>
      <code className="text-xs text-muted-foreground">{detail}</code>
    </div>
  );
}
