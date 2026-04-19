import { gatewayFetchJson } from "@/lib/gateway";

export interface UsageSummary {
  requestCount: number;
  okCount: number;
  errorCount: number;
  inputTokens: number;
  outputTokens: number;
  reasoningTokens: number;
  totalTokens: number;
  estimatedCostUsd?: number | null;
  firstAtMs?: number | null;
  lastAtMs?: number | null;
}

export interface AdminChannelEntry {
  channelId: string;
  name: string;
  kind?: string | null;
  builtinKind?: string | null;
  isBuiltin?: boolean;
  selected?: boolean;
  baseUrl?: string | null;
  responsesUrl?: string | null;
  modelsUrl?: string | null;
  hasApiKey?: boolean;
  apiKeyMasked?: string | null;
  ready?: boolean;
  reason?: string | null;
  canRename?: boolean;
  canDelete?: boolean;
  canSetKey?: boolean;
  canClearKey?: boolean;
  websiteUrl?: string | null;
}

export interface AdminAgentEntry {
  agentId: string;
  sessionId?: string | null;
  workspaceHostPath?: string | null;
  createdAtMs?: number;
  ownerUserId?: number | null;
  shortName?: string | null;
  allowedUserIds?: number[];
  model?: string | null;
  isOwner?: boolean;
  isDefault?: boolean;
}

export interface AdminUserSummary {
  userId: number;
  privateChatKey: string;
  agentCount: number;
  sessionCount: number;
  defaultAgentId: string;
  currentAgentId?: string | null;
  currentSessionId?: string | null;
  currentModel?: string | null;
  currentChannelId?: string | null;
  currentChannel?: AdminChannelEntry | null;
  channelCount: number;
  customChannelCount: number;
  readyChannelCount: number;
  lastActiveMs?: number | null;
  usage24h: UsageSummary;
  usageTotal: UsageSummary;
  initialized: boolean;
}

export interface AdminOverviewResponse {
  ok: true;
  version: string;
  totals: {
    userCount: number;
    agentCount: number;
    sessionCount: number;
    channelCount: number;
  };
  usage24h: UsageSummary;
  usageTotal: UsageSummary;
}

export interface AdminUsersResponse {
  ok: true;
  users: AdminUserSummary[];
}

export interface AdminUserDetailResponse {
  ok: true;
  user: AdminUserSummary;
  agents: AdminAgentEntry[];
  channels: {
    ok: true;
    userId: number;
    currentChannelId?: string | null;
    currentChannel?: AdminChannelEntry | null;
    channels: AdminChannelEntry[];
  };
  availableModels?: string[];
  models?: Array<{ id?: string; name?: string }>;
  modelSource?: string | null;
  modelError?: string | null;
  recentUsage: UsageEventEntry[];
}

export interface UsageEventEntry {
  createdAtMs: number;
  sessionId?: string | null;
  agentId?: string | null;
  ownerUserId?: number | null;
  channelId?: string | null;
  channelName?: string | null;
  model?: string | null;
  requestedModel?: string | null;
  provider?: string | null;
  upstreamUrl?: string | null;
  responseId?: string | null;
  status?: string | null;
  error?: string | null;
  requestStream?: boolean;
  inputTokens: number;
  outputTokens: number;
  reasoningTokens: number;
  totalTokens: number;
  estimatedCostUsd?: number | null;
}

export interface AdminUsageResponse {
  ok: true;
  filters: {
    userId?: number | null;
    agentId?: string | null;
    sessionId?: string | null;
    channelId?: string | null;
    sinceMs?: number | null;
    limit: number;
  };
  summary: UsageSummary;
  events: UsageEventEntry[];
}

export async function fetchAdminOverview(wsUrl: string): Promise<AdminOverviewResponse> {
  return gatewayFetchJson<AdminOverviewResponse>(wsUrl, "/admin/overview");
}

export async function fetchAdminUsers(wsUrl: string): Promise<AdminUsersResponse> {
  return gatewayFetchJson<AdminUsersResponse>(wsUrl, "/admin/users");
}

export async function fetchAdminUserDetail(wsUrl: string, userId: number): Promise<AdminUserDetailResponse> {
  return gatewayFetchJson<AdminUserDetailResponse>(wsUrl, `/admin/users/${encodeURIComponent(String(userId))}`);
}

export async function fetchAdminUsage(wsUrl: string, query?: URLSearchParams): Promise<AdminUsageResponse> {
  const suffix = query && query.toString() ? `?${query.toString()}` : "";
  return gatewayFetchJson<AdminUsageResponse>(wsUrl, `/admin/usage${suffix}`);
}
