import os
import pathlib
import socket
import subprocess
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "app_server_tcp_bridge.py"
RUN_APP_SERVER = ROOT / "run_app_server.sh"


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class AppServerTcpBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(self.tmpdir.name)
        self.home = tmp / "home"
        self.workspace = tmp / "workspace"
        self.runner = tmp / "runner.sh"
        self.runner.write_text(f"#!/usr/bin/env sh\nexec sh {RUN_APP_SERVER}\n", encoding="utf-8")
        self.runner.chmod(0o755)
        self.port = free_tcp_port()
        env = dict(os.environ)
        env.update(
            {
                "APP_HOME": str(self.home),
                "APP_WORKSPACE": str(self.workspace),
                "APP_SERVER_CMD": "cat",
                "ARGUS_APP_SERVER_RUNNER": str(self.runner),
                "ARGUS_APP_SERVER_TCP_HOST": "127.0.0.1",
                "ARGUS_APP_SERVER_TCP_PORT": str(self.port),
                "ARGUS_APP_SERVER_FIRST_LINE_TIMEOUT": "0.2s",
            }
        )
        self.proc = subprocess.Popen(
            ["python3", str(BRIDGE)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=False,
        )
        self._wait_for_bridge()

    def tearDown(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=2)
        self.tmpdir.cleanup()

    def _wait_for_bridge(self) -> None:
        deadline = time.monotonic() + 3
        last_error = None
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    return
            except OSError as exc:
                last_error = exc
                time.sleep(0.05)
        raise AssertionError(f"bridge did not start: {last_error}")

    def test_empty_probe_does_not_start_runner(self) -> None:
        with socket.create_connection(("127.0.0.1", self.port), timeout=1):
            pass
        time.sleep(0.3)

        self.assertFalse((self.workspace / "AGENTS.md").exists())

    def test_non_json_first_line_does_not_start_runner(self) -> None:
        with socket.create_connection(("127.0.0.1", self.port), timeout=1) as sock:
            sock.sendall(b"GET / HTTP/1.1\r\n\r\n")
            sock.shutdown(socket.SHUT_WR)
            try:
                self.assertEqual(sock.recv(1024), b"")
            except ConnectionResetError:
                pass
        time.sleep(0.1)

        self.assertFalse((self.workspace / "AGENTS.md").exists())

    def test_jsonl_connection_replays_first_line_and_remaining_input(self) -> None:
        payload = b'{"id":1,"method":"initialize"}\n{"method":"initialized"}\n'
        with socket.create_connection(("127.0.0.1", self.port), timeout=1) as sock:
            sock.sendall(payload)
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                data = sock.recv(1024)
                if not data:
                    break
                chunks.append(data)

        self.assertEqual(b"".join(chunks), payload)
        self.assertTrue((self.workspace / "AGENTS.md").exists())


if __name__ == "__main__":
    unittest.main()
