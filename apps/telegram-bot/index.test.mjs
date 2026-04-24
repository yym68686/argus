import test from "node:test";
import assert from "node:assert/strict";

import {
  deriveTelegramWebhookSecret,
  isTelegramGetUpdatesWebhookConflict,
  resolveTelegramWebhookConfig,
  telegramWebhookInfoLogFields
} from "./index.mjs";

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
