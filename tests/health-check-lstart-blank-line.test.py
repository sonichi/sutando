#!/usr/bin/env python3
"""A blank line in `ps -o pid=,lstart=` output must not derail the start-time parse.

`.stdout.strip().split("\n")` yields an empty element whenever ps emits a blank,
and the loop skips it. Nothing exercised that skip, so a regression there would
surface as a mis-parsed start time — a wrong stale verdict — not as a crash.

Run: python3 tests/health-check-lstart-blank-line.test.py
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import time
from pathlib import Path
from unittest import mock

os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-lstart-")

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hc_lstart", REPO / "src/health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

PID, PID2 = "515151", "515152"
LSTART = "Mon Aug 24 00:00:00 2026"
LSTART2 = "Mon Aug 24 01:00:00 2026"
_real = subprocess.run


def _run(cmd, *a, **kw):
    joined = " ".join(str(x) for x in (cmd if isinstance(cmd, list) else [cmd]))
    if "pgrep" in joined:
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{PID}\n{PID2}\n", stderr="")
    if "/bin/ps" in joined and "lstart" in joined:
        # The INTERIOR blank is the point. A leading/trailing one would be eaten
        # by .strip() and never reach the loop — the branch needs a row on each side.
        return subprocess.CompletedProcess(
            cmd, 0, stdout=f"{PID} {LSTART}\n\n{PID2} {LSTART2}\n", stderr="")
    # Everything else (git, lsof) must reach the real tool: _file_unchanged_since
    # asks git, and a stubbed failure there makes it answer "unchanged" and return.
    return _real(cmd, *a, **kw)


def _verdict(mtime: float) -> dict:
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "svc.py"
        src.write_text("# fixture\n")
        os.utime(src, (mtime, mtime))
        check = {"name": "svc", "status": "ok", "detail": "running"}
        with mock.patch.object(hc.subprocess, "run", _run):
            hc.mark_stale_if_outdated(check, src, "svc")
        return check


proc_start = time.mktime(time.strptime(LSTART, "%a %b %d %H:%M:%S %Y"))

newer = _verdict(time.time())
assert newer["status"] != "ok", f"blank line broke the parse: {newer}"
assert "newer" in newer["detail"] or "restart" in newer["detail"], newer

# CONTROL: a source OLDER than the process must stay ok, or the assertion above
# would pass on a function that flags everything.
older = _verdict(proc_start - 86_400)
assert older["status"] == "ok", f"control flagged an older source: {older}"

print("PASS — blank lines in ps output are skipped and the start time still parses")
