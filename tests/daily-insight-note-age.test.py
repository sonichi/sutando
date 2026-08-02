#!/usr/bin/env python3
"""`recent_7d` must date notes by git, not by mtime.

Regression test for an owner-visible falsehood in the 2026-08-02 briefing:

    You've created 356 notes in the last 7 days (356 total).

`recent_7d` was identically `total` because the filter read `st_mtime`. This
workspace is a git-backed vault synced across hosts, and both `git checkout`
and the rsync path stamp every file with the time of the *sync* — on that day
673 of 725 notes shared one mtime to the minute. Every note therefore fell
inside the seven-day window and the filter could not discriminate. The true
figure from git was 50. `skills/task-orphan-check/SKILL.md` documents the same
mtime trap for task files.

The discriminator here is a note **committed 60 days ago but touched now**:
mtime says new, git says old. Test 2 is the control — a genuinely recent note
must still be counted, so the fix cannot pass by counting nothing.

Run: python3 tests/daily-insight-note-age.test.py
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("daily_insight", REPO / "src" / "daily-insight.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# Fail informatively against a pre-fix module rather than dying on AttributeError,
# so the control run states WHY it failed instead of raising.
if not hasattr(_mod, "_note_creation_dates"):
    print("  FAIL: daily-insight has no _note_creation_dates — note age is still "
          "read from st_mtime, which the workspace sync resets")
    print("daily-insight-note-age: 0/1 passed — 1 FAILED")
    raise SystemExit(1)

_passed = 0
_failed = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def _git(repo: Path, *args: str, when: str | None = None) -> None:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    })
    if when:
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    subprocess.run(["git", "-C", str(repo), *args], env=env, check=True,
                   capture_output=True, text=True)


with tempfile.TemporaryDirectory() as _tmp:
    ws = Path(_tmp)
    notes = ws / "notes"
    notes.mkdir()
    _git(ws, "init", "-q")

    old = notes / "old-note.md"
    old.write_text("---\ntags: [alpha]\n---\nbody\n")
    _git(ws, "add", "notes/old-note.md")
    _git(ws, "commit", "-qm", "add old", when="2026-06-03T12:00:00 +0000")

    new = notes / "new-note.md"
    new.write_text("---\ntags: [beta]\n---\nbody\n")
    _git(ws, "add", "notes/new-note.md")
    _git(ws, "commit", "-qm", "add new")  # committed now

    # Reproduce the sync: BOTH files get a fresh mtime, exactly as a checkout
    # or rsync leaves them. Pre-fix this makes both look new.
    now = time.time()
    for f in (old, new):
        os.utime(f, (now, now))

    ok("precondition: both notes carry a fresh mtime (sync reproduced)",
       all(now - f.stat().st_mtime < 60 for f in (old, new)),
       "os.utime did not take — the discriminator would be vacuous")

    _orig_ws, _orig_dir = _mod.WORKSPACE, _mod.NOTES_DIR
    try:
        _mod.WORKSPACE = str(ws)
        _mod.NOTES_DIR = notes
        stats = _mod.analyze_note_activity()
    finally:
        _mod.WORKSPACE, _mod.NOTES_DIR = _orig_ws, _orig_dir

    ok("a note committed 60d ago is NOT recent, despite a fresh mtime",
       stats["recent_7d"] == 1, f"recent_7d={stats['recent_7d']}, expected 1")
    ok("control: the genuinely recent note IS still counted",
       stats["recent_7d"] >= 1, f"recent_7d={stats['recent_7d']} — fix counts nothing")
    ok("total still counts every .md on disk",
       stats["total"] == 2, f"got {stats['total']}")
    ok("recent_7d is not identically total (the pre-fix signature)",
       stats["recent_7d"] != stats["total"],
       f"recent_7d==total=={stats['total']} — filter still cannot discriminate")


# --- degradation contracts -------------------------------------------------
# Each of these is a path the fix falls back through when git cannot answer.
# They are the difference between "the counter degrades" and "the briefing
# crashes", so they are asserted rather than left to coverage.

with tempfile.TemporaryDirectory() as _tmp:
    ws = Path(_tmp)
    notes = ws / "notes"
    notes.mkdir()
    (notes / "n.md").write_text("body\n")

    _orig_ws, _orig_dir = _mod.WORKSPACE, _mod.NOTES_DIR
    _orig_run = _mod.subprocess.run
    _orig_dates = _mod._note_creation_dates

    def _boom(*a, **k):
        raise OSError("git not on PATH")

    try:
        _mod.WORKSPACE = str(ws)
        _mod.NOTES_DIR = notes

        # not a git work tree at all: git returns non-zero, no exception
        ok("no git repo: _note_creation_dates returns {} rather than raising",
           _mod._note_creation_dates(notes) == {}, "expected empty map")

        _mod.subprocess.run = _boom
        ok("git unavailable: _note_creation_dates degrades to {}",
           _mod._note_creation_dates(notes) == {}, "OSError must be swallowed")
        ok("git unavailable: _note_added_at degrades to ''",
           _mod._note_added_at(notes / "n.md") == "", "OSError must be swallowed")
        _mod.subprocess.run = _orig_run

        # a malformed stamp must fall through to mtime, not crash the briefing
        _mod._note_creation_dates = lambda d: {"n.md": "not-a-timestamp"}
        stats = _mod.analyze_note_activity()
        ok("malformed git stamp falls back to mtime instead of raising",
           stats["total"] == 1 and stats["recent_7d"] == 1,
           f"got {stats}")
    finally:
        _mod.subprocess.run = _orig_run
        _mod._note_creation_dates = _orig_dates
        _mod.WORKSPACE, _mod.NOTES_DIR = _orig_ws, _orig_dir

# Teardown must actually restore, or a later case silently runs against the
# monkeypatch. Asserted rather than trusted — the first version of this block
# reassigned the lambda to itself and "passed".
ok("teardown restored the real _note_creation_dates",
   _mod._note_creation_dates is _orig_dates and "lambda" not in repr(_mod._note_creation_dates),
   f"still patched: {_mod._note_creation_dates!r}")


print(f"daily-insight-note-age: {_passed}/{_passed + _failed} passed"
      + (f" — {_failed} FAILED" if _failed else ""))
raise SystemExit(1 if _failed else 0)
