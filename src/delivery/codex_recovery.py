"""Bounded Codex liveness probes; terminal errors never acknowledge or replay an owner task."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from delivery.codex_pickup import paths, send_keys
from delivery.pane_gate import CODEX, classify_pane, composer_text
from file_lock import locked_file
from runtime_observation import load
from workspace_default import write_status

BOOTSTRAP_PROMPT = "Reply exactly READY. Do not use tools."
BACKOFF_S = 30


def candidate(workspace, session, incarnation, now):
    record, _ = load(workspace, "core", now)
    if not record or record["observer"] != "codex-observer" or record["session"] != session:
        return None
    receipt = record.get("failed_task")
    try:
        started_at = datetime.strptime(incarnation.split(":", 1)[1], "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, IndexError):
        return None
    if (not incarnation or record.get("core_incarnation") != incarnation or not receipt
            or receipt["accepted_at"] < started_at
            or receipt["error_kind"] != "server-overloaded"
            or now - receipt["completed_at"] < BACKOFF_S):
        return None
    for directory in paths(workspace):
        if directory.exists() and any(directory.iterdir()):
            return None
    return record


def same_incarnation(incarnation):
    pid, _, _ = incarnation.partition(":")
    if not pid.isdigit():
        return False
    result = subprocess.run(["ps", "-o", "lstart=", "-p", pid], capture_output=True,
                            text=True, timeout=5, env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
    return result.returncode == 0 and incarnation == pid + ":" + " ".join(result.stdout.split())


def capture(socket, target):
    result = subprocess.run(["tmux", "-S", socket, "capture-pane", "-e", "-p", "-t", target],
                            capture_output=True, text=True, timeout=5, check=True)
    return result.stdout


def recover(workspace, session, incarnation, socket, target, now=None):
    workspace = Path(workspace)
    supplied_now = now
    now = time.time() if now is None else now
    path = Path(workspace) / "state/codex-recovery.json"
    with locked_file(path.with_suffix(".lock")):
        record = candidate(workspace, session, incarnation, now)
        if not record:
            return False
        try:
            if path.exists():
                if path.stat().st_size > 4096:
                    return False
                old = json.loads(path.read_text())
                if (not isinstance(old, dict) or not isinstance(old.get("incarnation"), str)
                        or not old["incarnation"] or any(not isinstance(old.get(key), (float, int))
                        or isinstance(old[key], bool) or not math.isfinite(old[key]) or old[key] <= 0
                        for key in ("claimed_at", "terminal_at"))):
                    return False
                if old.get("incarnation") == incarnation and not (
                        (record.get("last_completed_at") or 0) > old["claimed_at"]):
                    return False
        except (OSError, ValueError, KeyError, TypeError):
            return False
        pane = capture(socket, target)
        if classify_pane(pane, CODEX, workspace, socket, session).state != "idle-ready":
            return False
        write_status("codex-recovery.json", {"incarnation": incarnation, "claimed_at": now,
                     "terminal_at": record["failed_task"]["completed_at"]}, workspace)
        if not same_incarnation(incarnation) or send_keys(socket, target, incarnation, "-l", "--", BOOTSTRAP_PROMPT) != 0:
            return False
        current = candidate(workspace, session, incarnation, time.time() if supplied_now is None else now)
        if not current or current["failed_task"] != record["failed_task"]:
            return False
        if composer_text(capture(socket, target), CODEX) != BOOTSTRAP_PROMPT:
            return False
        return same_incarnation(incarnation) and send_keys(socket, target, incarnation, "C-m") == 0


def main():
    if sys.argv[1:] == ["prompt"]:
        print(BOOTSTRAP_PROMPT)
        return 0
    try:
        return 0 if recover(*sys.argv[1:]) else 1
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
