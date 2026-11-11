"""Codex idle pickup: correlate a completed parent turn and keep bounded local timing evidence."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from delivery.task_dispatch import inflight_is_live, mark_inflight
from file_lock import locked_file
from runtime_observation import load
from workspace_default import write_status

EVENTS = {"startup", "enqueued", "submitted", "yielded", "completed", "send_failed", "abandoned", "held", "refused"}


def paths(workspace):
    state = Path(workspace) / "state"
    return state / "codex-notifier-inflight", state / "codex-notifier-yielded"


def mark(workspace, filename, incarnation, prompt):
    inflight, _ = paths(workspace)
    mark_inflight(inflight, filename, incarnation, hashlib.sha256(prompt.encode()).hexdigest())


def ready(workspace, session, incarnation, now=None):
    record, _ = load(workspace, "core", time.time() if now is None else now)
    return bool(record and incarnation and record["observer"] == "codex-observer"
                and record["session"] == session and record.get("core_incarnation") == incarnation
                and record["phase"] == "idle" and record["motion"] == "idle"
                and record["condition"] == "healthy")


def retry_interval(workspace, session, incarnation, now=None):
    record, _ = load(workspace, "core", time.time() if now is None else now)
    if (not record or not incarnation or record["observer"] != "codex-observer"
            or record["session"] != session or record.get("core_incarnation") != incarnation
            or record["condition"] != "healthy" or record["phase"] in {"unknown", "failed", "waiting"}
            or record["motion"] == "unknown"):
        return 30
    path = Path(workspace) / "state" / "codex-pickup.json"
    try:
        if path.stat().st_size > 65536:
            return 30
        state = json.loads(path.read_text())
        if not isinstance(state, dict) or state.get("blocked") or state.get("admission"):
            return 30
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        return 30
    return 1


def yielded(workspace, filename, incarnation, session, timeout=3600, now=None):
    inflight, acknowledged = paths(workspace)
    if not incarnation or not inflight_is_live(inflight, filename, incarnation):
        return False
    if inflight_is_live(acknowledged, filename, incarnation):
        return True
    marker = inflight / filename
    if marker.stat().st_size > 1024:
        return False
    lines = marker.read_text().splitlines()
    current_time = time.time() if now is None else now
    if current_time - marker.stat().st_mtime >= float(timeout):
        record_event(workspace, "held", filename)
    record, _ = load(workspace, "core", current_time)
    if len(lines) != 2 or not record:
        return False
    receipt = record.get("completed_task")
    if (record["observer"] != "codex-observer" or record["session"] != session
            or record["phase"] != "idle" or record["condition"] != "healthy" or not receipt):
        return False
    if (receipt["prompt_sha256"] != lines[1] or receipt["core_incarnation"] != incarnation
            or receipt["accepted_at"] < marker.stat().st_mtime):
        return False
    mark_inflight(acknowledged, filename, incarnation)
    record_event(workspace, "yielded", filename)
    return True


def record_event(workspace, event, filename="", returncode=None, reason=None, incarnation=None):
    if event not in EVENTS:
        raise ValueError("unknown pickup event")
    workspace = Path(workspace)
    path = workspace / "state" / "codex-pickup.json"
    now = time.time()
    row = {"event": event, "at": now}
    if filename:
        row["task_key"] = hashlib.sha256(filename.encode()).hexdigest()[:20]
    if returncode is not None:
        row["returncode"] = int(returncode)
    with locked_file(path.with_suffix(".lock")):
        try:
            old = json.loads(path.read_text()) if path.stat().st_size < 65536 else {}
        except (OSError, ValueError):
            old = {}
        if not isinstance(old, dict):
            old = {}
        admission = old.get("admission")
        if event == "refused":
            admission = {"task_key": row["task_key"], "reason": reason,
                         "incarnation_key": hashlib.sha256(incarnation.encode()).hexdigest()[:20]}
            if old.get("admission") == admission:
                return
        elif event in {"startup", "submitted", "completed", "abandoned"}:
            admission = None
        blocked = old.get("blocked")
        if not isinstance(blocked, dict):
            blocked = None
        if event == "held":
            if blocked and blocked.get("task_key") == row.get("task_key"):
                return
            blocked = {"task_key": row["task_key"], "since": now, "reason": "unconfirmed-parent-turn"}
        elif event in {"yielded", "completed", "abandoned"} and blocked and blocked.get("task_key") == row.get("task_key"):
            blocked = None
        recent = old.get("recent", [])
        recent = [r for r in recent if isinstance(r, dict)][-63:] if isinstance(recent, list) else []
        if filename and event == "submitted":
            first = next((r["at"] for r in recent if r.get("task_key") == row["task_key"]
                          and r.get("event") == "enqueued"), None)
            if first is not None:
                row["wait_ms"] = max(0, round((now - first) * 1000))
        prior_counts = old.get("counts", {})
        prior_counts = prior_counts if isinstance(prior_counts, dict) else {}
        counts = {key: value if isinstance(value := prior_counts.get(key), int) and value >= 0 else 0 for key in EVENTS}
        counts[event] += 1
        code = old.get("code", {})
        if event == "startup":
            blocked = None
            src = Path(__file__).resolve().parents[1]
            code = {name: hashlib.sha256((src / rel).read_bytes()).hexdigest() for name, rel in {
                "notifier": "agent/codex/cli/task-notifier.sh", "pickup": "delivery/codex_pickup.py",
                "observer": "agent/codex/cli/codex-observer.mjs"}.items()}
        write_status("codex-pickup.json", {"schema": 1, "updated_at": now, "pid": os.getppid(),
                     "code": code, "blocked": blocked, "admission": admission, "counts": counts, "recent": recent + [row]}, workspace)


def refused(workspace, filename, session, incarnation):
    record, why = load(workspace, "core", time.time())
    if not record:
        reason = "observer-" + str(why)
    elif record.get("core_incarnation") != incarnation or record["session"] != session:
        reason = "observer-incarnation-mismatch"
    elif record["observer"] != "codex-observer":
        reason = "observer-mismatch"
    else:
        reason = record["reason"] or "lifecycle-" + record["phase"]
    record_event(workspace, "refused", filename, reason=reason, incarnation=incarnation)


def warning(workspace):
    path = Path(workspace) / "state" / "codex-pickup.json"
    try:
        if path.stat().st_size > 65536:
            return None
        record = json.loads(path.read_text())
        admission = record.get("admission") if isinstance(record, dict) else None
        if isinstance(admission, dict) and isinstance(admission.get("reason"), str):
            return "task admission is held: " + admission["reason"] + "; awaiting verified CLI readiness or recovery"
        blocked = record.get("blocked") if isinstance(record, dict) else None
        if isinstance(blocked, dict) and blocked.get("reason") == "unconfirmed-parent-turn":
            return "a task has no confirmed parent-turn receipt after the completion deadline; retained without replay"
    except (OSError, ValueError, TypeError):
        pass
    return None


def send_keys(socket, target, incarnation, *keys):
    from tmux_pane_keys import argv
    if not re.fullmatch(r"%[0-9]+", target) or not re.match(r"[0-9]+:", incarnation):
        return 2
    pid = int(incarnation.split(":", 1)[0])
    return subprocess.run(argv(socket, target, *keys, expected_pid=pid)).returncode


def main():
    command, workspace, *args = sys.argv[1:]
    try:
        if command == "ready":
            return 0 if ready(workspace, *args) else 1
        if command == "retry-interval":
            print(retry_interval(workspace, *args))
            return 0
        if command == "send":
            return send_keys(workspace, *args)
        if command == "mark":
            prompt = sys.stdin.read(1024 * 1024 + 1)
            if len(prompt) > 1024 * 1024:
                return 2
            mark(workspace, *args, prompt)
        elif command == "yielded":
            return 0 if yielded(workspace, *args) else 1
        elif command == "recover":
            from delivery.codex_recovery import recover
            return 0 if recover(workspace, *args) else 1
        elif command == "refused":
            refused(workspace, *args)
        elif command == "record":
            record_event(workspace, *args)
        else:
            return 2
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
