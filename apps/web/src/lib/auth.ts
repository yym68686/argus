"use client";

import { httpBaseFromWsUrl, loadGatewayAuthToken, summarizeHttpFailure } from "@/lib/gateway";

export interface ConsoleUser {
  userId: number;
  email: string;
  isAdmin: boolean;
  createdAtMs: number;
  updatedAtMs: number;
  lastLoginAtMs?: number | null;
  developerApiKeyCount?: number;
}

export interface IssuedDeveloperApiKey {
  keyId: string;
  name: string;
  token: string;
  tokenPreview?: string | null;
  createdAtMs?: number | null;
  expiresAtMs?: number | null;
  revokedAtMs?: number | null;
  active?: boolean;
}

export interface AuthStatusResponse {
  ok: true;
  hasUsers: boolean;
  userCount: number;
  allowRegistration: boolean;
  requireInvite?: boolean;
}

export interface AuthMeResponse {
  ok: true;
  user: ConsoleUser;
}

export interface AuthSessionResponse {
  ok: true;
  user: ConsoleUser;
  sessionToken: string;
  issuedDeveloperApiKey?: IssuedDeveloperApiKey | null;
}

function buildAuthHeaders(token?: string | null, extra?: HeadersInit): Headers {
  const headers = new Headers(extra ?? {});
  const effectiveToken = (token ?? loadGatewayAuthToken()).trim();
  if (effectiveToken && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${effectiveToken}`);
  }
  return headers;
}

async function fetchJson<T>(
  wsUrl: string,
  path: string,
  init?: RequestInit,
  token?: string | null
): Promise<T> {
  const base = httpBaseFromWsUrl(wsUrl);
  if (!base) throw new Error("Gateway URL is not configured");
  const headers = buildAuthHeaders(token, init?.headers);
  if (init?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const resp = await fetch(`${base}${path}`, {
    ...init,
    headers,
    cache: init?.cache ?? "no-store",
  });
  const text = await resp.text();
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  if (!resp.ok) {
    throw new Error(summarizeHttpFailure(resp, text, `Request to ${path}`));
  }
  return (payload as T) ?? ({} as T);
}

export function fetchAuthStatus(wsUrl: string): Promise<AuthStatusResponse> {
  return fetchJson<AuthStatusResponse>(wsUrl, "/auth/status", { method: "GET" }, null);
}

export function fetchAuthMe(wsUrl: string, token?: string | null): Promise<AuthMeResponse> {
  return fetchJson<AuthMeResponse>(wsUrl, "/auth/me", { method: "GET" }, token);
}

export function loginWithPassword(
  wsUrl: string,
  body: { email: string; password: string }
): Promise<AuthSessionResponse> {
  return fetchJson<AuthSessionResponse>(
    wsUrl,
    "/auth/login",
    {
      method: "POST",
      body: JSON.stringify(body),
    },
    null
  );
}

export function registerWithPassword(
  wsUrl: string,
  body: { email: string; password: string; inviteCode?: string }
): Promise<AuthSessionResponse> {
  return fetchJson<AuthSessionResponse>(
    wsUrl,
    "/auth/register",
    {
      method: "POST",
      body: JSON.stringify(body),
    },
    null
  );
}

export function logoutSession(wsUrl: string, token?: string | null): Promise<{ ok: true }> {
  return fetchJson<{ ok: true }>(wsUrl, "/auth/logout", { method: "POST" }, token);
}
