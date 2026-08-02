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

        # not a git work tree: git RAN and returned non-zero. That is an empty
        # result, NOT an unavailable git — the flag must say so, because the
        # caller uses it to decide whether per-file probes are safe.
        ok("no git repo: returns ({}, ran=True) — empty result, not unavailable",
           _mod._note_creation_dates(notes) == ({}, True),
           f"got {_mod._note_creation_dates(notes)!r}")

        _mod.subprocess.run = _boom
        ok("git unavailable: returns ({}, ran=False) — distinguishable from empty",
           _mod._note_creation_dates(notes) == ({}, False),
           f"got {_mod._note_creation_dates(notes)!r}")
        ok("git unavailable: _note_added_at degrades to ''",
           _mod._note_added_at(notes / "n.md") == "", "OSError must be swallowed")
        _mod.subprocess.run = _orig_run

        # a malformed stamp must fall through to mtime, not crash the briefing
        _mod._note_creation_dates = lambda d: ({"n.md": "not-a-timestamp"}, True)
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


# --- rename is not creation (review #2526 P1-1) ----------------------------
# `--diff-filter=A` alone reports a rename as an add, so a note written in June
# and renamed last week reads as new. Repro from the review, held directly.

with tempfile.TemporaryDirectory() as _tmp:
    ws = Path(_tmp)
    notes = ws / "notes"
    notes.mkdir()
    _git(ws, "init", "-q")

    old = notes / "old-name.md"
    old.write_text("body\n")
    _git(ws, "add", "notes/old-name.md")
    _git(ws, "commit", "-qm", "add", when="2026-06-03T12:00:00 +0000")
    _git(ws, "mv", "notes/old-name.md", "notes/new-name.md")
    _git(ws, "commit", "-qm", "rename", when="2026-07-31T12:00:00 +0000")

    renamed = notes / "new-name.md"
    _now = time.time()
    os.utime(renamed, (_now, _now))

    _o_ws, _o_dir = _mod.WORKSPACE, _mod.NOTES_DIR
    try:
        _mod.WORKSPACE, _mod.NOTES_DIR = str(ws), notes
        created, ran = _mod._note_creation_dates(notes)
        stats = _mod.analyze_note_activity()
    finally:
        _mod.WORKSPACE, _mod.NOTES_DIR = _o_ws, _o_dir

    ok("rename carries the ORIGINAL add date, not the rename date",
       created.get("new-name.md", "").startswith("2026-06-03"),
       f"got {created.get('new-name.md')!r}")
    ok("a note renamed last week but written in June is NOT recent",
       stats["recent_7d"] == 0, f"recent_7d={stats['recent_7d']}, expected 0")


# --- no per-note fan-out when git cannot run (review #2526 P1-2) -----------
# Pre-fix, a failed bulk query yielded created={} and _is_recent() then spawned
# one git per note — 356 on the real workspace, each with a 10s timeout.

with tempfile.TemporaryDirectory() as _tmp:
    ws = Path(_tmp)
    notes = ws / "notes"
    notes.mkdir()
    for i in range(3):
        (notes / f"n{i}.md").write_text("body\n")

    _o_ws, _o_dir, _o_run = _mod.WORKSPACE, _mod.NOTES_DIR, _mod.subprocess.run
    _calls = []

    def _count_and_raise(*a, **k):
        _calls.append(a[0] if a else None)
        raise OSError("git not on PATH")

    try:
        _mod.WORKSPACE, _mod.NOTES_DIR = str(ws), notes
        _mod.subprocess.run = _count_and_raise
        stats = _mod.analyze_note_activity()
    finally:
        _mod.subprocess.run = _o_run
        _mod.WORKSPACE, _mod.NOTES_DIR = _o_ws, _o_dir

    ok("git unavailable: exactly ONE git attempt for 3 notes (no per-note fan-out)",
       len(_calls) == 1, f"{len(_calls)} subprocess attempts: expected 1 bulk, no per-note probes")
    ok("git unavailable: still returns a usable count via mtime",
       stats["total"] == 3 and stats["recent_7d"] == 3, f"got {stats}")

    # and when the resolver itself says there is no git, zero spawns
    _o_argv = _mod.git_argv
    _calls2 = []

    def _boom_argv(*a):
        raise _mod_git_unavailable("no runnable git")

    from git_binary import GitUnavailable as _mod_git_unavailable  # noqa: E402
    try:
        _mod.WORKSPACE, _mod.NOTES_DIR = str(ws), notes
        _mod.git_argv = _boom_argv
        _mod.subprocess.run = lambda *a, **k: _calls2.append(1)
        stats2 = _mod.analyze_note_activity()
    finally:
        _mod.git_argv = _o_argv
        _mod.subprocess.run = _o_run
        _mod.WORKSPACE, _mod.NOTES_DIR = _o_ws, _o_dir

    ok("no runnable git: ZERO subprocess spawns",
       len(_calls2) == 0, f"{len(_calls2)} spawns despite GitUnavailable")
    ok("no runnable git: count still produced from mtime",
       stats2["total"] == 3, f"got {stats2}")


# --- no-history workspace must not fan out either (review #2526 round 3) ---
# git RUNS and succeeds, but the notes path has no history: not a worktree, or
# the workspace is ignored by the parent checkout. Measured at the review head:
# 15 subprocess calls for 14 notes. An empty bulk map means the repo has nothing
# to say about notes/, so asking it once per note gets the same nothing N times.
# The call count must be independent of N.

for _n_notes in (3, 9):
    with tempfile.TemporaryDirectory() as _tmp:
        ws = Path(_tmp)
        notes = ws / "notes"
        notes.mkdir()
        for i in range(_n_notes):
            (notes / f"n{i}.md").write_text("body\n")
        # deliberately NOT a git work tree

        _o_ws, _o_dir, _o_run = _mod.WORKSPACE, _mod.NOTES_DIR, _mod.subprocess.run
        _seen = []

        def _tally(*a, **k):
            _seen.append(a[0] if a else None)
            return _o_run(*a, **k)

        try:
            _mod.WORKSPACE, _mod.NOTES_DIR = str(ws), notes
            _mod.subprocess.run = _tally
            created, ran = _mod._note_creation_dates(notes)
            _seen.clear()
            stats = _mod.analyze_note_activity()
        finally:
            _mod.subprocess.run = _o_run
            _mod.WORKSPACE, _mod.NOTES_DIR = _o_ws, _o_dir

        ok(f"no-history workspace ({_n_notes} notes): bulk map is empty",
           created == {}, f"got {created!r}")
        ok(f"no-history workspace ({_n_notes} notes): exactly 1 git call, independent of N",
           len(_seen) == 1,
           f"{len(_seen)} calls for {_n_notes} notes — fan-out scales with N")
        ok(f"no-history workspace ({_n_notes} notes): still returns a count",
           stats["total"] == _n_notes, f"got {stats}")

# and the single definition, since a shadowed duplicate silently kept a
# bare-`git` body alive through one whole review round
import inspect  # noqa: E402
_src = inspect.getsource(_mod)
ok("exactly one _note_creation_dates definition (no shadowed duplicate)",
   _src.count("def _note_creation_dates(") == 1,
   f"{_src.count('def _note_creation_dates(')} definitions")
# Scoped to the two helpers this PR introduces. daily-insight.py has three
# OTHER bare-`git` call sites (author identity, repo-root, dev-activity log)
# that predate this change and are not in its diff — asserting on them would
# fail this PR for code it does not touch. Flagged in the PR body instead.
for _fn in (_mod._note_creation_dates, _mod._note_added_at):
    _fsrc = inspect.getsource(_fn)
    ok(f"{_fn.__name__} builds argv via git_argv, not bare git",
       "git_argv(" in _fsrc and '["git"' not in _fsrc,
       f"bare git argv in {_fn.__name__}")


# --- a no-history host must make NO note-creation claim (review #2526 rd 4) ---
# Bounding the subprocess count fixed the fan-out but not the falsehood: on a
# workspace that is not its own git repo, `recent_7d` is a restatement of mtime,
# and mtime here is the time of the last sync. Reported on the reviewer's host
# as "recent_7d=9" with a direct mtime count of exactly 9 — no creation-date
# evidence behind it. generate_insight() must be unable to say it, in EITHER
# direction: a confident zero is as unevidenced as a confident nine.

def _insight_text(ws, notes):
    """generate_insight() with every HIGHER-PRIORITY source silenced.

    generate_insight() returns the dev-activity headline first and never reaches
    the note branch when the ambient checkout has commits. Without these patches
    the control passes only on a host with no commits today — it did here, and
    failed on a reviewer host that read "Sutando shipped 17 commits...". A
    control whose result depends on ambient git activity proves nothing about
    note suppression.
    """
    _o_ws, _o_dir = _mod.WORKSPACE, _mod.NOTES_DIR
    _o_dev = _mod.analyze_dev_activity
    _o_calls = _mod.load_calls
    _o_tasks = _mod.analyze_task_patterns
    try:
        _mod.WORKSPACE, _mod.NOTES_DIR = str(ws), notes
        _mod.analyze_dev_activity = lambda *a, **k: None
        _mod.load_calls = lambda *a, **k: []
        _mod.analyze_task_patterns = lambda *a, **k: _mod.Counter()
        return _mod.generate_insight() or ""
    finally:
        _mod.analyze_dev_activity = _o_dev
        _mod.load_calls = _o_calls
        _mod.analyze_task_patterns = _o_tasks
        _mod.WORKSPACE, _mod.NOTES_DIR = _o_ws, _o_dir


for _n, _label in ((9, "positive claim"), (14, "reviewer's exact count")):
    with tempfile.TemporaryDirectory() as _tmp:
        ws = Path(_tmp)
        notes = ws / "notes"
        notes.mkdir()
        for i in range(_n):
            (notes / f"n{i}.md").write_text("---\ntags: [x]\n---\nbody\n")
        # NOT a git work tree -> no note history available

        _o_ws, _o_dir = _mod.WORKSPACE, _mod.NOTES_DIR
        try:
            _mod.WORKSPACE, _mod.NOTES_DIR = str(ws), notes
            st = _mod.analyze_note_activity()
        finally:
            _mod.WORKSPACE, _mod.NOTES_DIR = _o_ws, _o_dir

        ok(f"no-history ({_n} notes): age_known is False",
           st.get("age_known") is False, f"got {st.get('age_known')!r}")
        ok(f"no-history ({_n} notes): total still reported (date-independent)",
           st["total"] == _n, f"got {st['total']}")

        txt = _insight_text(ws, notes)
        ok(f"no-history ({_n} notes): insight makes NO note-creation claim ({_label})",
           "notes in the last 7 days" not in txt,
           f"emitted: {txt[:120]!r}")

# control: WITH real git history the claim is still made, so the suppression
# cannot pass by muting the feature outright
with tempfile.TemporaryDirectory() as _tmp:
    ws = Path(_tmp)
    notes = ws / "notes"
    notes.mkdir()
    _git(ws, "init", "-q")
    for i in range(7):
        f = notes / f"r{i}.md"
        f.write_text("---\ntags: [y]\n---\nbody\n")
        _git(ws, "add", f"notes/r{i}.md")
    _git(ws, "commit", "-qm", "add recent")

    _o_ws, _o_dir = _mod.WORKSPACE, _mod.NOTES_DIR
    try:
        _mod.WORKSPACE, _mod.NOTES_DIR = str(ws), notes
        st = _mod.analyze_note_activity()
    finally:
        _mod.WORKSPACE, _mod.NOTES_DIR = _o_ws, _o_dir

    ok("CONTROL: with git history, age_known is True",
       st.get("age_known") is True, f"got {st.get('age_known')!r}")
    ok("CONTROL: with git history, recent_7d is git-derived and non-zero",
       st["recent_7d"] == 7, f"got {st['recent_7d']}")
    txt = _insight_text(ws, notes)
    ok("CONTROL: with git history the claim IS emitted (suppression is not blanket)",
       "notes in the last 7 days" in txt, f"emitted: {txt[:120]!r}")


# --- mixed tracked/untracked must not be called evidenced (review rd 5) -----
# `age_known = bool(created)` flipped True on a single tracked note while every
# untracked note still contributed a sync-reset mtime to the SAME total. The
# reviewer's repro: 1 note committed 2026-06-03 + 7 untracked with fresh mtimes
# reported {total: 8, recent_7d: 7, age_known: True} — seven counted "creations"
# all from the source this module calls unreliable. That is the normal state
# during a rolling sync or just after writing a note, not an exotic one.

with tempfile.TemporaryDirectory() as _tmp:
    ws = Path(_tmp)
    notes = ws / "notes"
    notes.mkdir()
    _git(ws, "init", "-q")
    (notes / "tracked.md").write_text("---\ntags: [a]\n---\nbody\n")
    _git(ws, "add", "notes/tracked.md")
    _git(ws, "commit", "-qm", "old", when="2026-06-03T12:00:00 +0000")
    for i in range(7):
        (notes / f"untracked{i}.md").write_text("---\ntags: [a]\n---\nbody\n")

    _o_ws, _o_dir = _mod.WORKSPACE, _mod.NOTES_DIR
    try:
        _mod.WORKSPACE, _mod.NOTES_DIR = str(ws), notes
        st = _mod.analyze_note_activity()
    finally:
        _mod.WORKSPACE, _mod.NOTES_DIR = _o_ws, _o_dir

    ok("mixed corpus: age_known is False (1 tracked + 7 untracked)",
       st["age_known"] is False, f"got {st}")
    ok("mixed corpus: unevidenced counts the mtime-sourced notes",
       st.get("unevidenced") == 7, f"got {st.get('unevidenced')}")
    txt = _insight_text(ws, notes)
    ok("mixed corpus: no note-creation claim is emitted",
       "notes in the last 7 days" not in txt, f"emitted: {txt[:120]!r}")

# and the fully-tracked control still emits, so the stricter rule is not blanket
with tempfile.TemporaryDirectory() as _tmp:
    ws = Path(_tmp)
    notes = ws / "notes"
    notes.mkdir()
    _git(ws, "init", "-q")
    for i in range(7):
        f = notes / f"t{i}.md"
        f.write_text("---\ntags: [b]\n---\nbody\n")
        _git(ws, "add", f"notes/t{i}.md")
    _git(ws, "commit", "-qm", "all tracked")

    _o_ws, _o_dir = _mod.WORKSPACE, _mod.NOTES_DIR
    try:
        _mod.WORKSPACE, _mod.NOTES_DIR = str(ws), notes
        st = _mod.analyze_note_activity()
    finally:
        _mod.WORKSPACE, _mod.NOTES_DIR = _o_ws, _o_dir

    ok("CONTROL: fully-tracked corpus is evidenced (unevidenced == 0)",
       st["age_known"] is True and st.get("unevidenced") == 0, f"got {st}")
    txt = _insight_text(ws, notes)
    ok("CONTROL: fully-tracked corpus DOES emit the claim",
       "notes in the last 7 days" in txt, f"emitted: {txt[:120]!r}")


print(f"daily-insight-note-age: {_passed}/{_passed + _failed} passed"
      + (f" — {_failed} FAILED" if _failed else ""))
raise SystemExit(1 if _failed else 0)
