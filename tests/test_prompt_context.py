import asyncio
import importlib.util
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

SPEC = importlib.util.spec_from_file_location('argus_prompt_app', pathlib.Path(__file__).resolve().parents[1] / 'apps/api/app.py')
gateway = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gateway
SPEC.loader.exec_module(gateway)

class PromptContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = gateway.PersistedGatewayAutomationState()
        async def update(fn):
            fn(self.state)
        self.store = SimpleNamespace(state=self.state, update=mock.AsyncMock(side_effect=update))
        self.manager = gateway.AutomationManager(state_store=self.store, home_host_path=None, workspace_host_path=None)

    async def test_fugue_uses_runtime_mount_and_matches_local_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.manager._workspace_root_for_session = mock.Mock(return_value=root)
            for name, content in [('SOUL.md', 'purpose'), ('USER.md', 'preferences'), ('HEARTBEAT.md', 'heartbeat only')]:
                (root / name).write_text(content)
            with mock.patch.object(gateway, '_fugue_workspace_enabled', return_value=False), mock.patch.object(gateway, '_session_uses_remote_workspace', return_value=False):
                baseline = await self.manager._read_project_context_block(session_id='session', include_heartbeat=False)
            files = {str(pathlib.Path('/custom-runtime') / p.name): p.read_text() for p in root.iterdir() if p.is_file()}
            async def read(live, path, **kwargs):
                return files.get(path)
            async def write(live, path, text):
                files[path] = text
            with (
                mock.patch.object(gateway, '_fugue_workspace_enabled', return_value=True),
                mock.patch.object(gateway, '_session_uses_remote_workspace', return_value=False),
                mock.patch.object(gateway, '_fugue_cfg', return_value=SimpleNamespace(workspace_mount_path='/custom-runtime')),
                mock.patch.object(gateway, '_ensure_live_session', new=mock.AsyncMock(return_value=(object(), False))),
                mock.patch.object(gateway, '_live_fs_read_text', new=mock.AsyncMock(side_effect=read)) as reader,
                mock.patch.object(gateway, '_live_fs_write_text', new=mock.AsyncMock(side_effect=write)) as writer,
                mock.patch.object(gateway, '_sync_session_prompt_context_to_local', new=mock.AsyncMock(side_effect=AssertionError('control plane read'))),
                mock.patch.object(gateway, '_sync_session_workspace_file', new=mock.AsyncMock(side_effect=AssertionError('control plane write'))),
            ):
                result = await self.manager._read_project_context_block(session_id='session', include_heartbeat=False)
                self.assertEqual(result, baseline)
                self.assertNotIn('heartbeat only', result)
                self.assertEqual(writer.call_args.args[1], '/custom-runtime/AGENTS.md')
                files['/custom-runtime/USER.md'] = 'updated preferences'
                result = await self.manager._read_project_context_block(session_id='session', include_heartbeat=True)
                self.assertIn('updated preferences', result)
                self.assertIn('heartbeat only', result)
                self.assertTrue(all(c.args[1].startswith('/custom-runtime/') for c in reader.call_args_list))

    async def test_skills_use_runtime_mount_and_observe_deletions(self):
        self.manager._workspace_root_for_session = mock.Mock(return_value=pathlib.Path('/gateway-mirror'))
        with (
            mock.patch.object(gateway, '_fugue_workspace_enabled', return_value=True),
            mock.patch.object(gateway, '_session_uses_remote_workspace', return_value=False),
            mock.patch.object(gateway, '_fugue_cfg', return_value=SimpleNamespace(workspace_mount_path='/custom-runtime')),
            mock.patch.object(gateway, '_ensure_live_session', new=mock.AsyncMock(return_value=(object(), False))),
            mock.patch.object(gateway, '_live_fs_read_directory', new=mock.AsyncMock(return_value=[{'fileName':'example','isDirectory':True}])) as listing,
            mock.patch.object(gateway, '_live_fs_read_text', new=mock.AsyncMock(return_value='---\nname: example\ndescription: test description\n---\nbody')) as reader,
        ):
            result = await self.manager._read_skills_prompt_block(session_id='session')
            self.assertIn('test description', result)
            self.assertIn('/custom-runtime/skills/example/SKILL.md', result)
            self.assertEqual(reader.call_args.args[1], '/custom-runtime/skills/example/SKILL.md')
            listing.return_value = []
            self.assertEqual(await self.manager._read_skills_prompt_block(session_id='session'), '')

    async def test_empty_events_do_not_write_and_later_events_remain_fifo(self):
        self.state.sessions['session'] = gateway.PersistedSessionAutomation()
        for _ in range(2):
            self.assertEqual(await self.manager._drain_system_events('session', 'thread', max_events=20), [])
        self.store.update.assert_not_awaited()
        queue = self.state.sessions['session'].system_event_queues.setdefault('thread', [])
        queue.extend([gateway.PersistedSystemEvent(event_id=str(i), kind='test', text=str(i), created_at_ms=1) for i in range(3)])
        result = await self.manager._drain_system_events('session', 'thread', max_events=2)
        self.assertEqual(len(result), 2)
        self.assertTrue(result[0].endswith(' 0'))
        self.assertTrue(result[1].endswith(' 1'))
        self.assertEqual([e.text for e in queue], ['2'])
        self.store.update.assert_awaited_once()

if __name__ == '__main__':
    unittest.main()
