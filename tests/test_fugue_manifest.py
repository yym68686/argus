from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _service_block(manifest: str, service_name: str) -> str:
    marker = f"  {service_name}:\n"
    start = manifest.index(marker)
    cursor = start + len(marker)
    while cursor < len(manifest):
        next_line = manifest.find("\n  ", cursor)
        if next_line == -1:
            return manifest[start:]
        candidate_start = next_line + 1
        if not manifest.startswith("    ", candidate_start):
            return manifest[start:next_line]
        cursor = next_line + 1
    return manifest[start:]


class FugueManifestTest(unittest.TestCase):
    def test_fugue_telegram_bot_is_stateless_webhook_service(self) -> None:
        manifest = (ROOT / "fugue.yaml").read_text(encoding="utf-8")
        telegram_bot = _service_block(manifest, "telegram-bot")

        self.assertIn("public: true", telegram_bot)
        self.assertIn("port: 8080", telegram_bot)
        self.assertIn("TELEGRAM_DELIVERY_MODE: webhook", telegram_bot)
        self.assertNotIn("STATE_PATH:", telegram_bot)
        self.assertNotIn("persistent_storage:", telegram_bot)
        self.assertNotIn("/data", telegram_bot)
