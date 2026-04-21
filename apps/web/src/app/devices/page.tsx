"use client";

import * as React from "react";
import { Apple, Copy, KeyRound, Laptop, Monitor, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { useAuth } from "@/components/admin-gate";
import { Badge, EmptyState, Fact, InlineError, PanelCard, Skeleton } from "@/components/console-primitives";
import { ConsoleShell } from "@/components/console-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  type HostAgentEnrollTokenResponse,
  type HostAgentSummary,
  fetchHostAgents,
  issueHostAgentEnrollToken,
} from "@/lib/admin";
import { httpBaseFromWsUrl, useGatewayWsUrlState } from "@/lib/gateway";
import { cn } from "@/lib/utils";

type ClientPlatform = "mac" | "windows";

const DEFAULT_ENROLL_TTL_SEC = 6 * 60 * 60;

function detectClientPlatform(): ClientPlatform {
  if (typeof navigator === "undefined") return "mac";
  const nav = navigator as Navigator & { userAgentData?: { platform?: string } };
  const signature = [nav.userAgentData?.platform, navigator.platform, navigator.userAgent]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  if (signature.includes("win")) return "windows";
  return "mac";
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

function hostLabel(host: HostAgentSummary): string {
  return host.displayName?.trim() || host.hostId;
}

export default function DevicesPage() {
  const { user } = useAuth();
  const [wsUrl, setWsUrl] = useGatewayWsUrlState();
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

  if (!user?.isAdmin) return null;

  const showSkeleton = loading && !tokenInfo;

  return (
    <ConsoleShell
      title="Devices"
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
            Regenerate
          </Button>
          <Button type="button" disabled={!command} onClick={() => copyText(command, "Connect command copied")}>
            <Copy className="h-4 w-4" />
            Copy Command
          </Button>
        </div>
      }
    >
      {error ? <InlineError message={error} /> : null}

      <div className="grid gap-4">
        <section className="space-y-4">
          <PanelCard
            title="One-Step Connect"
            action={
              <div className="flex flex-wrap items-center gap-2">
                <PlatformButton
                  active={platform === "mac"}
                  icon={<Apple className="h-4 w-4" />}
                  label="macOS"
                  onClick={() => setPlatform("mac")}
                />
                <PlatformButton
                  active={platform === "windows"}
                  icon={<Monitor className="h-4 w-4" />}
                  label="Windows"
                  onClick={() => setPlatform("windows")}
                />
              </div>
            }
          >
            {showSkeleton ? (
              <div className="space-y-3">
                <Skeleton className="h-4 w-40 rounded-full" />
                <Skeleton className="h-28 rounded-[18px]" />
                <Skeleton className="h-10 rounded-xl" />
              </div>
            ) : (
              <div className="space-y-4">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone="primary">{platform === "windows" ? "Detected Windows" : "Detected macOS"}</Badge>
                  <Badge tone="success">One command</Badge>
                  <Badge tone="default">Desktop workspace</Badge>
                </div>

                <div className="rounded-[18px] border border-border/70 bg-background/18 p-3">
                  <div className="mb-2 flex items-center gap-2 text-sm font-medium text-foreground">
                    <Laptop className="h-4 w-4 text-primary" />
                    Run this on the computer you want to connect
                  </div>
                  <Textarea readOnly value={command} rows={4} className="font-mono text-[12.5px] leading-6" />
                </div>

                <div className="flex flex-wrap gap-2">
                  <Button type="button" disabled={!command} onClick={() => copyText(command, "Connect command copied")}>
                    <Copy className="h-4 w-4" />
                    Copy Command
                  </Button>
                  <Button
                    type="button"
                    variant="secondary"
                    disabled={!tokenInfo?.token}
                    onClick={() => copyText(tokenInfo?.token ?? "", "Token copied")}
                  >
                    <KeyRound className="h-4 w-4" />
                    Copy Token
                  </Button>
                </div>

                <p className="text-sm leading-6 text-muted-foreground">
                  The installer downloads the right Argus CLI for the machine, sets the default workspace under the
                  Desktop, and connects the local Codex host to this gateway with no extra steps.
                </p>
              </div>
            )}
          </PanelCard>

          <PanelCard title="Connected Devices">
            {showSkeleton ? (
              <div className="grid gap-3">
                {Array.from({ length: 3 }).map((_, index) => (
                  <div key={index} className="rounded-[18px] border border-border/70 bg-background/24 p-3">
                    <Skeleton className="h-4 w-32 rounded-full" />
                    <Skeleton className="mt-3 h-3 w-52 rounded-full" />
                  </div>
                ))}
              </div>
            ) : hosts.length ? (
              <div className="grid gap-3">
                {hosts.map((host) => (
                  <div key={host.hostId} className="rounded-[18px] border border-border/70 bg-background/24 p-3">
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
    <Button type="button" size="sm" variant={active ? "default" : "secondary"} onClick={onClick}>
      {icon}
      {label}
    </Button>
  );
}
