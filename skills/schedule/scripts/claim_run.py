#!/usr/bin/env python3
"""One record per schedule run, so a slot is started at most once and a lost run is reconciled.

The run's key is (schedule id, slot) from the server's briefing line, the first line of the
task body; without one it is the task's `agent_trigger_id`, then its `id`. The record lives
under `<workspace>/state/schedule/claims/` and every transition holds a per-key lock, so of any
number of concurrent or retried tasks for one run exactly one is told `run`.

States: claimed -> waiting_approval -> claimed (resumed) -> done. A `claimed` record older than
--stale-minutes whose task has no ready result becomes `needs_reconcile`: it is reported to the
owner, never re-run. At most once, with reconciliation; not exactly once.

Usage (all take --workspace <dir>):
  claim_run.py claim  --task-file <task> [--stale-minutes N]
      run <key> | resume <key> (exit 0); skip: <why> (exit 3); needs-reconcile <key> (exit 4)
  claim_run.py sweep  [--stale-minutes N]   prints each run that newly needs reconciling
  claim_run.py wait   --key <key>    claimed -> waiting_approval
  claim_run.py resume --key <key>    waiting_approval -> claimed (the owner approved)
  claim_run.py done   --key <key>    claimed|waiting_approval|needs_reconcile -> done
  claim_run.py release --key <key>   needs_reconcile -> removed (the owner chose to re-run)
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # lint-workspace-resolution: allow-repo-root
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))
from local_task_protocol import parse_task_headers  # noqa: E402
from task_disposition import ANSWERED, task_disposition  # noqa: E402

SKIP_EXIT, RECONCILE_EXIT, REFUSED_EXIT = 3, 4, 5
CLAIMED, WAITING, DONE, RECONCILE = "claimed", "waiting_approval", "done", "needs_reconcile"
STALE_MINUTES = 60
_BRIEFING_RE = re.compile(r"^\[Scheduled job ([^\s,\]]+), run ([^\s,\]]+), set up by [^\]]*\]")
_TRANSITIONS = {"wait": ({CLAIMED}, WAITING), "resume": ({WAITING}, CLAIMED),
                "done": ({CLAIMED, WAITING, RECONCILE}, DONE)}


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


def _record_path(workspace: Path, key: str) -> Path:
    return claims_dir(workspace) / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")


@contextmanager
def _locked(workspace: Path, key: str):
    d = claims_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    with open(_record_path(workspace, key).with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield _record_path(workspace, key)


def read(workspace: Path, key: str) -> "dict | None":
    try:
        return json.loads(_record_path(workspace, key).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"key": key, "state": RECONCILE, "task_id": "unknown", "updated_at": 0}


def _write(path: Path, record: dict) -> None:
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _answered(workspace: Path, task_id: str) -> bool:
    try:
        return task_disposition(workspace, task_id) == ANSWERED
    except ValueError:
        return False


def claim(workspace: Path, key: str, task_id: str, *, stale_minutes: int = STALE_MINUTES,
          now: "float | None" = None) -> "tuple[str, dict]":
    """('run'|'resume'|'skip'|'reconcile', record) for this task's attempt at the run."""
    now = time.time() if now is None else now
    with _locked(workspace, key) as path:
        rec = read(workspace, key)
        if rec is None:
            rec = {"key": key, "task_id": task_id, "state": CLAIMED,
                   "claimed_at": int(now), "updated_at": int(now)}
            _write(path, rec)
            return "run", rec
        state = rec.get("state")
        if state == WAITING:
            rec.update(state=CLAIMED, task_id=task_id, updated_at=int(now))
            _write(path, rec)
            return "resume", rec
        if state == CLAIMED and _settle_stale(workspace, path, rec, now, stale_minutes):
            return ("reconcile" if rec["state"] == RECONCILE else "skip"), rec
        return "skip", rec


def _settle_stale(workspace: Path, path: Path, rec: dict, now: float, stale_minutes: int) -> bool:
    """A stale `claimed` becomes done when its task was answered, else needs_reconcile."""
    if now - rec.get("updated_at", 0) < stale_minutes * 60:
        return False
    answered = _answered(workspace, rec.get("task_id", ""))
    rec.update(state=DONE if answered else RECONCILE, updated_at=int(now))
    _write(path, rec)
    return True


def sweep(workspace: Path, *, stale_minutes: int = STALE_MINUTES,
          now: "float | None" = None) -> "list[dict]":
    """Settle every stale `claimed` run; returns the ones that now need reconciling."""
    now = time.time() if now is None else now
    found = []
    d = claims_dir(workspace)
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        key = _key_of(p)
        if key is None:
            continue
        with _locked(workspace, key) as path:
            rec = read(workspace, key)
            if rec and rec.get("state") == CLAIMED and _settle_stale(
                    workspace, path, rec, now, stale_minutes) and rec["state"] == RECONCILE:
                found.append(rec)
    return found


def _key_of(path: Path) -> "str | None":
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("key")
    except (OSError, ValueError):
        return None


def transition(workspace: Path, key: str, cmd: str, *, now: "float | None" = None) -> "dict | None":
    """Apply wait/resume/done/release; None when the record is not in a state that allows it."""
    now = time.time() if now is None else now
    with _locked(workspace, key) as path:
        rec = read(workspace, key)
        if rec is None:
            return None
        if cmd == "release":
            if rec.get("state") != RECONCILE:
                return None
            path.unlink()
            return rec
        allowed, target = _TRANSITIONS[cmd]
        if rec.get("state") not in allowed:
            return None
        rec.update(state=target, updated_at=int(now))
        _write(path, rec)
        return rec


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
    c.add_argument("--stale-minutes", type=int, default=STALE_MINUTES)
    c.add_argument("--workspace")
    w = sub.add_parser("sweep")
    w.add_argument("--stale-minutes", type=int, default=STALE_MINUTES)
    w.add_argument("--workspace")
    for name in ("wait", "resume", "done", "release"):
        t = sub.add_parser(name)
        t.add_argument("--key", required=True)
        t.add_argument("--workspace")
    args = ap.parse_args(argv)
    ws = _workspace(args.workspace)

    if args.cmd == "sweep":
        for rec in sweep(ws, stale_minutes=args.stale_minutes):
            print(f"needs-reconcile {rec['key']} (claimed by {rec.get('task_id')}, no result)")
        return 0
    if args.cmd != "claim":
        rec = transition(ws, args.key, args.cmd)
        if rec is None:
            cur = read(ws, args.key)
            print(f"refused: {args.key} is {cur.get('state') if cur else 'unclaimed'}")
            return REFUSED_EXIT
        print(f"{args.cmd} {args.key}")
        return 0

    task_file = Path(args.task_file)
    text = task_file.read_text(encoding="utf-8", errors="replace")
    task_id = (parse_task_headers(text).get("id") or "").strip() or task_file.stem
    key = run_key(text, task_file.stem)
    verdict, rec = claim(ws, key, task_id, stale_minutes=args.stale_minutes)
    if verdict in ("run", "resume"):
        print(f"{verdict} {key}")
        return 0
    if verdict == "reconcile":
        print(f"needs-reconcile {key} (claimed by {rec.get('task_id')}, no result)")
        return RECONCILE_EXIT
    print(f"skip: {key} is {rec.get('state')} (task {rec.get('task_id', 'unknown')})")
    return SKIP_EXIT


if __name__ == "__main__":
    sys.exit(main())
