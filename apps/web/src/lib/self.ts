import { type AdminAgentEntry, type AdminChannelEntry, type UsageEventEntry, type UsageSummary } from "@/lib/admin";
import type { ConsoleUser } from "@/lib/auth";
import { gatewayFetchJson } from "@/lib/gateway";

export interface DeveloperLimits {
  maxApiKeys?: number | null;
  maxAgents?: number | null;
  maxManagedSessions?: number | null;
  apiRequestsPerMinute?: number | null;
  monthlyTokenQuota?: number | null;
}

export interface DeveloperCounts {
  apiKeys: number;
  agents: number;
  sessions: number;
}

export interface DeveloperKeyEntry {
  keyId: string;
  name: string;
  tokenPreview?: string | null;
  createdAtMs?: number | null;
  updatedAtMs?: number | null;
  lastUsedAtMs?: number | null;
  expiresAtMs?: number | null;
  revokedAtMs?: number | null;
  active?: boolean;
}

export interface SelfDeveloperKeysResponse {
  ok: true;
  userId: number;
  keys: DeveloperKeyEntry[];
  limits: DeveloperLimits;
  counts: DeveloperCounts;
  issuedKey?: (DeveloperKeyEntry & { token: string }) | null;
}

export interface SelfAgentsResponse {
  ok: true;
  userId: number;
  agents: AdminAgentEntry[];
  agent?: AdminAgentEntry | null;
  currentAgentId?: string | null;
  currentSessionId?: string | null;
  currentAgent?: AdminAgentEntry | null;
  currentChannelId?: string | null;
  currentChannel?: AdminChannelEntry | null;
  availableModels?: string[];
  models?: Array<{ id?: string; name?: string }>;
  modelSource?: string | null;
  modelError?: string | null;
  createdMain?: boolean;
  liveModelSynced?: boolean;
  limits: DeveloperLimits;
  counts: DeveloperCounts;
  deletedAgentId?: string | null;
}

export interface SelfAgentConnectionResponse {
  ok: true;
  userId: number;
  agentId: string;
  agent: AdminAgentEntry;
  sessionId: string;
  provider?: string | null;
  createdSession?: boolean;
  model?: string | null;
  currentChannelId?: string | null;
  currentChannel?: AdminChannelEntry | null;
  placement?: Record<string, unknown>;
  gatewayBaseUrl?: string | null;
  ws: {
    path: string;
    url: string;
    sessionId: string;
    requiresBearerToken?: boolean;
  };
  mcp?: {
    path?: string;
    url?: string;
    token?: string | null;
  };
  node?: {
    path?: string;
    url?: string;
    token?: string | null;
  };
  openai?: {
    path?: string;
    url?: string;
    token?: string | null;
  };
}

export interface SelfChannelsResponse {
  ok: true;
  userId: number;
  currentChannelId?: string | null;
  currentChannel?: AdminChannelEntry | null;
  channels: AdminChannelEntry[];
}

export interface SelfUsageResponse {
  ok: true;
  user: ConsoleUser;
  filters: {
    userId: number;
    agentId?: string | null;
    sessionId?: string | null;
    channelId?: string | null;
    sinceMs?: number | null;
    limit: number;
  };
  summary: UsageSummary;
  events: UsageEventEntry[];
}

export function fetchMyChannels(wsUrl: string): Promise<SelfChannelsResponse> {
  return gatewayFetchJson<SelfChannelsResponse>(wsUrl, "/me/channels");
}

export function fetchMyDeveloperKeys(wsUrl: string): Promise<SelfDeveloperKeysResponse> {
  return gatewayFetchJson<SelfDeveloperKeysResponse>(wsUrl, "/me/developer-keys");
}

export function createMyDeveloperKey(
  wsUrl: string,
  body: { name: string }
): Promise<SelfDeveloperKeysResponse> {
  return gatewayFetchJson<SelfDeveloperKeysResponse>(wsUrl, "/me/developer-keys", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function revokeMyDeveloperKey(wsUrl: string, keyId: string): Promise<SelfDeveloperKeysResponse> {
  return gatewayFetchJson<SelfDeveloperKeysResponse>(wsUrl, `/me/developer-keys/${encodeURIComponent(keyId)}`, {
    method: "DELETE",
  });
}

export function fetchMyAgents(wsUrl: string): Promise<SelfAgentsResponse> {
  return gatewayFetchJson<SelfAgentsResponse>(wsUrl, "/me/agents");
}

export function createMyAgent(
  wsUrl: string,
  body: { name: string }
): Promise<SelfAgentsResponse> {
  return gatewayFetchJson<SelfAgentsResponse>(wsUrl, "/me/agents", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function activateMyAgent(wsUrl: string, agentId: string): Promise<SelfAgentsResponse> {
  return gatewayFetchJson<SelfAgentsResponse>(wsUrl, `/me/agents/${encodeURIComponent(agentId)}/use`, {
    method: "POST",
  });
}

export function renameMyAgent(
  wsUrl: string,
  agentId: string,
  body: { name: string }
): Promise<SelfAgentsResponse> {
  return gatewayFetchJson<SelfAgentsResponse>(wsUrl, `/me/agents/${encodeURIComponent(agentId)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteMyAgent(wsUrl: string, agentId: string): Promise<SelfAgentsResponse> {
  return gatewayFetchJson<SelfAgentsResponse>(wsUrl, `/me/agents/${encodeURIComponent(agentId)}`, {
    method: "DELETE",
  });
}

export function setMyAgentModel(
  wsUrl: string,
  agentId: string,
  body: { model: string }
): Promise<SelfAgentsResponse> {
  return gatewayFetchJson<SelfAgentsResponse>(wsUrl, `/me/agents/${encodeURIComponent(agentId)}/model`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function fetchMyAgentConnection(
  wsUrl: string,
  agentId: string
): Promise<SelfAgentConnectionResponse> {
  return gatewayFetchJson<SelfAgentConnectionResponse>(wsUrl, `/me/agents/${encodeURIComponent(agentId)}/connection`);
}

export function createMyChannel(
  wsUrl: string,
  body: { name: string; baseUrl: string; apiKey: string }
): Promise<SelfChannelsResponse & { channel?: AdminChannelEntry | null }> {
  return gatewayFetchJson<SelfChannelsResponse & { channel?: AdminChannelEntry | null }>(wsUrl, "/me/channels", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function renameMyChannel(
  wsUrl: string,
  channelId: string,
  body: { name: string }
): Promise<SelfChannelsResponse & { channel?: AdminChannelEntry | null }> {
  return gatewayFetchJson<SelfChannelsResponse & { channel?: AdminChannelEntry | null }>(
    wsUrl,
    `/me/channels/${encodeURIComponent(channelId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(body),
    }
  );
}

export function deleteMyChannel(
  wsUrl: string,
  channelId: string
): Promise<SelfChannelsResponse & { deletedChannelId?: string | null }> {
  return gatewayFetchJson<SelfChannelsResponse & { deletedChannelId?: string | null }>(
    wsUrl,
    `/me/channels/${encodeURIComponent(channelId)}`,
    {
      method: "DELETE",
    }
  );
}

export function selectMyChannel(wsUrl: string, channelId: string): Promise<SelfChannelsResponse> {
  return gatewayFetchJson<SelfChannelsResponse>(wsUrl, `/me/channels/${encodeURIComponent(channelId)}/select`, {
    method: "POST",
  });
}

export function setMyChannelKey(
  wsUrl: string,
  channelId: string,
  body: { apiKey: string }
): Promise<SelfChannelsResponse & { channelId?: string | null; channel?: AdminChannelEntry | null }> {
  return gatewayFetchJson<SelfChannelsResponse & { channelId?: string | null; channel?: AdminChannelEntry | null }>(
    wsUrl,
    `/me/channels/${encodeURIComponent(channelId)}/key`,
    {
      method: "PUT",
      body: JSON.stringify(body),
    }
  );
}

export function clearMyChannelKey(
  wsUrl: string,
  channelId: string
): Promise<SelfChannelsResponse & { channelId?: string | null; channel?: AdminChannelEntry | null }> {
  return gatewayFetchJson<SelfChannelsResponse & { channelId?: string | null; channel?: AdminChannelEntry | null }>(
    wsUrl,
    `/me/channels/${encodeURIComponent(channelId)}/key`,
    {
      method: "DELETE",
    }
  );
}

export function fetchMyUsage(wsUrl: string, query?: URLSearchParams): Promise<SelfUsageResponse> {
  const suffix = query && query.toString() ? `?${query.toString()}` : "";
  return gatewayFetchJson<SelfUsageResponse>(wsUrl, `/me/usage${suffix}`);
}
