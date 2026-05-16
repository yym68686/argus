"use client";

import React from "react";
import { Bot, CheckCircle2, RefreshCw, Save, Trash2, XCircle } from "lucide-react";
import { toast } from "sonner";

import { EmptyState, Fact, InlineError, PanelCard, Skeleton, StatCard } from "@/components/console-primitives";
import { ConsoleShell } from "@/components/console-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  type AdminOverviewResponse,
  type GatewayApiAccessSettingsResponse,
  type TelegramBotTokenSettingsResponse,
  fetchAdminOverview,
  fetchGatewayApiAccessSettings,
  fetchTelegramBotTokenSettings,
  updateGatewayApiAccessSettings,
  updateTelegramBotTokenSettings,
} from "@/lib/admin";
import { formatInt } from "@/lib/format";
import { gatewayFetchJson, useGatewayWsUrlState } from "@/lib/gateway";
import { cn } from "@/lib/utils";

interface GatewayHealth {
  ok?: boolean;
}

export default function SettingsPage() {
  const [wsUrl, setWsUrl] = useGatewayWsUrlState();
  const [overview, setOverview] = React.useState<AdminOverviewResponse | null>(null);
  const [gatewayAccess, setGatewayAccess] = React.useState<GatewayApiAccessSettingsResponse | null>(null);
  const [telegramBotToken, setTelegramBotToken] = React.useState<TelegramBotTokenSettingsResponse | null>(null);
  const [telegramTokenDraft, setTelegramTokenDraft] = React.useState("");
  const [health, setHealth] = React.useState<GatewayHealth | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [gatewayAccessBusy, setGatewayAccessBusy] = React.useState(false);
  const [telegramTokenBusy, setTelegramTokenBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const showSkeleton = loading && !overview && !health && !gatewayAccess && !telegramBotToken;

  const refresh = React.useCallback(async (opts?: { notify?: boolean }) => {
    if (!wsUrl.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const [nextOverview, nextHealth, nextGatewayAccess, nextTelegramBotToken] = await Promise.all([
        fetchAdminOverview(wsUrl),
        gatewayFetchJson<GatewayHealth>(wsUrl, "/healthz"),
        fetchGatewayApiAccessSettings(wsUrl),
        fetchTelegramBotTokenSettings(wsUrl),
      ]);
      setOverview(nextOverview);
      setHealth(nextHealth);
      setGatewayAccess(nextGatewayAccess);
      setTelegramBotToken(nextTelegramBotToken);
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

  const toggleGatewayAccessDefault = React.useCallback(async () => {
    if (!wsUrl.trim()) return;
    if (!gatewayAccess) {
      toast.error("Gateway access state unavailable");
      return;
    }
    const nextEnabled = !gatewayAccess.gatewayOpenaiDefaultEnabled;
    setGatewayAccessBusy(true);
    try {
      const next = await updateGatewayApiAccessSettings(wsUrl, nextEnabled);
      setGatewayAccess(next);
      toast.success(nextEnabled ? "Gateway API default enabled" : "Gateway API default disabled");
    } catch (err) {
      toast.error((err as Error)?.message || String(err));
    } finally {
      setGatewayAccessBusy(false);
    }
  }, [gatewayAccess, wsUrl]);

  const saveTelegramToken = React.useCallback(async () => {
    if (!wsUrl.trim()) return;
    const nextToken = telegramTokenDraft.trim();
    if (!nextToken) {
      toast.error("Enter a Telegram bot token");
      return;
    }
    setTelegramTokenBusy(true);
    try {
      const next = await updateTelegramBotTokenSettings(wsUrl, nextToken);
      setTelegramBotToken(next);
      setTelegramTokenDraft("");
      toast.success("Telegram bot token saved");
    } catch (err) {
      toast.error((err as Error)?.message || String(err));
    } finally {
      setTelegramTokenBusy(false);
    }
  }, [telegramTokenDraft, wsUrl]);

  const clearTelegramToken = React.useCallback(async () => {
    if (!wsUrl.trim()) return;
    setTelegramTokenBusy(true);
    try {
      const next = await updateTelegramBotTokenSettings(wsUrl, null);
      setTelegramBotToken(next);
      setTelegramTokenDraft("");
      toast.success("Telegram bot token override cleared");
    } catch (err) {
      toast.error((err as Error)?.message || String(err));
    } finally {
      setTelegramTokenBusy(false);
    }
  }, [wsUrl]);

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
            <PanelCard
              title="Gateway API"
              action={
                <Button
                  type="button"
                  variant={gatewayAccess?.gatewayOpenaiDefaultEnabled ? "destructive" : "secondary"}
                  disabled={gatewayAccessBusy || !gatewayAccess}
                  onClick={() => void toggleGatewayAccessDefault()}
                >
                  {gatewayAccessBusy
                    ? "Saving…"
                    : gatewayAccess?.gatewayOpenaiDefaultEnabled
                      ? "Disable default"
                      : "Enable default"}
                </Button>
              }
            >
              {showSkeleton ? (
                <div className="grid gap-3 lg:grid-cols-2">
                  {Array.from({ length: 2 }).map((_, index) => (
                    <div
                      key={index}
                      className="min-w-0 border-l border-border/58 py-1.5 pl-3"
                    >
                      <Skeleton className="h-3 w-20 rounded-full" />
                      <Skeleton className="mt-3 h-5 w-24" />
                    </div>
                  ))}
                </div>
              ) : (
                <div className="grid gap-3 lg:grid-cols-2">
                  <Fact
                    label="Telegram default"
                    value={
                      gatewayAccess
                        ? gatewayAccess.gatewayOpenaiDefaultEnabled
                          ? "on"
                          : "off"
                        : "unknown"
                    }
                  />
                  <Fact
                    label="Overrides"
                    value={
                      gatewayAccess
                        ? `${formatInt(gatewayAccess.allowOverrideCount)} allow · ${formatInt(gatewayAccess.denyOverrideCount)} deny`
                        : "unknown"
                    }
                  />
                </div>
              )}
            </PanelCard>

            <PanelCard
              title="Telegram Bot"
              action={
                <div className="inline-flex min-h-9 items-center gap-2 rounded-lg border border-border/70 bg-background/28 px-3 text-xs font-medium text-muted-foreground">
                  <Bot className="h-4 w-4" />
                  {telegramBotToken?.source || "unknown"}
                </div>
              }
            >
              {showSkeleton ? (
                <div className="grid gap-3 lg:grid-cols-3">
                  {Array.from({ length: 3 }).map((_, index) => (
                    <div
                      key={index}
                      className="min-w-0 border-l border-border/58 py-1.5 pl-3"
                    >
                      <Skeleton className="h-3 w-24 rounded-full" />
                      <Skeleton className="mt-3 h-5 w-28" />
                    </div>
                  ))}
                </div>
              ) : (
                <div className="grid gap-4">
                  <div className="grid gap-3 lg:grid-cols-3">
                    <Fact label="Status" value={telegramBotToken?.configured ? "configured" : "missing"} />
                    <Fact label="Active token" value={telegramBotToken?.tokenMasked || "—"} mono />
                    <Fact
                      label="Saved override"
                      value={telegramBotToken?.hasStoredToken ? telegramBotToken.storedTokenMasked || "stored" : "none"}
                      mono
                    />
                  </div>
                  {telegramBotToken?.error ? <InlineError message={telegramBotToken.error} /> : null}
                  <form
                    className="grid gap-2 lg:grid-cols-[minmax(0,1fr)_auto_auto]"
                    onSubmit={(event) => {
                      event.preventDefault();
                      void saveTelegramToken();
                    }}
                  >
                    <Input
                      type="password"
                      value={telegramTokenDraft}
                      onChange={(event) => setTelegramTokenDraft(event.target.value)}
                      placeholder="123456789:AA..."
                      autoComplete="off"
                      spellCheck={false}
                    />
                    <Button type="submit" disabled={telegramTokenBusy}>
                      <Save className="h-4 w-4" />
                      {telegramTokenBusy ? "Saving…" : "Save token"}
                    </Button>
                    <Button
                      type="button"
                      variant="secondary"
                      disabled={telegramTokenBusy || !telegramBotToken?.hasStoredToken}
                      onClick={() => void clearTelegramToken()}
                    >
                      <Trash2 className="h-4 w-4" />
                      Clear
                    </Button>
                  </form>
                </div>
              )}
            </PanelCard>

            <PanelCard title="Connection" className="argus-data-grid">
              {showSkeleton ? (
                <div className="grid gap-3 lg:grid-cols-2">
                  {Array.from({ length: 6 }).map((_, index) => (
                    <div
                      key={index}
                      className="min-w-0 border-l border-border/58 py-1.5 pl-3"
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
                  label="Telegram bot"
                  ok={Boolean(telegramBotToken?.configured)}
                  detail="GET /admin/settings/telegram-bot-token"
                />
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
                      className="min-w-0 border-l border-border/58 py-1.5 pl-3"
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
    <div className="flex items-center justify-between gap-3 border-l border-border/58 py-2.5 pl-3 pr-1">
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
