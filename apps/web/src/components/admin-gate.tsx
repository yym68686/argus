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
      <div className="grid min-h-dvh place-items-center px-4 py-6">
        <main className="w-full max-w-[24rem]">
          <div className="argus-shell-panel-soft flex flex-col items-center rounded-[28px] px-6 py-10 text-center">
            <div className="argus-shell-glyph flex h-12 w-12 items-center justify-center rounded-[16px] border border-border/72">
              <ShieldCheck className="h-5 w-5 text-primary" />
            </div>
            <div className="mt-5 text-base font-semibold text-foreground">Loading</div>
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
    <div className="grid min-h-dvh place-items-center px-4 py-6">
      <main className="w-full max-w-[28rem]">
        <section className="argus-shell-panel-soft rounded-[30px] px-6 py-7 md:px-7 md:py-8">
          <div className="mx-auto w-full">
            <div className="argus-shell-glyph flex h-12 w-12 items-center justify-center rounded-[16px] border border-border/72">
              <ShieldCheck className="h-5 w-5 text-primary" />
            </div>

            <div className="mt-6">
              <h1 className="text-[clamp(1.8rem,3vw,2.5rem)] font-semibold tracking-[-0.05em] text-foreground">Admin</h1>
            </div>

            <form className="mt-6 space-y-4" onSubmit={handleSubmit}>
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
                  placeholder="Token"
                  spellCheck={false}
                />
              </div>

              {error ? (
                <div className="rounded-[18px] border border-destructive/36 bg-destructive/10 px-4 py-3 text-sm leading-6 text-destructive">
                  {error}
                </div>
              ) : null}

              <Button type="submit" size="lg" className="w-full justify-center" disabled={verifying}>
                <LockKeyhole className={cn("h-4 w-4", verifying ? "animate-pulse" : null)} />
                {verifying ? "Verifying…" : "Enter"}
              </Button>
            </form>
          </div>
        </section>
      </main>
    </div>
  );
}
