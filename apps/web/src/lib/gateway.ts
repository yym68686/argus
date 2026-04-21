import * as React from "react";

export const GATEWAY_WS_STORAGE_KEY = "argus.gateway.wsUrl";
export const GATEWAY_ADMIN_TOKEN_STORAGE_KEY = "argus.gateway.adminToken";
export const GATEWAY_AUTH_TOKEN_STORAGE_KEY = "argus.auth.sessionToken";
export const JUST_ISSUED_DEVELOPER_KEY_STORAGE_KEY = "argus.developer.justIssued";

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

export function loadGatewayAuthToken(): string {
  if (typeof window === "undefined") return "";
  try {
    const saved = window.localStorage.getItem(GATEWAY_AUTH_TOKEN_STORAGE_KEY);
    if (saved && saved.trim()) return saved.trim();
  } catch {
    // ignore
  }
  return "";
}

export function storeGatewayAuthToken(token: string): void {
  if (typeof window === "undefined") return;
  try {
    const trimmed = token.trim();
    if (trimmed) {
      window.localStorage.setItem(GATEWAY_AUTH_TOKEN_STORAGE_KEY, trimmed);
      window.localStorage.removeItem(GATEWAY_ADMIN_TOKEN_STORAGE_KEY);
    } else {
      window.localStorage.removeItem(GATEWAY_AUTH_TOKEN_STORAGE_KEY);
      window.localStorage.removeItem(GATEWAY_ADMIN_TOKEN_STORAGE_KEY);
    }
  } catch {
    // ignore
  }
  window.dispatchEvent(new Event("argus-auth-token"));
}

export function storeJustIssuedDeveloperKey(payload: unknown): void {
  if (typeof window === "undefined") return;
  try {
    if (payload == null) {
      window.sessionStorage.removeItem(JUST_ISSUED_DEVELOPER_KEY_STORAGE_KEY);
      return;
    }
    window.sessionStorage.setItem(JUST_ISSUED_DEVELOPER_KEY_STORAGE_KEY, JSON.stringify(payload));
  } catch {
    // ignore
  }
}

export function loadJustIssuedDeveloperKey<T>(): T | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(JUST_ISSUED_DEVELOPER_KEY_STORAGE_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

export function consumeJustIssuedDeveloperKey<T>(): T | null {
  const payload = loadJustIssuedDeveloperKey<T>();
  if (typeof window !== "undefined") {
    try {
      window.sessionStorage.removeItem(JUST_ISSUED_DEVELOPER_KEY_STORAGE_KEY);
    } catch {
      // ignore
    }
  }
  return payload;
}

export function clearGatewayAuthToken(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(GATEWAY_AUTH_TOKEN_STORAGE_KEY);
    window.localStorage.removeItem(GATEWAY_ADMIN_TOKEN_STORAGE_KEY);
  } catch {
    // ignore
  }
  window.dispatchEvent(new Event("argus-auth-token"));
}

function subscribeGatewayAuthToken(onStoreChange: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  const onStorage = (event: StorageEvent) => {
    if (event.key && event.key !== GATEWAY_AUTH_TOKEN_STORAGE_KEY) {
      return;
    }
    onStoreChange();
  };
  const onInternal = () => onStoreChange();
  window.addEventListener("storage", onStorage);
  window.addEventListener("argus-auth-token", onInternal);
  return () => {
    window.removeEventListener("storage", onStorage);
    window.removeEventListener("argus-auth-token", onInternal);
  };
}

export function useStoredGatewayAuthToken(): string {
  return React.useSyncExternalStore(subscribeGatewayAuthToken, loadGatewayAuthToken, () => "");
}

export function useGatewayAuthTokenState(): [string, (value: string) => void] {
  const stored = useStoredGatewayAuthToken();
  const [override, setOverride] = React.useState<string | null>(null);

  const value = override ?? stored;

  const setValue = React.useCallback((nextValue: string) => {
    setOverride(nextValue);
    storeGatewayAuthToken(nextValue);
  }, []);

  return [value, setValue];
}

export const loadGatewayAdminToken = loadGatewayAuthToken;
export const storeGatewayAdminToken = storeGatewayAuthToken;
export const clearGatewayAdminToken = clearGatewayAuthToken;
export const useStoredGatewayAdminToken = useStoredGatewayAuthToken;
export const useGatewayAdminTokenState = useGatewayAuthTokenState;

export function extractTokenFromWsUrl(url: string): string | null {
  try {
    const u = new URL(url);
    const token = u.searchParams.get("token");
    return token && token.trim() ? token : null;
  } catch {
    return null;
  }
}

export function withGatewayAuthToken(wsUrl: string, token?: string | null): string {
  const effectiveToken = (token ?? loadGatewayAuthToken()).trim();
  if (!effectiveToken) return wsUrl;
  try {
    const u = new URL(wsUrl);
    u.searchParams.set("token", effectiveToken);
    return u.toString();
  } catch {
    return wsUrl;
  }
}

export const withGatewayAdminToken = withGatewayAuthToken;

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
  const token = loadGatewayAuthToken() || extractTokenFromWsUrl(wsUrl);
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
  throw new Error(summarizeHttpFailure(resp, text, "Authentication check"));
}

export async function gatewayFetchJson<T>(wsUrl: string, path: string, init?: RequestInit): Promise<T> {
  const base = httpBaseFromWsUrl(wsUrl);
  if (!base) throw new Error("Gateway URL is not configured");
  const headers = buildGatewayHeaders(wsUrl, init?.headers);
  if (!headers.has("Content-Type") && init?.body) {
    headers.set("Content-Type", "application/json");
  }
  const requestInit: RequestInit = { ...init, headers };
  if (requestInit.cache === undefined) {
    requestInit.cache = "no-store";
  }
  const resp = await fetch(`${base}${path}`, requestInit);
  const text = await resp.text();
  const payload = text ? safeParseJson(text) : null;
  if (!resp.ok) {
    throw new Error(summarizeHttpFailure(resp, text, `Request to ${path}`));
  }
  return (payload as T) ?? ({} as T);
}

export function summarizeHttpFailure(resp: Response, text: string, scope = "Request"): string {
  const status = collapseWhitespace([String(resp.status || ""), resp.statusText || ""].filter(Boolean).join(" "));
  const detail = extractResponseDetail(text);
  const prefix = status ? `${scope} failed with ${status}.` : `${scope} failed.`;
  if (!detail) {
    return prefix;
  }
  const normalizedDetail = detail.replace(/[.\s]+$/g, "");
  if (status && normalizedDetail.toLowerCase() === status.toLowerCase()) {
    return prefix;
  }
  return `${prefix} ${normalizedDetail}.`;
}

function safeParseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function extractResponseDetail(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return "";

  const payload = safeParseJson(trimmed);
  const payloadDetail = extractJsonErrorDetail(payload);
  if (payloadDetail) {
    return payloadDetail;
  }

  if (looksLikeHtml(trimmed)) {
    return extractHtmlErrorDetail(trimmed);
  }

  return collapseWhitespace(trimmed).slice(0, 280);
}

function extractJsonErrorDetail(payload: unknown): string {
  if (!payload || typeof payload !== "object") return "";
  const record = payload as Record<string, unknown>;
  for (const key of ["detail", "error", "message", "title"]) {
    const value = record[key];
    if (typeof value === "string" && collapseWhitespace(value)) {
      return collapseWhitespace(value);
    }
  }
  return "";
}

function looksLikeHtml(text: string): boolean {
  return /<!doctype html/i.test(text) || /<html[\s>]/i.test(text) || /<(head|body|title|h1)\b/i.test(text);
}

function extractHtmlErrorDetail(text: string): string {
  const combined = collapseWhitespace(stripHtmlTags(text));
  const title = extractTagText(text, "title");
  const heading = extractTagText(text, "h1");
  const normalizedSignals = [combined, title, heading].filter(Boolean).join(" ");

  if (/bad gateway/i.test(normalizedSignals)) {
    return /cloudflare/i.test(normalizedSignals)
      ? "Bad gateway from the upstream host (Cloudflare edge page)"
      : "Bad gateway from the upstream host";
  }
  if (/gateway timeout/i.test(normalizedSignals)) {
    return /cloudflare/i.test(normalizedSignals)
      ? "Gateway timeout from the upstream host (Cloudflare edge page)"
      : "Gateway timeout from the upstream host";
  }
  if (/unauthorized/i.test(normalizedSignals)) {
    return "Unauthorized";
  }
  if (/forbidden/i.test(normalizedSignals)) {
    return "Forbidden";
  }

  return title || heading || "Gateway returned an unexpected HTML error page";
}

function extractTagText(text: string, tag: string): string {
  const match = text.match(new RegExp(`<${tag}\\b[^>]*>([\\s\\S]*?)</${tag}>`, "i"));
  return match?.[1] ? collapseWhitespace(stripHtmlTags(match[1])) : "";
}

function stripHtmlTags(text: string): string {
  return text
    .replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, " ")
    .replace(/<style\b[^<]*(?:(?!<\/style>)<[^<]*)*<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ");
}

function collapseWhitespace(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}
