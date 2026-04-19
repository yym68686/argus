"use client";

import * as React from "react";
import { LockKeyhole, LogOut, ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  clearGatewayAdminToken,
  loadGatewayWsUrl,
  storeGatewayAdminToken,
  useStoredGatewayAdminToken,
  verifyGatewayAdminAccess,
} from "@/lib/gateway";
import { cn } from "@/lib/utils";

interface AdminGateProps {
  children: React.ReactNode;
}

export function AdminGate({ children }: AdminGateProps) {
  const storedToken = useStoredGatewayAdminToken();
  const [draftToken, setDraftToken] = React.useState("");
  const [verifying, setVerifying] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const ready = Boolean(storedToken.trim());

  const handleSubmit = React.useCallback(
    async (event: React.FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      const token = draftToken.trim();
      if (!token) {
        setError("ARGUS_TOKEN is required.");
        return;
      }

      setVerifying(true);
      setError(null);
      try {
        const wsUrl = loadGatewayWsUrl();
        await verifyGatewayAdminAccess(wsUrl, token);
        storeGatewayAdminToken(token);
        setDraftToken("");
      } catch (nextError) {
        setError((nextError as Error)?.message || String(nextError));
      } finally {
        setVerifying(false);
      }
    },
    [draftToken],
  );

  const handleSignOut = React.useCallback(() => {
    clearGatewayAdminToken();
    setError(null);
  }, []);

  if (ready && storedToken.trim()) {
    return (
      <>
        <div className="fixed bottom-4 right-4 z-50">
          <Button type="button" variant="secondary" size="sm" onClick={handleSignOut}>
            <LogOut className="h-4 w-4" />
            Sign out
          </Button>
        </div>
        {children}
      </>
    );
  }

  return (
    <div className="relative min-h-dvh overflow-hidden bg-background text-foreground">
      <div className="pointer-events-none absolute inset-0">
        <div className="absolute inset-0 argus-landing-canvas" />
        <div className="absolute inset-0 argus-landing-grid-64 opacity-70" />
        <div className="absolute inset-0 argus-landing-noise" />
        <div className="absolute -left-[24%] -top-[20%] h-[60vh] w-[60vw] argus-landing-blob argus-landing-blob-a" />
        <div className="absolute -right-[26%] top-[8%] h-[55vh] w-[55vw] argus-landing-blob argus-landing-blob-b" />
      </div>

      <main className="relative z-[1] flex min-h-dvh items-center justify-center px-4 py-8">
        <div className="argus-shell-panel grid w-full max-w-[1080px] overflow-hidden rounded-[36px] lg:grid-cols-[1.05fr_0.95fr]">
          <section className="argus-data-grid border-b border-border/60 bg-background/58 p-8 lg:border-b-0 lg:border-r lg:p-10">
            <div className="argus-kicker">Administrator access</div>
            <h1 className="mt-4 text-[clamp(2.2rem,4vw,4.4rem)] font-semibold tracking-[-0.05em] text-foreground">
              Argus control surface.
            </h1>
            <p className="mt-4 max-w-xl text-base leading-8 text-muted-foreground">
              This frontend is operator-facing. Enter the same <code>ARGUS_TOKEN</code> used by the gateway and the UI
              will attach it to admin API requests and live session connections automatically.
            </p>

            <div className="mt-8 grid gap-4 sm:grid-cols-3">
              <InfoBlock title="One token" body="No separate web password. The existing Argus bearer token is the admin credential." />
              <InfoBlock title="Auto wiring" body="After sign-in, HTTP requests and WebSocket sessions inherit the token without embedding it in visible fields." />
              <InfoBlock title="Current host" body="The gateway WebSocket endpoint is derived from this deployment unless you change it later in Settings." />
            </div>
          </section>

          <section className="p-8 lg:p-10">
            <div className="mx-auto max-w-md">
              <div className="argus-shell-glyph flex h-14 w-14 items-center justify-center rounded-[22px] border border-border/70">
                <ShieldCheck className="h-6 w-6 text-primary" />
              </div>

              <div className="mt-6">
                <div className="argus-surface-label">Sign in</div>
                <h2 className="mt-3 text-2xl font-semibold tracking-[-0.03em] text-foreground">Unlock the operator console</h2>
                <p className="mt-3 text-sm leading-7 text-muted-foreground">
                  The token is validated against <code>/admin/overview</code> before the UI opens.
                </p>
              </div>

              <form className="mt-8 space-y-4" onSubmit={handleSubmit}>
                <div className="space-y-2">
                  <label className="argus-surface-label" htmlFor="argus-admin-token">
                    ARGUS_TOKEN
                  </label>
                  <Input
                    id="argus-admin-token"
                    type="password"
                    autoComplete="current-password"
                    value={draftToken}
                    onChange={(event) => setDraftToken(event.target.value)}
                    placeholder="Paste the gateway admin token"
                    spellCheck={false}
                  />
                </div>

                {error ? (
                  <div className="rounded-[22px] border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm leading-6 text-destructive">
                    {error}
                  </div>
                ) : null}

                <Button type="submit" size="lg" className="w-full" disabled={verifying}>
                  <LockKeyhole className={cn("h-4 w-4", verifying && "animate-pulse")} />
                  {verifying ? "Verifying…" : "Enter console"}
                </Button>
              </form>
            </div>
          </section>
        </div>
      </main>
    </div>
  );
}

function InfoBlock({ title, body }: { title: string; body: string }) {
  return (
    <div className="rounded-[24px] border border-border/60 bg-background/28 p-4 shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.04)]">
      <div className="argus-surface-label">{title}</div>
      <div className="mt-3 text-sm leading-6 text-muted-foreground">{body}</div>
    </div>
  );
}
