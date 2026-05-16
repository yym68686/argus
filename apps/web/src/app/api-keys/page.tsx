"use client";

import * as React from "react";
import { Check, Copy, KeyRound, Pencil, Plus, Trash2 } from "lucide-react";
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
  const [newName, setNewName] = React.useState("");
  const [newBaseUrl, setNewBaseUrl] = React.useState("");
  const [newApiKey, setNewApiKey] = React.useState("");

  const channels = React.useMemo(() => channelsState?.channels ?? [], [channelsState?.channels]);
  const developerKeys = React.useMemo(() => developerKeysState?.keys ?? [], [developerKeysState?.keys]);
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

  const copyText = React.useCallback((text: string, label: string) => {
    if (!text.trim()) return;
    void navigator.clipboard.writeText(text).then(
      () => toast.success(`${label} copied`),
      () => toast.error("Copy failed")
    );
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
      toast.success("Developer key created");
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
      title: "Revoke developer key?",
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
      toast.success("Developer key revoked");
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
    const confirmed = await confirm({
      title: "Delete channel?",
      body: `Delete ${selectedChannel.name}? Stored upstream key settings for this channel will be removed.`,
      confirmLabel: "Delete channel",
      tone: "destructive",
    });
    if (!confirmed) return;
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
  }, [applyChannels, confirm, selectedChannel, wsUrl]);

  if (!user) return null;

  return (
    <ConsoleShell title="API Keys">
      {confirmDialog}
      {error ? <InlineError message={error} /> : null}

      <div className="grid gap-4">
        <PanelCard title={user.email} className="argus-data-grid">
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
            <Fact label="Current" value={channelsState?.currentChannel?.name || "—"} />
            <Fact label="Channels" value={String(channels.length)} />
            <Fact label="Ready" value={`${readyCount}/${channels.length || 0}`} />
            <Fact label="Upstream keys" value={String(keyedCount)} />
            <Fact label="Access keys" value={String(developerKeysState?.counts.apiKeys ?? developerKeys.length)} />
          </div>
        </PanelCard>

        {revealedKey ? (
          <PanelCard
            title="New developer key"
            action={
              <Button type="button" size="sm" variant="secondary" onClick={() => copyText(revealedKey.token, "Developer key")}>
                <Copy className="h-4 w-4" />
                Copy
              </Button>
            }
          >
            <div className="grid gap-3">
              <div className="rounded-[16px] border border-primary/28 bg-primary/10 px-4 py-3 text-sm text-foreground">
                This token is shown once. Save it in your application config now.
              </div>
              <Fact label={revealedKey.name || "Developer key"} value={revealedKey.token} mono />
            </div>
          </PanelCard>
        ) : null}

        <PanelCard
          title="Gateway access keys"
          action={
            <div className="flex flex-wrap items-center gap-2">
              <Input
                value={developerKeyName}
                onChange={(event) => setDeveloperKeyName(event.target.value)}
                placeholder="key name"
                className="w-[13rem]"
              />
              <Button type="button" disabled={loading || saving} onClick={() => void createDeveloperKey()}>
                <Plus className="h-4 w-4" />
                Create
              </Button>
            </div>
          }
        >
          {developerKeys.length ? (
            <div className="space-y-2">
              {developerKeys.map((key) => (
                <div key={key.keyId} className="argus-row-shell flex flex-col gap-3 rounded-[16px] px-4 py-3 md:flex-row md:items-center md:justify-between">
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
            <EmptyState title="No developer keys" />
          )}
        </PanelCard>

        <PanelCard title="Upstream channels" contentClassName="min-w-0">
          <div className="grid gap-0 overflow-hidden rounded-[18px] border border-border/72 bg-background/16 xl:grid-cols-[minmax(17rem,20rem)_minmax(0,1fr)]">
            <aside className="border-b border-border/68 bg-background/18 xl:border-b-0 xl:border-r">
              <div className="flex items-center justify-between gap-3 border-b border-border/60 px-4 py-3">
                <div>
                  <div className="text-sm font-semibold text-foreground">Channels</div>
                  <div className="mt-0.5 text-xs text-muted-foreground">
                    {readyCount} ready · {keyedCount} with keys
                  </div>
                </div>
                <Badge tone={customCount ? "default" : "warning"}>{customCount} custom</Badge>
              </div>

              <div className="max-h-[28rem] space-y-1 overflow-y-auto p-2">
                {channels.length ? (
                  channels.map((channel) => {
                    const active = selectedChannel?.channelId === channel.channelId;
                    return (
                      <button
                        key={channel.channelId}
                        type="button"
                        onClick={() => focusChannel(channel)}
                        className={cn(
                          "group w-full rounded-xl px-3 py-3 text-left transition",
                          "hover:bg-background/42 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-ring/18",
                          active ? "bg-primary/10 shadow-[inset_3px_0_0_0_oklch(var(--primary)/0.72)]" : null
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
                          {channel.selected ? <Badge tone="primary">current</Badge> : null}
                          <Badge tone={channel.ready ? "success" : "default"}>
                            <TextSwap value={channel.ready ? "ready" : "pending"} />
                          </Badge>
                          <Badge tone={channel.hasApiKey ? "success" : "default"}>
                            {channel.hasApiKey ? "key" : "no key"}
                          </Badge>
                        </div>
                      </button>
                    );
                  })
                ) : (
                  <EmptyState title="No channels" />
                )}
              </div>

              <form
                className="grid gap-3 border-t border-border/60 p-4"
                onSubmit={(event) => {
                  event.preventDefault();
                  void createChannel();
                }}
              >
                <div>
                  <div className="text-sm font-semibold text-foreground">Add channel</div>
                  <div className="mt-1 text-xs leading-5 text-muted-foreground">
                    Store one upstream endpoint and its API key.
                  </div>
                </div>
                <LabeledInput
                  id="new-channel-name"
                  label="Name"
                  value={newName}
                  onChange={(event) => setNewName(event.target.value)}
                  placeholder="0-0.pro"
                />
                <LabeledInput
                  id="new-channel-base-url"
                  label="Base URL"
                  value={newBaseUrl}
                  onChange={(event) => setNewBaseUrl(event.target.value)}
                  placeholder="https://api.openai.com/v1"
                />
                <LabeledInput
                  id="new-channel-api-key"
                  label="API key"
                  value={newApiKey}
                  onChange={(event) => setNewApiKey(event.target.value)}
                  placeholder="sk-..."
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                />
                <Button type="submit" disabled={loading || saving}>
                  <Plus className="h-4 w-4" />
                  Add channel
                </Button>
              </form>
            </aside>

            <section className="min-w-0 p-4 md:p-5">
              <PanelReveal key={selectedChannel?.channelId ?? "channel-none"} open travel="12px">
                {selectedChannel ? (
                  <div className="grid gap-6">
                    <div className="flex flex-col gap-4 border-b border-border/60 pb-5 lg:flex-row lg:items-start lg:justify-between">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <h2 className="min-w-0 truncate text-[clamp(1.45rem,2vw,2rem)] font-semibold tracking-[-0.045em] text-foreground">
                            {selectedChannel.name}
                          </h2>
                          <Badge tone={channelTone(selectedChannel)}>{channelKind(selectedChannel)}</Badge>
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
                            Use channel
                          </Button>
                        ) : (
                          <Badge tone="primary">current channel</Badge>
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
                            Delete
                          </Button>
                        ) : null}
                      </div>
                    </div>

                    <div className="grid gap-5 2xl:grid-cols-[minmax(0,0.95fr)_minmax(22rem,1.05fr)]">
                      <div className="grid content-start gap-5">
                        <div>
                          <SectionHeading title="Status" />
                          <div className="mt-3 grid gap-0 overflow-hidden rounded-[14px] border border-border/64">
                            <DetailRow label="State" value={selectedChannel.ready ? "ready" : selectedChannel.reason || "pending"} />
                            <DetailRow label="ID" value={selectedChannel.channelId} mono />
                            <DetailRow label="Type" value={channelKind(selectedChannel)} />
                            <DetailRow label="Models" value={selectedChannel.modelsUrl || "—"} mono />
                          </div>
                        </div>

                        {selectedChannel.canRename ? (
                          <form
                            className="grid gap-3"
                            onSubmit={(event) => {
                              event.preventDefault();
                              void saveName();
                            }}
                          >
                            <SectionHeading title="Display name" />
                            <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
                              <Input
                                id="selected-channel-name"
                                value={nameDraft}
                                onChange={(event) => setNameDraft(event.target.value)}
                                placeholder="name"
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
                        className="grid content-start gap-4 rounded-[16px] border border-border/64 bg-background/20 p-4"
                        onSubmit={(event) => {
                          event.preventDefault();
                          void saveKey();
                        }}
                      >
                        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                          <div>
                            <SectionHeading title="Upstream key" />
                            <div className="mt-1 text-sm text-muted-foreground">
                              Stored: {selectedChannel.apiKeyMasked || (selectedChannel.hasApiKey ? "set" : "none")}
                            </div>
                          </div>
                          {selectedChannel.canClearKey ? (
                            <Button type="button" size="sm" variant="secondary" disabled={loading || saving} onClick={() => void clearKey()}>
                              Clear
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
                                value={keyDraft}
                                onChange={(event) => setKeyDraft(event.target.value)}
                                placeholder={selectedChannel.hasApiKey ? "Paste replacement key" : "Paste API key"}
                                type="password"
                                autoComplete="off"
                                spellCheck={false}
                              />
                              <Button type="submit" disabled={loading || saving || !keyDraft.trim()}>
                                <KeyRound className="h-4 w-4" />
                                Save key
                              </Button>
                            </div>
                          </div>
                        ) : (
                          <div className="rounded-[14px] border border-border/60 bg-background/22 px-3 py-3 text-sm text-muted-foreground">
                            This channel does not accept user-managed keys.
                          </div>
                        )}
                      </form>
                    </div>
                  </div>
                ) : (
                  <EmptyState title="No channel selected" />
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
    <div className="grid gap-2 border-b border-border/54 bg-background/14 px-3.5 py-3 last:border-b-0 sm:grid-cols-[7rem_minmax(0,1fr)]">
      <div className="text-xs font-medium text-muted-foreground">{label}</div>
      <div className={cn("min-w-0 break-words text-sm font-medium leading-6 text-foreground", mono ? "font-mono text-[12.5px]" : null)}>
        {value}
      </div>
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
