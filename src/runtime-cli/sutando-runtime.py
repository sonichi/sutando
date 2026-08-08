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
  sutando-runtime agent list
  sutando-runtime agent status <agentId>
  sutando-runtime sutando info|status|owner|allowlist
  sutando-runtime task submit "do the thing" [--priority normal]
  sutando-runtime task status|get-result|details|cancel <taskId>

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
    req.add_parser("list")
    for name in ("get", "wait", "cancel"):
        p = req.add_parser(name)
        p.add_argument("request_id")
        if name == "wait":
            p.add_argument("--timeout", type=float, default=300.0)

    agt = sub.add_parser("agent").add_subparsers(dest="cmd", required=True)
    agt.add_parser("list")
    agt.add_parser("status").add_argument("agent_id")

    idn = sub.add_parser("sutando").add_subparsers(dest="cmd", required=True)
    for name in ("info", "status", "owner", "allowlist"):
        idn.add_parser(name)

    rt = sub.add_parser("runtime").add_subparsers(dest="cmd", required=True)
    for name in ("health", "details"):
        rt.add_parser(name)

    hac = sub.add_parser("human-action").add_subparsers(dest="cmd", required=True)
    hreq = hac.add_parser("request")
    hreq.add_argument("--task-id")
    hreq.add_argument("--action", required=True)
    hreq.add_argument("--instructions")
    hreq.add_argument("--deadline")
    hreq.add_argument("--expires-in", type=float)
    for name in ("complete", "decline", "status"):
        p2 = hac.add_parser(name)
        p2.add_argument("request_id")
        if name in ("complete", "decline"):
            p2.add_argument("--note")

    ins = sub.add_parser("instance").add_subparsers(dest="cmd", required=True)
    ins.add_parser("list")
    ist = ins.add_parser("start")
    ist.add_argument("agent_id")
    ist.add_argument("--wait", type=float, default=10.0)
    iat = ins.add_parser("attach")
    iat.add_argument("agent_id")
    iat.add_argument("--print", action="store_true",
                     help="print the tmux command instead of exec'ing it")
    iop = ins.add_parser("open")
    iop.add_argument("agent_id")
    iop.add_argument("--window", action="store_true")

    tsk = sub.add_parser("task").add_subparsers(dest="cmd", required=True)
    tsk.add_parser("list")
    tsub = tsk.add_parser("submit")
    tsub.add_argument("text")
    tsub.add_argument("--priority", default="normal",
                      choices=["urgent", "normal", "low"])
    for name in ("status", "get-result", "details", "cancel"):
        tsk.add_parser(name).add_argument("task_id")

    args = ap.parse_args(argv)
    if args.group == "instance":
        # Registry discovery/start are FILE-based by design — they must work
        # with no daemon running, so they never require the socket.
        import instance_registry
        if args.cmd == "start":
            out = instance_registry.start_instance(args.agent_id,
                                                   wait_s=args.wait)
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0 if out.get("ok") else 1
        if args.cmd == "attach":
            out = instance_registry.attach(args.agent_id)
            if not out.get("ok"):
                print(json.dumps(out, ensure_ascii=False), file=sys.stderr)
                return 1
            if args.print:
                print(" ".join(out["argv"]))
                return 0
            import os as _os
            _os.execvp(out["argv"][0], out["argv"])  # hand the tty to tmux
            return 0
        if args.cmd == "open":
            import terminal_open
            out = terminal_open.open_instance(args.agent_id, window=args.window)
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0 if out.get("ok") else 1
        print(json.dumps({"instances": instance_registry.list_instances()},
                         ensure_ascii=False, indent=1))
        return 0
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
        elif args.group == "agent":
            result = (_rpc("agent.list", {}, timeout=15) if args.cmd == "list"
                      else _rpc("agent.status", {"agentId": args.agent_id},
                                timeout=15))
        elif args.group == "sutando":
            result = _rpc(f"sutando.{args.cmd}", {}, timeout=15)
        elif args.group == "runtime":
            result = _rpc(f"runtime.{args.cmd}", {}, timeout=15)
        elif args.group == "human-action":
            if args.cmd == "request":
                result = _rpc("human_action.request", {
                    "taskId": args.task_id, "action": args.action,
                    "instructions": args.instructions,
                    "deadline": args.deadline,
                    "expiresInS": args.expires_in}, timeout=15)
            else:
                params = {"requestId": args.request_id}
                if getattr(args, "note", None):
                    params["note"] = args.note
                result = _rpc(f"human_action.{args.cmd}", params, timeout=15)
        elif args.group == "task":
            if args.cmd == "list":
                result = _rpc("task.list", {}, timeout=15)
            elif args.cmd == "submit":
                result = _rpc("task.submit", {"task": args.text,
                                              "priority": args.priority},
                              timeout=15)
            else:
                result = _rpc(f"task.{args.cmd.replace('-', '_')}",
                              {"taskId": args.task_id}, timeout=15)
        elif args.group == "request" and args.cmd == "list":
            result = _rpc("request.list", {}, timeout=15)
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
