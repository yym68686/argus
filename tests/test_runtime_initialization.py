import asyncio
import importlib.util
import json
import pathlib
import sys
import unittest
from collections import deque
from unittest import mock
from types import SimpleNamespace


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("argus_initialization_app", ROOT / "apps/api/app.py")
gateway = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gateway
SPEC.loader.exec_module(gateway)


class Writer:
    def __init__(self):
        self.messages = []
        self.closed = False

    def write(self, raw):
        self.messages.append(json.loads(raw))

    async def drain(self):
        await asyncio.sleep(0)

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True


class Socket:
    def __init__(self):
        self.client_state = gateway.WebSocketState.CONNECTED
        self.query_params = {"session": "012345abcdef"}
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()

    async def accept(self):
        pass

    async def receive_text(self):
        message = await self.incoming.get()
        if message is None:
            self.client_state = gateway.WebSocketState.DISCONNECTED
            raise gateway.WebSocketDisconnect()
        return json.dumps(message)

    async def send_text(self, raw):
        self.outgoing.put_nowait(json.loads(raw))

    async def response(self, rid):
        async with asyncio.timeout(1):
            while True:
                response = await self.outgoing.get()
                if response.get("id") == rid:
                    return response

    async def close(self, **kwargs):
        self.incoming.put_nowait(None)


class RuntimeInitializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_thread_resume_failure_does_not_replace_history(self):
        persisted = SimpleNamespace(main_thread_id="thread-main")
        self.manager._store.state = SimpleNamespace(sessions={self.live.session_id: persisted})
        self.manager._ensure_initialized = mock.AsyncMock()
        self.manager._ensure_thread_loaded_or_resumed = mock.AsyncMock(side_effect=RuntimeError("thread-main already has an active writer"))
        self.manager._rpc = mock.AsyncMock(return_value={"thread": {"id": "replacement"}})
        with self.assertRaisesRegex(RuntimeError, "active writer"):
            await self.manager.ensure_main_thread(self.live.session_id)
        self.assertEqual(persisted.main_thread_id, "thread-main")
        self.manager._rpc.assert_not_awaited()

    async def test_resume_does_not_request_unused_conversation_history(self):
        self.manager._rpc = mock.AsyncMock(side_effect=[{"data": []}, {"thread": {"id": "thread-main"}}])
        await self.manager._ensure_thread_loaded_or_resumed(self.live, "thread-main")
        self.manager._rpc.assert_has_awaits([
            mock.call(self.live, "thread/loaded/list", {"limit": 200}),
            mock.call(self.live, "thread/resume", {"threadId": "thread-main", "excludeTurns": True}),
        ])

    async def test_loaded_thread_skips_resume(self):
        self.manager._rpc = mock.AsyncMock(return_value={"data": ["thread-main"]})
        await self.manager._ensure_thread_loaded_or_resumed(self.live, "thread-main")
        self.manager._rpc.assert_awaited_once_with(self.live, "thread/loaded/list", {"limit": 200})

    async def test_rpc_timing_does_not_log_prompts_or_responses(self):
        with self.assertLogs(gateway.latency_log, level="INFO") as logs:
            task = asyncio.create_task(self.manager._rpc(self.live, "turn/start", {"threadId": "thread-main", "input": [{"text": "private input"}]}))
            while not self.writer.messages:
                await asyncio.sleep(0)
            rid = self.writer.messages[-1]["id"]
            self.reader.feed_data((json.dumps({"id": rid, "result": {"privateResponse": "private output"}}) + "\n").encode())
            await task
        event = json.loads(logs.records[-1].getMessage())
        self.assertEqual(event["method"], "turn/start")
        self.assertEqual(event["outcome"], "ok")
        self.assertGreaterEqual(event["duration_ms"], 0)
        self.assertNotIn("private input", logs.output[0])
        self.assertNotIn("private output", logs.output[0])

    async def asyncSetUp(self):
        self.writer = Writer()
        self.reader = asyncio.StreamReader()
        self.live = gateway.LiveRuntimeSession(
            session_id="012345abcdef", provider="static", runtime_id="runtime-test",
            runtime_name="test", workspace_runtime_path="/workspace", jsonl_line_limit_bytes=1048576,
            upstream_host="127.0.0.1", upstream_port=7777, reader=self.reader, writer=self.writer,
            pump_task=None, attach_lock=asyncio.Lock(), attached_wss=set(), request_handler_ws=None,
            initialized_result=None, handshake_done=False, pending_initialize_ids=set(),
            initialize_waiters=[], pending_client_requests={}, pending_internal_requests={},
            next_upstream_id=1000000000, pending_server_requests={}, outbox=deque(),
            pending_client_turn_starts={}, turn_owners_by_thread={},
        )
        self.manager = gateway.AutomationManager(state_store=mock.Mock(), home_host_path=None, workspace_host_path=None)
        self.patchers = [
            mock.patch.object(gateway.app.state, "sessions_lock", asyncio.Lock(), create=True),
            mock.patch.object(gateway.app.state, "sessions", {}, create=True),
            mock.patch.object(gateway.app.state, "automation", None, create=True),
            mock.patch.object(gateway, "_extract_token", return_value="test"),
            mock.patch.object(gateway, "_requested_session_placement_from_query", return_value=None),
            mock.patch.object(gateway, "_ws_request_uses_managed_session", return_value=True),
            mock.patch.object(gateway, "_resolve_auth_principal_from_token", return_value=SimpleNamespace(kind="admin_token")),
            mock.patch.object(gateway, "_principal_can_access_session", return_value=True),
            mock.patch.object(gateway, "_ensure_live_session", new=mock.AsyncMock(return_value=(self.live, False))),
        ]
        self.sockets = []
        for p in self.patchers:
            p.start()
        await gateway._activate_live_session(self.live)

    async def asyncTearDown(self):
        for ws, task in self.sockets:
            await ws.close()
            await task
        self.reader.feed_eof()
        await self.live.pump_task
        for p in reversed(self.patchers):
            p.stop()

    async def wait_for_requests(self, count):
        for _ in range(100):
            if len(self.writer.messages) >= count:
                return
            await asyncio.sleep(0.001)
        self.fail("request was not sent")

    def respond(self, request, **payload):
        self.reader.feed_data((json.dumps({"id": request["id"], **payload}) + "\n").encode())

    def connect(self, rid=1, params=None):
        ws = Socket()
        if params is None:
            params = {"clientInfo": {"name": "test-client", "version": "1"}}
        ws.incoming.put_nowait({"id": rid, "method": "initialize", "params": params})
        task = asyncio.create_task(gateway.ws_proxy(ws))
        self.sockets.append((ws, task))
        return ws

    async def check_experimental_resume(self, initialize_request):
        # Model the upstream protocol gate so a successful initialize alone
        # cannot make this regression pass.
        opted_in = initialize_request["params"].get("capabilities", {}).get("experimentalApi") is True
        self.respond(initialize_request, result={"userAgent": "test/1"})
        resume = asyncio.create_task(self.manager._ensure_thread_loaded_or_resumed(self.live, "thread-main"))
        await self.wait_for_requests(3)  # initialize, initialized, loaded/list
        self.respond(self.writer.messages[-1], result={"data": []})
        await self.wait_for_requests(4)
        request = self.writer.messages[-1]
        self.assertEqual(request["method"], "thread/resume")
        self.assertTrue(request["params"]["excludeTurns"])
        if opted_in:
            self.respond(request, result={"thread": {"id": "thread-main", "turns": []}})
        else:
            self.respond(request, error={"code": -32600, "message": "thread/resume.excludeTurns requires experimentalApi capability"})
        await resume

    async def test_automation_first_negotiates_experimental_resume(self):
        initialize = asyncio.create_task(self.manager._ensure_initialized(self.live))
        await self.wait_for_requests(1)
        try:
            await self.check_experimental_resume(self.writer.messages[0])
        finally:
            await initialize
        ws = self.connect(61)
        self.assertIn("result", await ws.response(61))
        self.assertEqual(sum(m["method"] == "initialize" for m in self.writer.messages), 1)

    async def test_websocket_first_negotiates_gateway_capabilities(self):
        ws = self.connect(62)
        await self.wait_for_requests(1)
        await self.check_experimental_resume(self.writer.messages[0])
        self.assertIn("result", await ws.response(62))
        await self.manager._ensure_initialized(self.live)
        self.assertEqual(sum(m["method"] == "initialize" for m in self.writer.messages), 1)

    async def test_gateway_opt_in_preserves_other_client_parameters(self):
        params = {"clientInfo": {"name": "stable-client", "version": "1"}, "capabilities": {"experimentalApi": False, "optOutNotificationMethods": ["item/agentMessage/delta"]}}
        original = json.loads(json.dumps(params))
        ws = self.connect(63, params=params)
        await self.wait_for_requests(1)
        request = self.writer.messages[0]
        await self.check_experimental_resume(request)
        self.assertIn("result", await ws.response(63))
        self.assertEqual(request["params"]["clientInfo"], original["clientInfo"])
        self.assertEqual(request["params"]["capabilities"]["optOutNotificationMethods"], original["capabilities"]["optOutNotificationMethods"])
        self.assertEqual(params, original)

    async def test_late_internal_initialize_success_survives_caller_timeout(self):
        wait_for = asyncio.wait_for

        async def short_wait(awaitable, timeout):
            return await wait_for(awaitable, timeout=0.01)

        with mock.patch.object(gateway.asyncio, "wait_for", side_effect=short_wait):
            with self.assertRaises(TimeoutError):
                await self.manager._ensure_initialized(self.live)
        self.respond(self.writer.messages[0], result={"userAgent": "test/1"})
        await asyncio.sleep(0.02)
        self.assertEqual(self.live.initialized_result, {"userAgent": "test/1"})
        await self.manager._ensure_initialized(self.live)
        self.assertEqual([m["method"] for m in self.writer.messages], ["initialize", "initialized"])

    async def test_concurrent_internal_callers_initialize_only_once(self):
        tasks = [asyncio.create_task(self.manager._ensure_initialized(self.live)) for _ in range(3)]
        await self.wait_for_requests(1)
        await asyncio.sleep(0.01)
        requests = list(self.writer.messages)
        for i, request in enumerate(requests):
            if i == 0:
                self.respond(request, result={"userAgent": "test/1"})
            else:
                self.respond(request, error={"code": -32600, "message": "Already initialized"})
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual([m["method"] for m in self.writer.messages], ["initialize", "initialized"])

    async def test_initialize_error_does_not_complete_handshake(self):
        task = asyncio.create_task(self.manager._ensure_initialized(self.live))
        await self.wait_for_requests(1)
        self.respond(self.writer.messages[0], error={"code": -32602, "message": "Invalid clientInfo"})
        with self.assertRaisesRegex(RuntimeError, "Invalid clientInfo"):
            await task
        self.assertFalse(self.live.handshake_done)
        self.assertIsNone(self.live.initialized_result)
        self.assertEqual(len(self.writer.messages), 1)

    async def test_websocket_first_shares_handshake_with_automation(self):
        ws = self.connect(7)
        await self.wait_for_requests(1)
        internal = asyncio.create_task(self.manager._ensure_initialized(self.live))
        second_ws = self.connect(9)
        await asyncio.sleep(0.01)
        self.assertEqual(len(self.writer.messages), 1)
        self.respond(self.writer.messages[0], result={"userAgent": "test/1"})
        self.assertEqual((await ws.response(7))["result"], {"userAgent": "test/1"})
        self.assertEqual((await second_ws.response(9))["result"], {"userAgent": "test/1"})
        await internal
        ws.incoming.put_nowait({"method": "initialized"})
        second_ws.incoming.put_nowait({"method": "initialized"})
        await asyncio.sleep(0.01)
        self.assertEqual([m["method"] for m in self.writer.messages], ["initialize", "initialized"])

    async def test_websocket_reconnect_uses_late_internal_result(self):
        internal = asyncio.create_task(self.manager._ensure_initialized(self.live))
        await self.wait_for_requests(1)
        internal.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await internal
        first_ws = self.connect(11)
        await asyncio.sleep(0.01)
        await first_ws.close()
        await self.sockets[-1][1]
        self.respond(self.writer.messages[0], result={"userAgent": "test/1"})
        await asyncio.sleep(0.01)
        next_ws = self.connect(12)
        self.assertEqual((await next_ws.response(12))["result"], {"userAgent": "test/1"})
        self.assertEqual([m["method"] for m in self.writer.messages], ["initialize", "initialized"])

    async def test_error_reaches_all_clients_and_next_attempt_can_retry(self):
        ws = self.connect(21)
        await self.wait_for_requests(1)
        second = self.connect(22)
        internal = asyncio.create_task(self.manager._ensure_initialized(self.live))
        await asyncio.sleep(0.01)
        self.respond(self.writer.messages[0], error={"code": -32602, "message": "Invalid clientInfo"})
        for sock, rid in [(ws, 21), (second, 22)]:
            self.assertEqual((await sock.response(rid))["error"]["code"], -32602)
        with self.assertRaisesRegex(RuntimeError, "Invalid clientInfo"):
            await internal
        self.assertFalse(self.live.handshake_done)
        retry = self.connect(23)
        await self.wait_for_requests(2)
        self.respond(self.writer.messages[1], result={"userAgent": "test/1"})
        self.assertIn("result", await retry.response(23))

    async def test_runtime_close_releases_waiting_callers(self):
        internal = asyncio.create_task(self.manager._ensure_initialized(self.live))
        await self.wait_for_requests(1)
        self.reader.feed_eof()
        with self.assertRaisesRegex(RuntimeError, "Session closed"):
            await internal
        self.assertTrue(self.live.closed)

    async def test_cancelling_sender_during_write_keeps_shared_handshake(self):
        gate = asyncio.Event()
        with mock.patch.object(self.writer, "drain", side_effect=gate.wait):
            first = asyncio.create_task(self.manager._ensure_initialized(self.live))
            await self.wait_for_requests(1)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(self.manager._ensure_initialized(self.live))
            gate.set()
            self.respond(self.writer.messages[0], result={"userAgent": "test/1"})
            await second
        self.assertEqual([m["method"] for m in self.writer.messages], ["initialize", "initialized"])


if __name__ == "__main__":
    unittest.main()

class ShutdownRuntimeSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_closes_all_upstream_sessions(self):
        live = SimpleNamespace()
        with mock.patch.object(gateway.app.state, "sessions", {"sess-a": live}, create=True), mock.patch.object(gateway.app.state, "automation", None, create=True), mock.patch.object(gateway, "_close_live_session", new=mock.AsyncMock()) as close:
            await gateway._shutdown()
        close.assert_awaited_once_with("sess-a")

class ActiveWriterRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_retries_transient_active_writer(self):
        store = SimpleNamespace(state=SimpleNamespace(sessions={}), update=mock.AsyncMock())
        manager = gateway.AutomationManager(state_store=store, home_host_path=None, workspace_host_path=None)
        live = SimpleNamespace()
        manager._is_thread_loaded = mock.AsyncMock(return_value=False)
        manager._rpc = mock.AsyncMock(side_effect=[RuntimeError("thread abc already has an active writer"), {"thread": {"id": "abc"}}])
        with mock.patch.object(gateway.asyncio, "sleep", new=mock.AsyncMock()) as sleep:
            await manager._ensure_thread_loaded_or_resumed(live, "abc")
        self.assertEqual(manager._rpc.await_count, 2)
        sleep.assert_awaited_once()
