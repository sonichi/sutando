"""Task-bound wedge policy: durable debounce, recovery and safe waiting projections.

The progress publisher supplies the clock and task ownership. No process repair or pane text
leaves this module; shared probe signatures become fixed, audience-safe explanations.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path

from file_lock import locked_file
from local_task_protocol import valid_local_task_id

SILENCE_S = 180.0
CONFIRM_S = 20.0
DM_AFTER_S = 60.0
SIGNAL_FRESH_S = 60.0
TAIL_BYTES = 256 * 1024
MESSAGES = {
    "provider-limit": "Provider limit reached. Wait for reset or change provider.",
    "pane-prompt-blocked": "Approval or sign-in needed. Open the runtime to continue.",
    "watcher-not-ready": "Task watcher unavailable. Re-arm the runtime watcher.",
    "core-status-stale": "Runtime progress stopped. Check the runtime before restarting.",
    "activity-silent": "No recent task activity. Check the running tool or runtime.",
}
_PROBE_CACHE = {}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= 0 else None
    except (TypeError, ValueError):
        return None


def _read(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}


def _save(path, value):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def _signals(workspace, now):
    key = str(workspace.resolve())
    cached = _PROBE_CACHE.get(key)
    if cached and 0 <= now - cached[0] < 10:
        return cached[1]
    found = []
    try:
        from wedge_probes import probe_all
        found = [s.as_dict() for s in probe_all(workspace, now=now)]
    except (ImportError, OSError, ValueError):
        pass
    path = workspace / "state" / "core-supervisor.json"
    try:
        data = _read(path)
        if 0 <= now - path.stat().st_mtime <= SIGNAL_FRESH_S:
            if data.get("state") in ("blocked-human", "blocked-known", "logged-out"):
                from cli_wedge_patterns import ABNORMAL_PATTERNS, PROVIDER_LIMIT_PATTERNS
                text = " ".join(str(data.get(k) or "") for k in ("kind", "detail", "prompt"))
                limited = any(rx.search(text) for kind, rx in ABNORMAL_PATTERNS
                              if kind in PROVIDER_LIMIT_PATTERNS)
                found.insert(0, {"kind": "provider-limit" if limited else "pane-prompt-blocked"})
    except (OSError, ValueError):
        pass
    _PROBE_CACHE[key] = (now, found)
    return found


def _activity(workspace, task_id, status, now):
    snap = _read(workspace / "state" / "activity" / f"{task_id}.json")
    times = [status.get("since"), snap.get("last_activity_at"), snap.get("started_at")]
    try:
        with (workspace / "state" / "agent-activity.jsonl").open("rb") as stream:
            stream.seek(0, 2)
            start = max(0, stream.tell() - TAIL_BYTES)
            stream.seek(start)
            if start:
                stream.readline()
            for raw in stream.read().splitlines():
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(row, dict) and isinstance(row.get("task"), dict) and row["task"].get("id") == task_id:
                    if row.get("done") or row.get("kind") == "done":
                        return snap, None, True
                    times.append(row.get("ts"))
    except FileNotFoundError:
        pass
    stamps = [n for v in times if (n := _number(v)) is not None and n <= now]
    return snap, max(stamps) if stamps else None, snap.get("phase") in ("COMPLETED", "FAILED", "CANCELLED")


def _owner_notice(workspace, task_id, state, path):
    if state.get("dm_reserved"):
        return
    state["dm_reserved"] = True
    _save(path, state)  # reserve before enqueue: a crash may omit a DM, never duplicate one
    digest = hashlib.sha256(f"{task_id}:{state['first']}:{state['kind']}".encode()).hexdigest()[:24]
    result = workspace / "results" / f"proactive-wedge-{digest}.txt"
    result.parent.mkdir(parents=True, exist_ok=True)
    if result.exists():
        return
    tmp = result.with_name(f".{result.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(f"[dm-only]\nA task needs attention: {MESSAGES[state['kind']]}\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, result)


def _status_for(workspace, task_id, status, *, now=None, signals=None, silence_s=SILENCE_S):
    """Overlay accepted work only. Unknown, finished, queued and delegated work stays untouched.
    Signals are current observations, not events: their absence clears a signature's episode."""
    if not valid_local_task_id(task_id):
        return status
    workspace = Path(workspace)
    now = time.time() if now is None else now
    root = workspace / "state" / "task-wedges"
    path = root / f"{task_id}.json"
    if status is None and not path.exists():
        return None
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with locked_file(root / f".{task_id}.lock", create_mode=0o600):
        previous = _read(path)
        if status is None or (workspace / "results" / f"{task_id}.txt").exists():
            if previous:
                _save(path, {})
            return status
        snap, last, terminal = _activity(workspace, task_id, status, now)
        waiting = status.get("waiting") or {}
        accepted = snap.get("phase") == "RUNNING"
        excluded = terminal or status.get("phase") == "Delegated" or (
            waiting.get("reason") in ("queued", "busy", "delegated") and not accepted)
        kind = None
        if not excluded:
            observations = _signals(workspace, now) if signals is None else signals
            for signal in observations:
                if signal.get("kind") not in MESSAGES or signal.get("kind") == "activity-silent":
                    continue
                target = signal.get("task_id")
                if target != task_id and (target or not snap or snap.get("worker") not in (None, "", "core")):
                    continue
                since = _number(signal.get("since"))
                if signal["kind"] == "core-status-stale" and since is not None and last is not None and last > since:
                    continue
                kind = signal["kind"]
                break
            if kind is None and waiting.get("reason") == "permission":
                kind = "pane-prompt-blocked"
            if (kind is None and (status.get("phase") != "Waiting" or accepted)
                    and snap.get("phase") != "WAITING" and last is not None and now - last >= silence_s):
                kind = "activity-silent"
        if kind is None:
            if previous:
                _save(path, {})
            return status
        same = previous.get("kind") == kind and previous.get("generation") == snap.get("generation")
        if kind == "activity-silent" and last != previous.get("activity_at"):
            same = False
        state = previous if same else {"kind": kind, "generation": snap.get("generation"), "first": now}
        if kind == "activity-silent" and not same:
            state["first"] = last + silence_s
        state["activity_at"] = last
        _save(path, state)
        if kind != "activity-silent" and now - state["first"] < CONFIRM_S:
            return status
        if now - state["first"] >= DM_AFTER_S:
            _owner_notice(workspace, task_id, state, path)
        return {"phase": "Waiting", "summary": MESSAGES[kind], "since": state["first"],
                "waiting": {"reason": "blocked", "kind": kind}}


def status_for(workspace, task_id, status, *, now=None, signals=None, silence_s=SILENCE_S):
    """An unreadable observation is unknown, never a blocker or permission to replace state."""
    try:
        return _status_for(workspace, task_id, status, now=now, signals=signals, silence_s=silence_s)
    except (OSError, ValueError, TypeError, AttributeError):
        return status
