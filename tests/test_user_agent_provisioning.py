import asyncio
import importlib.util
import pathlib
import sys
import tempfile
import types
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


def make_request(query_string: str = ""):
    return argus_app.Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/me/agents/test/connection",
            "headers": [],
            "query_string": query_string.encode("utf-8"),
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "root_path": "",
        }
    )


class UserAgentProvisioningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.manager = argus_app.AutomationManager(
            state_store=InMemoryStateStore(),
            home_host_path=self.tmpdir.name,
            workspace_host_path=self.tmpdir.name,
        )

    async def asyncTearDown(self) -> None:
        await self.manager.stop()
        self.tmpdir.cleanup()

    async def test_create_user_agent_returns_pending_then_ready_and_is_idempotent(self) -> None:
        gate = asyncio.Event()

        async def fake_ensure_live_session(session_id: str, *, allow_create: bool):
            self.assertTrue(allow_create)
            self.assertTrue(session_id)
            await gate.wait()
            return types.SimpleNamespace(provider="fake"), True

        with (
            mock.patch.object(argus_app, "_require_user_agent_support", return_value=None),
            mock.patch.object(argus_app, "_ensure_live_session", side_effect=fake_ensure_live_session),
        ):
            created_agent, created = await self.manager.create_user_agent(user_id=1, short_name="alpha")
            self.assertTrue(created)
            self.assertEqual(created_agent.provisioning_state, argus_app.AGENT_PROVISIONING_STATE_PENDING)

            duplicate_agent, duplicate_created = await self.manager.create_user_agent(user_id=1, short_name="alpha")
            self.assertFalse(duplicate_created)
            self.assertEqual(duplicate_agent.agent_id, created_agent.agent_id)
            self.assertEqual(
                len(self.manager.list_agents_for_user(user_id=1)),
                1,
            )

            persisted_pending = self.manager._get_agent(created_agent.agent_id)
            self.assertIsNotNone(persisted_pending)
            self.assertEqual(
                persisted_pending.provisioning_state,
                argus_app.AGENT_PROVISIONING_STATE_PENDING,
            )

            gate.set()
            ready_agent = await self.manager.wait_for_user_agent_provisioning(
                agent_id=created_agent.agent_id,
                timeout_ms=2000,
            )

        self.assertIsNotNone(ready_agent)
        self.assertEqual(ready_agent.provisioning_state, argus_app.AGENT_PROVISIONING_STATE_READY)
        self.assertIsNone(ready_agent.provisioning_error)
        self.assertIsNotNone(ready_agent.last_ready_at_ms)

    async def test_failed_provisioning_can_be_retried(self) -> None:
        attempts = 0

        async def fake_ensure_live_session(session_id: str, *, allow_create: bool):
            nonlocal attempts
            self.assertTrue(allow_create)
            self.assertTrue(session_id)
            attempts += 1
            if attempts == 1:
                raise RuntimeError("filesystem temporary failure")
            return types.SimpleNamespace(provider="fake"), True

        with (
            mock.patch.object(argus_app, "_require_user_agent_support", return_value=None),
            mock.patch.object(argus_app, "_ensure_live_session", side_effect=fake_ensure_live_session),
            mock.patch.object(argus_app, "_provisioner_manages_runtime_sessions", return_value=False),
            mock.patch.object(self.manager, "_force_close_live_session", new=mock.AsyncMock()),
        ):
            agent, created = await self.manager.create_user_agent(user_id=1, short_name="retry-me")
            self.assertTrue(created)

            failed_agent = await self.manager.wait_for_user_agent_provisioning(
                agent_id=agent.agent_id,
                timeout_ms=2000,
            )
            self.assertIsNotNone(failed_agent)
            self.assertEqual(failed_agent.provisioning_state, argus_app.AGENT_PROVISIONING_STATE_FAILED)
            self.assertIn("filesystem temporary failure", failed_agent.provisioning_error or "")

            retried = await self.manager.retry_user_agent_provisioning(user_id=1, agent_id=agent.agent_id)
            self.assertEqual(retried.provisioning_state, argus_app.AGENT_PROVISIONING_STATE_PENDING)

            ready_agent = await self.manager.wait_for_user_agent_provisioning(
                agent_id=agent.agent_id,
                timeout_ms=2000,
            )

        self.assertIsNotNone(ready_agent)
        self.assertEqual(ready_agent.provisioning_state, argus_app.AGENT_PROVISIONING_STATE_READY)
        self.assertEqual(attempts, 2)

    async def test_connection_payload_returns_409_while_agent_is_pending(self) -> None:
        gate = asyncio.Event()

        async def fake_ensure_live_session(session_id: str, *, allow_create: bool):
            self.assertTrue(allow_create)
            self.assertTrue(session_id)
            await gate.wait()
            return types.SimpleNamespace(provider="fake"), True

        with (
            mock.patch.object(argus_app, "_require_user_agent_support", return_value=None),
            mock.patch.object(argus_app, "_ensure_live_session", side_effect=fake_ensure_live_session),
        ):
            agent, _ = await self.manager.create_user_agent(user_id=1, short_name="pending")
            response = await argus_app._self_agent_connection_payload(
                self.manager,
                request=make_request(),
                user_id=1,
                agent_id=agent.agent_id,
            )
            gate.set()
            await self.manager.wait_for_user_agent_provisioning(agent_id=agent.agent_id, timeout_ms=2000)

        self.assertIsInstance(response, argus_app.JSONResponse)
        self.assertEqual(response.status_code, 409)
        self.assertIn("provision", response.body.decode("utf-8").lower())

    async def test_connection_payload_waits_for_ready_agent(self) -> None:
        gate = asyncio.Event()

        async def fake_ensure_live_session(session_id: str, *, allow_create: bool):
            self.assertTrue(allow_create)
            self.assertTrue(session_id)
            await gate.wait()
            return types.SimpleNamespace(provider="fake"), True

        async def release_gate() -> None:
            await asyncio.sleep(0.05)
            gate.set()

        with (
            mock.patch.object(argus_app, "_require_user_agent_support", return_value=None),
            mock.patch.object(argus_app, "_ensure_live_session", side_effect=fake_ensure_live_session),
        ):
            agent, _ = await self.manager.create_user_agent(user_id=1, short_name="waitable")
            releaser = asyncio.create_task(release_gate())
            try:
                payload = await argus_app._self_agent_connection_payload(
                    self.manager,
                    request=make_request("wait=true&timeoutMs=1000"),
                    user_id=1,
                    agent_id=agent.agent_id,
                )
            finally:
                await releaser

        self.assertIsInstance(payload, dict)
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("agentId"), agent.agent_id)
        self.assertEqual(payload.get("provider"), "fake")

    async def test_connection_payload_caps_wait_timeout(self) -> None:
        gate = asyncio.Event()

        async def fake_ensure_live_session(session_id: str, *, allow_create: bool):
            self.assertTrue(allow_create)
            self.assertTrue(session_id)
            await gate.wait()
            return types.SimpleNamespace(provider="fake"), True

        with (
            mock.patch.object(argus_app, "_require_user_agent_support", return_value=None),
            mock.patch.object(argus_app, "_ensure_live_session", side_effect=fake_ensure_live_session),
            mock.patch.object(argus_app, "PUBLIC_AGENT_CONNECTION_WAIT_MAX_MS", 25),
            mock.patch.object(
                self.manager,
                "wait_for_user_agent_provisioning",
                new=mock.AsyncMock(side_effect=self.manager.wait_for_user_agent_provisioning),
            ) as wait_for_provisioning,
        ):
            agent, _ = await self.manager.create_user_agent(user_id=1, short_name="capped")
            response = await argus_app._self_agent_connection_payload(
                self.manager,
                request=make_request("wait=true&timeoutMs=120000"),
                user_id=1,
                agent_id=agent.agent_id,
            )
            gate.set()
            await self.manager.wait_for_user_agent_provisioning(agent_id=agent.agent_id, timeout_ms=2000)

        self.assertIsInstance(response, argus_app.JSONResponse)
        self.assertEqual(response.status_code, 409)
        wait_for_provisioning.assert_any_await(agent_id=agent.agent_id, timeout_ms=25)

    async def test_delete_user_agent_returns_before_cleanup_finishes(self) -> None:
        provision_gate = asyncio.Event()
        cleanup_gate = asyncio.Event()

        async def fake_ensure_live_session(session_id: str, *, allow_create: bool):
            self.assertTrue(allow_create)
            self.assertTrue(session_id)
            await provision_gate.wait()
            return types.SimpleNamespace(provider="fake"), True

        async def fake_remove_managed_runtime(session_id: str):
            await cleanup_gate.wait()
            return True

        with (
            mock.patch.object(argus_app, "_require_user_agent_support", return_value=None),
            mock.patch.object(argus_app, "_ensure_live_session", side_effect=fake_ensure_live_session),
            mock.patch.object(argus_app, "_provisioner_manages_runtime_sessions", return_value=True),
            mock.patch.object(argus_app, "_remove_managed_runtime", side_effect=fake_remove_managed_runtime),
            mock.patch.object(self.manager, "_force_close_live_session", new=mock.AsyncMock()),
        ):
            agent, _ = await self.manager.create_user_agent(user_id=1, short_name="delete-pending")
            result = await asyncio.wait_for(
                self.manager.delete_user_agent(
                    user_id=1,
                    chat_key="user:1:private",
                    agent_id=agent.agent_id,
                ),
                timeout=0.5,
            )
            self.assertTrue(result.get("cleanupScheduled"))
            self.assertIsNone(self.manager._get_agent(agent.agent_id))
            self.assertTrue(self.manager._agent_cleanup_tasks)
            cleanup_gate.set()
            await asyncio.wait_for(asyncio.gather(*list(self.manager._agent_cleanup_tasks)), timeout=1.0)


if __name__ == "__main__":
    unittest.main()
