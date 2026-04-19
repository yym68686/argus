"use client";

import React from "react";
import { CheckCircle2, RefreshCw, XCircle } from "lucide-react";
import { toast } from "sonner";

import { EmptyState, Fact, InlineError, PanelCard, StatCard } from "@/components/console-primitives";
import { ConsoleShell } from "@/components/console-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { type AdminOverviewResponse, fetchAdminOverview } from "@/lib/admin";
import { formatInt } from "@/lib/format";
import { gatewayFetchJson, loadGatewayWsUrl, storeGatewayWsUrl } from "@/lib/gateway";
import { cn } from "@/lib/utils";

interface GatewayHealth {
  ok?: boolean;
}

export default function SettingsPage() {
  const [wsUrl, setWsUrl] = React.useState(() => (typeof window === "undefined" ? "" : loadGatewayWsUrl()));
  const [overview, setOverview] = React.useState<AdminOverviewResponse | null>(null);
  const [health, setHealth] = React.useState<GatewayHealth | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!wsUrl.trim()) return;
    storeGatewayWsUrl(wsUrl);
  }, [wsUrl]);

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
      subtitle="Operator-level connection state for the gateway, including the shared access URL this browser will reuse across Workbench, Users, and Usage."
      actions={
        <div className="flex flex-col gap-2 md:items-end">
          <div className="flex flex-wrap items-center gap-2">
            <Input
              value={wsUrl}
              onChange={(event) => setWsUrl(event.target.value)}
              className="w-[320px]"
              placeholder="Gateway ws://.../ws?token=..."
              spellCheck={false}
            />
            <Button type="button" variant="secondary" disabled={loading} onClick={() => void refresh()}>
              <RefreshCw className={cn("mr-2 h-4 w-4", loading && "animate-spin")} />
              Refresh
            </Button>
          </div>
        </div>
      }
    >
      {error ? <InlineError message={error} /> : null}

      <div className="grid gap-6 xl:grid-cols-[1.15fr_0.85fr]">
        <section className="space-y-6">
          <div className="grid gap-4 md:grid-cols-3">
            <StatCard label="Gateway health" value={health?.ok ? "Healthy" : "Unknown"} tone={health?.ok ? "primary" : "default"} />
            <StatCard label="Tracked users" value={formatInt(overview?.totals.userCount)} />
            <StatCard label="Live sessions" value={formatInt(overview?.totals.sessionCount)} />
          </div>

          <PanelCard title="Connection" subtitle="This browser stores the shared gateway WebSocket URL in localStorage and reuses it across the operator console.">
            <div className="grid gap-4 lg:grid-cols-2">
              <Fact label="Saved WebSocket URL" value={wsUrl || "not configured"} />
              <Fact label="Gateway version" value={overview?.version || "unknown"} />
              <Fact label="Auth model" value="Bearer token embedded in the gateway WebSocket URL" />
              <Fact label="Health probe" value={health?.ok ? "GET /healthz returned ok" : "No healthy response yet"} />
            </div>
          </PanelCard>

          <PanelCard title="Operator notes" subtitle="This page documents what the refactored console assumes about the Argus control plane.">
            <div className="space-y-3 text-sm leading-relaxed text-muted-foreground">
              <p>
                The new Users and Usage tabs call the gateway&apos;s admin APIs on the same public host. In deployed mode,
                nginx must proxy <code>/admin/*</code>, <code>/sessions</code>, and <code>/openai/*</code> to the gateway.
              </p>
              <p>
                Token accounting is gateway-side only. The ledger is sourced from <code>/openai/v1/responses</code>, so
                usage appears when Requests API traffic flows through the Argus proxy.
              </p>
              <p>
                The console is still operator-facing. It assumes a trusted bearer token, not end-user login or per-user web
                sessions.
              </p>
            </div>
          </PanelCard>
        </section>

        <section className="space-y-6">
          <PanelCard title="Backend contracts" subtitle="Quick check of the APIs this console expects.">
            <div className="space-y-3">
              <ContractRow label="Gateway health" ok={Boolean(health?.ok)} detail="GET /healthz" />
              <ContractRow label="Admin overview" ok={Boolean(overview?.ok)} detail="GET /admin/overview" />
              <ContractRow
                label="Usage ledger"
                ok={Boolean(overview?.usageTotal)}
                detail="GET /admin/usage is expected to return summary + events"
              />
            </div>
          </PanelCard>

          <PanelCard title="Current state" subtitle="The current deploy should expose web, gateway, runtime, and Telegram bot behind one project.">
            {overview ? (
              <div className="grid gap-4">
                <Fact label="Tracked agents" value={formatInt(overview.totals.agentCount)} />
                <Fact label="Channels" value={formatInt(overview.totals.channelCount)} />
                <Fact label="Total 24h requests" value={formatInt(overview.usage24h.requestCount)} />
              </div>
            ) : (
              <EmptyState title="No overview yet" body="Configure the gateway URL above, then refresh to inspect the current backend." />
            )}
          </PanelCard>
        </section>
      </div>
    </ConsoleShell>
  );
}

function ContractRow({ label, ok, detail }: { label: string; ok: boolean; detail: string }) {
  return (
    <div className="flex items-start gap-3 rounded-2xl border border-border/60 bg-background/40 px-4 py-3">
      <div
        className={cn(
          "mt-0.5 flex h-8 w-8 items-center justify-center rounded-full border",
          ok ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-400" : "border-border/60 bg-background/60 text-muted-foreground"
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
