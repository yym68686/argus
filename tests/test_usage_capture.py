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


class UsageCaptureTests(unittest.TestCase):
    def test_extract_openai_usage_reads_completed_response_usage(self) -> None:
        payload = {
            "type": "response.completed",
            "response": {
                "id": "resp_123",
                "model": "gpt-5.4-2026-03-05",
                "status": "completed",
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 5,
                    "output_tokens_details": {"reasoning_tokens": 2},
                    "total_tokens": 16,
                },
            },
        }

        extracted = argus_app._extract_openai_usage(payload)

        self.assertEqual(extracted["responseId"], "resp_123")
        self.assertEqual(extracted["model"], "gpt-5.4-2026-03-05")
        self.assertEqual(extracted["status"], "completed")
        self.assertEqual(extracted["inputTokens"], 11)
        self.assertEqual(extracted["outputTokens"], 5)
        self.assertEqual(extracted["reasoningTokens"], 2)
        self.assertEqual(extracted["totalTokens"], 16)

    def test_record_openai_proxy_usage_preserves_flattened_stream_snapshot(self) -> None:
        store = mock.Mock()
        event = {
            "sessionId": "sess_123",
            "agentId": "u1-main",
            "ownerUserId": 1,
            "status": "ok",
        }
        snapshot = {
            "responseId": "resp_123",
            "model": "gpt-5.4-2026-03-05",
            "status": "completed",
            "inputTokens": 11,
            "outputTokens": 5,
            "reasoningTokens": 2,
            "totalTokens": 16,
        }

        with mock.patch.object(argus_app.app.state, "usage_store", store, create=True):
            argus_app._record_openai_proxy_usage(event, snapshot)

        recorded = store.record.call_args.args[0]
        self.assertEqual(recorded["responseId"], "resp_123")
        self.assertEqual(recorded["model"], "gpt-5.4-2026-03-05")
        self.assertEqual(recorded["status"], "completed")
        self.assertEqual(recorded["inputTokens"], 11)
        self.assertEqual(recorded["outputTokens"], 5)
        self.assertEqual(recorded["reasoningTokens"], 2)
        self.assertEqual(recorded["totalTokens"], 16)


if __name__ == "__main__":
    unittest.main()
