import { type AdminChannelEntry, type UsageEventEntry, type UsageSummary } from "@/lib/admin";
import type { ConsoleUser } from "@/lib/auth";
import { gatewayFetchJson } from "@/lib/gateway";

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
