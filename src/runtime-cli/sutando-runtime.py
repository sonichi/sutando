#!/usr/bin/env python3
"""sutando-runtime — CLI face of the local runtime API (v0).

The agent-facing command for human-collaboration requests; the agent never
touches the socket, JSON-RPC, or any remote API directly:

  sutando-runtime approval request --action github.pull_request.merge \
      --resource '{"repository":"o/r","pullRequest":1}' --reason "checks green"
  sutando-runtime elicitation request --question "Deploy where?" \
      --type single_select --options '["staging","production"]'
  sutando-runtime capability execute --action message.send \
      --resource '{"roomId":"!r:hs"}' --input '{"body":"hi"}'
  sutando-runtime request get <requestId>
  sutando-runtime request wait <requestId> --timeout 300
  sutando-runtime request cancel <requestId>

Issuing commands return immediately with {"requestId", "status": "pending"};
`request wait` blocks (bounded) for the resolution. Output is JSON on stdout;
exit 0 on a well-formed response (whatever the status), 1 on transport or
protocol error — status interpretation belongs to the caller.

Env: SUTANDO_RUNTIME_SOCKET (default <run dir>/sutando-runtime.sock).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import uuid
from pathlib import Path


# Canonical socket resolution shared with the daemon (review blocker: both
# sides duplicated a macOS-only fallback; rundir.py is the one policy).
_RUNTIME_API_DIR = Path(__file__).resolve().parent.with_name("runtime-api")
sys.path.insert(0, str(_RUNTIME_API_DIR))
from rundir import socket_path as _socket_path  # noqa: E402


def _rpc(method: str, params: dict, timeout: float) -> dict:
    frame = json.dumps({"jsonrpc": "2.0", "id": f"cli-{uuid.uuid4().hex[:8]}",
                        "method": method, "params": params},
                       ensure_ascii=False) + "\n"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(_socket_path())
        s.sendall(frame.encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    resp = json.loads(buf.decode("utf-8"))
    if "error" in resp:
        raise RuntimeError(f"{resp['error'].get('code')}: {resp['error'].get('message')}")
    return resp["result"]


def _jarg(v):
    return json.loads(v) if v else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sutando-runtime")
    sub = ap.add_subparsers(dest="group", required=True)

    apr = sub.add_parser("approval").add_subparsers(dest="cmd", required=True) \
             .add_parser("request")
    apr.add_argument("--task-id")
    apr.add_argument("--action", required=True)
    apr.add_argument("--resource")
    apr.add_argument("--input",
                     help="JSON input of the effect being approved (for "
                          "governed actions the daemon binds the approval to "
                          "the EXACT action+resource+input — e.g. the message "
                          "body for message.send)")
    apr.add_argument("--reason")
    apr.add_argument("--expires-in", type=float)

    eli = sub.add_parser("elicitation").add_subparsers(dest="cmd", required=True) \
             .add_parser("request")
    eli.add_argument("--task-id")
    eli.add_argument("--question", required=True)
    eli.add_argument("--type", default="single_select",
                     choices=["free_text", "single_select", "multi_select",
                              "confirmation"],
                     help="elicitation type (default: single_select; the v0 "
                          "server rejects free_text — card transport is "
                          "options-based)")
    eli.add_argument("--options")
    eli.add_argument("--expires-in", type=float)

    cap = sub.add_parser("capability").add_subparsers(dest="cmd", required=True) \
             .add_parser("execute")
    cap.add_argument("--task-id")
    cap.add_argument("--action", required=True)
    cap.add_argument("--resource")
    cap.add_argument("--input")
    cap.add_argument("--idempotency-key")
    cap.add_argument("--approval", help="approval requestId that authorizes this execution (consumed once)")

    req = sub.add_parser("request").add_subparsers(dest="cmd", required=True)
    for name in ("get", "wait", "cancel"):
        p = req.add_parser(name)
        p.add_argument("request_id")
        if name == "wait":
            p.add_argument("--timeout", type=float, default=300.0)

    args = ap.parse_args(argv)
    try:
        if args.group == "approval":
            result = _rpc("approval.request", {
                "taskId": args.task_id, "action": args.action,
                "resource": _jarg(args.resource), "input": _jarg(args.input),
                "reason": args.reason,
                "expiresInS": args.expires_in}, timeout=15)
        elif args.group == "elicitation":
            result = _rpc("elicitation.request", {
                "taskId": args.task_id, "question": args.question,
                "type": args.type, "options": _jarg(args.options),
                "expiresInS": args.expires_in}, timeout=15)
        elif args.group == "capability":
            result = _rpc("capability.execute", {
                "taskId": args.task_id, "action": args.action,
                "resource": _jarg(args.resource), "input": _jarg(args.input),
                "idempotencyKey": args.idempotency_key,
                "approvalRequestId": args.approval}, timeout=60)
        else:
            method = f"request.{args.cmd}"
            params = {"requestId": args.request_id}
            timeout = 15.0
            if args.cmd == "wait":
                params["timeoutS"] = args.timeout
                timeout = args.timeout + 10
            result = _rpc(method, params, timeout=timeout)
    except (OSError, RuntimeError, ValueError) as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
