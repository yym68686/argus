"use client";

import * as React from "react";
import { usePathname, useRouter } from "next/navigation";
import { AtSign, LockKeyhole, PlugZap } from "lucide-react";

import type { ConsoleUser, IssuedDeveloperApiKey } from "@/lib/auth";
import { fetchAuthMe, fetchAuthStatus, loginWithPassword, logoutSession, registerWithPassword } from "@/lib/auth";
import {
  clearGatewayAuthToken,
  storeGatewayAuthToken,
  storeJustIssuedDeveloperKey,
  useStoredGatewayAuthToken,
  useStoredGatewayWsUrl,
} from "@/lib/gateway";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

interface AdminGateProps {
  children: React.ReactNode;
}

interface AuthContextValue {
  user: ConsoleUser | null;
  token: string;
  hasUsers: boolean;
  allowRegistration: boolean;
  loading: boolean;
  logout: () => Promise<void>;
}

const AuthContext = React.createContext<AuthContextValue | null>(null);
const ADMIN_ONLY_PATH_PREFIXES = ["/users", "/settings", "/nodes", "/devices"];
const NOOP_SUBSCRIBE = () => () => {};

export function useAuth(): AuthContextValue {
  const value = React.useContext(AuthContext);
  if (!value) {
    throw new Error("useAuth must be used within AdminGate");
  }
  return value;
}

export function AdminGate({ children }: AdminGateProps) {
  const wsUrl = useStoredGatewayWsUrl();
  const storedToken = useStoredGatewayAuthToken();
  const pathname = usePathname() || "/";
  const router = useRouter();

  const hydrated = React.useSyncExternalStore(NOOP_SUBSCRIBE, () => true, () => false);
  const [user, setUser] = React.useState<ConsoleUser | null>(null);
  const [hasUsers, setHasUsers] = React.useState(false);
  const [allowRegistration, setAllowRegistration] = React.useState(true);
  const [requireInvite, setRequireInvite] = React.useState(false);
  const [loading, setLoading] = React.useState(true);
  const [submitting, setSubmitting] = React.useState(false);
  const [mode, setMode] = React.useState<"login" | "register">("login");
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [inviteCode, setInviteCode] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);

  const adminRouteRequested = React.useMemo(
    () =>
      ADMIN_ONLY_PATH_PREFIXES.some((prefix) =>
        prefix === pathname ? true : pathname === prefix || pathname.startsWith(`${prefix}/`)
      ),
    [pathname]
  );

  const refreshSession = React.useCallback(
    async (tokenOverride?: string | null) => {
      const effectiveToken = (tokenOverride ?? storedToken).trim();
      setLoading(true);
      try {
        const statusResult = await fetchAuthStatus(wsUrl);
        setHasUsers(Boolean(statusResult.hasUsers));
        setAllowRegistration(Boolean(statusResult.allowRegistration));
        setRequireInvite(Boolean(statusResult.requireInvite));

        if (!effectiveToken) {
          setUser(null);
          setMode(statusResult.hasUsers || !statusResult.allowRegistration ? "login" : "register");
          setError(null);
          return;
        }

        const me = await fetchAuthMe(wsUrl, effectiveToken);
        setUser(me.user);
        setMode(me.user ? "login" : statusResult.hasUsers ? "login" : "register");
        setError(null);
      } catch (nextError) {
        clearGatewayAuthToken();
        setUser(null);
        const fallbackStatus = await fetchAuthStatus(wsUrl).catch(() => null);
        if (fallbackStatus) {
          setHasUsers(Boolean(fallbackStatus.hasUsers));
          setAllowRegistration(Boolean(fallbackStatus.allowRegistration));
          setRequireInvite(Boolean(fallbackStatus.requireInvite));
          setMode(fallbackStatus.hasUsers || !fallbackStatus.allowRegistration ? "login" : "register");
        }
        setError(null);
      } finally {
        setLoading(false);
      }
    },
    [storedToken, wsUrl]
  );

  React.useEffect(() => {
    if (!hydrated) return;
    const run = async () => {
      await Promise.resolve();
      await refreshSession();
    };
    void run();
  }, [hydrated, refreshSession]);

  React.useEffect(() => {
    if (loading) return;
    if (!user) return;
    if (user.isAdmin) return;
    if (!adminRouteRequested) return;
    router.replace("/");
  }, [adminRouteRequested, loading, router, user]);

  const handleSubmit = React.useCallback(
    async (event: React.FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      setSubmitting(true);
      setError(null);
      try {
        if (mode === "register" && !allowRegistration) {
          throw new Error("Registration is currently disabled");
        }
        const body = mode === "register" ? { email: email.trim(), password, inviteCode: inviteCode.trim() } : { email: email.trim(), password };
        const result =
          mode === "register"
            ? await registerWithPassword(wsUrl, body)
            : await loginWithPassword(wsUrl, body);
        storeGatewayAuthToken(result.sessionToken);
        setUser(result.user);
        setHasUsers(true);
        setPassword("");
        if (mode === "register" && result.issuedDeveloperApiKey) {
          storeJustIssuedDeveloperKey(result.issuedDeveloperApiKey as IssuedDeveloperApiKey);
          router.replace("/api-keys");
        }
      } catch (nextError) {
        setError((nextError as Error)?.message || String(nextError));
      } finally {
        setSubmitting(false);
      }
    },
    [allowRegistration, email, inviteCode, mode, password, router, wsUrl]
  );

  const logout = React.useCallback(async () => {
    const token = storedToken.trim();
    try {
      if (token) {
        await logoutSession(wsUrl, token);
      }
    } catch {
      // ignore logout API errors; local sign-out still wins
    } finally {
      clearGatewayAuthToken();
      setUser(null);
      setPassword("");
      setMode(hasUsers || !allowRegistration ? "login" : "register");
      router.replace("/");
    }
  }, [allowRegistration, hasUsers, router, storedToken, wsUrl]);

  const contextValue = React.useMemo<AuthContextValue>(
    () => ({
      user,
      token: storedToken,
      hasUsers,
      allowRegistration,
      loading: loading || !hydrated,
      logout,
    }),
    [allowRegistration, hasUsers, hydrated, loading, logout, storedToken, user]
  );

  if (!hydrated || loading) {
    return (
      <div className="grid min-h-dvh place-items-center px-4 py-6">
        <main className="w-full max-w-[22rem]">
          <section className="argus-shell-panel-soft rounded-[28px] px-6 py-8">
            <div className="flex items-center gap-3">
              <span className="argus-shell-glyph flex h-11 w-11 items-center justify-center rounded-[14px] border border-border/72">
                <PlugZap className="h-[18px] w-[18px] text-primary" />
              </span>
              <div className="text-sm font-semibold uppercase tracking-[0.2em] text-foreground">Argus</div>
            </div>
          </section>
        </main>
      </div>
    );
  }

  if (!user) {
    return (
      <AuthContext.Provider value={contextValue}>
        <div className="grid min-h-dvh place-items-center px-4 py-6">
          <main className="w-full max-w-[26rem]">
            <section className="argus-shell-panel-soft rounded-[30px] px-6 py-7 md:px-7 md:py-8">
              <div className="flex items-center gap-3">
                <span className="argus-shell-glyph flex h-11 w-11 items-center justify-center rounded-[14px] border border-border/72">
                  <PlugZap className="h-[18px] w-[18px] text-primary" />
                </span>
                <div className="text-sm font-semibold uppercase tracking-[0.2em] text-foreground">Argus</div>
              </div>

              <div className="mt-6 flex gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant={mode === "login" ? "default" : "secondary"}
                  className="min-w-[6.5rem]"
                  onClick={() => {
                    setMode("login");
                    setError(null);
                  }}
                >
                  Sign in
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant={mode === "register" ? "default" : "secondary"}
                  className="min-w-[8rem]"
                  disabled={!allowRegistration}
                  onClick={() => {
                    if (!allowRegistration) return;
                    setMode("register");
                    setError(null);
                  }}
                >
                  Create account
                </Button>
              </div>

              {!allowRegistration ? (
                <div className="mt-4 rounded-[18px] border border-border/70 bg-background/24 px-4 py-3 text-sm leading-6 text-muted-foreground">
                  Registration is disabled on this gateway. Sign in with an existing account.
                </div>
              ) : null}

              <form className="mt-6 space-y-4" onSubmit={handleSubmit}>
                <div className="grid gap-1.5">
                  <label className="argus-surface-label" htmlFor="argus-email">
                    Email
                  </label>
                  <Input
                    id="argus-email"
                    type="email"
                    autoComplete={mode === "login" ? "username" : "email"}
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    placeholder="name@company.com"
                    spellCheck={false}
                  />
                </div>

                <div className="grid gap-1.5">
                  <label className="argus-surface-label" htmlFor="argus-password">
                    Password
                  </label>
                  <Input
                    id="argus-password"
                    type="password"
                    autoComplete={mode === "login" ? "current-password" : "new-password"}
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    placeholder="Password"
                    spellCheck={false}
                  />
                </div>

                {mode === "register" && requireInvite ? (
                  <div className="grid gap-1.5">
                    <label className="argus-surface-label" htmlFor="argus-invite">
                      Invite Code
                    </label>
                    <Input
                      id="argus-invite"
                      value={inviteCode}
                      onChange={(event) => setInviteCode(event.target.value)}
                      placeholder="invite code"
                      autoComplete="off"
                      spellCheck={false}
                    />
                  </div>
                ) : null}

                {error ? (
                  <div className="rounded-[18px] border border-destructive/36 bg-destructive/10 px-4 py-3 text-sm leading-6 text-destructive">
                    {error}
                  </div>
                ) : null}

                <Button
                  type="submit"
                  size="lg"
                  className="w-full justify-center"
                  disabled={submitting || (mode === "register" && !allowRegistration)}
                >
                  {mode === "register" ? <AtSign className="h-4 w-4" /> : <LockKeyhole className="h-4 w-4" />}
                  <span className={cn(submitting ? "opacity-80" : null)}>
                    {submitting ? "Working…" : mode === "register" ? "Create account" : "Sign in"}
                  </span>
                </Button>
              </form>
            </section>
          </main>
        </div>
      </AuthContext.Provider>
    );
  }

  if (adminRouteRequested && !user.isAdmin) {
    return (
      <AuthContext.Provider value={contextValue}>
        <div className="grid min-h-dvh place-items-center px-4 py-6">
          <main className="w-full max-w-[20rem]">
            <section className="argus-shell-panel-soft rounded-[28px] px-6 py-8">
              <div className="text-sm font-medium text-foreground">Redirecting…</div>
            </section>
          </main>
        </div>
      </AuthContext.Provider>
    );
  }

  return <AuthContext.Provider value={contextValue}>{children}</AuthContext.Provider>;
}
