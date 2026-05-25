#!/usr/bin/env python3
"""Tests for src/archive-stale-results.py — retention + dry-run logic."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "src" / "archive-stale-results.py"

PASS = 0
FAIL = 0

def ok(label):
    global PASS; PASS += 1; print(f"PASS  {label}")

def fail(label, detail=""):
    global FAIL; FAIL += 1
    print(f"FAIL  {label}" + (f": {detail}" if detail else ""))


def run_script(workspace: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "SUTANDO_WORKSPACE": str(workspace)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, env=env
    )


# ---------------------------------------------------------------------------
# T1 — script exists
# ---------------------------------------------------------------------------
if SCRIPT.exists():
    ok("T1: archive-stale-results.py exists")
else:
    fail("T1: archive-stale-results.py exists")

# ---------------------------------------------------------------------------
# T2 — script has module docstring
# ---------------------------------------------------------------------------
content = SCRIPT.read_text() if SCRIPT.exists() else ""
lines = content.splitlines()
body = "\n".join(l for l in lines if not l.startswith("#!")).lstrip()
if body.startswith('"""') or body.startswith("'''"):
    ok("T2: script has module docstring")
else:
    fail("T2: script has module docstring")

# ---------------------------------------------------------------------------
# T3 — no-op when results/ directory doesn't exist
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    # results/ intentionally absent
    proc = run_script(ws)
    if proc.returncode == 0 and "missing" in proc.stdout:
        ok("T3: exits 0 and reports 'missing' when results/ absent")
    else:
        fail("T3: exits 0 when results/ absent", f"rc={proc.returncode} out={proc.stdout.strip()[:80]}")

# ---------------------------------------------------------------------------
# T4 — archives files older than RETENTION_HOURS
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    results = ws / "results"
    results.mkdir()
    stale = results / "task-old.txt"
    stale.write_text("stale result")
    # Set mtime to 48h ago
    old_time = time.time() - 48 * 3600
    os.utime(stale, (old_time, old_time))
    proc = run_script(ws, {"RETENTION_HOURS": "24"})
    # File should be gone from results/
    if not stale.exists() and proc.returncode == 0:
        ok("T4: archives stale files older than RETENTION_HOURS")
    else:
        fail("T4: archives stale files", f"exists={stale.exists()} rc={proc.returncode} out={proc.stdout.strip()[:80]}")

# ---------------------------------------------------------------------------
# T5 — skips files newer than cutoff
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    results = ws / "results"
    results.mkdir()
    fresh = results / "task-fresh.txt"
    fresh.write_text("fresh result")
    # mtime = now (definitely within 24h)
    proc = run_script(ws, {"RETENTION_HOURS": "24"})
    if fresh.exists() and proc.returncode == 0:
        ok("T5: skips fresh files within RETENTION_HOURS cutoff")
    else:
        fail("T5: skips fresh files", f"exists={fresh.exists()} out={proc.stdout.strip()[:80]}")

# ---------------------------------------------------------------------------
# T6 — skips non-.txt files
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    results = ws / "results"
    results.mkdir()
    non_txt = results / "task-old.json"
    non_txt.write_text("{}")
    old_time = time.time() - 48 * 3600
    os.utime(non_txt, (old_time, old_time))
    proc = run_script(ws, {"RETENTION_HOURS": "24"})
    if non_txt.exists():
        ok("T6: skips non-.txt files even if stale")
    else:
        fail("T6: skips non-.txt files", "non-txt file was moved")

# ---------------------------------------------------------------------------
# T7 — skips subdirectories (archive-* dirs are not touched)
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    results = ws / "results"
    results.mkdir()
    archive_sub = results / "archive-2026-01-01"
    archive_sub.mkdir()
    old_file_in_archive = archive_sub / "task-old.txt"
    old_file_in_archive.write_text("archived")
    old_time = time.time() - 48 * 3600
    os.utime(old_file_in_archive, (old_time, old_time))
    proc = run_script(ws, {"RETENTION_HOURS": "24"})
    if old_file_in_archive.exists():
        ok("T7: does not touch files inside existing archive-* subdirs")
    else:
        fail("T7: does not touch archive-* subdir files", "file inside archive was moved")

# ---------------------------------------------------------------------------
# T8 — DRY_RUN=1 prints but does not move files
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    results = ws / "results"
    results.mkdir()
    stale = results / "task-stale.txt"
    stale.write_text("old result")
    old_time = time.time() - 48 * 3600
    os.utime(stale, (old_time, old_time))
    proc = run_script(ws, {"RETENTION_HOURS": "24", "DRY_RUN": "1"})
    if stale.exists() and "would archive" in proc.stdout:
        ok("T8: DRY_RUN=1 prints 'would archive' without moving file")
    else:
        fail("T8: DRY_RUN=1 dry-run mode", f"exists={stale.exists()} out={proc.stdout.strip()[:80]}")

# ---------------------------------------------------------------------------
# T9 — DRY_RUN case-insensitivity: 'No' and 'FALSE' disable dry-run
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    results = ws / "results"
    results.mkdir()
    stale = results / "task-casetest.txt"
    stale.write_text("old")
    old_time = time.time() - 48 * 3600
    os.utime(stale, (old_time, old_time))
    proc_no = run_script(ws, {"RETENTION_HOURS": "24", "DRY_RUN": "No"})
    # DRY_RUN=No should NOT be dry-run (file should be moved)
    if not stale.exists():
        ok("T9: DRY_RUN=No treats 'No' as falsy — file is moved (not dry-run)")
    else:
        fail("T9: DRY_RUN=No should disable dry-run", f"file still exists; out={proc_no.stdout.strip()[:80]}")

# ---------------------------------------------------------------------------
# T10 — archive directory named archive-YYYY-MM-DD
# ---------------------------------------------------------------------------
import re
with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    results = ws / "results"
    results.mkdir()
    stale = results / "task-dating.txt"
    stale.write_text("old")
    old_time = time.time() - 48 * 3600
    os.utime(stale, (old_time, old_time))
    proc = run_script(ws, {"RETENTION_HOURS": "24"})
    archive_dirs = [d for d in results.iterdir() if d.is_dir() and d.name.startswith("archive-")]
    if archive_dirs and re.match(r"archive-\d{4}-\d{2}-\d{2}", archive_dirs[0].name):
        ok(f"T10: archive dir named correctly ({archive_dirs[0].name})")
    else:
        fail("T10: archive dir named archive-YYYY-MM-DD", f"dirs={[d.name for d in results.iterdir() if d.is_dir()]}")

# ---------------------------------------------------------------------------
# T11 — archived file is inside archive-YYYY-MM-DD/
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    results = ws / "results"
    results.mkdir()
    stale = results / "task-moved.txt"
    stale.write_text("old")
    old_time = time.time() - 48 * 3600
    os.utime(stale, (old_time, old_time))
    proc = run_script(ws, {"RETENTION_HOURS": "24"})
    archive_dirs = [d for d in results.iterdir() if d.is_dir()]
    if archive_dirs:
        archived = list(archive_dirs[0].iterdir())
        if any(f.name == "task-moved.txt" for f in archived):
            ok("T11: archived file found inside archive-YYYY-MM-DD/")
        else:
            fail("T11: file inside archive dir", f"archive contents={[f.name for f in archived]}")
    else:
        fail("T11: archive dir created", "no archive dir found")

# ---------------------------------------------------------------------------
# T12 — multiple stale files all archived in same pass
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    results = ws / "results"
    results.mkdir()
    stales = []
    for i in range(3):
        f = results / f"task-multi-{i}.txt"
        f.write_text(f"old {i}")
        old_time = time.time() - 48 * 3600
        os.utime(f, (old_time, old_time))
        stales.append(f)
    proc = run_script(ws, {"RETENTION_HOURS": "24"})
    remaining = [f for f in stales if f.exists()]
    if not remaining and proc.returncode == 0:
        ok("T12: all 3 stale files archived in single pass")
    else:
        fail("T12: all stale files archived", f"{len(remaining)} remaining out={proc.stdout.strip()[:80]}")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{PASS}/{PASS+FAIL} tests passed")
if __name__ == "__main__":
    pass
