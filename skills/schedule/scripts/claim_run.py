#!/usr/bin/env python3
"""One record per schedule run, so a slot is started at most once and a lost run is reconciled.

The run's key is (schedule id, slot), read from the gateway-written `agent_trigger_id` header of
a `schedule.fired` task (`schedule|<room>|<schedule id>|<slot>|<agent>`), never from the body;
without one it is the raw `agent_trigger_id`, then the task's `id`. The run's tier is the
fire's attested `access_tier`, stored at claim; a missing or unknown tier is `guest`. The
record lives under `<workspace>/state/schedule/claims/` and every transition holds a per-key
lock, so of any number of concurrent or retried tasks for one run exactly one is told `run`.
That claim gets a run id and attempt 1; every later transition must present both, and
`resume` starts the next attempt, so a delayed transition from an earlier attempt is refused.

States: claimed -> waiting_approval -> claimed (resumed) -> done. Each attempt records the task
whose result is its outcome; `resume` takes the resuming task, so an earlier attempt's waiting
notice is never read as the outcome. A `claimed` record older than --stale-minutes is settled
from the outbox's own item for that task's result, found under the gateway's broker id:
CONFIRMED is done; NOT_DELIVERED, OUTCOME_UNKNOWN or no item is `needs_reconcile`, reported to
the owner and never replayed. At most once, with reconciliation; not exactly once.

Usage (all take --workspace <dir>):
  claim_run.py claim  --task-file <task> [--stale-minutes N]
      run <key> <run id> <attempt> <tier> (exit 0); skip: <why> (exit 3);
      needs-reconcile <key> <run id> <attempt> ... (exit 4)
  claim_run.py sweep  [--stale-minutes N]   prints each run that newly needs reconciling
  claim_run.py wait|done|release --key <key> --run-id <run id> --attempt <n>
  claim_run.py resume --key <key> --run-id <run id> --attempt <n> --task-file <resuming task>
      prints `resume <key> <run id> <n+1> <tier>`
      wait: claimed -> waiting_approval; resume: waiting_approval -> claimed (owner approved);
      done: claimed|waiting_approval|needs_reconcile -> done;
      release: needs_reconcile -> removed (the owner chose to run the slot again)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # lint-workspace-resolution: allow-repo-root
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))
if str(REPO / "packages" / "ag2-sparrow") not in sys.path:
    sys.path.append(str(REPO / "packages" / "ag2-sparrow"))
import file_lock, local_task_protocol, outbox, task_envelope  # noqa: E401,E402
from ag2_sparrow import gateway_instance  # noqa: E402

SKIP_EXIT, RECONCILE_EXIT, REFUSED_EXIT = 3, 4, 5
CLAIMED, WAITING, DONE, RECONCILE = "claimed", "waiting_approval", "done", "needs_reconcile"
STALE_MINUTES = 60
_SCHEDULE_TRIGGER_RE = re.compile(
    r"schedule\|![^|]+\|(sch-[0-9a-f]+)\|(\d{4}-\d\d-\d\dT\d\d:\d\d:00Z)\|@[^|]+")
_TRANSITIONS = {"wait": ({CLAIMED}, WAITING), "resume": ({WAITING}, CLAIMED),
                "done": ({CLAIMED, WAITING, RECONCILE}, DONE)}


def _headers(task_text: str, workspace: "Path | None") -> dict:
    return task_envelope.attested_task_headers(task_text, workspace).headers


def run_key(task_text: str, fallback_id: str = "", workspace: "Path | None" = None) -> str:
    """The run's identity from attested headers only: `schedule:<id>@<slot>`, else
    `trigger:<id>`, else `task:<id>`."""
    h = _headers(task_text, workspace)
    trig = (h.get("agent_trigger_id") or "").strip()
    m = _SCHEDULE_TRIGGER_RE.fullmatch(trig)
    if m and (h.get("event_type") or "").strip() == "schedule.fired":
        return f"schedule:{m.group(1)}@{m.group(2)}"
    if trig:
        return f"trigger:{trig}"
    return f"task:{(h.get('id') or '').strip() or fallback_id}"


def run_tier(task_text: str, workspace: "Path | None" = None) -> str:
    """The fire's attested tier; anything missing or outside the broker's tiers is `guest`."""
    raw = _headers(task_text, workspace).get("access_tier")
    tier = local_task_protocol.canonical_access_tier(raw)
    return tier if tier in local_task_protocol.BROKER_WIRE_TIERS else "guest"


def claims_dir(workspace: Path) -> Path:
    return workspace / "state" / "schedule" / "claims"


def _record_path(workspace: Path, key: str) -> Path:
    return claims_dir(workspace) / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")


@contextmanager
def _locked(workspace: Path, key: str):
    lock = _record_path(workspace, key).with_suffix(".lock")
    with file_lock.locked_file(lock, create_mode=0o600):
        yield _record_path(workspace, key)


def read(workspace: Path, key: str) -> "dict | None":
    try:
        return json.loads(_record_path(workspace, key).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"key": key, "state": RECONCILE, "task_id": "unknown", "run_id": "", "updated_at": 0}


def _write(path: Path, record: dict) -> None:
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _outbox_items(workspace: Path, task_id: str):
    """(root, item id) where a gateway delivers this local task's result: the instance that owns it."""
    results = workspace / "results"
    roots = sorted(p for p in results.iterdir() if p.name.startswith(".outbox")) if results.is_dir() else []
    for root in roots:
        instance = root.name[len(".outbox."):] if root.name.startswith(".outbox.") else ""
        if root.name in (".outbox", f".outbox.{instance}") and gateway_instance.owns(task_id, instance):
            yield root, gateway_instance.broker_tid(task_id, instance)


def delivery_outcome(workspace: Path, task_id: str) -> "str | None":
    """The outbox's outcome for this task's result; None when no owning outbox holds an item."""
    outcome = None
    for root, item_id in (_outbox_items(workspace, task_id) if task_id else ()):
        item = outbox.read_item(root, item_id)
        if item is None:
            continue
        if item.get("status") == "DELIVERED":
            return outbox.DeliveryOutcome.CONFIRMED.value
        if item.get("unreadable") or item.get("status") == "PARKED" or outbox.dispatch_unsettled(item):
            outcome = outbox.DeliveryOutcome.OUTCOME_UNKNOWN.value
        elif outcome is None:
            outcome = outbox.DeliveryOutcome.NOT_DELIVERED.value
    return outcome


def _settle_stale(workspace: Path, path: Path, rec: dict, now: float, stale_minutes: int) -> bool:
    """A stale `claimed` is done only on a CONFIRMED delivery; anything else needs reconciling."""
    if now - rec.get("updated_at", 0) < stale_minutes * 60:
        return False
    delivery = delivery_outcome(workspace, rec.get("task_id", ""))
    confirmed = delivery == outbox.DeliveryOutcome.CONFIRMED.value
    rec.update(state=DONE if confirmed else RECONCILE, delivery=delivery or "none",
               updated_at=int(now))
    _write(path, rec)
    return True


def claim(workspace: Path, key: str, task_id: str, *, stale_minutes: int = STALE_MINUTES,
          now: "float | None" = None, tier: str = "guest") -> "tuple[str, dict]":
    """('run'|'skip'|'reconcile', record) for this task's attempt at the run."""
    now = time.time() if now is None else now
    with _locked(workspace, key) as path:
        rec = read(workspace, key)
        if rec is None:
            rec = {"key": key, "task_id": task_id, "run_id": uuid.uuid4().hex, "attempt": 1,
                   "tier": tier, "state": CLAIMED, "claimed_at": int(now), "updated_at": int(now)}
            _write(path, rec)
            return "run", rec
        if rec.get("state") == CLAIMED and _settle_stale(workspace, path, rec, now, stale_minutes):
            return ("reconcile" if rec["state"] == RECONCILE else "skip"), rec
        return "skip", rec


def sweep(workspace: Path, *, stale_minutes: int = STALE_MINUTES,
          now: "float | None" = None) -> "list[dict]":
    """Settle every stale `claimed` run; returns the ones that now need reconciling."""
    now = time.time() if now is None else now
    found = []
    d = claims_dir(workspace)
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            key = json.loads(p.read_text(encoding="utf-8")).get("key")
        except (OSError, ValueError):
            continue
        with _locked(workspace, key) as path:
            rec = read(workspace, key)
            if rec and rec.get("state") == CLAIMED and _settle_stale(
                    workspace, path, rec, now, stale_minutes) and rec["state"] == RECONCILE:
                found.append(rec)
    return found


def transition(workspace: Path, key: str, cmd: str, run_id: str, attempt: int, *,
               task_id: str = "", now: "float | None" = None) -> "dict | None":
    """Apply wait/resume/done/release for the claim's current attempt; None when refused.
    `resume` needs the resuming task, whose result becomes the new attempt's outcome."""
    now = time.time() if now is None else now
    with _locked(workspace, key) as path:
        rec = read(workspace, key)
        if (rec is None or not run_id or rec.get("run_id") != run_id
                or rec.get("attempt") != attempt):
            return None
        if cmd == "release":
            if rec.get("state") != RECONCILE:
                return None
            path.unlink()
            return rec
        allowed, target = _TRANSITIONS[cmd]
        if rec.get("state") not in allowed:
            return None
        if cmd == "resume":
            if not task_id or task_id == rec.get("task_id"):
                return None
            rec.update(attempt=attempt + 1, task_id=task_id)
        rec.update(state=target, updated_at=int(now))
        _write(path, rec)
        return rec


def _task_id(task_file: Path, text: str) -> str:
    return (local_task_protocol.parse_task_headers(text).get("id") or "").strip() or task_file.stem


def _workspace(arg: "str | None") -> Path:
    if arg:
        return Path(arg)
    from workspace_default import resolve_workspace
    return resolve_workspace(migrate=False)


def _reconcile_line(rec: dict) -> str:
    return (f"needs-reconcile {rec['key']} {rec.get('run_id', '')} {rec.get('attempt', '')} "
            f"(task {rec.get('task_id')}, delivery {rec.get('delivery', 'none')})")


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim")
    c.add_argument("--task-file", required=True)
    w = sub.add_parser("sweep")
    for p in (c, w):
        p.add_argument("--stale-minutes", type=int, default=STALE_MINUTES)
        p.add_argument("--workspace")
    for name in ("wait", "resume", "done", "release"):
        t = sub.add_parser(name)
        t.add_argument("--key", required=True)
        t.add_argument("--run-id", required=True)
        t.add_argument("--attempt", type=int, required=True)
        t.add_argument("--workspace")
        if name == "resume":
            t.add_argument("--task-file", required=True)
    args = ap.parse_args(argv)
    ws = _workspace(args.workspace)

    if args.cmd == "sweep":
        for rec in sweep(ws, stale_minutes=args.stale_minutes):
            print(_reconcile_line(rec))
        return 0
    if args.cmd != "claim":
        resumer = ""
        if args.cmd == "resume":
            tf = Path(args.task_file)
            resumer = _task_id(tf, tf.read_text(encoding="utf-8", errors="replace"))
        rec = transition(ws, args.key, args.cmd, args.run_id, args.attempt, task_id=resumer)
        if rec is None:
            cur = read(ws, args.key)
            print(f"refused: {args.key} is {cur.get('state') if cur else 'unclaimed'}, "
                  "or another run id or attempt holds it")
            return REFUSED_EXIT
        print(f"{args.cmd} {args.key} {rec['run_id']} {rec['attempt']} {rec.get('tier', 'guest')}")
        return 0

    task_file = Path(args.task_file)
    text = task_file.read_text(encoding="utf-8", errors="replace")
    task_id = _task_id(task_file, text)
    key = run_key(text, task_file.stem, ws)
    verdict, rec = claim(ws, key, task_id, stale_minutes=args.stale_minutes,
                         tier=run_tier(text, ws))
    if verdict == "run":
        print(f"run {key} {rec['run_id']} {rec['attempt']} {rec['tier']}")
        return 0
    if verdict == "reconcile":
        print(_reconcile_line(rec))
        return RECONCILE_EXIT
    print(f"skip: {key} is {rec.get('state')} (task {rec.get('task_id', 'unknown')})")
    return SKIP_EXIT


if __name__ == "__main__":
    sys.exit(main())
