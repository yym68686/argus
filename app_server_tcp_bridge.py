#!/usr/bin/env python3

import os
import re
import socket
import subprocess
import sys
import threading
import time


def parse_timeout_seconds(raw: str) -> float:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([smhd]?)", raw.strip())
    if not match:
        raise ValueError("timeout must be like 10s, 0.5s, 1m, or 1h")
    value = float(match.group(1))
    unit = match.group(2)
    if unit == "m":
        value *= 60
    elif unit == "h":
        value *= 3600
    elif unit == "d":
        value *= 86400
    return value


def read_first_line(conn: socket.socket, timeout_s: float, max_bytes: int) -> bytes | None:
    deadline = time.monotonic() + timeout_s
    chunks: list[bytes] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        conn.settimeout(remaining)
        try:
            data = conn.recv(1)
        except socket.timeout:
            return None
        if not data:
            return None
        chunks.append(data)
        if data == b"\n":
            break
        if len(chunks) > max_bytes:
            raise ValueError("first JSONL line exceeds limit before app-server start")
    first_line = b"".join(chunks)
    if not first_line.lstrip().startswith(b"{"):
        return None
    return first_line


def socket_to_process(conn: socket.socket, proc: subprocess.Popen[bytes]) -> None:
    try:
        assert proc.stdin is not None
        while True:
            data = conn.recv(65536)
            if not data:
                break
            proc.stdin.write(data)
            proc.stdin.flush()
    except Exception:
        pass
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass


def process_to_socket(conn: socket.socket, proc: subprocess.Popen[bytes]) -> None:
    try:
        assert proc.stdout is not None
        while True:
            data = proc.stdout.read(65536)
            if not data:
                break
            conn.sendall(data)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass


def handle_connection(conn: socket.socket, runner_path: str, timeout_s: float, max_first_line_bytes: int) -> None:
    with conn:
        try:
            first_line = read_first_line(conn, timeout_s, max_first_line_bytes)
        except Exception as exc:
            print(f"argus bridge: dropping connection before app-server start: {exc}", file=sys.stderr)
            return
        finally:
            try:
                conn.settimeout(None)
            except Exception:
                pass
        if first_line is None:
            return

        env = dict(os.environ)
        env["ARGUS_APP_SERVER_STDIN_GATE"] = "0"
        proc = subprocess.Popen(
            [runner_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            bufsize=0,
        )
        try:
            assert proc.stdin is not None
            proc.stdin.write(first_line)
            proc.stdin.flush()
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
            return

        input_thread = threading.Thread(target=socket_to_process, args=(conn, proc), daemon=True)
        input_thread.start()
        process_to_socket(conn, proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def main() -> int:
    if not os.getenv("APP_SERVER_CMD"):
        print("ERROR: APP_SERVER_CMD is required (the command to start your app-server).", file=sys.stderr)
        return 2
    host = os.getenv("ARGUS_APP_SERVER_TCP_HOST", "0.0.0.0")
    port = int(os.getenv("ARGUS_APP_SERVER_TCP_PORT", "7777"))
    runner_path = os.getenv("ARGUS_APP_SERVER_RUNNER", "/app/run_app_server.sh")
    timeout_s = parse_timeout_seconds(os.getenv("ARGUS_APP_SERVER_FIRST_LINE_TIMEOUT", "10s"))
    max_first_line_bytes = int(os.getenv("ARGUS_APP_SERVER_FIRST_LINE_MAX_BYTES", str(1024 * 1024)))

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(int(os.getenv("ARGUS_APP_SERVER_TCP_BACKLOG", "128")))
        print(f"argus bridge: listening on {host}:{port}", file=sys.stderr, flush=True)
        while True:
            conn, _addr = listener.accept()
            thread = threading.Thread(
                target=handle_connection,
                args=(conn, runner_path, timeout_s, max_first_line_bytes),
                daemon=True,
            )
            thread.start()


if __name__ == "__main__":
    raise SystemExit(main())
