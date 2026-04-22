"use client";

import React from "react";
import { Plus, KeyRound, Trash2, UserRoundCheck, Pencil, Bot, RadioTower, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { Badge, EmptyState, Fact, InfoPill, InlineError, PanelCard, Skeleton } from "@/components/console-primitives";
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

function agentProvisioningState(agent: Pick<AdminAgentEntry, "provisioningState">): "pending" | "ready" | "failed" {
  const normalized = String(agent.provisioningState || "").trim().toLowerCase();
  if (normalized === "pending" || normalized === "failed") return normalized;
  return "ready";
}

function agentProvisioningTone(agent: Pick<AdminAgentEntry, "provisioningState">): "warning" | "success" | "default" {
  const state = agentProvisioningState(agent);
  if (state === "pending") return "warning";
  if (state === "failed") return "default";
  return "success";
}

function agentProvisioningLabel(agent: Pick<AdminAgentEntry, "provisioningState">): string {
  const state = agentProvisioningState(agent);
  if (state === "pending") return "provisioning";
  if (state === "failed") return "failed";
  return "ready";
}

function agentMutationToast(agent: AdminAgentEntry | null | undefined, created?: boolean, fallback = "Agent updated"): string {
  if (!agent) return fallback;
  const state = agentProvisioningState(agent);
  if (state === "pending") return "Agent provisioning started";
  if (state === "failed") return "Agent provisioning failed";
  return created === false ? "Agent already exists" : fallback;
}

export default function UsersPage() {
  const [wsUrl] = useGatewayWsUrlState();
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
  const [pendingActions, setPendingActions] = React.useState<Record<string, boolean>>({});

  const refreshUsers = React.useCallback(
    async (opts?: { preserveSelection?: boolean; notify?: boolean }) => {
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
        if (opts?.notify) {
          toast.error(message);
        }
      } finally {
        setUsersBusy(false);
      }
    },
    [wsUrl],
  );

  const setPendingAction = React.useCallback((key: string, nextValue: boolean) => {
    setPendingActions((prev) => {
      if (nextValue) {
        return { ...prev, [key]: true };
      }
      if (!prev[key]) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });
  }, []);

  const updateUserSummary = React.useCallback((userId: number, updater: (user: AdminUserSummary) => AdminUserSummary) => {
    setUsers((prev) => prev.map((user) => (user.userId === userId ? updater(user) : user)));
  }, []);

  const updateUserDetail = React.useCallback(
    (userId: number, updater: (value: AdminUserDetailResponse) => AdminUserDetailResponse) => {
      setDetail((prev) => {
        if (!prev || prev.user.userId !== userId) return prev;
        return updater(prev);
      });
    },
    [],
  );

  const refreshDetail = React.useCallback(
    async (userId: number | null, opts?: { keepVisible?: boolean }) => {
      if (!userId || userId <= 0) {
        setDetail(null);
        setDetailError(null);
        return;
      }
      setDetailBusy(true);
      setDetailError(null);
      if (!opts?.keepVisible) {
        setDetail(null);
      }
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

  const selectUser = React.useCallback(
    (userId: number) => {
      if (selectedUserId === userId && detail?.user.userId === userId) {
        return;
      }
      setSelectedUserId(userId);
      setDetailError(null);
      setDetailBusy(true);
      setDetail(null);
    },
    [detail, selectedUserId],
  );

  React.useEffect(() => {
    if (!wsUrl.trim()) return;
    const run = async () => {
      await Promise.resolve();
      await refreshUsers({ notify: false });
    };
    void run();
  }, [wsUrl, refreshUsers]);

  React.useEffect(() => {
    const run = async () => {
      await Promise.resolve();
      await refreshDetail(selectedUserId, { keepVisible: false });
    };
    void run();
  }, [selectedUserId, refreshDetail]);

  React.useEffect(() => {
    if (!selectedUserId || !detail?.agents.some((agent) => agentProvisioningState(agent) === "pending")) return;
    const timer = window.setTimeout(() => {
      void Promise.all([
        refreshUsers({ preserveSelection: true }),
        refreshDetail(selectedUserId, { keepVisible: true }),
      ]);
    }, 3000);
    return () => window.clearTimeout(timer);
  }, [detail, refreshDetail, refreshUsers, selectedUserId]);

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
      selectUser(userId);
      await refreshUsers({ preserveSelection: true });
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
      const response = await gatewayFetchJson<{ agent?: AdminAgentEntry | null; created?: boolean }>(
        wsUrl,
        `/admin/users/${selectedUserId}/agents`,
        {
          method: "POST",
          body: JSON.stringify({ name: newAgentName.trim() }),
        },
      );
      toast.success(agentMutationToast(response.agent, response.created, "Agent created"));
      setNewAgentName("");
      await Promise.all([
        refreshUsers({ preserveSelection: true }),
        refreshDetail(selectedUserId, { keepVisible: true }),
      ]);
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
      await Promise.all([
        refreshUsers({ preserveSelection: true }),
        refreshDetail(selectedUserId, { keepVisible: true }),
      ]);
    } catch (error) {
      toast.error((error as Error)?.message || String(error));
    }
  }

  async function activateAgent(agent: AdminAgentEntry): Promise<void> {
    if (!selectedUserId) return;
    if (agentProvisioningState(agent) !== "ready") {
      toast.error("Agent is not ready yet");
      return;
    }
    const actionKey = `agent-use:${agent.agentId}`;
    const previousUsers = users;
    const previousDetail = detail;
    const nextModel = modelDraftByAgent[agent.agentId]?.trim() || agent.model || detail?.user.currentModel || null;
    const nextSessionId = agent.sessionId || detail?.user.currentSessionId || null;
    setPendingAction(actionKey, true);
    updateUserSummary(selectedUserId, (user) => ({
      ...user,
      currentAgentId: agent.agentId,
      currentSessionId: nextSessionId,
      currentModel: nextModel,
    }));
    updateUserDetail(selectedUserId, (current) => ({
      ...current,
      user: {
        ...current.user,
        currentAgentId: agent.agentId,
        currentSessionId: nextSessionId,
        currentModel: nextModel,
      },
    }));
    try {
      await gatewayFetchJson(wsUrl, `/admin/users/${selectedUserId}/agents/${encodeURIComponent(agent.agentId)}/use`, {
        method: "POST",
      });
      toast.success("Current agent updated");
      await Promise.all([
        refreshUsers({ preserveSelection: true }),
        refreshDetail(selectedUserId, { keepVisible: true }),
      ]);
    } catch (error) {
      setUsers(previousUsers);
      setDetail(previousDetail);
      toast.error((error as Error)?.message || String(error));
    } finally {
      setPendingAction(actionKey, false);
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
      await Promise.all([
        refreshUsers({ preserveSelection: true }),
        refreshDetail(selectedUserId, { keepVisible: true }),
      ]);
    } catch (error) {
      toast.error((error as Error)?.message || String(error));
    }
  }

  async function retryAgent(agent: AdminAgentEntry): Promise<void> {
    if (!selectedUserId) return;
    try {
      const response = await gatewayFetchJson<{ agent?: AdminAgentEntry | null }>(
        wsUrl,
        `/admin/users/${selectedUserId}/agents/${encodeURIComponent(agent.agentId)}/retry`,
        { method: "POST" },
      );
      toast.success(agentMutationToast(response.agent, true, "Agent provisioning started"));
      await Promise.all([
        refreshUsers({ preserveSelection: true }),
        refreshDetail(selectedUserId, { keepVisible: true }),
      ]);
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
      await Promise.all([
        refreshUsers({ preserveSelection: true }),
        refreshDetail(selectedUserId, { keepVisible: true }),
      ]);
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
      await Promise.all([
        refreshUsers({ preserveSelection: true }),
        refreshDetail(selectedUserId, { keepVisible: true }),
      ]);
    } catch (error) {
      toast.error((error as Error)?.message || String(error));
    }
  }

  async function selectChannel(channel: AdminChannelEntry): Promise<void> {
    if (!selectedUserId) return;
    const actionKey = `channel-select:${channel.channelId}`;
    const previousUsers = users;
    const previousDetail = detail;
    setPendingAction(actionKey, true);
    updateUserSummary(selectedUserId, (user) => ({
      ...user,
      currentChannelId: channel.channelId,
      currentChannel: { ...(channel as AdminChannelEntry), selected: true },
    }));
    updateUserDetail(selectedUserId, (current) => ({
      ...current,
      user: {
        ...current.user,
        currentChannelId: channel.channelId,
        currentChannel: { ...(channel as AdminChannelEntry), selected: true },
      },
      channels: {
        ...current.channels,
        currentChannelId: channel.channelId,
        currentChannel: { ...(channel as AdminChannelEntry), selected: true },
        channels: current.channels.channels.map((entry) => ({
          ...entry,
          selected: entry.channelId === channel.channelId,
        })),
      },
    }));
    try {
      await gatewayFetchJson(
        wsUrl,
        `/admin/users/${selectedUserId}/channels/${encodeURIComponent(channel.channelId)}/select`,
        { method: "POST" },
      );
      toast.success("Channel selected");
      await Promise.all([
        refreshUsers({ preserveSelection: true }),
        refreshDetail(selectedUserId, { keepVisible: true }),
      ]);
    } catch (error) {
      setUsers(previousUsers);
      setDetail(previousDetail);
      toast.error((error as Error)?.message || String(error));
    } finally {
      setPendingAction(actionKey, false);
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
      await Promise.all([
        refreshUsers({ preserveSelection: true }),
        refreshDetail(selectedUserId, { keepVisible: true }),
      ]);
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
      await Promise.all([
        refreshUsers({ preserveSelection: true }),
        refreshDetail(selectedUserId, { keepVisible: true }),
      ]);
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
      await Promise.all([
        refreshUsers({ preserveSelection: true }),
        refreshDetail(selectedUserId, { keepVisible: true }),
      ]);
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
      await Promise.all([
        refreshUsers({ preserveSelection: true }),
        refreshDetail(selectedUserId, { keepVisible: true }),
      ]);
    } catch (error) {
      toast.error((error as Error)?.message || String(error));
    }
  }

  async function setBuiltinChannelAccess(
    channel: AdminChannelEntry,
    mode: "inherit" | "allow" | "deny",
  ): Promise<void> {
    if (!selectedUserId) return;
    const actionKey = `channel-access:${channel.channelId}`;
    setPendingAction(actionKey, true);
    try {
      await gatewayFetchJson(
        wsUrl,
        `/admin/users/${selectedUserId}/channels/${encodeURIComponent(channel.channelId)}/access`,
        {
          method: "PUT",
          body: JSON.stringify({ mode }),
        },
      );
      toast.success(
        mode === "allow"
          ? `${channel.name || channel.channelId} allowed`
          : mode === "deny"
            ? `${channel.name || channel.channelId} denied`
            : `${channel.name || channel.channelId} reset to default`,
      );
      await Promise.all([
        refreshUsers({ preserveSelection: true }),
        refreshDetail(selectedUserId, { keepVisible: true }),
      ]);
    } catch (error) {
      toast.error((error as Error)?.message || String(error));
    } finally {
      setPendingAction(actionKey, false);
    }
  }

  const availableModels = React.useMemo(() => {
    const fromList = detail?.availableModels?.filter(Boolean) ?? [];
    if (fromList.length) return fromList;
    const fromObjects = detail?.models?.map((item) => String(item.id || "").trim()).filter(Boolean) ?? [];
    return fromObjects;
  }, [detail]);

  const showUsersSkeleton = usersBusy && !users.length && !usersError;
  const showDetailSkeleton = Boolean(selectedUserId && detailBusy && !detail);
  const rosterTitle = users.length ? `Users (${users.length})` : "Users";

  return (
    <ConsoleShell title="Users">
      <div className="space-y-6">
        <PanelCard
          eyebrow="Fleet roster"
          title={rosterTitle}
          action={
            <div className="grid gap-2 lg:grid-cols-[auto_minmax(0,1fr)_auto]">
              <Button
                type="button"
                variant="secondary"
                disabled={usersBusy}
                onClick={() => void refreshUsers({ preserveSelection: true, notify: true })}
              >
                <RefreshCw className={cn("h-4 w-4", usersBusy ? "animate-spin" : null)} />
                Refresh
              </Button>
              <Input
                value={bootstrapUserId}
                onChange={(event) => setBootstrapUserId(event.target.value)}
                placeholder="Telegram user id"
                spellCheck={false}
              />
              <Button type="button" onClick={() => void bootstrapUser()}>
                <Plus className="h-4 w-4" />
                Bootstrap
              </Button>
            </div>
          }
        >
          {usersError ? <InlineError message={usersError} /> : null}
          {showUsersSkeleton ? (
            <UsersRosterSkeleton />
          ) : users.length ? (
            <div className="argus-table-shell rounded-[20px]">
              <table className="min-w-[980px] w-full border-collapse text-sm">
                <thead className="argus-table-head text-left text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
                  <tr>
                    <th className="px-4 py-3">User</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3">Current agent</th>
                    <th className="px-4 py-3">Channel</th>
                    <th className="px-4 py-3">Model</th>
                    <th className="px-4 py-3 text-right">Agents</th>
                    <th className="px-4 py-3 text-right">Channels</th>
                    <th className="px-4 py-3 text-right">Sessions</th>
                    <th className="px-4 py-3 text-right">24h tokens</th>
                    <th className="px-4 py-3">Last active</th>
                  </tr>
                </thead>
                <tbody>
                  {users.map((user) => {
                    const active = user.userId === selectedUserId;
                    return (
                      <tr
                        key={user.userId}
                        role="button"
                        tabIndex={0}
                        onClick={() => selectUser(user.userId)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            selectUser(user.userId);
                          }
                        }}
                        className={cn(
                          "cursor-pointer border-t border-border/60 align-top transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/30",
                          active ? "bg-primary/10" : "hover:bg-background/36",
                        )}
                      >
                        <td className="px-4 py-3">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-medium text-foreground">User {user.userId}</span>
                            {active ? <Badge tone="primary">selected</Badge> : null}
                          </div>
                          <div className="mt-1 font-mono text-[12.5px] text-muted-foreground">{user.privateChatKey}</div>
                        </td>
                        <td className="px-4 py-3">
                          <Badge tone={user.initialized ? "success" : "warning"}>
                            {user.initialized ? "initialized" : "pending"}
                          </Badge>
                        </td>
                        <td className="px-4 py-3 font-mono text-[12.5px]">{user.currentAgentId || user.defaultAgentId || "—"}</td>
                        <td className="px-4 py-3">
                          <div>{user.currentChannel?.name || user.currentChannelId || "gateway"}</div>
                          <div className="mt-1 text-[12px] text-muted-foreground">
                            {user.readyChannelCount}/{user.channelCount} ready
                          </div>
                        </td>
                        <td className="px-4 py-3 font-mono text-[12.5px]">{user.currentModel || "—"}</td>
                        <td className="px-4 py-3 text-right font-medium">{formatInt(user.agentCount)}</td>
                        <td className="px-4 py-3 text-right font-medium">{formatInt(user.channelCount)}</td>
                        <td className="px-4 py-3 text-right font-medium">{formatInt(user.sessionCount)}</td>
                        <td className="px-4 py-3 text-right font-medium">{formatCompact(user.usage24h.totalTokens)}</td>
                        <td className="px-4 py-3">
                          <div>{formatRelative(user.lastActiveMs)}</div>
                          <div className="mt-1 text-[12px] text-muted-foreground">{formatWhen(user.lastActiveMs)}</div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title="No users" />
          )}

          {selectedUserId ? (
            <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <Badge tone="default">selected user {selectedUserId}</Badge>
              <InfoPill label="users" value={String(users.length)} />
            </div>
          ) : null}
        </PanelCard>

        <section className="space-y-6">
          {usersError && !selectedUserId ? (
            <PanelCard title="User">
              <InlineError message={usersError} />
            </PanelCard>
          ) : !selectedUserId ? (
            <PanelCard title="User">
              <EmptyState title="No selection" />
            </PanelCard>
          ) : showDetailSkeleton ? (
            <UserDetailSkeleton userId={selectedUserId} />
          ) : detailError ? (
            <PanelCard title={`User ${selectedUserId}`}>
              <InlineError message={detailError} />
            </PanelCard>
          ) : detail ? (
            <>
              <PanelCard
                eyebrow="Selected user"
                title={`User ${detail.user.userId}`}
                subtitle={`Private chat ${detail.user.privateChatKey} · current channel ${detail.user.currentChannel?.name || detail.user.currentChannelId || "gateway"}`}
                className="argus-data-grid"
                action={
                  <div className="flex flex-wrap gap-2">
                    {detail.user.initialized ? <Badge tone="success">initialized</Badge> : <Badge tone="warning">pending</Badge>}
                    <Badge tone="default">{detail.user.agentCount} agents</Badge>
                    <Badge tone="default">{detail.user.channelCount} channels</Badge>
                  </div>
                }
              >
                <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                  <Fact label="24h tokens" value={formatCompact(detail.user.usage24h.totalTokens)} mono />
                  <Fact label="Total tokens" value={formatCompact(detail.user.usageTotal.totalTokens)} mono />
                  <Fact label="Current agent" value={detail.user.currentAgentId || "—"} mono />
                  <Fact label="Current session" value={detail.user.currentSessionId || "—"} mono />
                  <Fact label="Current channel" value={detail.user.currentChannel?.name || detail.user.currentChannelId || "gateway"} />
                  <Fact label="Current model" value={detail.user.currentModel || "—"} mono />
                  <Fact label="Ready channels" value={`${detail.user.readyChannelCount}/${detail.user.channelCount}`} />
                  <Fact label="Last active" value={formatRelative(detail.user.lastActiveMs)} />
                </div>
              </PanelCard>

              <div className="grid gap-4 xl:grid-cols-2">
                <PanelCard
                  eyebrow="Agent fleet"
                  title="Agents"
                  subtitle="Create dedicated agents, switch the active binding, and pin per-agent model defaults."
                  action={
                    <div className="grid gap-2 md:grid-cols-[minmax(0,1fr)_auto]">
                      <Input
                        value={newAgentName}
                        onChange={(event) => setNewAgentName(event.target.value)}
                        placeholder="new-agent"
                      />
                      <Button type="button" onClick={() => void createAgent()}>
                        <Plus className="h-4 w-4" />
                        Create agent
                      </Button>
                    </div>
                  }
                >
                  <div className="grid gap-3">
                    {detail.agents.map((agent) => (
                      <div key={agent.agentId} className="argus-row-shell rounded-[16px] px-4 py-3.5">
                        <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-2">
                              <div className="font-medium text-foreground">{agent.shortName || agent.agentId}</div>
                              {agent.isDefault ? <Badge tone="primary">main</Badge> : null}
                              {detail.user.currentAgentId === agent.agentId ? <Badge tone="success">current</Badge> : null}
                              <Badge tone={agentProvisioningTone(agent)}>{agentProvisioningLabel(agent)}</Badge>
                            </div>
                            <div className="mt-2 text-xs leading-5 text-muted-foreground">
                              session <code className="font-mono">{agent.sessionId || "—"}</code> · created {formatWhen(agent.createdAtMs)} · updated {formatWhen(agent.provisioningUpdatedAtMs || agent.lastReadyAtMs)}
                            </div>
                            {agent.provisioningError ? (
                              <div className="mt-2 text-xs leading-5 text-destructive">
                                {agent.provisioningError}
                              </div>
                            ) : null}
                          </div>

                          <div className="flex flex-wrap gap-2">
                            <Button
                              type="button"
                              size="sm"
                              variant="secondary"
                              disabled={Boolean(pendingActions[`agent-use:${agent.agentId}`]) || agentProvisioningState(agent) !== "ready"}
                              onClick={() => void activateAgent(agent)}
                            >
                              <UserRoundCheck className="h-4 w-4" />
                              {pendingActions[`agent-use:${agent.agentId}`] ? "Switching…" : "Use"}
                            </Button>
                            {agentProvisioningState(agent) === "failed" ? (
                              <Button type="button" size="sm" variant="secondary" onClick={() => void retryAgent(agent)}>
                                <RefreshCw className="h-4 w-4" />
                                Retry
                              </Button>
                            ) : null}
                            <Button
                              type="button"
                              size="sm"
                              variant="secondary"
                              disabled={agentProvisioningState(agent) === "pending"}
                              onClick={() => void renameAgent(agent)}
                            >
                              <Pencil className="h-4 w-4" />
                              Rename
                            </Button>
                            {!agent.isDefault ? (
                              <Button type="button" size="sm" variant="destructive" onClick={() => void deleteAgent(agent)}>
                                <Trash2 className="h-4 w-4" />
                                Delete
                              </Button>
                            ) : null}
                          </div>
                        </div>

                        <div className="mt-4 grid gap-2 md:grid-cols-[minmax(0,1fr)_auto]">
                          <select
                            value={modelDraftByAgent[agent.agentId] ?? agent.model ?? ""}
                            onChange={(event) =>
                              setModelDraftByAgent((prev) => ({ ...prev, [agent.agentId]: event.target.value }))
                            }
                            className="argus-select"
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
                          <Button type="button" size="sm" onClick={() => void saveAgentModel(agent)}>
                            <Bot className="h-4 w-4" />
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
                  subtitle="Switch upstreams and set gateway access."
                  action={
                    <div className="grid gap-2 md:grid-cols-2">
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
                        className="md:col-span-2"
                      />
                      <Button type="button" onClick={() => void createChannel()} className="md:col-span-2">
                        <Plus className="h-4 w-4" />
                        Create channel
                      </Button>
                    </div>
                  }
                >
                  <div className="grid gap-3">
                    {detail.channels.channels.map((channel) => (
                      <div key={channel.channelId} className="argus-row-shell rounded-[16px] px-4 py-3.5">
                        <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-2">
                              <div className="font-medium text-foreground">{channel.name}</div>
                              {channel.selected ? <Badge tone="primary">selected</Badge> : null}
                              {channel.channelId === "gateway" ? (
                                <Badge tone={gatewayAccessBadge(channel).tone}>{gatewayAccessBadge(channel).label}</Badge>
                              ) : null}
                              {channel.disabledByAdmin ? (
                                <Badge tone="warning">blocked</Badge>
                              ) : channel.ready ? (
                                <Badge tone="success">ready</Badge>
                              ) : (
                                <Badge tone="warning">needs setup</Badge>
                              )}
                              {channel.isBuiltin ? <Badge tone="default">{channel.builtinKind || "builtin"}</Badge> : null}
                            </div>
                            <div className="mt-2 text-xs leading-5 text-muted-foreground">
                              {channel.baseUrl || "Gateway-managed channel"}
                              {channel.apiKeyMasked ? ` · key ${channel.apiKeyMasked}` : ""}
                            </div>
                            {!channel.ready && channel.reason ? (
                              <div className="mt-2 text-xs leading-5 text-amber-300">{channel.reason}</div>
                            ) : null}
                          </div>

                          <div className="flex flex-wrap gap-2">
                            <Button
                              type="button"
                              size="sm"
                              variant="secondary"
                              disabled={channel.selected || !channel.ready || Boolean(pendingActions[`channel-select:${channel.channelId}`])}
                              onClick={() => void selectChannel(channel)}
                            >
                              <RadioTower className="h-4 w-4" />
                              {pendingActions[`channel-select:${channel.channelId}`] ? "Switching…" : "Select"}
                            </Button>
                            {channel.canAdminToggleAccess
                              ? gatewayAccessActions(channel).map((action) => (
                                  <Button
                                    key={`${channel.channelId}:${action.mode}`}
                                    type="button"
                                    size="sm"
                                    variant={action.variant}
                                    disabled={Boolean(pendingActions[`channel-access:${channel.channelId}`])}
                                    onClick={() => void setBuiltinChannelAccess(channel, action.mode)}
                                  >
                                    {pendingActions[`channel-access:${channel.channelId}`] ? "Saving…" : action.label}
                                  </Button>
                                ))
                              : null}
                            {channel.canRename ? (
                              <Button type="button" size="sm" variant="secondary" onClick={() => void renameChannel(channel)}>
                                <Pencil className="h-4 w-4" />
                                Rename
                              </Button>
                            ) : null}
                            {channel.canSetKey ? (
                              <Button type="button" size="sm" variant="secondary" onClick={() => void setChannelKey(channel)}>
                                <KeyRound className="h-4 w-4" />
                                Set key
                              </Button>
                            ) : null}
                            {channel.canClearKey ? (
                              <Button type="button" size="sm" variant="secondary" onClick={() => void clearChannelKey(channel)}>
                                Clear key
                              </Button>
                            ) : null}
                            {channel.canDelete ? (
                              <Button type="button" size="sm" variant="destructive" onClick={() => void deleteChannel(channel)}>
                                <Trash2 className="h-4 w-4" />
                                Delete
                              </Button>
                            ) : null}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </PanelCard>
              </div>

              <PanelCard eyebrow="Ledger" title="Usage">
                <div className="argus-table-shell rounded-[20px]">
                  <table className="w-full border-collapse text-sm">
                    <thead className="argus-table-head text-left text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
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
                          <td className="px-4 py-3 font-mono text-[12.5px]">{entry.agentId || "—"}</td>
                          <td className="px-4 py-3">{entry.channelName || entry.channelId || "—"}</td>
                          <td className="px-4 py-3 font-mono text-[12.5px]">{entry.model || entry.requestedModel || "—"}</td>
                          <td className="px-4 py-3 text-right font-medium">{formatInt(entry.totalTokens)}</td>
                          <td className="px-4 py-3">
                            <span className={cn(
                              "inline-flex items-center rounded-md border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.08em]",
                              entry.error ? "border-destructive/36 bg-destructive/10 text-destructive" : "border-emerald-500/28 bg-emerald-500/10 text-emerald-400"
                            )}>
                              {entry.error ? "error" : entry.status || "ok"}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {!detail.recentUsage.length ? (
                    <div className="px-4 py-8 text-center text-sm text-muted-foreground">No usage</div>
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

function gatewayAccessBadge(channel: AdminChannelEntry): { label: string; tone: "default" | "success" | "warning" } {
  const accessMode = channel.accessMode ?? "inherit";
  const defaultEnabled = channel.gatewayOpenaiDefaultEnabled !== false;
  if (accessMode === "allow") {
    return { label: "allow", tone: "success" };
  }
  if (accessMode === "deny") {
    return { label: "deny", tone: "warning" };
  }
  return {
    label: defaultEnabled ? "default on" : "default off",
    tone: defaultEnabled ? "default" : "warning",
  };
}

function gatewayAccessActions(
  channel: AdminChannelEntry,
): Array<{ mode: "inherit" | "allow" | "deny"; label: string; variant: "secondary" | "destructive" }> {
  const actions: Array<{ mode: "inherit" | "allow" | "deny"; label: string; variant: "secondary" | "destructive" }> = [];
  const enabled = channel.enabledForUser !== false;
  actions.push({
    mode: enabled ? "deny" : "allow",
    label: enabled ? "Deny" : "Allow",
    variant: enabled ? "destructive" : "secondary",
  });
  if ((channel.accessMode ?? "inherit") !== "inherit") {
    actions.push({
      mode: "inherit",
      label: "Default",
      variant: "secondary",
    });
  }
  return actions;
}

function UsersRosterSkeleton() {
  return (
    <div className="argus-table-shell rounded-[20px]">
      <table className="min-w-[980px] w-full border-collapse text-sm">
        <thead className="argus-table-head text-left text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
          <tr>
            {["User", "Status", "Current agent", "Channel", "Model", "Agents", "Channels", "Sessions", "24h tokens", "Last active"].map((label) => (
              <th key={label} className="px-4 py-3">
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {Array.from({ length: 6 }).map((_, index) => (
            <tr key={index} className="border-t border-border/60">
              <td className="px-4 py-3">
                <Skeleton className="h-4 w-28" />
                <Skeleton className="mt-2 h-3 w-40" />
              </td>
              <td className="px-4 py-3">
                <Skeleton className="h-6 w-24" />
              </td>
              <td className="px-4 py-3">
                <Skeleton className="h-4 w-28" />
              </td>
              <td className="px-4 py-3">
                <Skeleton className="h-4 w-24" />
                <Skeleton className="mt-2 h-3 w-16" />
              </td>
              <td className="px-4 py-3">
                <Skeleton className="h-4 w-24" />
              </td>
              <td className="px-4 py-3">
                <Skeleton className="ml-auto h-4 w-8" />
              </td>
              <td className="px-4 py-3">
                <Skeleton className="ml-auto h-4 w-8" />
              </td>
              <td className="px-4 py-3">
                <Skeleton className="ml-auto h-4 w-8" />
              </td>
              <td className="px-4 py-3">
                <Skeleton className="ml-auto h-4 w-16" />
              </td>
              <td className="px-4 py-3">
                <Skeleton className="h-4 w-20" />
                <Skeleton className="mt-2 h-3 w-28" />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function UserDetailSkeleton({ userId }: { userId: number }) {
  return (
    <>
      <PanelCard
        eyebrow="Selected user"
        title={`User ${userId}`}
        className="argus-data-grid"
      >
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {Array.from({ length: 8 }).map((_, index) => (
            <div
              key={index}
              className="rounded-[16px] border border-border/70 bg-background/24 px-3.5 py-3"
            >
              <Skeleton className="h-3 w-24 rounded-full" />
              <Skeleton className="mt-3 h-5 w-40" />
            </div>
          ))}
        </div>
      </PanelCard>

      <div className="grid gap-4 xl:grid-cols-2">
        <PanelCard eyebrow="Agent fleet" title="Agents">
          <div className="grid gap-3">
            {Array.from({ length: 2 }).map((_, index) => (
              <div
                key={index}
                className="rounded-[16px] border border-border/70 bg-background/24 px-4 py-3.5"
              >
                <Skeleton className="h-5 w-40" />
                <Skeleton className="mt-3 h-3 w-56" />
                <div className="mt-4 grid gap-2 md:grid-cols-[minmax(0,1fr)_7rem]">
                  <Skeleton className="h-10 w-full" />
                  <Skeleton className="h-10 w-full" />
                </div>
              </div>
            ))}
          </div>
        </PanelCard>

        <PanelCard eyebrow="Upstreams" title="Channels">
          <div className="grid gap-3">
            {Array.from({ length: 3 }).map((_, index) => (
              <div
                key={index}
                className="rounded-[16px] border border-border/70 bg-background/24 px-4 py-3.5"
              >
                <Skeleton className="h-5 w-32" />
                <Skeleton className="mt-3 h-3 w-64" />
                <div className="mt-4 flex flex-wrap gap-2">
                  <Skeleton className="h-8 w-24" />
                  <Skeleton className="h-8 w-36" />
                </div>
              </div>
            ))}
          </div>
        </PanelCard>
      </div>

      <PanelCard eyebrow="Ledger" title="Usage">
        <div className="space-y-3">
          {Array.from({ length: 6 }).map((_, index) => (
            <div key={index} className="grid gap-3 rounded-[16px] border border-border/70 bg-background/24 px-4 py-3 md:grid-cols-[1.1fr_0.9fr_0.9fr_1fr_0.6fr]">
              <Skeleton className="h-4 w-full" />
              <Skeleton className="h-4 w-full" />
              <Skeleton className="h-4 w-full" />
              <Skeleton className="h-4 w-full" />
              <Skeleton className="h-4 w-20" />
            </div>
          ))}
        </div>
      </PanelCard>
    </>
  );
}
