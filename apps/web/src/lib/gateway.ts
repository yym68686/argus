import * as React from "react";

export const GATEWAY_WS_STORAGE_KEY = "argus.gateway.wsUrl";
export const GATEWAY_ADMIN_TOKEN_STORAGE_KEY = "argus.gateway.adminToken";

export function sanitizeGatewayWsUrl(url: string): string {
  try {
    const u = new URL(url);
    u.searchParams.delete("session");
    u.searchParams.delete("token");
    return u.toString();
  } catch {
    return url;
  }
}

export function stripSessionFromWsUrl(url: string): string {
  try {
    const u = new URL(url);
    u.searchParams.delete("session");
    return u.toString();
  } catch {
    return url;
  }
}

export function defaultWsUrl(): string {
  const preset = (process.env.NEXT_PUBLIC_ARGUS_WS_URL ?? "").trim();
  if (typeof window === "undefined") return "ws://127.0.0.1:8080/ws";
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const host =
    window.location.port === "3000" ? `${window.location.hostname}:8080` : window.location.host;
  const fallback = new URL(`${proto}//${host}/ws`);

  if (preset) {
    try {
      const parsed = new URL(preset);
      if (window.location.protocol === "https:" && parsed.protocol === "ws:") {
        fallback.pathname = parsed.pathname || "/ws";
        fallback.search = parsed.search;
        return fallback.toString();
      }
      return parsed.toString();
    } catch {
      return preset;
    }
  }

  return fallback.toString();
}

export function loadGatewayWsUrl(): string {
  if (typeof window === "undefined") return sanitizeGatewayWsUrl(defaultWsUrl());
  try {
    const saved = window.localStorage.getItem(GATEWAY_WS_STORAGE_KEY);
    if (saved && saved.trim()) return sanitizeGatewayWsUrl(saved.trim());
  } catch {
    // ignore
  }
  return sanitizeGatewayWsUrl(defaultWsUrl());
}

export function storeGatewayWsUrl(url: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(GATEWAY_WS_STORAGE_KEY, sanitizeGatewayWsUrl(url));
  } catch {
    // ignore
  }
}

function subscribeGatewayWsUrl(onStoreChange: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  const onStorage = (event: StorageEvent) => {
    if (event.key && event.key !== GATEWAY_WS_STORAGE_KEY) return;
    onStoreChange();
  };
  window.addEventListener("storage", onStorage);
  return () => window.removeEventListener("storage", onStorage);
}

export function useStoredGatewayWsUrl(): string {
  return React.useSyncExternalStore(subscribeGatewayWsUrl, loadGatewayWsUrl, () => "");
}

export function useGatewayWsUrlState(): [string, (value: string) => void] {
  const stored = useStoredGatewayWsUrl();
  const [override, setOverride] = React.useState<string | null>(null);

  const value = override ?? stored;

  const setValue = React.useCallback((nextValue: string) => {
    setOverride(nextValue);
    storeGatewayWsUrl(nextValue);
  }, []);

  return [value, setValue];
}

export function loadGatewayAdminToken(): string {
  if (typeof window === "undefined") return "";
  try {
    const saved = window.localStorage.getItem(GATEWAY_ADMIN_TOKEN_STORAGE_KEY);
    if (saved && saved.trim()) return saved.trim();
  } catch {
    // ignore
  }

  try {
    const savedWsUrl = window.localStorage.getItem(GATEWAY_WS_STORAGE_KEY);
    const savedToken = extractTokenFromWsUrl(savedWsUrl ?? "");
    if (savedToken) return savedToken;
  } catch {
    // ignore
  }

  return extractTokenFromWsUrl(defaultWsUrl()) ?? "";
}

export function storeGatewayAdminToken(token: string): void {
  if (typeof window === "undefined") return;
  try {
    const trimmed = token.trim();
    if (trimmed) {
      window.localStorage.setItem(GATEWAY_ADMIN_TOKEN_STORAGE_KEY, trimmed);
    } else {
      window.localStorage.removeItem(GATEWAY_ADMIN_TOKEN_STORAGE_KEY);
    }
  } catch {
    // ignore
  }
  window.dispatchEvent(new Event("argus-admin-token"));
}

export function clearGatewayAdminToken(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(GATEWAY_ADMIN_TOKEN_STORAGE_KEY);
  } catch {
    // ignore
  }
  window.dispatchEvent(new Event("argus-admin-token"));
}

function subscribeGatewayAdminToken(onStoreChange: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  const onStorage = (event: StorageEvent) => {
    if (event.key && event.key !== GATEWAY_ADMIN_TOKEN_STORAGE_KEY && event.key !== GATEWAY_WS_STORAGE_KEY) return;
    onStoreChange();
  };
  const onInternal = () => onStoreChange();
  window.addEventListener("storage", onStorage);
  window.addEventListener("argus-admin-token", onInternal);
  return () => {
    window.removeEventListener("storage", onStorage);
    window.removeEventListener("argus-admin-token", onInternal);
  };
}

export function useStoredGatewayAdminToken(): string {
  return React.useSyncExternalStore(subscribeGatewayAdminToken, loadGatewayAdminToken, () => "");
}

export function useGatewayAdminTokenState(): [string, (value: string) => void] {
  const stored = useStoredGatewayAdminToken();
  const [override, setOverride] = React.useState<string | null>(null);

  const value = override ?? stored;

  const setValue = React.useCallback((nextValue: string) => {
    setOverride(nextValue);
    storeGatewayAdminToken(nextValue);
  }, []);

  return [value, setValue];
}

export function extractTokenFromWsUrl(url: string): string | null {
  try {
    const u = new URL(url);
    const token = u.searchParams.get("token");
    return token && token.trim() ? token : null;
  } catch {
    return null;
  }
}

export function withGatewayAdminToken(wsUrl: string, token?: string | null): string {
  const effectiveToken = (token ?? loadGatewayAdminToken()).trim();
  if (!effectiveToken) return wsUrl;
  try {
    const u = new URL(wsUrl);
    u.searchParams.set("token", effectiveToken);
    return u.toString();
  } catch {
    return wsUrl;
  }
}

export function httpBaseFromWsUrl(url: string): string | null {
  try {
    const u = new URL(url);
    const proto = u.protocol === "wss:" ? "https:" : "http:";
    return `${proto}//${u.host}`;
  } catch {
    return null;
  }
}

export function buildGatewayHeaders(wsUrl: string, extra?: HeadersInit): Headers {
  const headers = new Headers(extra ?? {});
  const token = extractTokenFromWsUrl(wsUrl) ?? loadGatewayAdminToken();
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return headers;
}

export async function verifyGatewayAdminAccess(wsUrl: string, token: string): Promise<void> {
  const base = httpBaseFromWsUrl(wsUrl);
  if (!base) throw new Error("Gateway URL is not configured");
  const headers = new Headers({ Authorization: `Bearer ${token.trim()}` });
  const resp = await fetch(`${base}/admin/overview`, { headers, cache: "no-store" });
  const text = await resp.text();
  if (resp.ok) return;
  const payload = text ? safeParseJson(text) : null;
  const detail =
    payload && typeof payload === "object" && payload !== null && "detail" in payload
      ? String((payload as { detail?: unknown }).detail ?? "")
      : text;
  throw new Error(detail || `Authentication failed: ${resp.status}`);
}

export async function gatewayFetchJson<T>(wsUrl: string, path: string, init?: RequestInit): Promise<T> {
  const base = httpBaseFromWsUrl(wsUrl);
  if (!base) throw new Error("Gateway URL is not configured");
  const headers = buildGatewayHeaders(wsUrl, init?.headers);
  if (!headers.has("Content-Type") && init?.body) {
    headers.set("Content-Type", "application/json");
  }
  const resp = await fetch(`${base}${path}`, { ...init, headers });
  const text = await resp.text();
  const payload = text ? safeParseJson(text) : null;
  if (!resp.ok) {
    const detail =
      payload && typeof payload === "object" && payload !== null && "detail" in payload
        ? String((payload as { detail?: unknown }).detail ?? "")
        : text;
    throw new Error(detail || `Request failed: ${resp.status}`);
  }
  return (payload as T) ?? ({} as T);
}

function safeParseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}
