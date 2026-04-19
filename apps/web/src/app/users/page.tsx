"use client";

import React from "react";
import { RefreshCw, Plus, KeyRound, Trash2, UserRoundCheck, Pencil, Bot, RadioTower } from "lucide-react";
import { toast } from "sonner";

import { Badge, EmptyState, Fact, InfoPill, InlineError, PanelCard, StatCard } from "@/components/console-primitives";
import { ConsoleShell } from "@/components/console-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  type AdminAgentEntry,
  type AdminChannelEntry,
  type AdminUserDetailResponse,
  type AdminUserSummary,
  fetchAdminUserDetail,
  fetchAdminUsers,
} from "@/lib/admin";
import { formatCompact, formatInt, formatRelative, formatWhen } from "@/lib/format";
import { cn } from "@/lib/utils";
import { gatewayFetchJson, useGatewayWsUrlState } from "@/lib/gateway";

export default function UsersPage() {
  const [wsUrl, setWsUrl] = useGatewayWsUrlState();
  const [users, setUsers] = React.useState<AdminUserSummary[]>([]);
  const [usersBusy, setUsersBusy] = React.useState(false);
  const [usersError, setUsersError] = React.useState<string | null>(null);
  const [selectedUserId, setSelectedUserId] = React.useState<number | null>(null);
  const [detail, setDetail] = React.useState<AdminUserDetailResponse | null>(null);
  const [detailBusy, setDetailBusy] = React.useState(false);
  const [detailError, setDetailError] = React.useState<string | null>(null);
  const [bootstrapUserId, setBootstrapUserId] = React.useState("");
  const [newAgentName, setNewAgentName] = React.useState("");
  const [newChannelName, setNewChannelName] = React.useState("");
  const [newChannelBaseUrl, setNewChannelBaseUrl] = React.useState("");
  const [newChannelApiKey, setNewChannelApiKey] = React.useState("");
  const [modelDraftByAgent, setModelDraftByAgent] = React.useState<Record<string, string>>({});

  const refreshUsers = React.useCallback(
    async (opts?: { preserveSelection?: boolean }) => {
      setUsersBusy(true);
      setUsersError(null);
      try {
        const response = await fetchAdminUsers(wsUrl);
        const list = response.users ?? [];
        setUsers(list);
        setSelectedUserId((prev) => {
          if (opts?.preserveSelection && prev && list.some((item) => item.userId === prev)) return prev;
          if (prev && list.some((item) => item.userId === prev)) return prev;
          return list[0]?.userId ?? null;
        });
      } catch (error) {
        const message = (error as Error)?.message || String(error);
        setUsersError(message);
        toast.error(message);
      } finally {
        setUsersBusy(false);
      }
    },
    [wsUrl],
  );

  const refreshDetail = React.useCallback(
    async (userId: number | null) => {
      if (!userId || userId <= 0) {
        setDetail(null);
        setDetailError(null);
        return;
      }
      setDetailBusy(true);
      setDetailError(null);
      try {
        const response = await fetchAdminUserDetail(wsUrl, userId);
        setDetail(response);
        const availableModels = response.availableModels ?? response.models?.map((item) => item.id || "").filter(Boolean) ?? [];
        const nextDrafts: Record<string, string> = {};
        for (const agent of response.agents ?? []) {
          const current = String(agent.model || "").trim();
          nextDrafts[agent.agentId] = current || availableModels[0] || "";
        }
        setModelDraftByAgent(nextDrafts);
      } catch (error) {
        const message = (error as Error)?.message || String(error);
        setDetailError(message);
      } finally {
        setDetailBusy(false);
      }
    },
    [wsUrl],
  );

  React.useEffect(() => {
    if (!wsUrl.trim()) return;
    const run = async () => {
      await Promise.resolve();
      await refreshUsers();
    };
    void run();
  }, [wsUrl, refreshUsers]);

  React.useEffect(() => {
    const run = async () => {
      await Promise.resolve();
      await refreshDetail(selectedUserId);
    };
    void run();
  }, [selectedUserId, refreshDetail]);

  async function bootstrapUser(): Promise<void> {
    const value = bootstrapUserId.trim();
    const userId = Number(value);
    if (!Number.isFinite(userId) || userId <= 0) {
      toast.error("Enter a valid Telegram user id");
      return;
    }
    try {
      await gatewayFetchJson(wsUrl, `/admin/users/${encodeURIComponent(String(userId))}/bootstrap`, {
        method: "POST",
      });
      toast.success(`User ${userId} initialized`);
      setBootstrapUserId("");
      setSelectedUserId(userId);
      await refreshUsers({ preserveSelection: true });
      await refreshDetail(userId);
    } catch (error) {
      toast.error((error as Error)?.message || String(error));
    }
  }

  async function createAgent(): Promise<void> {
    if (!selectedUserId) {
      toast.error("Select a user first");
      return;
    }
    if (!newAgentName.trim()) {
      toast.error("Enter an agent name");
      return;
    }
    try {
      await gatewayFetchJson(wsUrl, `/admin/users/${selectedUserId}/agents`, {
        method: "POST",
        body: JSON.stringify({ name: newAgentName.trim() }),
      });
      toast.success("Agent created");
      setNewAgentName("");
      await refreshUsers({ preserveSelection: true });
      await refreshDetail(selectedUserId);
    } catch (error) {
      toast.error((error as Error)?.message || String(error));
    }
  }

  async function renameAgent(agent: AdminAgentEntry): Promise<void> {
    if (!selectedUserId) return;
    const nextName = window.prompt("Rename agent", agent.shortName || agent.agentId || "");
    if (!nextName) return;
    try {
      await gatewayFetchJson(wsUrl, `/admin/users/${selectedUserId}/agents/${encodeURIComponent(agent.agentId)}`, {
        method: "PATCH",
        body: JSON.stringify({ newName: nextName.trim() }),
      });
      toast.success("Agent renamed");
      await refreshUsers({ preserveSelection: true });
      await refreshDetail(selectedUserId);
    } catch (error) {
      toast.error((error as Error)?.message || String(error));
    }
  }

  async function activateAgent(agent: AdminAgentEntry): Promise<void> {
    if (!selectedUserId) return;
    try {
      await gatewayFetchJson(wsUrl, `/admin/users/${selectedUserId}/agents/${encodeURIComponent(agent.agentId)}/use`, {
        method: "POST",
      });
      toast.success("Current agent updated");
      await refreshUsers({ preserveSelection: true });
      await refreshDetail(selectedUserId);
    } catch (error) {
      toast.error((error as Error)?.message || String(error));
    }
  }

  async function deleteAgent(agent: AdminAgentEntry): Promise<void> {
    if (!selectedUserId) return;
    if (!window.confirm(`Delete agent ${agent.shortName || agent.agentId}?`)) return;
    try {
      await gatewayFetchJson(wsUrl, `/admin/users/${selectedUserId}/agents/${encodeURIComponent(agent.agentId)}`, {
        method: "DELETE",
      });
      toast.success("Agent deleted");
      await refreshUsers({ preserveSelection: true });
      await refreshDetail(selectedUserId);
    } catch (error) {
      toast.error((error as Error)?.message || String(error));
    }
  }

  async function saveAgentModel(agent: AdminAgentEntry): Promise<void> {
    if (!selectedUserId) return;
    const model = modelDraftByAgent[agent.agentId]?.trim();
    if (!model) {
      toast.error("Choose a model");
      return;
    }
    try {
      await gatewayFetchJson(wsUrl, `/admin/users/${selectedUserId}/agents/${encodeURIComponent(agent.agentId)}/model`, {
        method: "POST",
        body: JSON.stringify({ model }),
      });
      toast.success("Model updated");
      await refreshUsers({ preserveSelection: true });
      await refreshDetail(selectedUserId);
    } catch (error) {
      toast.error((error as Error)?.message || String(error));
    }
  }

  async function createChannel(): Promise<void> {
    if (!selectedUserId) {
      toast.error("Select a user first");
      return;
    }
    if (!newChannelName.trim() || !newChannelBaseUrl.trim() || !newChannelApiKey.trim()) {
      toast.error("Name, base URL, and API key are required");
      return;
    }
    try {
      await gatewayFetchJson(wsUrl, `/admin/users/${selectedUserId}/channels`, {
        method: "POST",
        body: JSON.stringify({
          name: newChannelName.trim(),
          baseUrl: newChannelBaseUrl.trim(),
          apiKey: newChannelApiKey.trim(),
        }),
      });
      toast.success("Channel created");
      setNewChannelName("");
      setNewChannelBaseUrl("");
      setNewChannelApiKey("");
      await refreshUsers({ preserveSelection: true });
      await refreshDetail(selectedUserId);
    } catch (error) {
      toast.error((error as Error)?.message || String(error));
    }
  }

  async function selectChannel(channel: AdminChannelEntry): Promise<void> {
    if (!selectedUserId) return;
    try {
      await gatewayFetchJson(
        wsUrl,
        `/admin/users/${selectedUserId}/channels/${encodeURIComponent(channel.channelId)}/select`,
        { method: "POST" },
      );
      toast.success("Channel selected");
      await refreshUsers({ preserveSelection: true });
      await refreshDetail(selectedUserId);
    } catch (error) {
      toast.error((error as Error)?.message || String(error));
    }
  }

  async function renameChannel(channel: AdminChannelEntry): Promise<void> {
    if (!selectedUserId) return;
    const nextName = window.prompt("Rename channel", channel.name || channel.channelId || "");
    if (!nextName) return;
    try {
      await gatewayFetchJson(
        wsUrl,
        `/admin/users/${selectedUserId}/channels/${encodeURIComponent(channel.channelId)}`,
        {
          method: "PATCH",
          body: JSON.stringify({ newName: nextName.trim() }),
        },
      );
      toast.success("Channel renamed");
      await refreshUsers({ preserveSelection: true });
      await refreshDetail(selectedUserId);
    } catch (error) {
      toast.error((error as Error)?.message || String(error));
    }
  }

  async function deleteChannel(channel: AdminChannelEntry): Promise<void> {
    if (!selectedUserId) return;
    if (!window.confirm(`Delete channel ${channel.name || channel.channelId}?`)) return;
    try {
      await gatewayFetchJson(
        wsUrl,
        `/admin/users/${selectedUserId}/channels/${encodeURIComponent(channel.channelId)}`,
        { method: "DELETE" },
      );
      toast.success("Channel deleted");
      await refreshUsers({ preserveSelection: true });
      await refreshDetail(selectedUserId);
    } catch (error) {
      toast.error((error as Error)?.message || String(error));
    }
  }

  async function setChannelKey(channel: AdminChannelEntry): Promise<void> {
    if (!selectedUserId) return;
    const nextKey = window.prompt(`Set API key for ${channel.name || channel.channelId}`, "");
    if (!nextKey) return;
    try {
      await gatewayFetchJson(
        wsUrl,
        `/admin/users/${selectedUserId}/channels/${encodeURIComponent(channel.channelId)}/key`,
        {
          method: "PUT",
          body: JSON.stringify({ apiKey: nextKey.trim() }),
        },
      );
      toast.success("Key updated");
      await refreshUsers({ preserveSelection: true });
      await refreshDetail(selectedUserId);
    } catch (error) {
      toast.error((error as Error)?.message || String(error));
    }
  }

  async function clearChannelKey(channel: AdminChannelEntry): Promise<void> {
    if (!selectedUserId) return;
    if (!window.confirm(`Clear API key for ${channel.name || channel.channelId}?`)) return;
    try {
      await gatewayFetchJson(
        wsUrl,
        `/admin/users/${selectedUserId}/channels/${encodeURIComponent(channel.channelId)}/key`,
        { method: "DELETE" },
      );
      toast.success("Key cleared");
      await refreshUsers({ preserveSelection: true });
      await refreshDetail(selectedUserId);
    } catch (error) {
      toast.error((error as Error)?.message || String(error));
    }
  }

  const availableModels = React.useMemo(() => {
    const fromList = detail?.availableModels?.filter(Boolean) ?? [];
    if (fromList.length) return fromList;
    const fromObjects = detail?.models?.map((item) => String(item.id || "").trim()).filter(Boolean) ?? [];
    return fromObjects;
  }, [detail]);

  return (
    <ConsoleShell
      title="Users"
      subtitle="Manage Telegram-backed users, their agent fleets, API channels, and recent activity from one operator view."
      actions={
        <div className="flex flex-col gap-2 md:items-end">
          <div className="flex flex-wrap items-center gap-2">
            <Input
              value={wsUrl}
              onChange={(event) => setWsUrl(event.target.value)}
              className="w-[320px]"
              placeholder="Gateway wss://.../ws"
              spellCheck={false}
            />
            <Button type="button" variant="secondary" onClick={() => void refreshUsers()} disabled={usersBusy}>
              <RefreshCw className={cn("mr-2 h-4 w-4", usersBusy && "animate-spin")} />
              Refresh
            </Button>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Input
              value={bootstrapUserId}
              onChange={(event) => setBootstrapUserId(event.target.value)}
              className="w-[160px]"
              placeholder="Telegram user id"
              spellCheck={false}
            />
            <Button type="button" onClick={() => void bootstrapUser()}>
              <Plus className="mr-2 h-4 w-4" />
              Bootstrap user
            </Button>
          </div>
        </div>
      }
    >
      <div className="grid gap-6 xl:grid-cols-[360px_1fr]">
        <section className="space-y-4">
          <PanelCard eyebrow="Fleet roster" title="Users" subtitle={usersBusy ? "Refreshing…" : `${users.length} tracked users`}>
            {usersError ? <InlineError message={usersError} /> : null}
            <div className="space-y-2">
              {users.map((user) => {
                const active = user.userId === selectedUserId;
                return (
                  <button
                    key={user.userId}
                    type="button"
                    onClick={() => setSelectedUserId(user.userId)}
                    className={cn(
                      "w-full rounded-[22px] border px-4 py-4 text-left transition-colors",
                      active
                        ? "border-primary/25 bg-primary/10"
                        : "border-border/60 bg-background/30 hover:border-border hover:bg-background/55"
                    )}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="font-medium text-foreground">User {user.userId}</div>
                        <div className="mt-1 text-xs text-muted-foreground">
                          {user.currentChannel?.name || user.currentChannelId || "gateway"} · {user.currentModel || "gpt-5.4"}
                        </div>
                      </div>
                      <span className="rounded-full border border-border/60 px-2 py-0.5 text-[11px] text-muted-foreground">
                        {formatCompact(user.usage24h.totalTokens)} tok/24h
                      </span>
                    </div>
                    <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-muted-foreground">
                      <InfoPill label="agents" value={String(user.agentCount)} />
                      <InfoPill label="channels" value={String(user.channelCount)} />
                      <InfoPill label="last active" value={formatRelative(user.lastActiveMs)} />
                    </div>
                  </button>
                );
              })}
              {!users.length && !usersError ? (
                <EmptyState title="No users yet" body="Bootstrap a Telegram user id to create the first managed account." />
              ) : null}
            </div>
          </PanelCard>
        </section>

        <section className="space-y-6">
          {!selectedUserId ? (
            <PanelCard title="User detail" subtitle="Select a user to inspect agents, channels, and usage.">
              <EmptyState title="Nothing selected" body="Pick a user from the list on the left." />
            </PanelCard>
          ) : detailBusy && !detail ? (
            <PanelCard title="Loading user" subtitle={`Fetching ${selectedUserId}…`}>
              <EmptyState title="Loading…" body="Fetching user detail and recent usage." />
            </PanelCard>
          ) : detailError ? (
            <PanelCard title="User detail" subtitle={`User ${selectedUserId}`}>
              <InlineError message={detailError} />
            </PanelCard>
          ) : detail ? (
            <>
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                <StatCard
                  label="24h tokens"
                  value={formatCompact(detail.user.usage24h.totalTokens)}
                  tone="primary"
                  hint="Rolling activity window for this operator-managed user."
                />
                <StatCard label="Total tokens" value={formatCompact(detail.user.usageTotal.totalTokens)} hint="All recorded requests for this user." />
                <StatCard label="Agents" value={String(detail.user.agentCount)} hint="Dedicated runtime bindings or persistent personas." />
                <StatCard label="Last active" value={formatRelative(detail.user.lastActiveMs)} hint="Most recent observed interaction." />
              </div>

              <PanelCard
                eyebrow="Selected user"
                title={`User ${detail.user.userId}`}
                subtitle={`Private chat ${detail.user.privateChatKey} · current channel ${detail.user.currentChannel?.name || detail.user.currentChannelId || "gateway"}`}
                className="argus-data-grid"
              >
                <div className="grid gap-4 lg:grid-cols-2">
                  <Fact label="Current agent" value={detail.user.currentAgentId || "—"} mono />
                  <Fact label="Current session" value={detail.user.currentSessionId || "—"} mono />
                  <Fact label="Ready channels" value={`${detail.user.readyChannelCount}/${detail.user.channelCount}`} />
                  <Fact label="Current model" value={detail.user.currentModel || "—"} mono />
                </div>
              </PanelCard>

              <PanelCard
                eyebrow="Agent fleet"
                title="Agents"
                subtitle="Create dedicated agents, move the user's default binding, and pin per-agent model defaults."
                action={
                  <div className="flex flex-wrap items-center gap-2">
                    <Input
                      value={newAgentName}
                      onChange={(event) => setNewAgentName(event.target.value)}
                      className="w-[180px]"
                      placeholder="new-agent"
                    />
                    <Button type="button" onClick={() => void createAgent()}>
                      <Plus className="mr-2 h-4 w-4" />
                      Create agent
                    </Button>
                  </div>
                }
              >
                <div className="grid gap-3">
                  {detail.agents.map((agent) => (
                    <div
                      key={agent.agentId}
                      className="rounded-[24px] border border-border/60 bg-background/30 p-4 shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.04)]"
                    >
                      <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <div className="font-medium text-foreground">{agent.shortName || agent.agentId}</div>
                            {agent.isDefault ? <Badge tone="primary">main</Badge> : null}
                            {detail.user.currentAgentId === agent.agentId ? <Badge tone="success">current</Badge> : null}
                          </div>
                          <div className="mt-2 text-xs text-muted-foreground">
                            session <code className="font-mono">{agent.sessionId || "—"}</code> · created {formatWhen(agent.createdAtMs)}
                          </div>
                        </div>

                        <div className="flex flex-wrap gap-2">
                          <Button type="button" variant="secondary" onClick={() => void activateAgent(agent)}>
                            <UserRoundCheck className="mr-2 h-4 w-4" />
                            Use
                          </Button>
                          <Button type="button" variant="secondary" onClick={() => void renameAgent(agent)}>
                            <Pencil className="mr-2 h-4 w-4" />
                            Rename
                          </Button>
                          {!agent.isDefault ? (
                            <Button type="button" variant="destructive" onClick={() => void deleteAgent(agent)}>
                              <Trash2 className="mr-2 h-4 w-4" />
                              Delete
                            </Button>
                          ) : null}
                        </div>
                      </div>

                      <div className="mt-4 flex flex-col gap-2 md:flex-row md:items-center">
                        <select
                          value={modelDraftByAgent[agent.agentId] ?? agent.model ?? ""}
                          onChange={(event) =>
                            setModelDraftByAgent((prev) => ({ ...prev, [agent.agentId]: event.target.value }))
                          }
                          className={cn(
                            "h-11 min-w-[220px] rounded-2xl border border-border/60 bg-background/60 px-3 text-sm text-foreground outline-none",
                            "focus:border-primary/40 focus:ring-4 focus:ring-ring/20"
                          )}
                        >
                          {availableModels.length ? (
                            availableModels.map((model) => (
                              <option key={model} value={model}>
                                {model}
                              </option>
                            ))
                          ) : (
                            <option value={agent.model || ""}>{agent.model || "gpt-5.4"}</option>
                          )}
                        </select>
                        <Button type="button" onClick={() => void saveAgentModel(agent)}>
                          <Bot className="mr-2 h-4 w-4" />
                          Save model
                        </Button>
                      </div>
                    </div>
                  ))}
                </div>
              </PanelCard>

              <PanelCard
                eyebrow="Upstreams"
                title="Channels"
                subtitle="Switch the current upstream, rotate per-user keys, and keep the builtin gateway as fallback."
                action={
                  <div className="grid gap-2 md:grid-cols-[160px_220px_220px_auto]">
                    <Input
                      value={newChannelName}
                      onChange={(event) => setNewChannelName(event.target.value)}
                      placeholder="channel name"
                    />
                    <Input
                      value={newChannelBaseUrl}
                      onChange={(event) => setNewChannelBaseUrl(event.target.value)}
                      placeholder="https://provider/v1"
                    />
                    <Input
                      value={newChannelApiKey}
                      onChange={(event) => setNewChannelApiKey(event.target.value)}
                      placeholder="api key"
                      type="password"
                    />
                    <Button type="button" onClick={() => void createChannel()}>
                      <Plus className="mr-2 h-4 w-4" />
                      Create channel
                    </Button>
                  </div>
                }
              >
                <div className="grid gap-3">
                  {detail.channels.channels.map((channel) => (
                    <div key={channel.channelId} className="rounded-[24px] border border-border/60 bg-background/30 p-4 shadow-[inset_0_1px_0_0_oklch(var(--foreground)/0.04)]">
                      <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <div className="font-medium text-foreground">{channel.name}</div>
                            {channel.selected ? <Badge tone="primary">selected</Badge> : null}
                            {channel.ready ? <Badge tone="success">ready</Badge> : <Badge tone="warning">needs setup</Badge>}
                            {channel.isBuiltin ? <Badge tone="default">{channel.builtinKind || "builtin"}</Badge> : null}
                          </div>
                          <div className="mt-2 truncate text-xs text-muted-foreground">
                            {channel.baseUrl || "Gateway-managed channel"} {channel.apiKeyMasked ? `· key ${channel.apiKeyMasked}` : ""}
                          </div>
                          {!channel.ready && channel.reason ? (
                            <div className="mt-2 text-xs text-amber-400">{channel.reason}</div>
                          ) : null}
                        </div>

                        <div className="flex flex-wrap gap-2">
                          <Button type="button" variant="secondary" disabled={channel.selected} onClick={() => void selectChannel(channel)}>
                            <RadioTower className="mr-2 h-4 w-4" />
                            Select
                          </Button>
                          {channel.canRename ? (
                            <Button type="button" variant="secondary" onClick={() => void renameChannel(channel)}>
                              <Pencil className="mr-2 h-4 w-4" />
                              Rename
                            </Button>
                          ) : null}
                          {channel.canSetKey ? (
                            <Button type="button" variant="secondary" onClick={() => void setChannelKey(channel)}>
                              <KeyRound className="mr-2 h-4 w-4" />
                              Set key
                            </Button>
                          ) : null}
                          {channel.canClearKey ? (
                            <Button type="button" variant="secondary" onClick={() => void clearChannelKey(channel)}>
                              Clear key
                            </Button>
                          ) : null}
                          {channel.canDelete ? (
                            <Button type="button" variant="destructive" onClick={() => void deleteChannel(channel)}>
                              <Trash2 className="mr-2 h-4 w-4" />
                              Delete
                            </Button>
                          ) : null}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </PanelCard>

              <PanelCard eyebrow="Ledger" title="Recent usage" subtitle="Last 100 Responses calls attributed to this user.">
                <div className="argus-table-shell rounded-[24px]">
                  <table className="w-full border-collapse text-sm">
                    <thead className="argus-table-head text-left text-xs uppercase tracking-[0.22em] text-muted-foreground">
                      <tr>
                        <th className="px-4 py-3">When</th>
                        <th className="px-4 py-3">Agent</th>
                        <th className="px-4 py-3">Channel</th>
                        <th className="px-4 py-3">Model</th>
                        <th className="px-4 py-3 text-right">Tokens</th>
                        <th className="px-4 py-3">Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.recentUsage.map((entry) => (
                        <tr
                          key={[
                            entry.createdAtMs,
                            entry.responseId || "",
                            entry.sessionId || "",
                            entry.agentId || "",
                            entry.channelId || "",
                            entry.model || entry.requestedModel || "",
                          ].join(":")}
                          className="border-t border-border/60"
                        >
                          <td className="px-4 py-3 text-muted-foreground">{formatWhen(entry.createdAtMs)}</td>
                          <td className="px-4 py-3 font-mono text-[13px]">{entry.agentId || "—"}</td>
                          <td className="px-4 py-3">{entry.channelName || entry.channelId || "—"}</td>
                          <td className="px-4 py-3 font-mono text-[13px]">{entry.model || entry.requestedModel || "—"}</td>
                          <td className="px-4 py-3 text-right font-medium">{formatInt(entry.totalTokens)}</td>
                          <td className="px-4 py-3">
                            <span className={cn(
                              "rounded-full border px-2 py-1 text-[11px]",
                              entry.error ? "border-destructive/40 bg-destructive/10 text-destructive" : "border-emerald-500/30 bg-emerald-500/10 text-emerald-400"
                            )}>
                              {entry.error ? "error" : entry.status || "ok"}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {!detail.recentUsage.length ? (
                    <div className="px-4 py-8 text-center text-sm text-muted-foreground">No recorded usage yet.</div>
                  ) : null}
                </div>
              </PanelCard>
            </>
          ) : null}
        </section>
      </div>
    </ConsoleShell>
  );
}
