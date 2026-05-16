"use client";

import * as React from "react";
import { Copy, KeyRound, Laptop, Monitor, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { Badge, EmptyState, Fact, InlineError, PanelCard, Skeleton } from "@/components/console-primitives";
import { ConsoleShell } from "@/components/console-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  type HostAgentEnrollTokenResponse,
  type HostAgentSummary,
  fetchHostAgents,
  issueHostAgentEnrollToken,
} from "@/lib/admin";
import { httpBaseFromWsUrl, useGatewayWsUrlState } from "@/lib/gateway";
import { cn } from "@/lib/utils";

type ClientPlatform = "unix" | "windows";

const DEFAULT_ENROLL_TTL_SEC = 6 * 60 * 60;

function detectClientPlatform(): ClientPlatform {
  if (typeof navigator === "undefined") return "unix";
  const nav = navigator as Navigator & { userAgentData?: { platform?: string } };
  const signature = [nav.userAgentData?.platform, navigator.platform, navigator.userAgent]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  if (signature.includes("win")) return "windows";
  return "unix";
}

function buildInstallCommand(httpBase: string, token: string, platform: ClientPlatform): string {
  const encodedToken = encodeURIComponent(token);
  if (platform === "windows") {
    return `powershell -NoProfile -ExecutionPolicy Bypass -Command "irm '${httpBase}/host-agent/install.ps1?token=${encodedToken}' | iex"`;
  }
  return `curl -fsSL '${httpBase}/host-agent/install.sh?token=${encodedToken}' | bash`;
}

function formatStamp(value?: number | null): string {
  if (!value || !Number.isFinite(value)) return "—";
  return new Date(value).toLocaleString();
}

function formatExpiry(value?: number | null): string {
  if (!value || !Number.isFinite(value)) return "Refresh to issue a token";
  return `Valid until ${formatStamp(value)}`;
}

function workspaceLabel(value?: string | null): string {
  const trimmed = value?.trim();
  if (!trimmed) return "Desktop workspace";
  return trimmed;
}

function hostLabel(host: HostAgentSummary): string {
  return host.displayName?.trim() || host.hostId;
}

export default function DevicesPage() {
  const [wsUrl, setWsUrl] = useGatewayWsUrlState();
  const [detectedPlatform] = React.useState<ClientPlatform>(detectClientPlatform);
  const [platform, setPlatform] = React.useState<ClientPlatform>(detectClientPlatform);
  const [tokenInfo, setTokenInfo] = React.useState<HostAgentEnrollTokenResponse | null>(null);
  const [hosts, setHosts] = React.useState<HostAgentSummary[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const autoLoadedForRef = React.useRef<string>("");

  const httpBase = React.useMemo(() => httpBaseFromWsUrl(wsUrl), [wsUrl]);
  const command = httpBase && tokenInfo?.token ? buildInstallCommand(httpBase, tokenInfo.token, platform) : "";

  const refresh = React.useCallback(
    async (opts?: { notify?: boolean }) => {
      if (!wsUrl.trim()) return;
      setLoading(true);
      setError(null);
      try {
        const [tokenResult, hostsResult] = await Promise.allSettled([
          issueHostAgentEnrollToken(wsUrl, {
            ttlSec: DEFAULT_ENROLL_TTL_SEC,
          }),
          fetchHostAgents(wsUrl),
        ]);
        if (tokenResult.status !== "fulfilled") {
          throw tokenResult.reason;
        }
        setTokenInfo(tokenResult.value);
        if (hostsResult.status === "fulfilled") {
          setHosts(Array.isArray(hostsResult.value.hosts) ? hostsResult.value.hosts : []);
        }
        if (opts?.notify) {
          toast.success("Connect command refreshed");
        }
      } catch (err) {
        const message = (err as Error)?.message || String(err);
        setError(message);
        if (opts?.notify) {
          toast.error(message);
        }
      } finally {
        setLoading(false);
      }
    },
    [wsUrl]
  );

  React.useEffect(() => {
    const trimmed = wsUrl.trim();
    if (!trimmed) return;
    if (autoLoadedForRef.current === trimmed) return;
    autoLoadedForRef.current = trimmed;
    void refresh({ notify: false });
  }, [refresh, wsUrl]);

  function copyText(text: string, successMessage: string): void {
    if (!text.trim()) return;
    void navigator.clipboard.writeText(text).then(
      () => toast.success(successMessage),
      () => toast.error("Copy failed")
    );
  }

  const showSkeleton = loading && !tokenInfo;

  return (
    <ConsoleShell title="Devices">
      {error ? <InlineError message={error} /> : null}

      <div className="grid gap-5">
        <section className="space-y-4">
          <section
            aria-labelledby="host-connect-heading"
            className="argus-shell-panel-soft overflow-hidden rounded-[22px]"
          >
            {showSkeleton ? (
              <div className="grid gap-0 lg:grid-cols-[minmax(22rem,0.42fr)_minmax(0,1fr)]">
                <div className="border-b border-border/60 p-4 md:p-5 lg:border-b-0 lg:border-r">
                  <Skeleton className="h-5 w-40 rounded-full" />
                  <Skeleton className="mt-3 h-4 w-56 rounded-full" />
                  <Skeleton className="mt-6 h-10 rounded-xl" />
                  <Skeleton className="mt-4 h-20 rounded-[16px]" />
                </div>
                <div className="p-4 md:p-5">
                  <Skeleton className="h-5 w-56 rounded-full" />
                  <Skeleton className="mt-4 h-28 rounded-[18px]" />
                  <Skeleton className="mt-4 h-10 w-40 rounded-xl" />
                </div>
              </div>
            ) : (
              <div className="grid gap-0 lg:grid-cols-[minmax(22rem,0.42fr)_minmax(0,1fr)]">
                <div className="border-b border-border/60 p-4 md:p-5 lg:border-b-0 lg:border-r">
                  <h2 id="host-connect-heading" className="text-[1.05rem] font-semibold text-foreground">
                    One-Step Connect
                  </h2>
                  <p className="mt-2 max-w-[28rem] text-sm leading-6 text-muted-foreground">
                    Generate a host install command for this gateway.
                  </p>

                  <div className="mt-6 space-y-4">
                    <div>
                      <label
                        htmlFor="devices-gateway-url"
                        className="argus-surface-label block"
                      >
                        Gateway URL
                      </label>
                      <div className="mt-2 flex min-w-0 flex-col gap-2 sm:flex-row">
                        <Input
                          id="devices-gateway-url"
                          name="gateway-url"
                          type="url"
                          value={wsUrl}
                          onChange={(event) => setWsUrl(event.target.value)}
                          className="min-w-0 flex-1"
                          placeholder="wss://example.com/ws"
                          autoComplete="off"
                          spellCheck={false}
                          translate="no"
                        />
                        <Button
                          type="button"
                          variant="secondary"
                          disabled={loading}
                          onClick={() => void refresh({ notify: true })}
                          className="shrink-0"
                        >
                          <RefreshCw className={cn("h-4 w-4", loading ? "animate-spin" : null)} />
                          {loading ? "Refreshing…" : "Regenerate"}
                        </Button>
                      </div>
                    </div>

                    <div>
                      <div className="argus-surface-label">Platform</div>
                      <div className="mt-2 grid grid-cols-2 gap-2">
                        <PlatformButton
                          active={platform === "unix"}
                          icon={<Laptop className="h-4 w-4" aria-hidden="true" />}
                          label="macOS/Linux"
                          onClick={() => setPlatform("unix")}
                        />
                        <PlatformButton
                          active={platform === "windows"}
                          icon={<Monitor className="h-4 w-4" aria-hidden="true" />}
                          label="Windows"
                          onClick={() => setPlatform("windows")}
                        />
                      </div>
                      <p className="mt-2 text-xs leading-5 text-muted-foreground">
                        Detected {detectedPlatform === "windows" ? "Windows" : "macOS/Linux"}. Switch if this command
                        is for another machine.
                      </p>
                    </div>

                    <div className="grid gap-3 border-t border-border/56 pt-4">
                      <ConnectMeta label="Workspace" value={workspaceLabel(tokenInfo?.workspaceBasePath)} />
                      <div className="grid gap-1">
                        <div className="argus-surface-label">Token</div>
                        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                          <div className="break-words text-sm leading-6 text-foreground">
                            {formatExpiry(tokenInfo?.expiresAtMs)}
                          </div>
                          <Button
                            type="button"
                            size="sm"
                            variant="secondary"
                            disabled={!tokenInfo?.token}
                            onClick={() => copyText(tokenInfo?.token ?? "", "Token copied")}
                            className="shrink-0"
                          >
                            <KeyRound className="h-4 w-4" />
                            Copy Token
                          </Button>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>

                <div className="flex min-w-0 flex-col p-4 md:p-5">
                  <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
                        <Laptop className="h-4 w-4 text-primary" aria-hidden="true" />
                        Run on the computer you want to connect
                      </div>
                      <p className="mt-2 max-w-[44rem] text-sm leading-6 text-muted-foreground">
                        The installer adds the Argus CLI, sets the desktop workspace, and links the local Codex host to this gateway.
                      </p>
                    </div>
                    <Button
                      type="button"
                      disabled={!command}
                      onClick={() => copyText(command, "Connect command copied")}
                      className="shrink-0 md:self-start"
                    >
                      <Copy className="h-4 w-4" />
                      Copy Command
                    </Button>
                  </div>

                  <div
                    className="mt-4 min-h-[8.5rem] rounded-[18px] border border-border/72 bg-background/24 p-4 shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.04)]"
                    translate="no"
                  >
                    <pre className="max-h-[14rem] overflow-auto whitespace-pre-wrap break-words font-mono text-[12.5px] leading-6 text-foreground">
                      <code>{command || "Refresh to generate an install command."}</code>
                    </pre>
                  </div>

                  <p className="mt-4 text-sm leading-6 text-muted-foreground">
                    After the command succeeds, the host appears in Connected Devices below.
                  </p>
                </div>
              </div>
            )}
          </section>

          <PanelCard title="Connected Devices">
            {showSkeleton ? (
              <div className="grid gap-3">
                {Array.from({ length: 3 }).map((_, index) => (
                  <div key={index} className="border-l border-border/58 px-3 py-2.5">
                    <Skeleton className="h-4 w-32 rounded-full" />
                    <Skeleton className="mt-3 h-3 w-52 rounded-full" />
                  </div>
                ))}
              </div>
            ) : hosts.length ? (
              <div className="grid gap-3">
                {hosts.map((host) => (
                  <div key={host.hostId} className="border-l border-border/58 px-3 py-2.5">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <div className="font-medium text-foreground">{hostLabel(host)}</div>
                        <div className="text-xs text-muted-foreground">{host.hostId}</div>
                      </div>
                      <Badge tone={host.connected ? "success" : "default"}>
                        {host.connected ? "Connected" : "Offline"}
                      </Badge>
                    </div>
                    <div className="mt-3 grid gap-2 md:grid-cols-2">
                      <Fact label="Platform" value={host.platform || "unknown"} />
                      <Fact label="Last seen" value={formatStamp(host.lastSeenAtMs)} />
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState title="No devices connected yet" />
            )}
          </PanelCard>
        </section>
      </div>
    </ConsoleShell>
  );
}

function PlatformButton({
  active,
  icon,
  label,
  onClick,
}: {
  active: boolean;
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <Button
      type="button"
      size="sm"
      variant={active ? "default" : "secondary"}
      onClick={onClick}
      className="h-10 min-w-0 justify-center px-3"
    >
      {icon}
      <span className="min-w-0 truncate">{label}</span>
    </Button>
  );
}

function ConnectMeta({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid gap-1">
      <div className="argus-surface-label">{label}</div>
      <div className="mt-1 break-words text-sm leading-6 text-foreground">{value}</div>
    </div>
  );
}
