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

  const refresh = React.useCallback(async () => {
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
      toast.error(message);
    } finally {
      setLoading(false);
    }
  }, [wsUrl]);

  React.useEffect(() => {
    if (!wsUrl.trim()) return;
    const run = async () => {
      await Promise.resolve();
      await refresh();
    };
    void run();
  }, [refresh, wsUrl]);

  return (
    <ConsoleShell
      title="Settings"
      subtitle="Connection posture, backend contracts, and deploy assumptions for the gateway this browser will reuse across the entire operator console."
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
              hint={health?.ok ? "Public health probe is returning ok." : "The browser has not observed a healthy response yet."}
            />
            <StatCard label="Gateway version" value={overview?.version || "unknown"} hint="Build reported by /admin/overview." />
            <StatCard label="Tracked users" value={formatInt(overview?.totals.userCount)} hint="Users currently visible to the operator console." />
            <StatCard label="Live sessions" value={formatInt(overview?.totals.sessionCount)} hint="Current runtime sessions on the connected gateway." />
          </div>
        )}

        <div className="grid gap-4 xl:grid-cols-[1.1fr_0.9fr]">
          <section className="space-y-4">
            <PanelCard
              eyebrow="Connection"
              title="Gateway connection contract"
              subtitle="The browser stores the shared gateway WebSocket URL locally and reuses it across Workbench, Users, Usage, and Nodes."
              className="argus-data-grid"
            >
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
                  <Fact label="Saved WebSocket URL" value={wsUrl || "not configured"} mono />
                  <Fact label="Gateway version" value={overview?.version || "unknown"} />
                  <Fact label="Health probe" value={health?.ok ? "GET /healthz returned ok" : "No healthy response yet"} />
                  <Fact label="Auth model" value="Admin bearer token is stored in the browser and attached automatically" />
                  <Fact label="Ledger source" value="/openai/v1/responses on the gateway proxy" mono />
                  <Fact label="Expected host shape" value="Same public host for /ws, /admin/*, /sessions, and /openai/*" />
                </div>
              )}
            </PanelCard>

            <PanelCard
              eyebrow="Runbook"
              title="Operator assumptions"
              subtitle="This UI is intentionally opinionated about the shape of the Argus control plane."
            >
              <div className="grid gap-3">
                <div className="argus-row-shell rounded-[16px] px-4 py-3">
                  <div className="argus-surface-label">Shared host</div>
                  <p className="mt-2 text-sm leading-6 text-muted-foreground">
                    In deployed mode, nginx should proxy <code>/admin/*</code>, <code>/sessions</code>, <code>/openai/*</code>, and <code>/ws</code> on the same public origin.
                  </p>
                </div>
                <div className="argus-row-shell rounded-[16px] px-4 py-3">
                  <div className="argus-surface-label">Usage accounting</div>
                  <p className="mt-2 text-sm leading-6 text-muted-foreground">
                    Token and request accounting is gateway-side only. Usage appears once Requests API traffic flows through the Argus proxy.
                  </p>
                </div>
                <div className="argus-row-shell rounded-[16px] px-4 py-3">
                  <div className="argus-surface-label">Trust model</div>
                  <p className="mt-2 text-sm leading-6 text-muted-foreground">
                    The frontend assumes a trusted operator with a bearer token. It is not designed for end-user login or per-user browser sessions.
                  </p>
                </div>
              </div>
            </PanelCard>
          </section>

          <section className="space-y-4">
            <PanelCard eyebrow="Contracts" title="Backend checks" subtitle="Quick verification of the APIs this console expects to exist.">
              <div className="space-y-3">
                <ContractRow label="Gateway health" ok={Boolean(health?.ok)} detail="GET /healthz" />
                <ContractRow label="Admin overview" ok={Boolean(overview?.ok)} detail="GET /admin/overview" />
                <ContractRow
                  label="Usage ledger"
                  ok={Boolean(overview?.usageTotal)}
                  detail="GET /admin/usage should return summary plus event rows"
                />
              </div>
            </PanelCard>

            <PanelCard
              eyebrow="Deploy snapshot"
              title="Current deployment"
              subtitle="The operator console expects the project to expose web, gateway, runtime, and Telegram bot as one cohesive deploy."
            >
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
                <EmptyState title="No overview yet" body="Configure the gateway URL above, then refresh to inspect the current backend." />
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
    <div className="argus-row-shell flex items-start gap-3 rounded-[16px] px-4 py-3">
      <div
        className={cn(
          "mt-0.5 flex h-8 w-8 items-center justify-center rounded-lg border",
          ok
            ? "border-emerald-500/28 bg-emerald-500/10 text-emerald-400"
            : "border-border/70 bg-background/40 text-muted-foreground"
        )}
      >
        {ok ? <CheckCircle2 className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
      </div>
      <div className="min-w-0">
        <div className="font-medium text-foreground">{label}</div>
        <div className="mt-1 text-sm text-muted-foreground">{detail}</div>
      </div>
    </div>
  );
}
