import asyncio
import errno
import importlib.util
import pathlib
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("argus_attachment_app", ROOT / "apps/api/app.py")
gateway = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gateway
SPEC.loader.exec_module(gateway)


class AttachmentRetryTests(unittest.TestCase):
    def test_wrapped_get_file_reset_retries_and_succeeds(self):
        reset = urllib.error.URLError(ConnectionResetError(errno.ECONNRESET, "reset"))
        wrapped = RuntimeError("getFile failed")
        wrapped.__cause__ = reset
        fn = mock.Mock(side_effect=[wrapped, wrapped, {"file_path": "photos/test.jpg"}])
        with mock.patch.object(gateway.time, "sleep") as sleep:
            result = gateway._telegram_attachment_read_with_retry_sync(fn, "secret", "file")
        self.assertEqual(result, {"file_path": "photos/test.jpg"})
        self.assertEqual(fn.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(0.25), mock.call(0.5)])

    def test_retry_limit_is_three_and_logs_no_exception_secrets(self):
        fn = mock.Mock(side_effect=ConnectionResetError("https://telegram.invalid/botsecret/file-private"))
        with mock.patch.object(gateway.time, "sleep"), self.assertLogs(gateway.log, level="INFO") as logs:
            with self.assertRaises(ConnectionResetError):
                gateway._telegram_attachment_read_with_retry_sync(fn)
        self.assertEqual(fn.call_count, 3)
        self.assertNotIn("botsecret", str(logs.output))
        self.assertNotIn("file-private", str(logs.output))

    def test_http_transient_vs_permanent_errors(self):
        for code, attempts in [(429, 3), (500, 3), (503, 3), (400, 1), (401, 1), (404, 1)]:
            with self.subTest(code=code):
                fn = mock.Mock(side_effect=urllib.error.HTTPError("private-url", code, "error", {}, None))
                with mock.patch.object(gateway.time, "sleep"), self.assertRaises(urllib.error.HTTPError):
                    gateway._telegram_attachment_read_with_retry_sync(fn)
                self.assertEqual(fn.call_count, attempts)

    def test_local_disk_and_permission_errors_are_not_retried(self):
        for number, reason in [(errno.ENOSPC, "storage_full"), (errno.EDQUOT, "storage_full"), (errno.EACCES, "permission")]:
            with self.subTest(number=number):
                error = OSError(number, "local error")
                fn = mock.Mock(side_effect=error)
                with mock.patch.object(gateway.time, "sleep") as sleep, self.assertRaises(OSError):
                    gateway._telegram_attachment_read_with_retry_sync(fn)
                self.assertEqual(fn.call_count, 1)
                sleep.assert_not_called()
                self.assertEqual(gateway._telegram_attachment_failure_reason(error), reason)

    def test_failed_download_cleans_partial_and_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = pathlib.Path(tmp) / "image.jpg"
            dest.write_bytes(b"existing")
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.read.side_effect = [b"partial", ConnectionResetError("reset")]
            with mock.patch.object(gateway.urllib.request, "urlopen", return_value=response):
                with self.assertRaises(RuntimeError):
                    gateway._telegram_download_file_to_path_sync("secret", "photos/image.jpg", dest, max_bytes=1024)
            self.assertEqual(dest.read_bytes(), b"existing")
            self.assertFalse(dest.with_suffix(".jpg.tmp").exists())

    def test_oversize_download_cleans_partial_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = pathlib.Path(tmp) / "image.jpg"
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = b"oversized"
            with mock.patch.object(gateway.urllib.request, "urlopen", return_value=response) as call:
                with self.assertRaises(RuntimeError) as caught:
                    gateway._telegram_attachment_read_with_retry_sync(
                        gateway._telegram_download_file_to_path_sync, "secret", "image.jpg", dest, max_bytes=2,
                    )
            self.assertEqual(gateway._telegram_attachment_failure_reason(caught.exception), "too_large")
            self.assertEqual(call.call_count, 1)
            self.assertFalse(dest.exists())
            self.assertFalse(dest.with_suffix(".jpg.tmp").exists())


class AttachmentStagingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = gateway.AutomationManager(state_store=mock.Mock(), home_host_path=None, workspace_host_path=None)
        self.manager.get_effective_telegram_bot_token = mock.Mock(return_value="secret-token")
        self.manager._workspace_root_for_session = mock.Mock(return_value=pathlib.Path(self.tmp.name))
        self.refs = [{"fileId": "private-file", "kind": "photo"}]

    async def stage(self):
        return await self.manager._stage_telegram_attachments(
            session_id="012345abcdef", source_chat_key="fixture-chat", telegram_attachments=self.refs,
        )

    async def test_network_failure_reports_get_file_not_save(self):
        with mock.patch.object(gateway, "_session_uses_remote_workspace", return_value=False), \
                mock.patch.object(gateway, "_telegram_get_file_sync", side_effect=ConnectionResetError("secret-token private-file")), \
                mock.patch.object(gateway.time, "sleep"), self.assertLogs(gateway.log) as logs:
            with self.assertRaises(gateway.TelegramAttachmentError) as caught:
                await self.stage()
        error = caught.exception
        self.assertEqual((error.stage, error.reason), ("get_file", "network"))
        self.assertIn("获取 Telegram 附件信息", str(error))
        self.assertIn("含文字", str(error))
        self.assertNotIn("secret-token", str(error) + str(logs.output))
        self.assertNotIn("private-file", str(error) + str(logs.output))

    async def test_local_save_disk_full_is_distinct_and_not_retried(self):
        with mock.patch.object(gateway, "_session_uses_remote_workspace", return_value=False), \
                mock.patch.object(gateway, "_telegram_get_file_sync", return_value={"file_path": "image.jpg"}), \
                mock.patch.object(gateway, "_telegram_download_file_to_path_sync", side_effect=OSError(errno.ENOSPC, "full")) as download:
            with self.assertRaises(gateway.TelegramAttachmentError) as caught:
                await self.stage()
        self.assertEqual((caught.exception.stage, caught.exception.reason), ("save", "storage_full"))
        self.assertEqual(download.call_count, 1)

    async def test_remote_download_retries_but_remote_write_does_not(self):
        with mock.patch.object(gateway, "_session_uses_remote_workspace", return_value=True), \
                mock.patch.object(gateway, "_ensure_live_session", new=mock.AsyncMock(return_value=(mock.Mock(), False))), \
                mock.patch.object(gateway, "_telegram_get_file_sync", return_value={"file_path": "image.jpg"}), \
                mock.patch.object(gateway, "_telegram_download_file_bytes_sync", side_effect=[ConnectionResetError(), b"complete"]) as download, \
                mock.patch.object(gateway.time, "sleep"), \
                mock.patch.object(gateway, "_live_fs_write_bytes", new=mock.AsyncMock(side_effect=RuntimeError("No space left on device"))) as write:
            with self.assertRaises(gateway.TelegramAttachmentError) as caught:
                await self.stage()
        self.assertEqual(download.call_count, 2)
        write.assert_awaited_once()
        self.assertEqual((caught.exception.stage, caught.exception.reason), ("save", "storage_full"))

    async def test_success_stages_attachment_once(self):
        with mock.patch.object(gateway, "_session_uses_remote_workspace", return_value=False), \
                mock.patch.object(gateway, "_telegram_get_file_sync", return_value={"file_path": "image.jpg"}), \
                mock.patch.object(gateway, "_telegram_download_file_to_path_sync", return_value=42), \
                mock.patch.object(gateway, "_sync_session_workspace_file", new=mock.AsyncMock()):
            result = await self.stage()
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].is_image)
        self.assertEqual(result[0].file_size, 42)

    async def test_partial_batch_failure_is_not_silently_accepted(self):
        self.refs.append({"fileId": "unavailable-file"})
        with mock.patch.object(gateway, "_session_uses_remote_workspace", return_value=False), \
                mock.patch.object(gateway, "_telegram_get_file_sync", side_effect=[{"file_path": "image.jpg"}, RuntimeError("invalid file")]), \
                mock.patch.object(gateway, "_telegram_download_file_to_path_sync", return_value=42), \
                mock.patch.object(gateway, "_sync_session_workspace_file", new=mock.AsyncMock()):
            with self.assertRaises(gateway.TelegramAttachmentError):
                await self.stage()

    async def test_staging_failure_does_not_submit_text_or_consume_pending_attachments(self):
        self.manager._stage_telegram_attachments = mock.AsyncMock(side_effect=gateway.TelegramAttachmentError("get_file", "network"))
        self.manager._consume_pending_telegram_attachments = mock.AsyncMock()
        self.manager._rpc = mock.AsyncMock()
        with self.assertRaises(gateway.TelegramAttachmentError):
            await self.manager.enqueue_user_input(
                session_id="012345abcdef", thread_id=None, target="main", text="caption",
                source_channel="telegram", source_chat_key="fixture-chat", telegram_attachments=self.refs,
            )
        self.manager._consume_pending_telegram_attachments.assert_not_awaited()
        self.manager._rpc.assert_not_awaited()


class PendingAttachmentLatencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / "state.sqlite3"
        self.store = gateway.AutomationStateStore(self.db)
        self.manager = gateway.AutomationManager(state_store=self.store, home_host_path=None, workspace_host_path=None)

    async def consume(self):
        return await self.manager._consume_pending_telegram_attachments(session_id="session", chat_key="chat")

    async def test_absent_attachments_neither_wait_for_lock_nor_write(self):
        self.store.state.sessions["session"] = gateway.PersistedSessionAutomation()
        backend = self.store._backend
        with mock.patch.object(backend, "_write_state_sync") as write:
            async with backend._lock:
                self.assertEqual(await asyncio.wait_for(self.consume(), timeout=0.5), [])
            write.assert_not_called()

    async def test_concurrent_consumers_write_once_and_leave_other_chat_untouched(self):
        attachment = gateway.PersistedStagedAttachment(path="inbox/fixture.txt")
        await self.manager._append_pending_telegram_attachments(session_id="session", chat_key="chat", attachments=[attachment])
        await self.manager._append_pending_telegram_attachments(session_id="session", chat_key="other", attachments=[attachment])
        backend = self.store._backend
        with mock.patch.object(backend, "_write_state_sync", wraps=backend._write_state_sync) as write:
            async with backend._lock:
                tasks = [asyncio.create_task(self.consume()) for _ in range(2)]
                await asyncio.sleep(0)
                self.assertFalse(any(task.done() for task in tasks))
            results = await asyncio.gather(*tasks)
            self.assertEqual(sorted(map(len, results)), [0, 1])
            self.assertEqual(next(result for result in results if result)[0]["path"], "inbox/fixture.txt")
            write.assert_called_once()
        reloaded = await gateway.AutomationStateStore(self.db).load()
        self.assertEqual(list(reloaded.sessions["session"].pending_telegram_attachments_by_chat), ["other"])

    async def test_postgres_skips_only_explicit_opt_in_noop(self):
        backend = gateway._PostgresAutomationStateStore(
            "postgresql://localhost/fixture", sqlite_state_path=None, legacy_state_path=None,
        )
        with mock.patch.object(backend, "_write_state_sync") as write:
            await backend.update(lambda _: False, skip_unchanged=True)
            write.assert_not_called()
            await backend.update(lambda _: False)
            await backend.update(lambda _: True, skip_unchanged=True)
            self.assertEqual(write.call_count, 2)


if __name__ == "__main__":
    unittest.main()
