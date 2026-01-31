#!/usr/bin/env python3

import argparse
import json
import socket
import sys
from typing import Any, Dict, Optional, TextIO


def send_line(fp, obj: Dict[str, Any]) -> None:
    fp.write((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))
    fp.flush()


def read_line(fp) -> Optional[Dict[str, Any]]:
    line = fp.readline()
    if not line:
        return None
    return json.loads(line.decode("utf-8"))


def is_server_request(msg: Dict[str, Any]) -> bool:
    return "id" in msg and "method" in msg


def is_response(msg: Dict[str, Any]) -> bool:
    return "id" in msg and "method" not in msg and ("result" in msg or "error" in msg)


def respond_jsonrpc_error(fp, request_id: Any, *, code: int, message: str) -> None:
    send_line(fp, {"id": request_id, "error": {"code": code, "message": message}})


def maybe_handle_server_request(
    fp,
    msg: Dict[str, Any],
    *,
    auto_approve: bool,
    log_fp: Optional[TextIO],
) -> bool:
    request_id = msg.get("id")
    method = msg.get("method")

    if method == "item/commandExecution/requestApproval":
        decision = "accept" if auto_approve else "decline"
        send_line(fp, {"id": request_id, "result": {"decision": decision}})
        if log_fp:
            print(f"[approval] commandExecution -> {decision}", file=log_fp)
        return True

    if method == "item/fileChange/requestApproval":
        decision = "accept" if auto_approve else "decline"
        send_line(fp, {"id": request_id, "result": {"decision": decision}})
        if log_fp:
            print(f"[approval] fileChange -> {decision}", file=log_fp)
        return True

    respond_jsonrpc_error(
        fp,
        request_id,
        code=-32601,
        message=f"Unsupported server request: {method}",
    )
    return True


def wait_for_response(
    fp,
    *,
    target_id: int,
    raw: bool,
    auto_approve: bool,
    log_fp: Optional[TextIO],
) -> Dict[str, Any]:
    while True:
        msg = read_line(fp)
        if msg is None:
            raise RuntimeError("Connection closed")

        if raw:
            print(json.dumps(msg, ensure_ascii=False))

        if is_server_request(msg):
            maybe_handle_server_request(fp, msg, auto_approve=auto_approve, log_fp=log_fp)
            continue

        if is_response(msg) and msg.get("id") == target_id:
            return msg


def main() -> int:
    parser = argparse.ArgumentParser(description="Minimal Codex app-server TCP smoke test.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--cwd", default="/workspace")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--cmd", help="Run command/exec with a single argv0 (e.g. pwd).")
    mode.add_argument("--prompt", help="Start a thread and send a turn (chat).")

    parser.add_argument(
        "--approval-policy",
        default="never",
        choices=["untrusted", "on-failure", "on-request", "never"],
        help="Ask-for-approval policy for thread/turn (v2).",
    )
    parser.add_argument(
        "--sandbox",
        default="workspace-write",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help="Thread sandbox mode (v2).",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Auto-approve command/file-change approval requests (unsafe).",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print every JSON-RPC line (debug).",
    )
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    args = parser.parse_args()

    sock = socket.create_connection((args.host, args.port), timeout=args.timeout_seconds)
    try:
        fp = sock.makefile("rwb", buffering=0)
        log_fp: Optional[TextIO] = None if args.raw else sys.stderr

        send_line(
            fp,
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {"name": "smoke_client", "title": "Smoke Client", "version": "0.0.1"}
                },
            },
        )
        init_resp = wait_for_response(
            fp,
            target_id=0,
            raw=args.raw,
            auto_approve=args.auto_approve,
            log_fp=log_fp,
        )
        if "error" in init_resp:
            print(json.dumps(init_resp["error"], ensure_ascii=False), file=sys.stderr)
            return 2

        send_line(fp, {"method": "initialized", "params": {}})

        if args.prompt:
            send_line(
                fp,
                {
                    "method": "thread/start",
                    "id": 10,
                    "params": {
                        "cwd": args.cwd,
                        "approvalPolicy": args.approval_policy,
                        "sandbox": args.sandbox,
                    },
                },
            )
            thread_resp = wait_for_response(
                fp,
                target_id=10,
                raw=args.raw,
                auto_approve=args.auto_approve,
                log_fp=log_fp,
            )
            if "error" in thread_resp:
                print(json.dumps(thread_resp["error"], ensure_ascii=False), file=sys.stderr)
                return 2

            thread_id = thread_resp["result"]["thread"]["id"]
            if log_fp:
                print(f"[thread] {thread_id}", file=log_fp)

            send_line(
                fp,
                {
                    "method": "turn/start",
                    "id": 11,
                    "params": {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": args.prompt}],
                        "cwd": args.cwd,
                        "approvalPolicy": args.approval_policy,
                        "sandboxPolicy": {
                            "type": "externalSandbox",
                            "networkAccess": "enabled",
                        },
                    },
                },
            )
            turn_resp = wait_for_response(
                fp,
                target_id=11,
                raw=args.raw,
                auto_approve=args.auto_approve,
                log_fp=log_fp,
            )
            if "error" in turn_resp:
                print(json.dumps(turn_resp["error"], ensure_ascii=False), file=sys.stderr)
                return 2

            turn_id = turn_resp["result"]["turn"]["id"]
            if log_fp:
                print(f"[turn] {turn_id}", file=log_fp)

            agent_text = ""
            saw_delta = False
            while True:
                msg = read_line(fp)
                if msg is None:
                    print("[connection closed]", file=sys.stderr)
                    return 2

                if args.raw:
                    print(json.dumps(msg, ensure_ascii=False))
                    if is_server_request(msg):
                        maybe_handle_server_request(
                            fp, msg, auto_approve=args.auto_approve, log_fp=log_fp
                        )
                        continue
                    if msg.get("method") == "turn/completed":
                        params = msg.get("params") or {}
                        if (params.get("turn") or {}).get("id") == turn_id:
                            return 0
                    continue

                if is_server_request(msg):
                    maybe_handle_server_request(
                        fp, msg, auto_approve=args.auto_approve, log_fp=log_fp
                    )
                    continue

                if msg.get("method") == "item/agentMessage/delta":
                    params = msg.get("params") or {}
                    if params.get("turnId") == turn_id:
                        delta = params.get("delta") or ""
                        saw_delta = True
                        agent_text += delta
                        sys.stdout.write(delta)
                        sys.stdout.flush()
                    continue

                if msg.get("method") == "item/completed" and not saw_delta:
                    params = msg.get("params") or {}
                    if params.get("turnId") == turn_id:
                        item = params.get("item") or {}
                        if item.get("type") == "agentMessage":
                            text = item.get("text") or ""
                            agent_text = text
                            sys.stdout.write(text)
                            sys.stdout.flush()
                    continue

                if msg.get("method") == "turn/completed":
                    params = msg.get("params") or {}
                    if (params.get("turn") or {}).get("id") == turn_id:
                        if agent_text and not agent_text.endswith("\n"):
                            print()
                        return 0

        cmd = args.cmd or "pwd"
        send_line(
            fp,
            {
                "method": "command/exec",
                "id": 1,
                "params": {
                    "command": [cmd],
                    "cwd": args.cwd,
                    "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "enabled"},
                    "timeoutMs": int(args.timeout_seconds * 1000),
                },
            },
        )
        resp = wait_for_response(
            fp,
            target_id=1,
            raw=True,
            auto_approve=args.auto_approve,
            log_fp=log_fp,
        )
        if "error" in resp:
            print(json.dumps(resp["error"], ensure_ascii=False), file=sys.stderr)
            return 2
        return 0
    finally:
        try:
            sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
