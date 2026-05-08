import asyncio
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


class GatewayProxyRouteTests(unittest.IsolatedAsyncioTestCase):
    def make_request(self, path: str, method: str = "GET"):
        return argus_app.Request(
            {
                "type": "http",
                "method": method,
                "path": path,
                "headers": [(b"host", b"testserver")],
                "query_string": b"",
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 12345),
                "root_path": "",
            }
        )

    async def test_openai_compact_proxy_forwards_to_compact_child_path(self) -> None:
        request = self.make_request("/openai/v1/responses/compact", method="POST")
        upstream_payload = {"type": "response.completed", "response": {"id": "resp_1"}}
        upstream = mock.Mock()
        upstream.status_code = 200
        upstream.headers = {"content-type": "application/json"}
        upstream.content = b'{"ok":true}'
        upstream.json.return_value = upstream_payload
        upstream.close = mock.Mock()

        automation = mock.Mock()
        automation.resolve_openai_proxy_target_for_session.return_value = {
            "ownerUserId": None,
            "channelId": "gateway",
            "name": "gateway",
            "upstreamUrl": "https://upstream.invalid/v1/responses",
            "apiKey": "sk-test",
        }

        with (
            mock.patch.object(argus_app, "_openai_proxy_require_token", side_effect=lambda req: setattr(req.state, "openai_scoped_session_id", "sess_123")),
            mock.patch.object(argus_app, "_get_automation_or_500", return_value=automation),
            mock.patch.object(argus_app.requests, "post", return_value=upstream) as post_mock,
        ):
            response = argus_app.openai_responses_compact_proxy(request, {"model": "gpt-5.4"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b'{"ok":true}')
        self.assertEqual(
            post_mock.call_args.args[0],
            "https://upstream.invalid/v1/responses/compact",
        )

    async def test_mcp_get_returns_streaming_response_instead_of_405(self) -> None:
        request = self.make_request("/mcp", method="GET")
        session = mock.Mock(protocol_version="2025-11-25")

        with (
            mock.patch.object(argus_app, "_mcp_require_token", return_value=None),
            mock.patch.object(argus_app, "_mcp_get_session", return_value=session),
        ):
            response = await argus_app.mcp_get(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "text/event-stream")
        self.assertEqual(response.headers.get("MCP-Protocol-Version"), "2025-11-25")

    async def test_non_user_turn_skips_telegram_delivery_without_warning(self) -> None:
        manager = argus_app.AutomationManager(
            state_store=InMemoryStateStore(),
            home_host_path="/tmp",
            workspace_host_path="/tmp",
        )
        events: list[tuple[str, str, dict[str, object]]] = []

        def fake_event_log(level: str, event: str, **fields):
            events.append((level, event, fields))

        with (
            mock.patch.object(argus_app, "_event_log", side_effect=fake_event_log),
            mock.patch.object(manager, "_resolve_telegram_target_for_source", return_value=None),
            mock.patch.object(manager, "_resolve_telegram_target_for_turn", return_value=None),
        ):
            await manager._deliver_turn_text(
                "sess_1",
                "thread_1",
                "cron result",
                turn_id="turn_1",
                turn_kind=argus_app.TURN_KIND_CRON,
                source_channel=None,
                source_chat_key=None,
            )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "info")
        self.assertEqual(events[0][2]["reason"], "non_user_turn")

        events.clear()

        with (
            mock.patch.object(argus_app, "_event_log", side_effect=fake_event_log),
            mock.patch.object(manager, "_resolve_telegram_target_for_source", return_value=None),
            mock.patch.object(manager, "_resolve_telegram_target_for_turn", return_value=None),
        ):
            await manager._deliver_turn_text(
                "sess_1",
                "thread_1",
                "user result",
                turn_id="turn_2",
                turn_kind=argus_app.TURN_KIND_USER,
                source_channel=None,
                source_chat_key=None,
            )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "info")
        self.assertEqual(events[0][2]["reason"], "missing_target")


if __name__ == "__main__":
    unittest.main()
