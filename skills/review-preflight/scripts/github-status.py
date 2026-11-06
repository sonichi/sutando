#!/usr/bin/env python3
"""Collect bounded current PR status and render a canonical publication body.

Read-only by default; optional foreground posting delegates to the runtime approval owner.
No receipt file input, free-form body, votes, merges or authority grants.
"""
import argparse
import datetime
import importlib.util
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from decision_evidence import fingerprint
from outcome_claims import summarize, render


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


decision = load("status_decision", "github-decision.py")
evidence = load("status_evidence", "github-evidence.py")


def collect(repo, number, expected_head=None, runner=None, budget=24):
    deadline = time.monotonic() + budget

    def run(args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(args, budget)
        if runner:
            return runner(args)
        return subprocess.run(args, capture_output=True, text=True, timeout=min(6, remaining))

    policy = decision.collect(repo, number, expected_head, runner=run)
    checks = evidence.collect(repo, number, expected_head, runner=run)
    result = summarize(checks, policy, datetime.datetime.now(datetime.timezone.utc).timestamp())
    try:
        proc = run(["gh", "api", f"repos/{repo}/pulls/{number}"])
        after = json.loads(proc.stdout) if proc.returncode == 0 else None
        if not isinstance(after, dict) or fingerprint(after) != policy.get("metadata_digest"):
            raise ValueError()
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        result = {"schema": 1, "merge_outcome": "unknown", "checks_status": "unknown",
                  "overall_readiness": "unknown", "review_status": "unknown", "owner_authority": "unknown",
                  "errors": ["Mutable PR metadata changed or final observation unavailable"]}
    result["body"] = render(result)
    result["observed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result["scope_note"] = "Canonical body only; existing arbitrary-prose publication paths remain outside this command."
    return result


def publish(result, repo, number, room, tool, runner=None, now=None, task_id=None):
    if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
        return {"ok": False, "reason": "Publication task ID must be nonempty; no publication attempted"}
    if result.get("errors") != [] or result.get("repository") != repo or result.get("pr") != number or not result.get("head_sha"):
        return {"ok": False, "reason": "Incomplete status evidence; no publication attempted"}
    clock = now if callable(now) else lambda: datetime.datetime.now(datetime.timezone.utc).timestamp() if now is None else now
    try:
        observed = datetime.datetime.fromisoformat(result["observed_at"].replace("Z", "+00:00"))
        age = clock() - observed.timestamp()
        if observed.tzinfo is None or not 0 <= age <= 30:
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return {"ok": False, "reason": "Publication observation expired; recollect before posting"}
    deadline = time.monotonic() + 30 - age
    body = f"{repo}#{number}, head {result.get('head_sha')}, observed {result.get('observed_at')}: {render(result)}"
    effect = ["--action", "message.send", "--resource", json.dumps({"roomId": room}),
              "--input", json.dumps({"body": body})]
    if task_id is not None:
        effect.extend(["--task-id", task_id])

    def call(args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(args, 30)
        argv = [sys.executable, str(tool), *args]
        proc = runner(argv) if runner else subprocess.run(argv, capture_output=True, text=True, timeout=min(20, remaining))
        value = json.loads(proc.stdout)
        if proc.returncode != 0 or not isinstance(value, dict):
            raise ValueError()
        return value

    executing = False
    try:
        approval = call(["approval", "request", *effect, "--expires-in", "30",
                         "--reason", "Publish this exact current PR status to the selected room"])
        rid = approval.get("requestId")
        if not isinstance(rid, str) or not rid or approval.get("status") != "pending":
            raise ValueError()
        waited = call(["request", "wait", rid, "--timeout", "10"])
        if waited.get("requestId") != rid:
            raise ValueError()
        if waited.get("status") != "approved":
            return {"ok": False, "state": "NOT_APPROVED", "approval_request_id": rid,
                    "reason": "Runtime approval did not resolve approved; no execution attempted"}
        if not 0 <= clock() - observed.timestamp() <= 30:
            return {"ok": False, "state": "EXPIRED", "approval_request_id": rid,
                    "reason": "Observation expired while awaiting approval; recollect before posting"}
        executing = True
        receipt = call(["capability", "execute", *effect, "--approval", rid,
                        "--idempotency-key", str(uuid.uuid4())])
        posted = receipt.get("result") or {}
        if receipt.get("status") != "completed" or not isinstance(posted, dict) or posted.get("executed") is not True or not isinstance(posted.get("eventId"), str) or not posted["eventId"]:
            return {"ok": False, "state": "OUTCOME_UNKNOWN", "approval_request_id": rid,
                    "reason": "Runtime execution not confirmed; no retry attempted"}
        return {"ok": True, "state": "CONFIRMED", "event_id": posted["eventId"],
                "approval_request_id": rid, "execution_request_id": receipt.get("requestId")}
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return {"ok": False, "state": "OUTCOME_UNKNOWN" if executing else "APPROVAL_UNKNOWN",
                "reason": "Runtime outcome unavailable; no retry attempted"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("pr", type=int)
    parser.add_argument("--expect-head")
    parser.add_argument("--room", help="Explicit foreground publication destination")
    parser.add_argument("--room-tool", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--runtime-tool", type=Path, help="Adapter-supplied sutando-runtime.py CLI path")
    parser.add_argument("--task-id", help="Supplied publication task context; does not authenticate the caller")
    args = parser.parse_args()
    if args.room_tool:
        parser.error("Direct room publication is disabled; use --room with --runtime-tool for exact-effect approval")
    if bool(args.room) != bool(args.runtime_tool):
        parser.error("--room and --runtime-tool must be supplied together")
    if args.runtime_tool and not args.runtime_tool.is_file():
        parser.error("--runtime-tool must be an installed CLI file")
    if args.task_id is not None and (not args.room or not args.task_id.strip()):
        parser.error("--task-id requires publication and a nonempty value")
    result = collect(args.repo, args.pr, args.expect_head)
    if args.room:
        result["publication"] = publish(result, args.repo, args.pr, args.room, args.runtime_tool, task_id=args.task_id)
    print(json.dumps(result, indent=2))
    return 2 if result["errors"] or result.get("publication", {}).get("ok") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
