import contextlib
import importlib.util
import os
import pathlib
import sys
import tempfile
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


class MarkdownComplexityTests(unittest.TestCase):
    def test_markdown_to_telegram_html_preserves_inline_placeholders(self) -> None:
        rendered = argus_app._markdown_to_telegram_html(
            "# Title\n"
            "Use `code` and [docs](https://example.com).\n\n"
            "```python\n"
            "print('ok')\n"
            "```\n"
            "- item"
        )

        self.assertEqual(
            rendered,
            "<b>Title</b>\n"
            "Use <code>code</code> and <a href=\"https://example.com\">docs</a>.\n"
            "\n"
            "<pre><code>print('ok')\n</code></pre>\n"
            "• item",
        )


class SQLiteStateStoreComplexityTests(unittest.TestCase):
    def test_state_probe_and_meta_load_use_combined_queries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = argus_app._SQLiteAutomationStateStore(pathlib.Path(tmpdir) / "state.db", legacy_state_path=None)
            with contextlib.closing(store._connect()) as conn:
                store._initialize_schema(conn)
                self.assertFalse(store._db_has_state(conn))
                with conn:
                    conn.executemany(
                        "INSERT INTO automation_state_meta(key, value_text, updated_at_ms) VALUES (?, ?, ?)",
                        [
                            ("version", "7", 1),
                            ("gateway_openai_default_enabled", "false", 1),
                            ("telegram_bot_token", "123:abc", 1),
                        ],
                    )
                state = store._read_state(conn)

            with contextlib.closing(store._connect()) as probe_conn:
                self.assertTrue(store._db_has_state(probe_conn))
            self.assertEqual(state.version, 7)
            self.assertFalse(state.gateway_openai_default_enabled)
            self.assertEqual(state.telegram_bot_token, "123:abc")


class GatewayOpenAIDefaultTests(unittest.TestCase):
    def test_default_is_enabled_when_env_is_unset(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            state = argus_app.PersistedGatewayAutomationState()
        self.assertTrue(state.gateway_openai_default_enabled)
        self.assertEqual(state.gateway_openai_default_source, "default")

    def test_env_can_disable_the_default(self) -> None:
        with mock.patch.dict(os.environ, {"ARGUS_GATEWAY_OPENAI_DEFAULT_ENABLED": "false"}, clear=True):
            state = argus_app.PersistedGatewayAutomationState()
        self.assertFalse(state.gateway_openai_default_enabled)
        self.assertEqual(state.gateway_openai_default_source, "env")

    def test_env_false_migrates_legacy_stored_gateway_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = argus_app._SQLiteAutomationStateStore(pathlib.Path(tmpdir) / "state.db", legacy_state_path=None)
            with contextlib.closing(store._connect()) as conn:
                store._initialize_schema(conn)
                with conn:
                    conn.executemany(
                        "INSERT INTO automation_state_meta(key, value_text, updated_at_ms) VALUES (?, ?, ?)",
                        [
                            ("version", "13", 1),
                            ("gateway_openai_default_enabled", "true", 1),
                        ],
                    )
                with mock.patch.dict(os.environ, {"ARGUS_GATEWAY_OPENAI_DEFAULT_ENABLED": "false"}, clear=True):
                    state = store._read_state(conn)

        self.assertFalse(state.gateway_openai_default_enabled)
        self.assertEqual(state.gateway_openai_default_source, "env")

    def test_admin_gateway_default_survives_env_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = argus_app._SQLiteAutomationStateStore(pathlib.Path(tmpdir) / "state.db", legacy_state_path=None)
            with contextlib.closing(store._connect()) as conn:
                store._initialize_schema(conn)
                with conn:
                    conn.executemany(
                        "INSERT INTO automation_state_meta(key, value_text, updated_at_ms) VALUES (?, ?, ?)",
                        [
                            ("version", "14", 1),
                            ("gateway_openai_default_enabled", "true", 1),
                            ("gateway_openai_default_source", "admin", 1),
                        ],
                    )
                with mock.patch.dict(os.environ, {"ARGUS_GATEWAY_OPENAI_DEFAULT_ENABLED": "false"}, clear=True):
                    state = store._read_state(conn)

        self.assertTrue(state.gateway_openai_default_enabled)
        self.assertEqual(state.gateway_openai_default_source, "admin")


class UserDeletionComplexityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_store = InMemoryStateStore()
        self.manager = argus_app.AutomationManager(
            state_store=self.state_store,
            home_host_path=self.tmpdir.name,
            workspace_host_path=self.tmpdir.name,
        )

    async def asyncTearDown(self) -> None:
        await self.manager.stop()
        self.tmpdir.cleanup()

    async def test_delete_user_keeps_sessions_still_referenced_by_other_agents(self) -> None:
        state = self.state_store.state
        state.agents = {
            "u1-main": argus_app.PersistedAgentRuntime(
                agent_id="u1-main",
                session_id="shared-session",
                workspace_host_path=str(pathlib.Path(self.tmpdir.name) / "u1-main"),
                created_at_ms=1,
                owner_user_id=1,
            ),
            "u1-extra": argus_app.PersistedAgentRuntime(
                agent_id="u1-extra",
                session_id="owned-session",
                workspace_host_path=str(pathlib.Path(self.tmpdir.name) / "u1-extra"),
                created_at_ms=1,
                owner_user_id=1,
            ),
            "u2-main": argus_app.PersistedAgentRuntime(
                agent_id="u2-main",
                session_id="shared-session",
                workspace_host_path=str(pathlib.Path(self.tmpdir.name) / "u2-main"),
                created_at_ms=1,
                owner_user_id=2,
            ),
        }
        state.sessions = {
            "shared-session": argus_app.PersistedSessionAutomation(owner_user_id=1),
            "owned-session": argus_app.PersistedSessionAutomation(owner_user_id=1),
        }

        with mock.patch.object(self.manager, "_schedule_user_agent_cleanup") as schedule_cleanup:
            result = await self.manager.delete_user(user_id=1)

        self.assertEqual(set(result["deletedAgentIds"]), {"u1-main", "u1-extra"})
        self.assertEqual(result["deletedSessionIds"], ["owned-session"])
        self.assertIn("shared-session", state.sessions)
        self.assertNotIn("owned-session", state.sessions)
        schedule_cleanup.assert_called_once_with(agent_id="u1-extra", session_id="owned-session")


if __name__ == "__main__":
    unittest.main()
