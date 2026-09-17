#!/usr/bin/env python3
"""Stop hook: the owner's card clicks are applied when the agent's turn ends, so a click never
waits for anyone to remember --apply. Reads nothing from stdin, never blocks the turn."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import platform
import sys
from pathlib import Path


def load_rf():
    spec = importlib.util.spec_from_file_location(
        "report_feedback", Path(__file__).resolve().parents[1] / "report-feedback.py")
    rf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rf)
    return rf


def main(workspace: Path | None = None, rf=None) -> int:
    try:
        rf = rf or load_rf()
        ws = workspace or rf.resolve_workspace()
        if rf.pending_clicks(ws) == 0:
            return 0
        from file_lock import lock_fd, unlock_fd
        lock = ws / "state" / "feedback-reports.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            lock_fd(fd, blocking=False)
            acquired = True
            with contextlib.redirect_stdout(io.StringIO()):
                rf.apply_clicks(ws, rf.read_prefs(ws), platform.node().split(".")[0],
                                release_automatic=False)
        finally:
            if acquired:
                unlock_fd(fd)
            os.close(fd)
    except Exception:  # noqa: BLE001 — a hook that raises blocks the agent; the next turn retries
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
