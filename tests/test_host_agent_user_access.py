import importlib.util
import json
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


class EmptyRegistry:
    async def list_connected(self, *args, **kwargs):
        return []


def make_request(path: str, *, token: str, method: str = "GET", body=None):
    raw_body = json.dumps(body if body is not None else {}).encode("utf-8")
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": raw_body, "more_body": False}

    return argus_app.Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
                (b"authorization", f"Bearer {token}".encode("utf-8")),
            ],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "root_path": "",
        },
        receive,
    )


class HostAgentUserAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.manager = argus_app.AutomationManager(
            state_store=InMemoryStateStore(),
            home_host_path=self.tmpdir.name,
            workspace_host_path=self.tmpdir.name,
        )
        admin = await self.manager.register_console_user(email="admin@example.com", password="password123")
        user = await self.manager.register_console_user(email="user@example.com", password="password123")
        _admin_session, self.admin_token = await self.manager.issue_console_session(user_id=admin.user_id)
        _user_session, self.user_token = await self.manager.issue_console_session(user_id=user.user_id)
        self.admin_user_id = admin.user_id
        self.user_id = user.user_id
        self.patchers = [
            mock.patch.object(argus_app.app.state, "automation", self.manager, create=True),
            mock.patch.object(argus_app.app.state, "runtime_host_registry", EmptyRegistry(), create=True),
            mock.patch.object(argus_app.app.state, "node_registry", EmptyRegistry(), create=True),
        ]
        for patcher in self.patchers:
            patcher.start()

    async def asyncTearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        await self.manager.stop()
        self.tmpdir.cleanup()

    async def test_regular_user_enroll_token_is_scoped_to_self(self) -> None:
        response = await argus_app.host_agent_enroll_token(
            make_request("/host-agent/enroll-token", token=self.user_token, method="POST", body={"ttlSec": 300})
        )

        self.assertTrue(response["ok"])
        self.assertTrue(response["token"].startswith(argus_app.HOST_AGENT_ENROLL_TOKEN_PREFIX))
        self.assertEqual(response["scopeType"], argus_app.HOST_BINDING_SCOPE_USER)
        self.assertEqual(response["scopeId"], str(self.user_id))
        self.assertIn('--scope-type "user"', response["command"])
        self.assertIn(f'--scope-id "{self.user_id}"', response["command"])

    async def test_regular_user_cannot_request_global_or_other_user_scope(self) -> None:
        with self.assertRaises(argus_app.HTTPException) as global_ctx:
            await argus_app.host_agent_enroll_token(
                make_request(
                    "/host-agent/enroll-token",
                    token=self.user_token,
                    method="POST",
                    body={"scopeType": "global"},
                )
            )
        self.assertEqual(global_ctx.exception.status_code, 403)

        with self.assertRaises(argus_app.HTTPException) as other_user_ctx:
            await argus_app.host_agent_enroll_token(
                make_request(
                    "/host-agent/enroll-token",
                    token=self.user_token,
                    method="POST",
                    body={"scopeType": "user", "scopeId": self.admin_user_id},
                )
            )
        self.assertEqual(other_user_ctx.exception.status_code, 403)

    async def test_regular_user_lists_only_their_bound_hosts(self) -> None:
        user_response = await argus_app.host_agent_enroll_token(
            make_request("/host-agent/enroll-token", token=self.user_token, method="POST", body={"ttlSec": 300})
        )
        await self.manager.claim_host_agent(
            enroll_token=user_response["token"],
            host_id="user-host",
            display_name="User Host",
            platform="darwin",
            version="test",
            set_default=False,
            scope_type=None,
            scope_id=None,
            workspace_base_path="/Users/example/Desktop",
        )
        _admin_pending, admin_enroll_token = await self.manager.issue_host_enroll_token(
            host_id_hint=None,
            scope_type=argus_app.HOST_BINDING_SCOPE_GLOBAL,
            scope_id="default",
            workspace_base_path="/srv/argus",
            ttl_s=300,
        )
        await self.manager.claim_host_agent(
            enroll_token=admin_enroll_token,
            host_id="admin-host",
            display_name="Admin Host",
            platform="linux",
            version="test",
            set_default=True,
            scope_type=None,
            scope_id=None,
            workspace_base_path="/srv/argus",
        )

        user_list = await argus_app.list_host_agents(
            make_request("/host-agents", token=self.user_token, method="GET")
        )
        admin_list = await argus_app.list_host_agents(
            make_request("/host-agents", token=self.admin_token, method="GET")
        )

        self.assertEqual([host["hostId"] for host in user_list["hosts"]], ["user-host"])
        self.assertEqual({host["hostId"] for host in admin_list["hosts"]}, {"admin-host", "user-host"})


if __name__ == "__main__":
    unittest.main()
