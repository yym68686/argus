"use client";

import * as React from "react";
import { Laptop, Monitor } from "lucide-react";
import { toast } from "sonner";

import { Badge, EmptyState, Fact, InlineError, PanelCard, Skeleton } from "@/components/console-primitives";
import { ConsoleShell } from "@/components/console-shell";
import { Button } from "@/components/ui/button";
import {
  type HostAgentEnrollTokenResponse,
  type HostAgentSummary,
  fetchHostAgents,
  issueHostAgentEnrollToken,
} from "@/lib/admin";
import { httpBaseFromWsUrl, useGatewayWsUrlState } from "@/lib/gateway";

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

function hostLabel(host: HostAgentSummary): string {
  return host.displayName?.trim() || host.hostId;
}

function hostIsOnline(host: HostAgentSummary): boolean {
  return Boolean(host.connected ?? host.online ?? host.runtimeConnected ?? host.controlConnected ?? host.nodeConnected);
}

function platformLabel(platform: ClientPlatform): string {
  return platform === "windows" ? "Windows" : "macOS/Linux";
}

function tokenIsFresh(
  tokenInfo: HostAgentEnrollTokenResponse | null,
  tokenGatewayUrl: string,
  wsUrl: string
): tokenInfo is HostAgentEnrollTokenResponse {
  if (!tokenInfo?.token) return false;
  if (tokenGatewayUrl !== wsUrl.trim()) return false;
  if (!tokenInfo.expiresAtMs || !Number.isFinite(tokenInfo.expiresAtMs)) return true;
  return tokenInfo.expiresAtMs > Date.now() + 60_000;
}

export default function DevicesPage() {
  const [wsUrl] = useGatewayWsUrlState();
  const [detectedPlatform] = React.useState<ClientPlatform>(detectClientPlatform);
  const [tokenInfo, setTokenInfo] = React.useState<HostAgentEnrollTokenResponse | null>(null);
  const [tokenGatewayUrl, setTokenGatewayUrl] = React.useState("");
  const [hosts, setHosts] = React.useState<HostAgentSummary[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [copyingPlatform, setCopyingPlatform] = React.useState<ClientPlatform | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const autoLoadedForRef = React.useRef<string>("");

  const httpBase = React.useMemo(() => httpBaseFromWsUrl(wsUrl), [wsUrl]);

  const issueEnrollToken = React.useCallback(async () => {
    const gatewayUrl = wsUrl.trim();
    if (!gatewayUrl) {
      throw new Error("Gateway URL is not configured.");
    }
    const issued = await issueHostAgentEnrollToken(wsUrl, {
      ttlSec: DEFAULT_ENROLL_TTL_SEC,
    });
    setTokenInfo(issued);
    setTokenGatewayUrl(gatewayUrl);
    return issued;
  }, [wsUrl]);

  const refresh = React.useCallback(
    async () => {
      if (!wsUrl.trim()) return;
      setLoading(true);
      setError(null);
      try {
        const [tokenResult, hostsResult] = await Promise.allSettled([issueEnrollToken(), fetchHostAgents(wsUrl)]);
        if (tokenResult.status !== "fulfilled") {
          throw tokenResult.reason;
        }
        if (hostsResult.status === "fulfilled") {
          setHosts(Array.isArray(hostsResult.value.hosts) ? hostsResult.value.hosts : []);
        }
      } catch (err) {
        const message = (err as Error)?.message || String(err);
        setError(message);
      } finally {
        setLoading(false);
      }
    },
    [issueEnrollToken, wsUrl]
  );

  React.useEffect(() => {
    const trimmed = wsUrl.trim();
    if (!trimmed) return;
    if (autoLoadedForRef.current === trimmed) return;
    autoLoadedForRef.current = trimmed;
    void refresh();
  }, [refresh, wsUrl]);

  const copyInstallCommand = React.useCallback(
    async (copyPlatform: ClientPlatform) => {
      if (!httpBase) {
        toast.error("Gateway URL is not configured.");
        return;
      }
      setCopyingPlatform(copyPlatform);
      setError(null);
      try {
        const activeToken = tokenIsFresh(tokenInfo, tokenGatewayUrl, wsUrl) ? tokenInfo : await issueEnrollToken();
        const command = buildInstallCommand(httpBase, activeToken.token, copyPlatform);
        await navigator.clipboard.writeText(command);
        toast.success(`${platformLabel(copyPlatform)} command copied`);
      } catch (err) {
        const message = (err as Error)?.message || String(err);
        setError(message);
        toast.error(message);
      } finally {
        setCopyingPlatform(null);
      }
    },
    [httpBase, issueEnrollToken, tokenGatewayUrl, tokenInfo, wsUrl]
  );

  const showSkeleton = loading && !tokenInfo;
  const copyDisabled = loading || copyingPlatform !== null || !wsUrl.trim() || !httpBase;

  return (
    <ConsoleShell
      title="Devices"
      actions={
        <ConnectCommandActions
          detectedPlatform={detectedPlatform}
          disabled={copyDisabled}
          copyingPlatform={copyingPlatform}
          onCopy={copyInstallCommand}
        />
      }
    >
      {error ? <InlineError message={error} /> : null}

      <div className="grid gap-5">
        <section className="space-y-4">
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
                {hosts.map((host) => {
                  const online = hostIsOnline(host);
                  return (
                    <div key={host.hostId} className="border-l border-border/58 px-3 py-2.5">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div>
                          <div className="font-medium text-foreground">{hostLabel(host)}</div>
                          <div className="text-xs text-muted-foreground">{host.hostId}</div>
                        </div>
                        <Badge tone={online ? "success" : "default"}>{online ? "Connected" : "Offline"}</Badge>
                      </div>
                      <div className="mt-3 grid gap-2 md:grid-cols-2">
                        <Fact label="Platform" value={host.platform || "unknown"} />
                        <Fact label="Last seen" value={formatStamp(host.lastSeenAtMs)} />
                      </div>
                    </div>
                  );
                })}
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

function ConnectCommandActions({
  detectedPlatform,
  disabled,
  copyingPlatform,
  onCopy,
}: {
  detectedPlatform: ClientPlatform;
  disabled: boolean;
  copyingPlatform: ClientPlatform | null;
  onCopy: (platform: ClientPlatform) => void | Promise<void>;
}) {
  const platforms: ClientPlatform[] =
    detectedPlatform === "windows" ? ["windows", "unix"] : ["unix", "windows"];

  return (
    <div className="grid min-w-0 grid-cols-1 gap-2 sm:grid-cols-2">
      {platforms.map((platform, index) => (
        <Button
          key={platform}
          type="button"
          variant={index === 0 ? "default" : "secondary"}
          disabled={disabled}
          aria-label={`Copy ${platformLabel(platform)} install command`}
          onClick={() => void onCopy(platform)}
          className="h-11 min-w-0 justify-start px-3.5"
        >
          {platform === "windows" ? (
            <Monitor className="h-4 w-4 shrink-0" aria-hidden="true" />
          ) : (
            <Laptop className="h-4 w-4 shrink-0" aria-hidden="true" />
          )}
          <span className="min-w-0 truncate">
            {copyingPlatform === platform ? "Copying…" : `Copy ${platformLabel(platform)}`}
          </span>
        </Button>
      ))}
    </div>
  );
}
