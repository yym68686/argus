import asyncio
import importlib.util
import pathlib
import sys
import tempfile
import threading
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


class DummyWriter:
    def __init__(self) -> None:
        self._closing = False

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True


class RuntimeSessionProvisioningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.patchers = [
            mock.patch.object(argus_app.app.state, "sessions_lock", asyncio.Lock(), create=True),
            mock.patch.object(argus_app.app.state, "sessions", {}, create=True),
            mock.patch.object(argus_app.app.state, "session_provision_locks", {}, create=True),
        ]
        for patcher in self.patchers:
            patcher.start()

    async def asyncTearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmpdir.cleanup()

    async def test_fugue_provisioning_serializes_same_session(self) -> None:
        session_id = "3416ab8781ab"
        cfg = argus_app.FugueProvisionConfig(
            base_url="https://fugue.invalid",
            token="token",
            project_id="project_123",
            runtime_id="runtime_123",
            gateway_internal_host="gateway.internal",
            runtime_cmd="codex serve",
            connect_timeout_s=1.0,
        )
        provision_started = threading.Event()
        release_provision = threading.Event()
        create_calls = 0

        def fake_ensure_session_app_ready_sync(patched_cfg, patched_session_id, allow_create):
            nonlocal create_calls
            self.assertIs(patched_cfg, cfg)
            self.assertEqual(patched_session_id, session_id)
            self.assertTrue(allow_create)
            create_calls += 1
            provision_started.set()
            self.assertTrue(release_provision.wait(timeout=2.0))
            return (
                {
                    "id": "app_123",
                    "name": argus_app._fugue_app_name(cfg, session_id),
                    "internal_service": {"host": "fugue.internal", "port": 7777},
                },
                True,
            )

        async def fake_wait_for_tcp(host: str, port: int, timeout_s: float):
            self.assertEqual(host, "fugue.internal")
            self.assertEqual(port, 7777)
            self.assertEqual(timeout_s, cfg.connect_timeout_s)
            return asyncio.StreamReader(), DummyWriter()

        async def fake_activate_live_session(live):
            async with argus_app.app.state.sessions_lock:
                argus_app.app.state.sessions[live.session_id] = live
            return live

        with (
            mock.patch.object(argus_app, "_fugue_cfg", return_value=cfg),
            mock.patch.object(argus_app, "_resolve_workspace_host_path_for_session", return_value=None),
            mock.patch.object(argus_app, "_derive_default_workspace_host_path_for_session", return_value=self.tmpdir.name),
            mock.patch.object(argus_app, "_fugue_ensure_session_app_ready_sync", side_effect=fake_ensure_session_app_ready_sync),
            mock.patch.object(argus_app, "_wait_for_tcp", side_effect=fake_wait_for_tcp),
            mock.patch.object(argus_app, "_activate_live_session", side_effect=fake_activate_live_session),
            mock.patch.object(argus_app, "_sync_remote_workspace_templates", new=mock.AsyncMock()),
        ):
            first_task = asyncio.create_task(argus_app._ensure_live_fugue_session(session_id, allow_create=True))
            self.assertTrue(await asyncio.to_thread(provision_started.wait, 2.0))

            second_task = asyncio.create_task(argus_app._ensure_live_fugue_session(session_id, allow_create=True))
            await asyncio.sleep(0.05)

            self.assertEqual(create_calls, 1)
            self.assertFalse(second_task.done())

            release_provision.set()
            (first_live, first_created), (second_live, second_created) = await asyncio.gather(first_task, second_task)

        self.assertEqual(create_calls, 1)
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertIs(first_live, second_live)
        self.assertEqual(first_live.runtime_id, "app_123")

    async def test_fugue_session_discovery_handles_suffixed_app_names(self) -> None:
        cfg = argus_app.FugueProvisionConfig(
            base_url="https://fugue.invalid",
            token="token",
            project_id="project_123",
            runtime_id="runtime_123",
            gateway_internal_host="gateway.internal",
            runtime_cmd="codex serve",
            connect_timeout_s=1.0,
        )
        apps = [
            {
                "id": "app_123",
                "project_id": "project_123",
                "name": "argus-session-3416ab8781ab-meadow",
                "status": {"phase": "deployed"},
                "updated_at": "2026-04-23T05:24:55Z",
            },
            {
                "id": "app_other",
                "project_id": "project_123",
                "name": "argus-session-1234567890ab-river",
                "status": {"phase": "failed"},
                "updated_at": "2026-04-23T05:20:00Z",
            },
        ]
        delete_calls: list[tuple[str, str, dict[str, str] | None]] = []

        def fake_request_json_sync(patched_cfg, method, path, **kwargs):
            self.assertIs(patched_cfg, cfg)
            delete_calls.append((method, path, kwargs.get("params")))
            return {}

        with (
            mock.patch.object(argus_app, "_fugue_cfg", return_value=cfg),
            mock.patch.object(argus_app, "_fugue_list_apps_sync", return_value=apps),
            mock.patch.object(argus_app, "_fugue_request_json_sync", side_effect=fake_request_json_sync),
        ):
            listed = argus_app._fugue_list_argus_sessions_sync()
            found = argus_app._fugue_find_session_app_sync(cfg, "3416ab8781ab")
            deleted = argus_app._fugue_delete_session_sync("3416ab8781ab", True)

        self.assertEqual(len(listed), 2)
        self.assertEqual(listed[0]["sessionId"], "1234567890ab")
        self.assertEqual(listed[1]["sessionId"], "3416ab8781ab")
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], "app_123")
        self.assertTrue(deleted)
        self.assertEqual(delete_calls, [("DELETE", "/v1/apps/app_123", {"force": "true"})])


if __name__ == "__main__":
    unittest.main()
