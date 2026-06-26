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

    def test_runtime_image_ssh_allows_fugue_root_login(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        start_runtime = (ROOT / "start_runtime.sh").read_text(encoding="utf-8")

        self.assertIn("openssh-server", dockerfile)
        self.assertIn("ENV HOME=/workspace", dockerfile)
        self.assertIn("APP_HOME=/workspace/.argus", dockerfile)
        self.assertIn("/workspace/.argus", dockerfile)
        self.assertIn("sed -i 's#^root:", dockerfile)
        self.assertIn("PermitRootLogin prohibit-password", dockerfile)
        self.assertIn("AuthorizedKeysFile /root/.ssh/authorized_keys", dockerfile)
        self.assertIn("AllowUsers root", dockerfile)
        self.assertNotIn("PermitRootLogin no", dockerfile)
        self.assertNotIn("AllowUsers fugue", dockerfile)
        self.assertIn("mkdir -p /run/sshd /root/.ssh /workspace/.argus", start_runtime)
        self.assertIn("FUGUE_SSH_SESSION_ENV_CONFIG", start_runtime)
        self.assertIn("Include $FUGUE_SSH_SESSION_ENV_CONFIG", start_runtime)
        self.assertIn("/usr/sbin/sshd -t", start_runtime)
        self.assertNotIn("/home/fugue/.ssh", start_runtime)

    def test_web_image_starts_root_ssh_for_fugue_endpoint(self) -> None:
        dockerfile = (ROOT / "apps/web/Dockerfile").read_text(encoding="utf-8")
        start_web = (ROOT / "apps/web/start_web.sh").read_text(encoding="utf-8")

        self.assertIn("openssh-server", dockerfile)
        self.assertIn("PermitRootLogin prohibit-password", dockerfile)
        self.assertIn("AuthorizedKeysFile /root/.ssh/authorized_keys", dockerfile)
        self.assertIn("AllowUsers root", dockerfile)
        self.assertIn("EXPOSE 22", dockerfile)
        self.assertIn('CMD ["/app/start_web.sh"]', dockerfile)
        self.assertIn("FUGUE_SSH_SESSION_ENV_CONFIG", start_web)
        self.assertIn("Include $FUGUE_SSH_SESSION_ENV_CONFIG", start_web)
        self.assertIn("sshd -t", start_web)
        self.assertIn("/usr/sbin/sshd -D -e &", start_web)
        self.assertIn("exec /docker-entrypoint.sh nginx -g 'daemon off;'", start_web)
