import asyncio
import importlib.util
import json
import os
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


def make_response(status_code: int, payload=None):
    response = mock.Mock()
    response.status_code = status_code
    if payload is None:
        response.content = b""
    else:
        response.content = json.dumps(payload).encode("utf-8")
        response.json.return_value = payload
    return response


class FugueApiHelperTests(unittest.TestCase):
    def _base_fugue_env(self, **overrides):
        env = {
            "ARGUS_FUGUE_BASE_URL": "https://fugue.invalid",
            "ARGUS_FUGUE_TOKEN": "token",
            "ARGUS_FUGUE_PROJECT_ID": "project_123",
            "ARGUS_GATEWAY_INTERNAL_HOST": "gateway.internal",
            "ARGUS_RUNTIME_CMD": "codex serve",
        }
        env.update(overrides)
        return env

    def test_fugue_request_json_sync_retries_transient_get_failures(self) -> None:
        cfg = argus_app.FugueProvisionConfig(
            base_url="https://fugue.invalid",
            token="token",
            project_id="project_123",
            runtime_id="runtime_123",
            gateway_internal_host="gateway.internal",
            runtime_cmd="codex serve",
            connect_timeout_s=1.0,
        )
        retryable = make_response(502, {"error": "bad gateway"})
        success = make_response(200, {"ok": True})

        with (
            mock.patch.object(argus_app.requests, "request", side_effect=[retryable, success]) as request_mock,
            mock.patch.object(argus_app.time, "sleep") as sleep_mock,
        ):
            payload = argus_app._fugue_request_json_sync(cfg, "GET", "/v1/apps", expected_statuses=(200,))

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(request_mock.call_count, 2)
        sleep_mock.assert_called_once_with(0.25)

    def test_fugue_request_json_sync_does_not_retry_post_failures(self) -> None:
        cfg = argus_app.FugueProvisionConfig(
            base_url="https://fugue.invalid",
            token="token",
            project_id="project_123",
            runtime_id="runtime_123",
            gateway_internal_host="gateway.internal",
            runtime_cmd="codex serve",
            connect_timeout_s=1.0,
        )
        retryable = make_response(502, {"error": "bad gateway"})

        with (
            mock.patch.object(argus_app.requests, "request", return_value=retryable) as request_mock,
            mock.patch.object(argus_app.time, "sleep") as sleep_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, r"POST /v1/apps/import-image failed with 502"):
                argus_app._fugue_request_json_sync(
                    cfg,
                    "POST",
                    "/v1/apps/import-image",
                    body={"project_id": cfg.project_id},
                    expected_statuses=(202,),
                )

        request_mock.assert_called_once()
        sleep_mock.assert_not_called()

    def test_fugue_cfg_rejects_static_image_without_explicit_mode(self) -> None:
        env = self._base_fugue_env(ARGUS_FUGUE_RUNTIME_IMAGE="registry.invalid/app@sha256:deadbeef")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "ARGUS_FUGUE_RUNTIME_IMAGE_MODE=static"):
                argus_app._fugue_cfg()

    def test_fugue_cfg_rejects_image_with_compose_selector(self) -> None:
        env = self._base_fugue_env(
            ARGUS_FUGUE_RUNTIME_IMAGE="registry.invalid/app@sha256:deadbeef",
            ARGUS_FUGUE_RUNTIME_IMAGE_MODE="static",
            ARGUS_FUGUE_RUNTIME_COMPOSE_SERVICE="runtime",
        )
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "cannot be set together"):
                argus_app._fugue_cfg()

    def test_fugue_cfg_accepts_compose_runtime_source(self) -> None:
        env = self._base_fugue_env(ARGUS_FUGUE_RUNTIME_COMPOSE_SERVICE="runtime")
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = argus_app._fugue_cfg()
        self.assertEqual(cfg.runtime_compose_service, "runtime")
        self.assertIsNone(cfg.image)

    def test_fugue_runtime_image_from_app_prefers_current_spec_image(self) -> None:
        app_data = {
            "id": "app_template",
            "name": "runtime",
            "source": {"resolved_image_ref": "registry.invalid/runtime@sha256:old"},
            "spec": {"image": "registry.invalid/runtime@sha256:new"},
        }
        self.assertEqual(
            argus_app._fugue_runtime_image_from_app(app_data),
            "registry.invalid/runtime@sha256:new",
        )

    def test_fugue_preflight_uses_template_inventory(self) -> None:
        cfg = argus_app.FugueProvisionConfig(
            base_url="https://fugue.invalid",
            token="token",
            project_id="project_123",
            runtime_id="runtime_123",
            gateway_internal_host="gateway.internal",
            runtime_compose_service="runtime",
            runtime_cmd="codex serve",
            connect_timeout_s=1.0,
        )
        app_data = {
            "id": "app_template",
            "name": "runtime",
            "project_id": "project_123",
            "source": {"compose_service": "runtime", "resolved_image_ref": "registry.invalid/runtime@sha256:old"},
            "spec": {"image": "registry.invalid/runtime@sha256:new"},
            "status": {"phase": "deployed", "last_operation_id": "op_template"},
        }
        inventory = {
            "app_id": "app_template",
            "registry_configured": True,
            "versions": [
                {
                    "image_ref": "registry.invalid/runtime@sha256:new",
                    "runtime_image_ref": "registry.invalid/runtime@sha256:new",
                    "status": "available",
                    "current": True,
                }
            ],
        }

        with (
            mock.patch.object(argus_app, "_fugue_list_apps_sync", return_value=[app_data]),
            mock.patch.object(argus_app, "_fugue_get_app_sync", return_value=app_data),
            mock.patch.object(argus_app, "_fugue_get_app_image_inventory_sync", return_value=inventory),
            mock.patch.object(argus_app, "_registry_manifest_exists_sync") as registry_check,
        ):
            resolution = argus_app._fugue_preflight_runtime_image_sync(cfg)

        self.assertEqual(resolution.image_ref, "registry.invalid/runtime@sha256:new")
        self.assertEqual(resolution.source, "runtime_compose_service")
        self.assertEqual(resolution.runtime_app_id, "app_template")
        registry_check.assert_not_called()

    def test_fugue_preflight_missing_inventory_image_is_clear(self) -> None:
        cfg = argus_app.FugueProvisionConfig(
            base_url="https://fugue.invalid",
            token="token",
            project_id="project_123",
            runtime_id="runtime_123",
            gateway_internal_host="gateway.internal",
            runtime_compose_service="runtime",
            runtime_cmd="codex serve",
            connect_timeout_s=1.0,
        )
        app_data = {
            "id": "app_template",
            "name": "runtime",
            "project_id": "project_123",
            "source": {"compose_service": "runtime"},
            "spec": {"image": "registry.invalid/runtime@sha256:missing"},
        }
        inventory = {
            "app_id": "app_template",
            "registry_configured": True,
            "versions": [
                {
                    "image_ref": "registry.invalid/runtime@sha256:missing",
                    "runtime_image_ref": "registry.invalid/runtime@sha256:missing",
                    "status": "missing",
                    "current": True,
                }
            ],
        }

        with (
            mock.patch.object(argus_app, "_fugue_list_apps_sync", return_value=[app_data]),
            mock.patch.object(argus_app, "_fugue_get_app_sync", return_value=app_data),
            mock.patch.object(argus_app, "_fugue_get_app_image_inventory_sync", return_value=inventory),
        ):
            with self.assertRaisesRegex(argus_app.FugueRuntimeImageMissingError, "configured runtime image digest is missing"):
                argus_app._fugue_preflight_runtime_image_sync(cfg)

    def test_fugue_preflight_falls_back_to_registry_when_inventory_forbidden(self) -> None:
        cfg = argus_app.FugueProvisionConfig(
            base_url="https://fugue.invalid",
            token="token",
            project_id="project_123",
            runtime_id="runtime_123",
            gateway_internal_host="gateway.internal",
            runtime_compose_service="runtime",
            runtime_cmd="codex serve",
            connect_timeout_s=1.0,
        )
        app_data = {
            "id": "app_template",
            "name": "runtime",
            "project_id": "project_123",
            "source": {
                "compose_service": "runtime",
                "resolved_image_ref": "fugue-registry.svc.cluster.local:5000/runtime:git-abc123",
            },
            "spec": {"image": "registry.fugue.internal:5000/runtime@sha256:available"},
        }

        def fake_registry_check(image_ref: str, *, timeout_s: float) -> bool:
            if image_ref.startswith("registry.fugue.internal:"):
                raise RuntimeError("registry manifest check failed: name or service not known")
            return image_ref == "fugue-registry.svc.cluster.local:5000/runtime:git-abc123"

        with (
            mock.patch.object(argus_app, "_fugue_list_apps_sync", return_value=[app_data]),
            mock.patch.object(argus_app, "_fugue_get_app_sync", return_value=app_data),
            mock.patch.object(
                argus_app,
                "_fugue_get_app_image_inventory_sync",
                side_effect=RuntimeError("Fugue API GET /v1/apps/app_template/images failed with 403"),
            ),
            mock.patch.object(argus_app, "_registry_manifest_exists_sync", side_effect=fake_registry_check) as registry_check,
        ):
            resolution = argus_app._fugue_preflight_runtime_image_sync(cfg)

        self.assertEqual(resolution.image_ref, "registry.fugue.internal:5000/runtime@sha256:available")
        self.assertEqual(resolution.registry_check_ref, "fugue-registry.svc.cluster.local:5000/runtime:git-abc123")
        self.assertEqual(registry_check.call_count, 2)

    def test_fugue_create_session_does_not_post_when_image_preflight_fails(self) -> None:
        cfg = argus_app.FugueProvisionConfig(
            base_url="https://fugue.invalid",
            token="token",
            project_id="project_123",
            runtime_id="runtime_123",
            gateway_internal_host="gateway.internal",
            image="registry.invalid/runtime@sha256:missing",
            runtime_image_mode="static",
            runtime_cmd="codex serve",
            connect_timeout_s=1.0,
        )
        resolution = argus_app.FugueRuntimeImageResolution(
            image_ref="registry.invalid/runtime@sha256:missing",
            source="static",
        )
        err = argus_app.FugueRuntimeImageMissingError(
            "configured runtime image digest is missing: registry.invalid/runtime@sha256:missing",
            image_ref=resolution.image_ref,
        )

        with (
            mock.patch.object(argus_app, "_fugue_preflight_runtime_image_sync", side_effect=err),
            mock.patch.object(argus_app, "_fugue_request_json_sync") as request_mock,
        ):
            with self.assertRaises(argus_app.FugueRuntimeImageMissingError):
                argus_app._fugue_create_session_app_sync(cfg, "3416ab8781ab")

        request_mock.assert_not_called()

    def test_fugue_session_record_exposes_runtime_debug_fields(self) -> None:
        record = argus_app._fugue_session_record_from_app(
            "3416ab8781ab",
            {
                "id": "app_123",
                "name": "argus-session-3416ab8781ab",
                "source": {"resolved_image_ref": "registry.invalid/runtime@sha256:old"},
                "spec": {"image": "registry.invalid/runtime@sha256:new"},
                "status": {
                    "phase": "failed",
                    "last_message": "MANIFEST_UNKNOWN: manifest unknown",
                    "last_operation_id": "op_123",
                },
            },
        )
        self.assertEqual(record["runtimeAppId"], "app_123")
        self.assertEqual(record["imageRef"], "registry.invalid/runtime@sha256:new")
        self.assertEqual(record["lastMessage"], "MANIFEST_UNKNOWN: manifest unknown")
        self.assertEqual(record["operationId"], "op_123")

    def test_runtime_provision_close_reason_classifies_missing_digest(self) -> None:
        err = argus_app.FugueRuntimeImageMissingError("configured runtime image digest is missing")
        self.assertEqual(argus_app._runtime_provision_ws_close_reason(err), "Runtime image digest missing")


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

    async def test_fugue_wait_for_app_ready_surfaces_operation_failure(self) -> None:
        cfg = argus_app.FugueProvisionConfig(
            base_url="https://fugue.invalid",
            token="token",
            project_id="project_123",
            runtime_id="runtime_123",
            gateway_internal_host="gateway.internal",
            runtime_cmd="codex serve",
            connect_timeout_s=1.0,
        )
        app_data = {
            "id": "app_123",
            "status": {
                "phase": "deploying",
                "last_message": "deployment progressing (0/1 ready replicas)",
                "last_operation_id": "op_123",
            },
        }
        operation_data = {
            "id": "op_123",
            "status": "failed",
            "error_message": "Unschedulable: 0/5 nodes are available",
        }

        with (
            mock.patch.object(argus_app, "_fugue_get_app_sync", return_value=app_data),
            mock.patch.object(argus_app, "_fugue_get_operation_sync", return_value=operation_data),
        ):
            with self.assertRaisesRegex(RuntimeError, "Unschedulable: 0/5 nodes are available"):
                argus_app._fugue_wait_for_app_ready_sync(cfg, "app_123")

    async def test_fugue_wait_for_app_ready_prefers_current_ready_state(self) -> None:
        cfg = argus_app.FugueProvisionConfig(
            base_url="https://fugue.invalid",
            token="token",
            project_id="project_123",
            runtime_id="runtime_123",
            gateway_internal_host="gateway.internal",
            runtime_cmd="codex serve",
            connect_timeout_s=1.0,
        )
        app_data = {
            "id": "app_123",
            "status": {
                "phase": "deployed",
                "last_message": "deployment finished",
                "last_operation_id": "op_old",
            },
            "internal_service": {"host": "runtime.internal", "port": 7777},
        }

        with mock.patch.object(argus_app, "_fugue_get_operation_sync") as get_operation:
            with mock.patch.object(argus_app, "_fugue_get_app_sync", return_value=app_data):
                ready = argus_app._fugue_wait_for_app_ready_sync(cfg, "app_123")

        self.assertEqual(ready, app_data)
        get_operation.assert_not_called()

    async def test_fugue_wait_for_app_ready_timeout_includes_operation_context(self) -> None:
        cfg = argus_app.FugueProvisionConfig(
            base_url="https://fugue.invalid",
            token="token",
            project_id="project_123",
            runtime_id="runtime_123",
            gateway_internal_host="gateway.internal",
            runtime_cmd="codex serve",
            connect_timeout_s=10.0,
        )
        app_data = {
            "id": "app_123",
            "status": {
                "phase": "deploying",
                "last_message": "deployment progressing (0/1 ready replicas)",
                "last_operation_id": "op_123",
            },
        }
        operation_data = {
            "id": "op_123",
            "status": "running",
            "message": "still reconciling",
        }

        with (
            mock.patch.object(argus_app, "_fugue_get_app_sync", return_value=app_data),
            mock.patch.object(argus_app, "_fugue_get_operation_sync", return_value=operation_data),
            mock.patch.object(argus_app.time, "time", side_effect=[0.0, 61.0]),
        ):
            with self.assertRaisesRegex(
                TimeoutError,
                r"operation=op_123:running .*operationDetail=still reconciling",
            ):
                argus_app._fugue_wait_for_app_ready_sync(cfg, "app_123")


if __name__ == "__main__":
    unittest.main()
