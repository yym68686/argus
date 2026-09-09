import asyncio
import importlib.util
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

SPEC = importlib.util.spec_from_file_location('argus_drain_app', pathlib.Path(__file__).resolve().parents[1] / 'apps/api/app.py')
gateway = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gateway
SPEC.loader.exec_module(gateway)

class GatewayDrainTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = gateway.AutomationManager(state_store=SimpleNamespace(state=gateway.PersistedGatewayAutomationState()), home_host_path=None, workspace_host_path=None)
        self.live = SimpleNamespace(pending_internal_requests={}, pending_client_requests={}, pending_server_requests={}, turn_owners_by_thread={}, close_attached_websockets=mock.AsyncMock())

    async def test_drain_stops_scheduling_and_waits_for_turns_and_delivery(self):
        lane = self.manager.lane('session', 'thread')
        lane.busy = True
        task = None
        with (
            mock.patch.object(gateway.app.state, 'sessions', {'session': self.live}, create=True),
            mock.patch.object(gateway, '_fugue_drain_started_sync', return_value=True),
            mock.patch.object(gateway, '_close_live_session', new=mock.AsyncMock()) as close,
        ):
            try:
                task = asyncio.create_task(self.manager._fugue_drain_loop())
                await asyncio.sleep(.65)
                self.assertTrue(self.manager._draining)
                self.assertFalse(self.manager._drain_finishing)
                close.assert_not_awaited()
                self.assertEqual(await self.manager._cron_tick(), 1.0)
                with mock.patch.object(gateway, '_provisioner_supports_runtime_automation', side_effect=AssertionError('new heartbeat must not start')):
                    await self.manager._heartbeat_tick(forced=True)
                lane.busy = False
                self.manager._active_work = 1  # final delivery is still running
                await asyncio.sleep(.65)
                close.assert_not_awaited()
                self.manager._active_work = 0
                lane.followups.append({'text': 'already accepted'})
                self.assertTrue(self.manager._has_drain_work())
                lane.followups.clear()
                await asyncio.wait_for(task, 4)
                self.assertTrue(self.manager._drain_finishing)
                self.live.close_attached_websockets.assert_awaited_once_with(code=1012, reason='Gateway rolling deployment')
                close.assert_awaited_once_with('session')
            finally:
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_pending_rpc_and_turn_owner_keep_connection_alive(self):
        with mock.patch.object(gateway.app.state, 'sessions', {'session': self.live}, create=True):
            self.assertFalse(self.manager._has_drain_work())
            for name in ('pending_internal_requests','pending_client_requests','pending_server_requests','turn_owners_by_thread'):
                getattr(self.live, name)['id'] = object()
                self.assertTrue(self.manager._has_drain_work(), name)
                getattr(self.live, name).clear()
            lane = self.manager.lane('session','thread')
            async with lane.lock:
                self.assertTrue(self.manager._has_drain_work())

    async def test_tracked_work_is_cleared_on_error_and_cancellation(self):
        @gateway._tracked_automation_work
        async def failing(manager):
            self.assertEqual(manager._active_work, 1)
            raise RuntimeError('failed')
        with self.assertRaises(RuntimeError):
            await failing(self.manager)
        self.assertEqual(self.manager._active_work, 0)
        started = asyncio.Event()
        @gateway._tracked_automation_work
        async def waiting(manager):
            started.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(waiting(self.manager))
        await started.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.manager._active_work, 0)

    def test_metrics_probe_requires_explicit_positive_counter(self):
        for body, expected in [(b'fugue_app_drain_prestop_requests_total 1\n',True),(b'fugue_app_drain_prestop_requests_total 0\n',False),(b'# unavailable',False)]:
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = body
            opener = mock.Mock()
            opener.open.return_value = response
            with mock.patch.object(gateway.urllib.request,'build_opener',return_value=opener):
                self.assertEqual(gateway._fugue_drain_started_sync(),expected)
        with mock.patch.object(gateway.urllib.request,'build_opener') as build:
            build.return_value.open.side_effect=OSError('no sidecar')
            self.assertFalse(gateway._fugue_drain_started_sync())

if __name__ == '__main__':unittest.main()
