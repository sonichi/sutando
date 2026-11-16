#!/usr/bin/env python3
"""Claim one schedule run before acting on it, so a slot is acted on at most once.

The run's key is (schedule id, slot) from the server's briefing line, the first line of the
task body; without one it is the task's `agent_trigger_id`, then its `id`. The claim is a file
under `<workspace>/state/schedule/claims/` created with O_CREAT|O_EXCL, so of any number of
concurrent or retried tasks for the same run exactly one gets `run`.

A claim is taken before the job starts and never released: a crash after the claim loses that
run instead of repeating its side effects (at most once, not exactly once).

Usage:
  claim_run.py claim --task-file <task> [--workspace <dir>]
    prints `run <key>` (exit 0) or `skip: <key> already claimed by <task id>` (exit 3)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # lint-workspace-resolution: allow-repo-root
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))
from local_task_protocol import parse_task_headers  # noqa: E402

SKIP_EXIT = 3
_BRIEFING_RE = re.compile(r"^\[Scheduled job ([^\s,\]]+), run ([^\s,\]]+), set up by [^\]]*\]")


def run_key(task_text: str, fallback_id: str = "") -> str:
    """The run's identity: `schedule:<id>@<slot>`, else `trigger:<id>`, else `task:<id>`."""
    parsed = parse_task_headers(task_text)
    first = parsed.body.split("\n", 1)[0].strip()
    m = _BRIEFING_RE.match(first)
    if m:
        return f"schedule:{m.group(1)}@{m.group(2)}"
    trig = (parsed.get("agent_trigger_id") or "").strip()
    if trig:
        return f"trigger:{trig}"
    return f"task:{(parsed.get('id') or '').strip() or fallback_id}"


def claims_dir(workspace: Path) -> Path:
    return workspace / "state" / "schedule" / "claims"


def claim(workspace: Path, key: str, task_id: str) -> "tuple[bool, dict]":
    """(True, record) when this call took the claim; (False, existing record) otherwise."""
    d = claims_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    path = d / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")
    record = {"key": key, "task_id": task_id, "claimed_at": int(time.time())}
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            return False, json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False, {"key": key, "task_id": "unknown"}
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(record, f)
        f.flush()
        os.fsync(f.fileno())
    return True, record


def _workspace(arg: "str | None") -> Path:
    if arg:
        return Path(arg)
    from workspace_default import resolve_workspace
    return resolve_workspace(migrate=False)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim")
    c.add_argument("--task-file", required=True)
    c.add_argument("--workspace")
    args = ap.parse_args(argv)

    task_file = Path(args.task_file)
    text = task_file.read_text(encoding="utf-8", errors="replace")
    task_id = (parse_task_headers(text).get("id") or "").strip() or task_file.stem
    key = run_key(text, task_file.stem)
    won, record = claim(_workspace(args.workspace), key, task_id)
    if won:
        print(f"run {key}")
        return 0
    print(f"skip: {key} already claimed by {record.get('task_id', 'unknown')}")
    return SKIP_EXIT


if __name__ == "__main__":
    sys.exit(main())
