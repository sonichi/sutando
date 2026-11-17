#!/usr/bin/env python3
"""status-ack: one factual status line for a task the core has not answered, from outside its turn.

Each pass observes every queued task (the unanswered-tasks probe, the attested task envelope,
the activity phase, the result state, the shared ack claim), asks src/status_ack_policy.py
whether to speak, and speaks only by publishing a proactive message about that task: the
gateway's outbox lane sends it, threads it on the task's own message and re-checks the task
just before the send. Nothing here posts, retries or claims a delivery itself.

Before publishing it must win `ack:<task>:initial` (src/ack_claim.py), the claim the core's
notify also takes, then re-check the task. An ack counts as sent only once the outbox holds
its DELIVERED receipt. While only a standby watcher holds the inbox past a threshold, the
owner gets one DM naming the remedy, once per outage.

It speaks only for a task no session has picked up that shows no activity card and no progress
notice; a picked-up task is the card's and the progress notifier's to report.

Off unless STATUS_ACK_ENABLED is true (CLI > env > manifest config, skills/MANIFEST.md).
`serve()` is the hosted loop (scripts/status_ack_worker.py, the manifest's supervised_worker):
one live runner per workspace, a startup recovery pass, then a pass per poll.
Usage: status_ack.py --workspace <ws> [--loop] [--enabled 1]
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional

SKILL_DIR = Path(__file__).resolve().parents[1]
# parents[3] is the checkout this skill ships in; its src/ and scripts/ are the core it observes.
_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ack_claim
import ack_overlap
import local_record
import proactive_routing
import skill_config
import status_ack_policy as policy
from activity_bus import cancel_target
from outbox import list_items
from proactive_dispatch_gate import dispatch_veto
from result_markers import has_skip_action, parse_markers
from result_publish import publish_text
from task_archive import find_task_file
from task_envelope import attested_task_headers, verify_text

import first_seen

HOLDER = "status-ack"
CFG_ENABLED = "STATUS_ACK_ENABLED"
CFG_DEADLINE = {"urgent": "STATUS_ACK_FIRST_ACK_DEADLINE_URGENT_S",
                "normal": "STATUS_ACK_FIRST_ACK_DEADLINE_NORMAL_S",
                "low": "STATUS_ACK_FIRST_ACK_DEADLINE_LOW_S"}
CFG_POLL = "STATUS_ACK_POLL_S"
CFG_WATCHER_DOWN = "STATUS_ACK_WATCHER_DOWN_AFTER_S"
#: Sources with no attested Matrix room and event: never acknowledged here.
NON_MATRIX_SOURCES = frozenset({"discord", "telegram", "slack", "voice", "phone", "whatsapp", "imessage"})
#: How often the hosted loop prunes first-seen and ack-claim records of tasks that left the queue.
PRUNE_EVERY_S = 3600.0
OWNER_ALERT = ("The core is not reading its task inbox: only the standby watcher holds it, and {n} "
               "task(s) are waiting unread. To fix it, restart the core.")


def manifest_config(manifest: Optional[Path] = None) -> dict:
    return skill_config.manifest_config(manifest or SKILL_DIR / "manifest.json")


def config_value(key: str, cli=None, environ=None, manifest_cfg=None) -> str:
    """CLI > env > manifest config; an empty value counts as unset."""
    return skill_config.config_value(key, SKILL_DIR / "manifest.json", cli, environ, manifest_cfg)


def _positive(raw: str, default: float) -> float:
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class Settings:
    enabled: bool
    deadlines: Mapping[str, float]
    poll_s: float = 15.0
    watcher_down_after_s: float = 120.0


def settings(enabled=None, environ=None, manifest_cfg=None) -> Settings:
    get = lambda k, c=None: config_value(k, c, environ, manifest_cfg)  # noqa: E731
    deadlines = {p: _positive(get(k), policy.DEFAULT_FIRST_ACK_DEADLINE_S[p]) for p, k in CFG_DEADLINE.items()}
    return Settings(enabled=get(CFG_ENABLED, enabled).lower() in skill_config.TRUE, deadlines=deadlines,
                    poll_s=_positive(get(CFG_POLL), 15.0), watcher_down_after_s=_positive(get(CFG_WATCHER_DOWN), 120.0))


_PROBE = []


def _load_probe():
    if _PROBE:
        return _PROBE[0]
    spec = importlib.util.spec_from_file_location("unanswered_tasks_probe", _REPO / "scripts" / "unanswered-tasks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _PROBE.append(mod)
    return mod


def probe_snapshot(ws: Path, now: float, holders=None) -> Optional[dict]:
    """The PR 2 probe's machine-readable view, or None when it cannot answer (nothing is said then)."""
    try:
        return _load_probe().snapshot(ws, 0.0, now=now, holders=holders)
    except (SystemExit, ImportError, OSError):
        return None


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def attested_headers(text: str, ws: Path) -> Optional[dict]:
    """The routing headers only when the bridge's envelope stamp verifies; otherwise None."""
    if verify_text(text, ws).get("verdict") != "verified":
        return None
    return dict(attested_task_headers(text, ws).headers)


def result_state(ws: Path, tid: str, probe_answered: bool = False) -> str:
    """none / answered / no-send / sending / archived, read from results/ and tasks/ now."""
    results = ws / "results"
    if find_task_file(ws / "tasks", tid) is None:
        return "archived"
    body = _read(results / f"{tid}.txt")
    if body is not None:
        return "no-send" if has_skip_action(parse_markers(body).actions) else "answered"
    if any(results.glob(f"{tid}.sending*")) or any(results.glob(f"{tid}.txt.sending*")):
        return "sending"
    return "answered" if probe_answered else "none"


def cancel_pending(ws: Path, tid: str, own_file: str) -> bool:
    for f in sorted((ws / "tasks").glob("task-*.txt")):
        if f.name != own_file and cancel_target(_read(f)) == tid:
            return True
    return False


def _outage_path(ws: Path) -> Path:
    return ws / "state" / "status-ack" / "watcher-outage.json"


def _outage(ws: Path) -> Optional[dict]:
    try:
        d = json.loads(_outage_path(ws).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("since"), (int, float)) else None


def _alert_key(outage: dict) -> str:
    return f"alert:outage-{int(outage['since'])}:watcher-down"


def owner_alerted(ws: Path) -> bool:
    """True only once the current outage's alert holds a delivery receipt."""
    outage = _outage(ws)
    return outage is not None and ack_claim.state(ws, _alert_key(outage)) == "confirmed"


def _receipt(ws: Path, name: str) -> Optional[dict]:
    stem = Path(name).stem
    for item in list_items(ws / "results" / proactive_routing.PROACTIVE_OUTBOX_DIRNAME):
        if str(item.get("item_id") or "").startswith(f"{stem}~"):
            return item
    return None


def reconcile(ws: Path) -> list:
    """Settle this runner's published claims from the outbox: DELIVERED with an event id is
    confirmed; withdrawn before the send sent nothing and is released; any other park is unknown."""
    settled = []
    for rec in ack_claim.held_by(ws, HOLDER, "claimed"):
        via = rec.get("via")
        item = _receipt(ws, via) if via else None
        if item is None:
            continue
        key, status, reason = rec["key"], item.get("status"), str(item.get("reason") or "")
        ref = item.get("provider_ref")
        if status == "DELIVERED" and isinstance(ref, str) and ref.startswith("$"):
            if ack_claim.update(ws, key, HOLDER, "confirmed", gen=rec.get("gen"), provider_ref=ref,
                                room=item.get("destination")):
                settled.append((key, "confirmed"))
        elif status == "PARKED" and reason.startswith("withdrawn:"):
            if ack_claim.release(ws, key, HOLDER, gen=rec.get("gen")):
                settled.append((key, "released"))
        elif status == "PARKED":
            if ack_claim.update(ws, key, HOLDER, "unknown", gen=rec.get("gen"), reason=reason):
                settled.append((key, "unknown"))
    return settled


def _gen(claimed) -> Optional[str]:
    return claimed if isinstance(claimed, str) else None


def _stamp_ms(now: float) -> int:
    return int(now * 1000)


def watch_inbox(ws: Path, snap: dict, cfg: Settings, now: float) -> Optional[str]:
    """Track a standby-only outage; past the threshold, queue one owner-DM alert for it. Returns
    the alert's file name when this pass published it."""
    inbox = (snap.get("inbox") or {}).get("state")
    outage = _outage(ws)
    if inbox == "session":
        _outage_path(ws).unlink(missing_ok=True)
        return None
    if inbox != "standby_only":
        return None
    if outage is None:
        local_record.write_whole(_outage_path(ws), {"since": now})
        return None
    if now - outage["since"] < cfg.watcher_down_after_s:
        return None
    key = _alert_key(outage)
    gen = ack_claim.claim(ws, key, HOLDER, now=now, pid=os.getpid())
    if not gen:
        return None
    waiting = sum(1 for t in snap.get("tasks") or [] if t.get("unclaimed_age_s") is not None)
    name = proactive_routing.proactive_filename(_stamp_ms(now))
    publish_text(ws / "results" / name, OWNER_ALERT.format(n=waiting) + "\n")
    ack_claim.update(ws, key, HOLDER, gen=_gen(gen), via=name)
    return name


def observe(ws: Path, row: dict, inbox: str, alerted: bool, now: Optional[float] = None):
    """(TaskObservation, None) or (None, why this task is not considered at all)."""
    tid, own_file = row["task_id"], row.get("file") or f"{row['task_id']}.txt"
    text = _read(ws / "tasks" / own_file)
    if text is None:
        return None, "the task left the queue"
    headers = attested_headers(text, ws)
    if headers is None:
        return None, "the task envelope is not verified"
    if (headers.get("source") or "").strip().lower() in NON_MATRIX_SOURCES:
        return None, "not a Matrix source"
    seen = first_seen.record(ws, tid, time.time() if now is None else now)
    intake = row.get("intake_at") if seen == first_seen.UNREADABLE else policy.intake_at(tid, headers, seen)
    return policy.TaskObservation(
        task_id=tid, headers=headers, intake_at=intake, phase=row.get("phase"),
        result=result_state(ws, tid, bool(row.get("answered"))), ack=ack_claim.state(ws, policy.ack_key(tid)),
        worker_holder=row.get("holder"), inbox=inbox,
        cancel_instruction=cancel_target(text) is not None,
        cancel_requested=cancel_pending(ws, tid, own_file), owner_alerted=alerted,
        card=ack_overlap.card_shown(ws, tid), progress_notice=ack_overlap.progress_shown(ws, tid)), None


def routed_body(decision) -> Optional[str]:
    """The ack's file body with the verified room and thread root as leading markers, or None when
    the markers do not parse back to exactly them; an ack is never sent unthreaded."""
    dest = decision.destination
    body = f"[channel: {dest.room}]\n[thread: {dest.thread_root}]\n{decision.text}\n"
    actions = [(a.kind, a.value) for a in parse_markers(body).actions]
    if sorted(actions) != sorted([("redirect", dest.room), ("thread", dest.thread_root)]):
        return None
    return body


def worker_holder(ws: Path, tid: str):
    """The probe's own worker-holder lookup (it delegates to worker-pool); raising means unreadable."""
    return _load_probe()._holder(ws, tid)


def recheck(ws: Path, tid: str) -> Optional[str]:
    """Why an ack decided a moment ago must not be published now, or None."""
    state = result_state(ws, tid)
    if state != "none":
        return f"result state is now {state}"
    overlap = ack_overlap.overlap_veto(ws, tid, worker_holder)
    if overlap is not None:
        return overlap
    try:
        veto = dispatch_veto(tid, ws)
    except Exception as exc:  # noqa: BLE001 - an unchecked task is never messaged
        return f"the task could not be checked ({exc})"
    return veto


@dataclass
class Report:
    enabled: bool
    spoken: list = field(default_factory=list)
    withdrawn: dict = field(default_factory=dict)
    skipped: dict = field(default_factory=dict)
    settled: list = field(default_factory=list)
    alert: Optional[str] = None


def run_once(ws: Path, cfg: Settings, now: Optional[float] = None, holders=None,
             before_publish: Optional[Callable[[str], None]] = None) -> Report:
    """One pass. Disabled, it reads and writes nothing."""
    report = Report(enabled=cfg.enabled)
    if not cfg.enabled:
        return report
    ws = Path(ws)
    now = time.time() if now is None else now
    report.settled = reconcile(ws)
    snap = probe_snapshot(ws, now, holders)
    if snap is None:
        report.skipped["*"] = "the probe could not answer"
        return report
    report.alert = watch_inbox(ws, snap, cfg, now)
    inbox, alerted = (snap.get("inbox") or {}).get("state") or "unknown", owner_alerted(ws)
    for row in snap.get("tasks") or []:
        tid = row["task_id"]
        obs, why = observe(ws, row, inbox, alerted, now)
        if obs is None:
            report.skipped[tid] = why
            continue
        decision = policy.decide(obs, now, cfg.deadlines)
        if decision.action != policy.SPEAK:
            report.skipped[tid] = decision.reason
            continue
        name = proactive_routing.about_task_filename(tid, _stamp_ms(now), channel="ag2space")
        gen = ack_claim.claim(ws, decision.key, HOLDER, now=now, template=decision.template, pid=os.getpid(), via=name)
        if not gen:
            report.skipped[tid] = "another sender holds the initial ack"
            continue
        if before_publish is not None:
            before_publish(tid)
        veto = recheck(ws, tid)
        if veto is not None:
            ack_claim.release(ws, decision.key, HOLDER, gen=_gen(gen))
            report.withdrawn[tid] = veto
            continue
        body = routed_body(decision)
        if body is None:
            ack_claim.release(ws, decision.key, HOLDER, gen=_gen(gen))
            report.withdrawn[tid] = "the verified thread could not be carried"
            continue
        publish_text(ws / "results" / name, body)
        report.spoken.append(tid)
    return report


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _published(ws: Path, via) -> bool:
    """True when the claim's file is in results/, claimed by the gateway, archived after delivery
    (`archive/<via>` or the bridge's `archive/<stem>-<ts>.txt`), or has an outbox receipt."""
    if not isinstance(via, str) or not via:
        return False
    results, stem = ws / "results", Path(via).stem
    archive = results / "archive"
    return ((results / via).exists() or any(results.glob(f"{stem}.sending*"))
            or (archive / via).exists() or any(archive.glob(f"{stem}-[0-9]*.txt"))
            or _receipt(ws, via) is not None)


def recover(ws: Path) -> list:
    """Startup: a claim this runner holds whose file was never published (or that names none),
    from a process that is gone, becomes unknown: it is never released or re-announced. A claim
    whose file is published is left for reconcile(); a live process's claim is left alone."""
    settled = []
    for rec in ack_claim.held_by(ws, HOLDER, "claimed"):
        if (rec.get("pid") != os.getpid() and _pid_alive(rec.get("pid"))) or _published(ws, rec.get("via")):
            continue
        if ack_claim.update(ws, rec["key"], HOLDER, "unknown", gen=rec.get("gen"),
                            reason="the runner stopped between claim and publish"):
            settled.append((rec["key"], "unknown"))
    return settled


def _queued(ws: Path):
    tasks = ws / "tasks"
    if not tasks.is_dir():
        return lambda tid: True
    return lambda tid: find_task_file(tasks, tid) is not None


def prune(ws: Path, now: float) -> dict:
    """Bound the runner's records: first-seen and ack claims of tasks that left the queue, and
    alerts of outages that ended, once past retention. A queued task keeps every record."""
    queued, outage = _queued(ws), _outage(ws)
    current = _alert_key(outage) if outage else None

    def keep(rec: dict) -> bool:
        kind, subject, _ = rec["key"].split(":")
        if kind in ("ack", "progress"):
            return queued(subject)
        if kind == "alert":
            return rec["key"] == current
        return True

    return {"first_seen": first_seen.prune(ws, queued, now),
            "claims": ack_claim.prune(ws, keep, now, first_seen.RETENTION_S)}


def runner_lock(ws: Path):
    """The open lock file when this process is now the workspace's one live runner, else None."""
    path = ws / "state" / "status-ack" / "runner.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def serve(ws: Path, settings_fn: Callable[[], Settings] = settings, passes: Optional[int] = None,
          clock: Optional[Callable[[], float]] = None, sleep: Optional[Callable[[float], None]] = None,
          holders=None, emit: Optional[Callable[[dict], None]] = None):
    """The hosted loop. Settings are re-read every pass; while off it touches nothing and keeps
    waiting, so a supervisor never sees it exit. Recovery runs once, under the runner lock."""
    clock, sleep = clock or time.time, sleep or time.sleep
    emit = emit or (lambda d: print(json.dumps(d, sort_keys=True, default=str), flush=True))
    ws, lock, recovered, last_prune, n = Path(ws), None, None, None, 0
    try:
        while passes is None or n < passes:
            n += 1
            cfg = settings_fn()
            if cfg.enabled and lock is None:
                lock = runner_lock(ws)
            if not cfg.enabled:
                emit({"enabled": False})
            elif lock is None:
                emit({"enabled": True, "skipped": {"*": "another runner holds this workspace"}})
            else:
                now, first = clock(), recovered is None
                if first:
                    recovered = recover(ws)
                report = run_once(ws, cfg, now=now, holders=holders).__dict__
                if first:
                    report["recovered"] = recovered
                if last_prune is None or now - last_prune >= PRUNE_EVERY_S:
                    report["pruned"], last_prune = prune(ws, now), now
                emit(report)
            sleep(cfg.poll_s)
    finally:
        if lock is not None:
            lock.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="status-ack runner (off unless enabled)")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--enabled", default=None, help=f"overrides {CFG_ENABLED}")
    ap.add_argument("--loop", action="store_true", help="repeat every STATUS_ACK_POLL_S seconds")
    a = ap.parse_args(argv)
    if a.loop:
        serve(Path(a.workspace), lambda: settings(enabled=a.enabled))
        return 0
    r = run_once(Path(a.workspace), settings(enabled=a.enabled))
    print(json.dumps(r.__dict__, sort_keys=True, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
