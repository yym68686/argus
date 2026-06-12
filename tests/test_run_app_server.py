import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "run_app_server.sh"


class RunAppServerScriptTests(unittest.TestCase):
    def _run_script(self, *, input_bytes: bytes, app_server_cmd: str, timeout: float = 5.0) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            env = dict(os.environ)
            env.update(
                {
                    "APP_HOME": str(tmp / "home"),
                    "APP_WORKSPACE": str(tmp / "workspace"),
                    "APP_SERVER_CMD": app_server_cmd,
                    "ARGUS_APP_SERVER_FIRST_LINE_TIMEOUT": "0.2s",
                }
            )
            return subprocess.run(
                ["sh", str(SCRIPT)],
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                timeout=timeout,
                check=False,
            )

    def test_empty_probe_input_does_not_start_app_server(self) -> None:
        result = self._run_script(input_bytes=b"", app_server_cmd="printf started")

        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(result.stdout, b"")

    def test_non_json_first_line_does_not_start_app_server(self) -> None:
        result = self._run_script(input_bytes=b"GET / HTTP/1.1\r\n\r\n", app_server_cmd="printf started")

        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(result.stdout, b"")

    def test_jsonl_first_line_and_remaining_input_are_replayed(self) -> None:
        payload = b'{"id":1,"method":"initialize"}\n{"method":"initialized"}\n'
        result = self._run_script(input_bytes=payload, app_server_cmd="cat")

        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(result.stdout, payload)


if __name__ == "__main__":
    unittest.main()
