import importlib.util
import json
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


class GatewayOpenAISettingsTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self) -> argus_app.AutomationManager:
        return argus_app.AutomationManager(
            state_store=InMemoryStateStore(),
            home_host_path="/tmp",
            workspace_host_path="/tmp",
        )

    async def test_stored_gateway_key_and_base_url_override_env(self) -> None:
        manager = self.make_manager()
        try:
            with mock.patch.dict(
                argus_app.os.environ,
                {
                    "OPENAI_API_KEY": "sk-env-key-value",
                    "ARGUS_OPENAI_RESPONSES_UPSTREAM_URL": "https://env.example/v1/responses",
                },
                clear=False,
            ):
                before = manager.get_gateway_openai_access_settings()
                self.assertEqual(before["gatewayOpenaiApiKeySource"], "env")
                self.assertEqual(before["gatewayOpenaiBaseUrl"], "https://env.example/v1")

                after = await manager.set_gateway_openai_settings(
                    api_key="sk-stored-key-value",
                    update_api_key=True,
                    base_url="https://proxy.example/v1/responses",
                    update_base_url=True,
                )

                self.assertEqual(after["gatewayOpenaiApiKeySource"], "stored")
                self.assertEqual(after["gatewayOpenaiApiKeyMasked"], "sk-s***alue")
                self.assertEqual(after["gatewayOpenaiBaseUrl"], "https://proxy.example/v1")
                self.assertEqual(after["gatewayOpenaiResponsesUrl"], "https://proxy.example/v1/responses")

                target = manager.resolve_openai_proxy_target_for_session("ownerless")
                self.assertEqual(target["apiKey"], "sk-stored-key-value")
                self.assertEqual(target["baseUrl"], "https://proxy.example/v1")
                self.assertEqual(target["upstreamUrl"], "https://proxy.example/v1/responses")

                redacted = json.dumps(manager._store.state.to_json(redact_secrets=True), sort_keys=True)
                self.assertIn("sk-s***alue", redacted)
                self.assertNotIn("sk-stored-key-value", redacted)
        finally:
            await manager.stop()

    async def test_clearing_stored_gateway_settings_falls_back_to_env(self) -> None:
        manager = self.make_manager()
        try:
            with mock.patch.dict(
                argus_app.os.environ,
                {
                    "ARGUS_OPENAI_API_KEY": "sk-env-key-value",
                    "ARGUS_OPENAI_RESPONSES_UPSTREAM_URL": "https://env.example/v1/responses",
                },
                clear=False,
            ):
                await manager.set_gateway_openai_settings(
                    api_key="sk-stored-key-value",
                    update_api_key=True,
                    base_url="https://proxy.example/v1",
                    update_base_url=True,
                )
                after = await manager.set_gateway_openai_settings(
                    api_key=None,
                    update_api_key=True,
                    base_url=None,
                    update_base_url=True,
                )

                self.assertEqual(after["gatewayOpenaiApiKeySource"], "env")
                self.assertFalse(after["hasStoredGatewayOpenaiApiKey"])
                self.assertEqual(after["gatewayOpenaiBaseUrl"], "https://env.example/v1")
                self.assertFalse(after["hasStoredGatewayOpenaiBaseUrl"])

                target = manager.resolve_openai_proxy_target_for_session("ownerless")
                self.assertEqual(target["apiKey"], "sk-env-key-value")
                self.assertEqual(target["upstreamUrl"], "https://env.example/v1/responses")
        finally:
            await manager.stop()

    async def test_invalid_gateway_base_url_is_rejected(self) -> None:
        manager = self.make_manager()
        try:
            with self.assertRaises(ValueError):
                await manager.set_gateway_openai_settings(base_url="ftp://example.invalid/v1", update_base_url=True)
        finally:
            await manager.stop()


if __name__ == "__main__":
    unittest.main()
