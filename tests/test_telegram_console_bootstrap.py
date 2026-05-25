import importlib.util
import pathlib
import sys
import unittest


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


class TelegramConsoleBootstrapTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self) -> argus_app.AutomationManager:
        return argus_app.AutomationManager(
            state_store=InMemoryStateStore(),
            home_host_path="/tmp",
            workspace_host_path="/tmp",
        )

    async def test_bootstrap_creates_console_user_with_tg_login_and_password(self) -> None:
        manager = self.make_manager()

        user, link, profile, password, created = await manager.ensure_telegram_console_user(
            telegram_user_id=123456789,
            chat_key="123456789",
            username="alice",
            first_name="Alice",
            last_name="Example",
        )

        self.assertTrue(created)
        self.assertEqual(user.user_id, 123456789)
        self.assertEqual(user.username, "123456789")
        self.assertIsNone(user.email)
        self.assertEqual(link.console_user_id, 123456789)
        self.assertEqual(profile.user_id, 123456789)
        self.assertIsNotNone(password)
        self.assertGreaterEqual(len(password or ""), 8)
        self.assertEqual(manager.authenticate_console_user(email="123456789", password=password).user_id, 123456789)

    async def test_bootstrap_is_idempotent_and_keeps_login_working(self) -> None:
        manager = self.make_manager()

        first_user, _, _, password, created_first = await manager.ensure_telegram_console_user(
            telegram_user_id=42,
            chat_key="42",
            username="first",
        )
        second_user, _, _, second_password, created_second = await manager.ensure_telegram_console_user(
            telegram_user_id=42,
            chat_key="42",
            username="second",
        )

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first_user.user_id, second_user.user_id)
        self.assertEqual(password, second_password)
        self.assertEqual(manager.authenticate_console_user(email="42", password=password).user_id, 42)

    async def test_binding_email_allows_email_and_tg_login(self) -> None:
        manager = self.make_manager()

        _, _, _, password, _ = await manager.ensure_telegram_console_user(
            telegram_user_id=77,
            chat_key="77",
            username="user77",
        )
        updated = await manager.set_console_user_email(user_id=77, email="user77@example.com")

        self.assertEqual(updated.email, "user77@example.com")
        self.assertEqual(manager.authenticate_console_user(email="user77@example.com", password=password).user_id, 77)
        self.assertEqual(manager.authenticate_console_user(email="77", password=password).user_id, 77)


if __name__ == "__main__":
    unittest.main()
