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
  const hydrated = React.useSyncExternalStore(
    () => () => {},
    () => true,
    () => false,
  );
  const ready = hydrated && Boolean(storedToken.trim());

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

  if (!hydrated) {
    return (
      <div className="relative min-h-dvh px-3 py-3 md:px-4 md:py-4">
        <main className="mx-auto grid min-h-[calc(100dvh-1.5rem)] max-w-[1480px] place-items-center">
          <div className="argus-shell-panel flex w-full max-w-[38rem] flex-col items-center rounded-[30px] px-6 py-10 text-center">
            <div className="argus-shell-glyph flex h-12 w-12 items-center justify-center rounded-[16px] border border-border/72">
              <ShieldCheck className="h-5 w-5 text-primary" />
            </div>
            <div className="argus-kicker mt-5">Argus operator console</div>
            <div className="argus-display-ui mt-2 text-[clamp(1.9rem,3vw,3rem)] text-foreground">Preparing the control surface</div>
            <p className="mt-3 max-w-[34rem] text-sm leading-7 text-muted-foreground">
              Loading local operator state and resolving whether this browser already has an admin token.
            </p>
          </div>
        </main>
      </div>
    );
  }

  if (ready && storedToken.trim()) {
    return (
      <>
        <div className="fixed right-4 top-4 z-50">
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
    <div className="relative min-h-dvh px-3 py-3 md:px-4 md:py-4">
      <main className="mx-auto grid min-h-[calc(100dvh-1.5rem)] max-w-[1480px] grid-cols-1 gap-3 xl:grid-cols-[1.08fr_0.92fr]">
        <section className="argus-shell-panel argus-data-grid flex flex-col justify-between rounded-[30px] px-5 py-6 md:px-8 md:py-8">
          <div>
            <div className="argus-kicker">Argus operator console</div>
            <h1 className="argus-display mt-3 text-[clamp(2.4rem,4.2vw,5rem)] text-foreground">
              Trusted access for a self-hosted control plane.
            </h1>
            <p className="mt-4 max-w-[44rem] text-base leading-8 text-muted-foreground">
              This frontend is intentionally operator-facing. It assumes a shared bearer token, exposes fleet-level state, and keeps the gateway URL, runtime sessions, upstream channels, and usage ledger in one dense surface.
            </p>
          </div>

          <div className="grid gap-3 md:grid-cols-3">
            <InfoBlock
              title="One credential"
              body="The same ARGUS_TOKEN protects admin HTTP calls and WebSocket session access."
            />
            <InfoBlock
              title="No extra login layer"
              body="The browser stores the token locally after verification against /admin/overview."
            />
            <InfoBlock
              title="Deploy-aware"
              body="The console reuses the configured gateway endpoint instead of inventing a second control URL."
            />
          </div>
        </section>

        <section className="argus-shell-panel flex items-center rounded-[30px] px-5 py-6 md:px-8 md:py-8">
          <div className="mx-auto w-full max-w-[34rem]">
            <div className="argus-shell-glyph flex h-12 w-12 items-center justify-center rounded-[16px] border border-border/72">
              <ShieldCheck className="h-5 w-5 text-primary" />
            </div>

            <div className="mt-6">
              <div className="argus-surface-label">Admin sign-in</div>
              <h2 className="argus-display-ui mt-2 text-[clamp(1.8rem,2.7vw,3rem)] text-foreground">
                Unlock the operator surface
              </h2>
              <p className="mt-3 text-sm leading-7 text-muted-foreground">
                The token is checked before the UI opens. Once verified, subsequent admin requests inherit it automatically.
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

              <div className="rounded-[18px] border border-border/72 bg-background/22 px-4 py-3 text-sm leading-6 text-muted-foreground">
                The token is validated through <code>/admin/overview</code>. It never needs to be appended to visible URLs.
              </div>

              {error ? (
                <div className="rounded-[18px] border border-destructive/36 bg-destructive/10 px-4 py-3 text-sm leading-6 text-destructive">
                  {error}
                </div>
              ) : null}

              <Button type="submit" size="lg" className="w-full justify-center" disabled={verifying}>
                <LockKeyhole className={cn("h-4 w-4", verifying ? "animate-pulse" : null)} />
                {verifying ? "Verifying…" : "Enter console"}
              </Button>
            </form>
          </div>
        </section>
      </main>
    </div>
  );
}

function InfoBlock({ title, body }: { title: string; body: string }) {
  return (
    <div className="rounded-[18px] border border-border/72 bg-background/22 px-4 py-4 shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.04)]">
      <div className="argus-surface-label">{title}</div>
      <div className="mt-2 text-sm leading-6 text-muted-foreground">{body}</div>
    </div>
  );
}
