import test from "node:test";
import assert from "node:assert/strict";

import {
  buildArgusCliInstallCommand,
  buildNodeDisconnectCommand,
  buildNodeConnectionCommand,
  buildNodeLogsCommand,
  buildNodeReconnectCommand,
  derivePublicNodeWsUrl,
  deriveTelegramWebhookSecret,
  isTelegramGetUpdatesWebhookConflict,
  isTelegramMessageDirectedAtBot,
  normalizeChatSettingKey,
  normalizeChatSettings,
  resolveTelegramBotTokenConfig,
  resolveTelegramWebhookConfig,
  telegramSourceFromMessage,
  telegramTokenHash,
  telegramTokenRefreshIntervalMs,
  telegramWebhookInfoLogFields
} from "./index.mjs";

test("normalizeChatSettings defaults Telegram chat settings on", () => {
  assert.deepEqual(normalizeChatSettings(null), {
    replyToMessages: true,
    sendCommentary: true,
    sendTyping: true
  });
  assert.deepEqual(normalizeChatSettings({ replyToMessages: false, sendCommentary: false }), {
    replyToMessages: false,
    sendCommentary: false,
    sendTyping: true
  });
  assert.equal(normalizeChatSettingKey("sendCommentary"), "sendCommentary");
  assert.equal(normalizeChatSettingKey("bad"), null);
});

test("resolveTelegramWebhookConfig falls back to polling when no webhook URL is available", () => {
  const config = resolveTelegramWebhookConfig({
    deliveryMode: "auto",
    explicitUrl: null,
    appUrl: null,
    explicitSecret: null,
    telegramToken: "123:abc",
    appId: null
  });

  assert.deepEqual(config, { mode: "polling" });
});

test("resolveTelegramWebhookConfig derives the Fugue webhook URL in auto mode", () => {
  const config = resolveTelegramWebhookConfig({
    deliveryMode: "auto",
    explicitUrl: null,
    appUrl: "https://argus-telegram-bot.fugue.pro/",
    explicitSecret: null,
    telegramToken: "123:abc",
    appId: "app_123"
  });

  assert.equal(config.mode, "webhook");
  assert.equal(config.url, "https://argus-telegram-bot.fugue.pro/telegram/webhook");
  assert.equal(config.path, "/telegram/webhook");
  assert.equal(config.secretToken, deriveTelegramWebhookSecret(null, "123:abc", "app_123"));
});

test("isTelegramGetUpdatesWebhookConflict only matches Telegram webhook conflicts", () => {
  assert.equal(
    isTelegramGetUpdatesWebhookConflict(
      new Error("Telegram getUpdates failed: Conflict: can't use getUpdates method while webhook is active; use deleteWebhook to delete the webhook first")
    ),
    true
  );
  assert.equal(
    isTelegramGetUpdatesWebhookConflict(
      new Error("Telegram getUpdates failed: Conflict: terminated by setWebhook request")
    ),
    true
  );
  assert.equal(
    isTelegramGetUpdatesWebhookConflict(
      new Error("Telegram sendMessage failed: Bad Request: chat not found")
    ),
    false
  );
});

test("resolveTelegramBotTokenConfig gives stored gateway token precedence over env token", () => {
  const config = resolveTelegramBotTokenConfig({
    envToken: "111:env-token",
    settings: { source: "stored", token: "222:stored-token" }
  });

  assert.equal(config.token, "222:stored-token");
  assert.equal(config.source, "gateway-stored");
  assert.equal(config.tokenHash, telegramTokenHash("222:stored-token"));
});

test("resolveTelegramBotTokenConfig falls back to env unless gateway is authoritative", () => {
  assert.deepEqual(resolveTelegramBotTokenConfig({ envToken: "111:env-token", settings: null }), {
    token: "111:env-token",
    source: "env",
    tokenHash: telegramTokenHash("111:env-token")
  });

  assert.deepEqual(
    resolveTelegramBotTokenConfig({
      envToken: "111:env-token",
      settings: { source: "env", token: "333:gateway-env-token" }
    }),
    {
      token: "111:env-token",
      source: "env",
      tokenHash: telegramTokenHash("111:env-token")
    }
  );

  assert.deepEqual(
    resolveTelegramBotTokenConfig({
      envToken: null,
      settings: { source: "env", token: "333:gateway-env-token" }
    }),
    {
      token: "333:gateway-env-token",
      source: "gateway",
      tokenHash: telegramTokenHash("333:gateway-env-token")
    }
  );
});

test("telegramTokenRefreshIntervalMs clamps unsafe refresh intervals", () => {
  assert.equal(telegramTokenRefreshIntervalMs("500"), 15_000);
  assert.equal(telegramTokenRefreshIntervalMs("2500"), 2500);
  assert.equal(telegramTokenRefreshIntervalMs("bad"), 15_000);
});

test("telegramWebhookInfoLogFields redacts webhook URLs and preserves diagnostics", () => {
  const fields = telegramWebhookInfoLogFields({
    url: "https://example.com/telegram/webhook?token=super-secret",
    pending_update_count: 27,
    last_error_date: 1,
    last_error_message: "Wrong response from the webhook: 403 Forbidden",
    ip_address: "188.114.96.0",
    has_custom_certificate: false,
    allowed_updates: ["message", "callback_query", "", null]
  });

  assert.deepEqual(fields, {
    webhook_url: "https://example.com/telegram/webhook?token=***",
    webhook_pending_update_count: 27,
    webhook_last_error_at: "1970-01-01T00:00:01.000Z",
    webhook_last_error_message: "Wrong response from the webhook: 403 Forbidden",
    webhook_ip_address: "188.114.96.0",
    webhook_allowed_updates: ["message", "callback_query"]
  });
});

test("telegramSourceFromMessage includes Telegram sender identity", () => {
  const source = telegramSourceFromMessage(
    {
      chat: { id: -4633273294, type: "supergroup" },
      from: {
        id: 917527833,
        username: "@alice_dev",
        first_name: "Alice",
        last_name: "Ng"
      }
    },
    "-4633273294"
  );

  assert.deepEqual(source, {
    channel: "telegram",
    chatKey: "-4633273294",
    telegramUserId: 917527833,
    username: "alice_dev",
    firstName: "Alice",
    lastName: "Ng"
  });
});

test("isTelegramMessageDirectedAtBot detects explicit group mentions", () => {
  assert.equal(
    isTelegramMessageDirectedAtBot(
      {
        chat: { id: -4633273294, type: "supergroup" },
        text: "hey @arguschat_bot please check this",
        entities: [{ type: "mention", offset: 4, length: 14 }]
      },
      "arguschat_bot"
    ),
    true
  );
  assert.equal(
    isTelegramMessageDirectedAtBot(
      {
        chat: { id: -4633273294, type: "supergroup" },
        text: "this is for everyone else"
      },
      "arguschat_bot"
    ),
    false
  );
});

test("isTelegramMessageDirectedAtBot detects replies to the bot", () => {
  assert.equal(
    isTelegramMessageDirectedAtBot(
      {
        chat: { id: -4633273294, type: "supergroup" },
        text: "follow up",
        reply_to_message: {
          from: {
            id: 12345,
            is_bot: true,
            username: "ArgusChat_Bot"
          },
          text: "previous bot answer"
        }
      },
      "@arguschat_bot"
    ),
    true
  );
});

test("buildNodeConnectionCommand renders a shell-safe copyable command", () => {
  assert.equal(
    buildNodeConnectionCommand("ws://example.com:8080/nodes/ws?token=abc123"),
    "argus --host example.com:8080 --token abc123"
  );
  assert.equal(
    buildNodeConnectionCommand("wss://example.com/nodes/ws?token=abc123"),
    "argus --gateway https://example.com --token abc123"
  );
  assert.equal(
    buildNodeConnectionCommand("ws://example.com:8080/custom/ws?token=abc123"),
    'argus --url "ws://example.com:8080/custom/ws?token=abc123"'
  );
  assert.equal(buildNodeConnectionCommand(""), null);
});

test("buildArgusCliInstallCommand renders the GitHub installer command", () => {
  assert.equal(
    buildArgusCliInstallCommand(),
    'curl -fsSL "https://raw.githubusercontent.com/yym68686/argus/main/scripts/install-argus.sh" | bash'
  );
});

test("node lifecycle commands are short copyable commands", () => {
  assert.equal(buildNodeLogsCommand(), "argus logs");
  assert.equal(buildNodeReconnectCommand(), "argus reconnect");
  assert.equal(buildNodeDisconnectCommand(), "argus disconnect");
});

test("derivePublicNodeWsUrl prefers the public base url for copy commands", () => {
  assert.equal(
    derivePublicNodeWsUrl({
      publicNodeWsUrl: null,
      publicBaseUrl: "http://91.103.121.64:8080",
      fallbackBaseUrl: "http://gateway:8080",
      pathName: "/nodes/ws",
      token: "argus-node-v1.session.sig"
    }),
    "ws://91.103.121.64:8080/nodes/ws?token=argus-node-v1.session.sig"
  );

  assert.equal(
    derivePublicNodeWsUrl({
      publicNodeWsUrl: "wss://example.com/custom/ws",
      publicBaseUrl: null,
      fallbackBaseUrl: "http://gateway:8080",
      pathName: "/nodes/ws",
      token: "abc123"
    }),
    "wss://example.com/custom/ws?token=abc123"
  );
});
