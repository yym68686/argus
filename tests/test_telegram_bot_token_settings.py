import importlib.util
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "apps/api/app.py"
SPEC = importlib.util.spec_from_file_location("argus_app", APP_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load module from {APP_PATH}")
argus_app = importlib.util.module_from_spec(SPEC)
sys.modules.setdefault("argus_app", argus_app)
SPEC.loader.exec_module(argus_app)


class InMemoryStateStore:
    def __init__(self) -> None:
        self.state = argus_app.PersistedGatewayAutomationState()

    async def load(self):
        return self.state

    async def update(self, fn):
        fn(self.state)
        return self.state


class TelegramBotTokenSettingsTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self) -> argus_app.AutomationManager:
        return argus_app.AutomationManager(
            state_store=InMemoryStateStore(),
            home_host_path="/tmp",
            workspace_host_path="/tmp",
        )

    async def test_stored_telegram_token_overrides_env_and_is_masked(self) -> None:
        manager = self.make_manager()

        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "111111:env-token-value"}, clear=False):
            before = manager.get_telegram_bot_token_settings()
            self.assertEqual(before["source"], "env")
            self.assertEqual(before["tokenMasked"], "1111***alue")

            after = await manager.set_telegram_bot_token(token="222222:stored-token-value")

            self.assertEqual(after["source"], "stored")
            self.assertTrue(after["hasStoredToken"])
            self.assertEqual(after["tokenMasked"], "2222***alue")
            self.assertEqual(manager.get_effective_telegram_bot_token(), "222222:stored-token-value")
            self.assertEqual(manager._store.state.to_json(redact_secrets=True)["telegramBotToken"], "2222***alue")
            self.assertNotIn("stored-token-value", manager._store.state.to_json(redact_secrets=True)["telegramBotToken"])

    async def test_clearing_stored_telegram_token_falls_back_to_env(self) -> None:
        manager = self.make_manager()

        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "111111:env-token-value"}, clear=False):
            await manager.set_telegram_bot_token(token="222222:stored-token-value")
            after = await manager.set_telegram_bot_token(token=None)

            self.assertEqual(after["source"], "env")
            self.assertFalse(after["hasStoredToken"])
            self.assertEqual(manager.get_effective_telegram_bot_token(), "111111:env-token-value")

    async def test_invalid_telegram_token_is_rejected(self) -> None:
        manager = self.make_manager()

        with self.assertRaises(ValueError):
            await manager.set_telegram_bot_token(token="not-a-token")


if __name__ == "__main__":
    unittest.main()
