"use client";

import * as React from "react";
import { Check, Copy, KeyRound, Pencil, Plus, Trash2, X } from "lucide-react";
import { toast } from "sonner";

import { useConfirmDialog } from "@/components/confirm-dialog";
import { useAuth } from "@/components/admin-gate";
import { Badge, EmptyState, Fact, InlineError, PanelCard } from "@/components/console-primitives";
import { ConsoleShell } from "@/components/console-shell";
import { PanelReveal, TextSwap } from "@/components/transitions";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { AdminChannelEntry } from "@/lib/admin";
import type { IssuedDeveloperApiKey } from "@/lib/auth";
import { formatWhen } from "@/lib/format";
import { consumeJustIssuedDeveloperKey, useGatewayWsUrlState } from "@/lib/gateway";
import {
  createMyDeveloperKey,
  clearMyChannelKey,
  createMyChannel,
  deleteMyChannel,
  fetchMyDeveloperKeys,
  fetchMyChannels,
  renameMyChannel,
  revokeMyDeveloperKey,
  selectMyChannel,
  setMyChannelKey,
  type DeveloperKeyEntry,
  type SelfDeveloperKeysResponse,
  type SelfChannelsResponse,
} from "@/lib/self";
import { cn } from "@/lib/utils";

function channelKind(channel: AdminChannelEntry): string {
  return channel.builtinKind || channel.kind || (channel.isBuiltin ? "builtin" : "custom");
}

function providerStatus(channel: AdminChannelEntry): { label: string; tone: "success" | "warning" | "default" } {
  if (channel.ready) return { label: "ready", tone: "success" };
  if (channel.disabledByAdmin) return { label: "disabled", tone: "default" };
  if (!channel.hasApiKey && channel.canSetKey) return { label: "needs key", tone: "warning" };
  return { label: "pending", tone: "default" };
}

function isVisibleUserProvider(channel: AdminChannelEntry): boolean {
  return channel.enabledForUser !== false && channel.disabledByAdmin !== true;
}

function visibleChannelsState(nextState: SelfChannelsResponse): SelfChannelsResponse {
  const channels = nextState.channels.filter(isVisibleUserProvider);
  const currentChannel =
    channels.find((channel) => channel.channelId === nextState.currentChannelId) ??
    (nextState.currentChannel && isVisibleUserProvider(nextState.currentChannel) ? nextState.currentChannel : null);
  return {
    ...nextState,
    channels,
    currentChannelId: currentChannel?.channelId ?? null,
    currentChannel,
  };
}

export default function ApiKeysPage() {
  const { user } = useAuth();
  const { confirm, confirmDialog } = useConfirmDialog();
  const [wsUrl] = useGatewayWsUrlState();
  const [channelsState, setChannelsState] = React.useState<SelfChannelsResponse | null>(null);
  const [developerKeysState, setDeveloperKeysState] = React.useState<SelfDeveloperKeysResponse | null>(null);
  const [selectedChannelId, setSelectedChannelId] = React.useState("");
  const selectedChannelIdRef = React.useRef("");
  const [loading, setLoading] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [developerKeyName, setDeveloperKeyName] = React.useState("default");
  const [revealedKey, setRevealedKey] = React.useState<(DeveloperKeyEntry & { token: string }) | null>(() => {
    const justIssued = consumeJustIssuedDeveloperKey<IssuedDeveloperApiKey>();
    return justIssued?.token ? justIssued : null;
  });

  const [nameDraft, setNameDraft] = React.useState("");
  const [keyDraft, setKeyDraft] = React.useState("");
  const [addingChannel, setAddingChannel] = React.useState(false);
  const [newName, setNewName] = React.useState("");
  const [newBaseUrl, setNewBaseUrl] = React.useState("");
  const [newApiKey, setNewApiKey] = React.useState("");

  const channels = React.useMemo(() => channelsState?.channels ?? [], [channelsState?.channels]);
  const developerKeys = React.useMemo(() => developerKeysState?.keys ?? [], [developerKeysState?.keys]);
  const currentChannelId = channelsState?.currentChannelId ?? "";
  const readyCount = channels.filter((channel) => channel.ready).length;
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
      const visibleState = visibleChannelsState(nextState);
      const candidates = [
        preferredChannelId,
        selectedChannelIdRef.current,
        visibleState.currentChannelId,
        visibleState.channels[0]?.channelId,
      ];
      let nextSelectedId = "";
      for (const candidate of candidates) {
        if (!candidate) continue;
        if (visibleState.channels.some((channel) => channel.channelId === candidate)) {
          nextSelectedId = candidate;
          break;
        }
      }
      const nextSelectedChannel = visibleState.channels.find((channel) => channel.channelId === nextSelectedId) ?? null;
      selectedChannelIdRef.current = nextSelectedId;
      setChannelsState(visibleState);
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

  const copyText = React.useCallback((text: string, label: string) => {
    if (!text.trim()) return;
    void navigator.clipboard.writeText(text).then(
      () => toast.success(`${label} copied`),
      () => toast.error("Copy failed")
    );
  }, []);

  const resetNewChannelDraft = React.useCallback(() => {
    setNewName("");
    setNewBaseUrl("");
    setNewApiKey("");
  }, []);

  const refresh = React.useCallback(
    async (opts?: { notify?: boolean }) => {
      if (!wsUrl.trim()) return;
      setLoading(true);
      setError(null);
      try {
        const [nextChannelsState, nextDeveloperKeysState] = await Promise.all([
          fetchMyChannels(wsUrl),
          fetchMyDeveloperKeys(wsUrl),
        ]);
        applyChannels(nextChannelsState);
        setDeveloperKeysState(nextDeveloperKeysState);
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

  const createDeveloperKey = React.useCallback(async () => {
    if (!wsUrl.trim()) return;
    setSaving(true);
    setError(null);
    try {
      const result = await createMyDeveloperKey(wsUrl, { name: developerKeyName });
      setDeveloperKeysState(result);
      if (result.issuedKey) {
        setRevealedKey(result.issuedKey);
      }
      toast.success("Argus API key created");
    } catch (nextError) {
      const message = (nextError as Error)?.message || String(nextError);
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  }, [developerKeyName, wsUrl]);

  const revokeDeveloperKey = React.useCallback(async (keyId: string) => {
    if (!wsUrl.trim()) return;
    const confirmed = await confirm({
      title: "Revoke Argus API key?",
      body: "Applications using this key will no longer be able to access the gateway.",
      confirmLabel: "Revoke key",
      tone: "destructive",
    });
    if (!confirmed) return;
    setSaving(true);
    setError(null);
    try {
      const result = await revokeMyDeveloperKey(wsUrl, keyId);
      setDeveloperKeysState(result);
      toast.success("Argus API key revoked");
    } catch (nextError) {
      const message = (nextError as Error)?.message || String(nextError);
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  }, [confirm, wsUrl]);

  const createChannel = React.useCallback(async () => {
    if (!wsUrl.trim()) return;
    const providerName = newName.trim();
    const providerBaseUrl = newBaseUrl.trim();
    const providerApiKey = newApiKey.trim();
    if (!providerName || !providerBaseUrl || !providerApiKey) {
      toast.error("Provider name, base URL, and API key are required");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const result = await createMyChannel(wsUrl, {
        name: providerName,
        baseUrl: providerBaseUrl,
        apiKey: providerApiKey,
      });
      applyChannels(result, result.channel?.channelId || null);
      resetNewChannelDraft();
      setAddingChannel(false);
      toast.success("Provider added");
    } catch (nextError) {
      const message = (nextError as Error)?.message || String(nextError);
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  }, [applyChannels, newApiKey, newBaseUrl, newName, resetNewChannelDraft, wsUrl]);

  const selectChannel = React.useCallback(async () => {
    if (!selectedChannel || !wsUrl.trim() || selectedChannel.selected) return;
    setSaving(true);
    setError(null);
    try {
      const result = await selectMyChannel(wsUrl, selectedChannel.channelId);
      applyChannels(result, selectedChannel.channelId);
      toast.success("Default provider updated");
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
    const confirmed = await confirm({
      title: "Delete provider?",
      body: `Delete ${selectedChannel.name}? Stored API key settings for this provider will be removed.`,
      confirmLabel: "Delete provider",
      tone: "destructive",
    });
    if (!confirmed) return;
    setSaving(true);
    setError(null);
    try {
      const result = await deleteMyChannel(wsUrl, selectedChannel.channelId);
      applyChannels(result, result.currentChannelId || null);
      toast.success("Provider deleted");
    } catch (nextError) {
      const message = (nextError as Error)?.message || String(nextError);
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  }, [applyChannels, confirm, selectedChannel, wsUrl]);

  if (!user) return null;

  return (
    <ConsoleShell title="API Keys">
      {confirmDialog}
      {error ? <InlineError message={error} /> : null}

      <div className="grid gap-4">
        <PanelCard title={user.email || user.username || `User ${user.userId}`} className="argus-data-grid">
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
            <Fact label="Default Provider" value={channelsState?.currentChannel?.name || "—"} />
            <Fact label="Providers" value={String(channels.length)} />
            <Fact label="Ready" value={`${readyCount}/${channels.length || 0}`} />
            <Fact label="Provider Keys" value={String(keyedCount)} />
            <Fact label="Argus API keys" value={String(developerKeysState?.counts.apiKeys ?? developerKeys.length)} />
          </div>
        </PanelCard>

        {revealedKey ? (
          <PanelCard
            title="New Argus API key"
            action={
              <Button type="button" size="sm" variant="secondary" onClick={() => copyText(revealedKey.token, "Argus API key")}>
                <Copy className="h-4 w-4" />
                Copy
              </Button>
            }
          >
            <div className="grid gap-3">
              <div className="rounded-[16px] border border-primary/28 bg-primary/10 px-4 py-3 text-sm text-foreground">
                This token is shown once. Save it in your application config now.
              </div>
              <Fact label={revealedKey.name || "Argus API key"} value={revealedKey.token} mono />
            </div>
          </PanelCard>
        ) : null}

        <PanelCard
          title="Argus API keys"
          action={
            <div className="flex flex-wrap items-center gap-2">
              <label htmlFor="developer-key-name" className="sr-only">
                Argus API key name
              </label>
              <Input
                id="developer-key-name"
                name="developer-key-name"
                value={developerKeyName}
                onChange={(event) => setDeveloperKeyName(event.target.value)}
                placeholder="default…"
                autoComplete="off"
                spellCheck={false}
                className="w-[13rem]"
              />
              <Button type="button" disabled={loading || saving} onClick={() => void createDeveloperKey()}>
                <Plus className="h-4 w-4" />
                Create Argus API Key
              </Button>
            </div>
          }
        >
          {developerKeys.length ? (
            <div className="space-y-2">
              {developerKeys.map((key) => (
                <div key={key.keyId} className="flex flex-col gap-3 border-l border-border/58 px-3 py-2.5 md:flex-row md:items-center md:justify-between">
                  <div className="min-w-0">
                    <div className="truncate font-medium text-foreground">{key.name}</div>
                    <div className="mt-1 truncate text-xs text-muted-foreground">{key.keyId}</div>
                    <div className="mt-2 flex flex-wrap gap-2">
                      <Badge tone={key.active ? "success" : "default"}>{key.active ? "active" : "revoked"}</Badge>
                      {key.tokenPreview ? <Badge tone="default">{key.tokenPreview}</Badge> : null}
                      {key.lastUsedAtMs ? <Badge tone="default">used {formatWhen(key.lastUsedAtMs)}</Badge> : null}
                      {key.expiresAtMs ? <Badge tone="warning">expires {formatWhen(key.expiresAtMs)}</Badge> : null}
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {!key.active ? null : (
                      <Button type="button" size="sm" variant="destructive" disabled={loading || saving} onClick={() => void revokeDeveloperKey(key.keyId)}>
                        <Trash2 className="h-4 w-4" />
                        Revoke
                      </Button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <EmptyState title="No Argus API keys" />
          )}
        </PanelCard>

        <PanelCard
          title="LLM providers"
          className="overflow-hidden"
          contentClassName="-mx-4 -mb-4 -mt-4 min-w-0 md:-mx-5 md:-mb-5"
          action={
            <Button
              type="button"
              variant={addingChannel ? "secondary" : "default"}
              disabled={loading || saving}
              aria-expanded={addingChannel}
              aria-controls="new-provider-form"
              onClick={() => setAddingChannel((open) => !open)}
            >
              {addingChannel ? <X className="h-4 w-4" /> : <Plus className="h-4 w-4" />}
              {addingChannel ? "Close Form" : "Add Provider"}
            </Button>
          }
        >
          <PanelReveal open={addingChannel} travel="10px">
            <form
              id="new-provider-form"
              className="border-b border-border/60 bg-background/16 p-4 md:p-5"
              onSubmit={(event) => {
                event.preventDefault();
                void createChannel();
              }}
            >
              <div className="grid gap-3 xl:grid-cols-[minmax(10rem,0.85fr)_minmax(16rem,1.15fr)_minmax(13rem,1fr)_auto] xl:items-end">
                <LabeledInput
                  id="new-channel-name"
                  name="new-channel-name"
                  label="Provider"
                  value={newName}
                  onChange={(event) => setNewName(event.target.value)}
                  placeholder="0-0.pro…"
                  autoComplete="off"
                  spellCheck={false}
                />
                <LabeledInput
                  id="new-channel-base-url"
                  name="new-channel-base-url"
                  label="Base URL"
                  value={newBaseUrl}
                  onChange={(event) => setNewBaseUrl(event.target.value)}
                  placeholder="https://api.openai.com/v1…"
                  type="url"
                  autoComplete="off"
                  spellCheck={false}
                />
                <LabeledInput
                  id="new-channel-api-key"
                  name="new-channel-api-key"
                  label="Provider API key"
                  value={newApiKey}
                  onChange={(event) => setNewApiKey(event.target.value)}
                  placeholder="sk-…"
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                />
                <div className="flex flex-wrap items-center gap-2 xl:justify-end">
                  <Button type="submit" disabled={loading || saving}>
                    <Plus className="h-4 w-4" />
                    Add Provider
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    disabled={loading || saving}
                    onClick={() => {
                      setAddingChannel(false);
                      resetNewChannelDraft();
                    }}
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            </form>
          </PanelReveal>

          <div className="grid min-h-[32rem] xl:grid-cols-[minmax(16rem,19rem)_minmax(0,1fr)]">
            <aside className="border-b border-border/60 bg-background/10 xl:border-b-0 xl:border-r" aria-label="LLM providers">
              <div className="max-h-[28rem] overflow-y-auto">
                {channels.length ? (
                  channels.map((channel) => {
                    const active = selectedChannel?.channelId === channel.channelId;
                    const status = providerStatus(channel);
                    return (
                      <button
                        key={channel.channelId}
                        type="button"
                        aria-current={active ? "page" : undefined}
                        onClick={() => focusChannel(channel)}
                        className={cn(
                          "group w-full border-b border-border/42 px-4 py-3.5 text-left transition last:border-b-0",
                          "hover:bg-background/34 focus-visible:bg-primary/10 focus-visible:outline-none focus-visible:shadow-[inset_2px_0_0_0_oklch(var(--primary)/0.6)]",
                          active ? "bg-primary/10 shadow-[inset_2px_0_0_0_oklch(var(--primary)/0.6)]" : null
                        )}
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0">
                            <div className="truncate text-sm font-semibold text-foreground">{channel.name}</div>
                            <div className="mt-1 truncate font-mono text-[11px] text-muted-foreground">{channel.channelId}</div>
                          </div>
                          <ChannelStatusDot channel={channel} />
                        </div>
                        <div className="mt-2 flex flex-wrap items-center gap-1.5">
                          {channel.selected ? <Badge tone="primary">default</Badge> : null}
                          <Badge tone={status.tone}>
                            <TextSwap value={status.label} />
                          </Badge>
                          <Badge tone={channel.hasApiKey ? "success" : "default"}>
                            {channel.hasApiKey ? "key" : "no key"}
                          </Badge>
                        </div>
                      </button>
                    );
                  })
                ) : (
                  <div className="px-4 py-8 text-sm text-muted-foreground">No providers</div>
                )}
              </div>
            </aside>

            <section className="min-w-0 p-5 md:p-6">
              <PanelReveal key={selectedChannel?.channelId ?? "channel-none"} open travel="12px">
                {selectedChannel ? (
                  <div className="grid gap-6">
                    <div className="flex flex-col gap-4 border-b border-border/60 pb-5 lg:flex-row lg:items-start lg:justify-between">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <h2 className="min-w-0 truncate text-[clamp(1.45rem,2vw,2rem)] font-semibold tracking-[-0.045em] text-foreground">
                            {selectedChannel.name}
                          </h2>
                          <Badge tone="default">{channelKind(selectedChannel)}</Badge>
                        </div>
                        <div className="mt-2 truncate font-mono text-xs text-muted-foreground">
                          {selectedChannel.baseUrl || "No base URL"}
                        </div>
                      </div>

                      <div className="flex flex-wrap items-center gap-2">
                        {!selectedChannel.selected ? (
                          <Button
                            type="button"
                            size="sm"
                            disabled={loading || saving || !selectedChannel.ready}
                            onClick={() => void selectChannel()}
                          >
                            <Check className="h-4 w-4" />
                            Set as Default
                          </Button>
                        ) : (
                          <Badge tone="primary">Default provider</Badge>
                        )}
                        {selectedChannel.canDelete ? (
                          <Button
                            type="button"
                            size="sm"
                            variant="ghost"
                            disabled={loading || saving}
                            onClick={() => void removeChannel()}
                            className="text-destructive hover:border-destructive/48 hover:bg-destructive/10"
                          >
                            <Trash2 className="h-4 w-4" />
                            Delete Provider
                          </Button>
                        ) : null}
                      </div>
                    </div>

                    <div className="grid gap-6 2xl:grid-cols-[minmax(0,0.95fr)_minmax(22rem,1.05fr)]">
                      <div className="grid content-start gap-5">
                        <div>
                          <SectionHeading title="Status" />
                          <dl className="mt-3 divide-y divide-border/54 border-y border-border/54">
                            <DetailRow label="State" value={selectedChannel.ready ? "ready" : selectedChannel.reason || "pending"} />
                            <DetailRow label="Provider ID" value={selectedChannel.channelId} mono />
                            <DetailRow label="Type" value={channelKind(selectedChannel)} />
                            <DetailRow label="Models endpoint" value={selectedChannel.modelsUrl || "—"} mono />
                          </dl>
                        </div>

                        {selectedChannel.canRename ? (
                          <form
                            className="grid gap-3"
                            onSubmit={(event) => {
                              event.preventDefault();
                              void saveName();
                            }}
                          >
                            <SectionHeading title="Provider name" />
                            <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
                              <Input
                                id="selected-channel-name"
                                name="selected-channel-name"
                                value={nameDraft}
                                onChange={(event) => setNameDraft(event.target.value)}
                                placeholder="Provider name…"
                                autoComplete="off"
                                spellCheck={false}
                              />
                              <Button
                                type="submit"
                                variant="secondary"
                                disabled={loading || saving || !nameDraft.trim() || nameDraft.trim() === selectedChannel.name}
                              >
                                <Pencil className="h-4 w-4" />
                                Rename
                              </Button>
                            </div>
                          </form>
                        ) : null}
                      </div>

                      <form
                        className="grid content-start gap-4 border-t border-border/54 pt-5 2xl:border-l 2xl:border-t-0 2xl:pl-6 2xl:pt-0"
                        onSubmit={(event) => {
                          event.preventDefault();
                          void saveKey();
                        }}
                      >
                        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                          <div>
                            <SectionHeading title="Provider API key" />
                            <div className="mt-1 text-sm text-muted-foreground">
                              Stored: {selectedChannel.apiKeyMasked || (selectedChannel.hasApiKey ? "set" : "none")}
                            </div>
                          </div>
                          {selectedChannel.canClearKey ? (
                            <Button type="button" size="sm" variant="secondary" disabled={loading || saving} onClick={() => void clearKey()}>
                              Remove Key
                            </Button>
                          ) : null}
                        </div>

                        {selectedChannel.canSetKey ? (
                          <div className="grid gap-2">
                            <label htmlFor="selected-channel-api-key" className="text-xs font-medium text-muted-foreground">
                              New API key
                            </label>
                            <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
                              <Input
                                id="selected-channel-api-key"
                                name="selected-channel-api-key"
                                value={keyDraft}
                                onChange={(event) => setKeyDraft(event.target.value)}
                                placeholder={selectedChannel.hasApiKey ? "Paste replacement key…" : "Paste API key…"}
                                type="password"
                                autoComplete="off"
                                spellCheck={false}
                              />
                              <Button type="submit" disabled={loading || saving || !keyDraft.trim()}>
                                <KeyRound className="h-4 w-4" />
                                Save API Key
                              </Button>
                            </div>
                          </div>
                        ) : (
                          <div className="text-sm leading-6 text-muted-foreground">
                            This provider is managed by the gateway.
                          </div>
                        )}
                      </form>
                    </div>
                  </div>
                ) : (
                  <div className="py-8 text-sm text-muted-foreground">No provider selected</div>
                )}
              </PanelReveal>
            </section>
          </div>
        </PanelCard>
      </div>
    </ConsoleShell>
  );
}

function SectionHeading({ title }: { title: string }) {
  return <h3 className="text-xs font-semibold uppercase tracking-[0.08em] text-muted-foreground">{title}</h3>;
}

function DetailRow({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="grid gap-2 py-3 sm:grid-cols-[7rem_minmax(0,1fr)]">
      <dt className="text-xs font-medium text-muted-foreground">{label}</dt>
      <dd className={cn("min-w-0 break-words text-sm font-medium leading-6 text-foreground", mono ? "font-mono text-[12.5px]" : null)}>
        {value}
      </dd>
    </div>
  );
}

function LabeledInput({
  id,
  label,
  className,
  ...props
}: React.ComponentProps<typeof Input> & { id: string; label: string }) {
  return (
    <div className={cn("grid gap-1.5", className)}>
      <label htmlFor={id} className="text-xs font-medium text-muted-foreground">
        {label}
      </label>
      <Input id={id} {...props} />
    </div>
  );
}

function ChannelStatusDot({ channel }: { channel: AdminChannelEntry }) {
  const state = channel.ready ? "ok" : channel.hasApiKey ? "warn" : "idle";
  return <span aria-hidden className="argus-status-dot mt-1" data-state={state} />;
}
