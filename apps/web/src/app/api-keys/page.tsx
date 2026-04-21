"use client";

import * as React from "react";
import { Check, KeyRound, Pencil, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { useAuth } from "@/components/admin-gate";
import { Badge, EmptyState, Fact, InlineError, PanelCard } from "@/components/console-primitives";
import { ConsoleShell } from "@/components/console-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { AdminChannelEntry } from "@/lib/admin";
import { useGatewayWsUrlState } from "@/lib/gateway";
import {
  clearMyChannelKey,
  createMyChannel,
  deleteMyChannel,
  fetchMyChannels,
  renameMyChannel,
  selectMyChannel,
  setMyChannelKey,
  type SelfChannelsResponse,
} from "@/lib/self";
import { cn } from "@/lib/utils";

function channelTone(channel: AdminChannelEntry): "primary" | "success" | "warning" | "default" {
  if (channel.selected) return "primary";
  if (channel.ready) return "success";
  if (channel.hasApiKey) return "warning";
  return "default";
}

function channelKind(channel: AdminChannelEntry): string {
  return channel.builtinKind || channel.kind || (channel.isBuiltin ? "builtin" : "custom");
}

export default function ApiKeysPage() {
  const { user } = useAuth();
  const [wsUrl] = useGatewayWsUrlState();
  const [channelsState, setChannelsState] = React.useState<SelfChannelsResponse | null>(null);
  const [selectedChannelId, setSelectedChannelId] = React.useState("");
  const selectedChannelIdRef = React.useRef("");
  const [loading, setLoading] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const [nameDraft, setNameDraft] = React.useState("");
  const [keyDraft, setKeyDraft] = React.useState("");
  const [newName, setNewName] = React.useState("");
  const [newBaseUrl, setNewBaseUrl] = React.useState("");
  const [newApiKey, setNewApiKey] = React.useState("");

  const channels = React.useMemo(() => channelsState?.channels ?? [], [channelsState?.channels]);
  const currentChannelId = channelsState?.currentChannelId ?? "";
  const readyCount = channels.filter((channel) => channel.ready).length;
  const customCount = channels.filter((channel) => !channel.isBuiltin).length;
  const keyedCount = channels.filter((channel) => channel.hasApiKey).length;

  const selectedChannel = React.useMemo(() => {
    if (selectedChannelId) {
      const direct = channels.find((channel) => channel.channelId === selectedChannelId);
      if (direct) return direct;
    }
    if (currentChannelId) {
      const current = channels.find((channel) => channel.channelId === currentChannelId);
      if (current) return current;
    }
    return channels[0] ?? null;
  }, [channels, currentChannelId, selectedChannelId]);

  const applyChannels = React.useCallback(
    (nextState: SelfChannelsResponse, preferredChannelId?: string | null) => {
      const candidates = [
        preferredChannelId,
        selectedChannelIdRef.current,
        nextState.currentChannelId,
        nextState.channels[0]?.channelId,
      ];
      let nextSelectedId = "";
      for (const candidate of candidates) {
        if (!candidate) continue;
        if (nextState.channels.some((channel) => channel.channelId === candidate)) {
          nextSelectedId = candidate;
          break;
        }
      }
      const nextSelectedChannel = nextState.channels.find((channel) => channel.channelId === nextSelectedId) ?? null;
      selectedChannelIdRef.current = nextSelectedId;
      setChannelsState(nextState);
      setSelectedChannelId(nextSelectedId);
      setNameDraft(nextSelectedChannel?.name ?? "");
      setKeyDraft("");
    },
    []
  );

  const focusChannel = React.useCallback((channel: AdminChannelEntry) => {
    selectedChannelIdRef.current = channel.channelId;
    setSelectedChannelId(channel.channelId);
    setNameDraft(channel.name);
    setKeyDraft("");
  }, []);

  const refresh = React.useCallback(
    async (opts?: { notify?: boolean }) => {
      if (!wsUrl.trim()) return;
      setLoading(true);
      setError(null);
      try {
        const nextState = await fetchMyChannels(wsUrl);
        applyChannels(nextState);
        if (opts?.notify) {
          toast.success("Refreshed");
        }
      } catch (nextError) {
        const message = (nextError as Error)?.message || String(nextError);
        setError(message);
        if (opts?.notify) {
          toast.error(message);
        }
      } finally {
        setLoading(false);
      }
    },
    [applyChannels, wsUrl]
  );

  React.useEffect(() => {
    if (!wsUrl.trim()) return;
    const run = async () => {
      await Promise.resolve();
      await refresh({ notify: false });
    };
    void run();
  }, [refresh, wsUrl]);

  const createChannel = React.useCallback(async () => {
    if (!wsUrl.trim()) return;
    if (!newName.trim() || !newBaseUrl.trim() || !newApiKey.trim()) {
      toast.error("Name, base URL, and API key are required");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const result = await createMyChannel(wsUrl, {
        name: newName,
        baseUrl: newBaseUrl,
        apiKey: newApiKey,
      });
      applyChannels(result, result.channel?.channelId || null);
      setNewName("");
      setNewBaseUrl("");
      setNewApiKey("");
      toast.success("Channel added");
    } catch (nextError) {
      const message = (nextError as Error)?.message || String(nextError);
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  }, [applyChannels, newApiKey, newBaseUrl, newName, wsUrl]);

  const selectChannel = React.useCallback(async () => {
    if (!selectedChannel || !wsUrl.trim() || selectedChannel.selected) return;
    setSaving(true);
    setError(null);
    try {
      const result = await selectMyChannel(wsUrl, selectedChannel.channelId);
      applyChannels(result, selectedChannel.channelId);
      toast.success("Current channel updated");
    } catch (nextError) {
      const message = (nextError as Error)?.message || String(nextError);
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  }, [applyChannels, selectedChannel, wsUrl]);

  const saveName = React.useCallback(async () => {
    if (!selectedChannel || !selectedChannel.canRename || !wsUrl.trim()) return;
    if (!nameDraft.trim()) {
      toast.error("Name is required");
      return;
    }
    if (nameDraft.trim() === selectedChannel.name) return;
    setSaving(true);
    setError(null);
    try {
      const result = await renameMyChannel(wsUrl, selectedChannel.channelId, { name: nameDraft });
      applyChannels(result, selectedChannel.channelId);
      toast.success("Name updated");
    } catch (nextError) {
      const message = (nextError as Error)?.message || String(nextError);
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  }, [applyChannels, nameDraft, selectedChannel, wsUrl]);

  const saveKey = React.useCallback(async () => {
    if (!selectedChannel || !selectedChannel.canSetKey || !wsUrl.trim()) return;
    if (!keyDraft.trim()) {
      toast.error("API key is required");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const result = await setMyChannelKey(wsUrl, selectedChannel.channelId, { apiKey: keyDraft });
      applyChannels(result, selectedChannel.channelId);
      setKeyDraft("");
      toast.success("Key updated");
    } catch (nextError) {
      const message = (nextError as Error)?.message || String(nextError);
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  }, [applyChannels, keyDraft, selectedChannel, wsUrl]);

  const clearKey = React.useCallback(async () => {
    if (!selectedChannel || !selectedChannel.canClearKey || !wsUrl.trim()) return;
    setSaving(true);
    setError(null);
    try {
      const result = await clearMyChannelKey(wsUrl, selectedChannel.channelId);
      applyChannels(result, selectedChannel.channelId);
      setKeyDraft("");
      toast.success("Key cleared");
    } catch (nextError) {
      const message = (nextError as Error)?.message || String(nextError);
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  }, [applyChannels, selectedChannel, wsUrl]);

  const removeChannel = React.useCallback(async () => {
    if (!selectedChannel || !selectedChannel.canDelete || !wsUrl.trim()) return;
    if (!window.confirm(`Delete ${selectedChannel.name}?`)) return;
    setSaving(true);
    setError(null);
    try {
      const result = await deleteMyChannel(wsUrl, selectedChannel.channelId);
      applyChannels(result, result.currentChannelId || null);
      toast.success("Channel deleted");
    } catch (nextError) {
      const message = (nextError as Error)?.message || String(nextError);
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  }, [applyChannels, selectedChannel, wsUrl]);

  if (!user) return null;

  return (
    <ConsoleShell title="API Keys">
      {error ? <InlineError message={error} /> : null}

      <div className="grid gap-4">
        <PanelCard title={user.email} className="argus-data-grid">
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <Fact label="Current" value={channelsState?.currentChannel?.name || "—"} />
            <Fact label="Channels" value={String(channels.length)} />
            <Fact label="Ready" value={`${readyCount}/${channels.length || 0}`} />
            <Fact label="Keys" value={String(keyedCount)} />
          </div>
        </PanelCard>

        <div className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
          <section className="space-y-4">
            <PanelCard title="Channels">
              {channels.length ? (
                <div className="space-y-2">
                  {channels.map((channel) => {
                    const active = selectedChannel?.channelId === channel.channelId;
                    return (
                      <button
                        key={channel.channelId}
                        type="button"
                        onClick={() => focusChannel(channel)}
                        className={cn(
                          "argus-row-shell w-full rounded-[16px] px-4 py-3 text-left",
                          active ? "border-primary/28 bg-primary/10" : "hover:border-border hover:bg-background/36"
                        )}
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0">
                            <div className="truncate font-medium text-foreground">{channel.name}</div>
                            <div className="mt-1 truncate text-xs text-muted-foreground">{channel.channelId}</div>
                          </div>
                          <Badge tone={channelTone(channel)}>{channelKind(channel)}</Badge>
                        </div>
                        <div className="mt-3 flex flex-wrap gap-2">
                          {channel.selected ? <Badge tone="primary">current</Badge> : null}
                          <Badge tone={channel.ready ? "success" : "default"}>{channel.ready ? "ready" : "pending"}</Badge>
                          <Badge tone={channel.hasApiKey ? "success" : "default"}>
                            {channel.hasApiKey ? "key" : "no key"}
                          </Badge>
                        </div>
                      </button>
                    );
                  })}
                </div>
              ) : (
                <EmptyState title="No channels" />
              )}
            </PanelCard>

            <PanelCard title="New">
              <div className="grid gap-3">
                <Input value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="name" />
                <Input value={newBaseUrl} onChange={(event) => setNewBaseUrl(event.target.value)} placeholder="https://api.openai.com/v1" />
                <Input
                  value={newApiKey}
                  onChange={(event) => setNewApiKey(event.target.value)}
                  placeholder="api key"
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                />
                <Button type="button" disabled={loading || saving} onClick={() => void createChannel()}>
                  <Plus className="h-4 w-4" />
                  Add
                </Button>
              </div>
            </PanelCard>
          </section>

          <section className="space-y-4">
            <PanelCard
              title={selectedChannel?.name || "Channel"}
              action={
                selectedChannel ? (
                  <div className="flex flex-wrap items-center gap-2">
                    {!selectedChannel.selected ? (
                      <Button
                        type="button"
                        size="sm"
                        disabled={loading || saving || !selectedChannel.ready}
                        onClick={() => void selectChannel()}
                      >
                        <Check className="h-4 w-4" />
                        Use
                      </Button>
                    ) : null}
                    {selectedChannel.canDelete ? (
                      <Button
                        type="button"
                        size="sm"
                        variant="destructive"
                        disabled={loading || saving}
                        onClick={() => void removeChannel()}
                      >
                        <Trash2 className="h-4 w-4" />
                        Delete
                      </Button>
                    ) : null}
                  </div>
                ) : null
              }
            >
              {selectedChannel ? (
                <div className="grid gap-4">
                  {selectedChannel.canRename ? (
                    <div className="grid gap-2 md:grid-cols-[minmax(0,1fr)_auto]">
                      <Input value={nameDraft} onChange={(event) => setNameDraft(event.target.value)} placeholder="name" />
                      <Button
                        type="button"
                        variant="secondary"
                        disabled={loading || saving || !nameDraft.trim() || nameDraft.trim() === selectedChannel.name}
                        onClick={() => void saveName()}
                      >
                        <Pencil className="h-4 w-4" />
                        Rename
                      </Button>
                    </div>
                  ) : null}

                  <div className="grid gap-3 md:grid-cols-2">
                    <Fact label="ID" value={selectedChannel.channelId} mono />
                    <Fact label="Type" value={channelKind(selectedChannel)} />
                    <Fact label="Base" value={selectedChannel.baseUrl || "—"} mono />
                    <Fact label="State" value={selectedChannel.ready ? "ready" : selectedChannel.reason || "pending"} />
                  </div>
                </div>
              ) : (
                <EmptyState title="No selection" />
              )}
            </PanelCard>

            <PanelCard
              title="Key"
              action={
                selectedChannel?.canSetKey ? (
                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      type="button"
                      size="sm"
                      disabled={loading || saving || !keyDraft.trim()}
                      onClick={() => void saveKey()}
                    >
                      <KeyRound className="h-4 w-4" />
                      Save
                    </Button>
                    {selectedChannel.canClearKey ? (
                      <Button type="button" size="sm" variant="secondary" disabled={loading || saving} onClick={() => void clearKey()}>
                        Clear
                      </Button>
                    ) : null}
                  </div>
                ) : null
              }
            >
              {selectedChannel ? (
                <div className="grid gap-4">
                  {selectedChannel.canSetKey ? (
                    <Input
                      value={keyDraft}
                      onChange={(event) => setKeyDraft(event.target.value)}
                      placeholder={selectedChannel.hasApiKey ? "new api key" : "api key"}
                      type="password"
                      autoComplete="off"
                      spellCheck={false}
                    />
                  ) : null}

                  <div className="grid gap-3 md:grid-cols-2">
                    <Fact label="Stored" value={selectedChannel.apiKeyMasked || (selectedChannel.hasApiKey ? "set" : "none")} />
                    <Fact label="Models" value={selectedChannel.modelsUrl || "—"} mono />
                  </div>
                </div>
              ) : (
                <EmptyState title="No key" />
              )}
            </PanelCard>

            {customCount ? null : (
              <PanelCard title="Custom">
                <EmptyState title="No custom keys" />
              </PanelCard>
            )}
          </section>
        </div>
      </div>
    </ConsoleShell>
  );
}
