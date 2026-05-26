#!/usr/bin/env python3
"""Behavioral tests for `src/check-pending-questions.py`.

Tests the parsing and gate functions in isolation — no macOS notifications
are fired, no Discord DMs sent. The module-level path globals
(PQ_FILE, LAST_NOTIFY_FILE, PRESENTER_SENTINEL, VOICE_LOG, RESULTS_DIR)
are monkeypatched to tempdir fixtures so nothing touches the live workspace.

Functions under test:
  presenter_mode_active()  — checks sentinel file + expiry timestamp
  get_waiting_questions()  — parses pending-questions.md sections
  should_notify()          — 1-hour cooldown guard
  voice_client_connected() — reads voice-agent.log [Health] lines

Run: python3 tests/check-pending-questions.test.py
Exit: 0 on pass, non-zero on fail.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SRC = REPO / "src"

# -----------------------------------------------------------------------
# Module loader
# -----------------------------------------------------------------------
_WORKSPACE = tempfile.mkdtemp(prefix="sutando-test-pq-")
os.environ["SUTANDO_WORKSPACE"] = _WORKSPACE
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

for _n in ("check-pending-questions", "workspace_default", "util_paths"):
    sys.modules.pop(_n, None)

_spec = importlib.util.spec_from_file_location(
    "check_pending_questions", _SRC / "check-pending-questions.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

PASS = 0
FAIL = 0
_TD = Path(_WORKSPACE)


def ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"PASS  {label}")


def fail(label: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL  {label}" + (f": {detail}" if detail else ""))


def _patch(sentinel=None, pq_file=None, last_notify=None, voice_log=None, results_dir=None):
    """Context-manager-ish: set module paths to given values, restore on exit."""
    class _Ctx:
        def __enter__(self):
            self._orig = {
                "PRESENTER_SENTINEL": _mod.PRESENTER_SENTINEL,
                "PQ_FILE": _mod.PQ_FILE,
                "LAST_NOTIFY_FILE": _mod.LAST_NOTIFY_FILE,
                "VOICE_LOG": _mod.VOICE_LOG,
                "RESULTS_DIR": _mod.RESULTS_DIR,
            }
            if sentinel is not None:
                _mod.PRESENTER_SENTINEL = sentinel
            if pq_file is not None:
                _mod.PQ_FILE = pq_file
            if last_notify is not None:
                _mod.LAST_NOTIFY_FILE = last_notify
            if voice_log is not None:
                _mod.VOICE_LOG = voice_log
            if results_dir is not None:
                _mod.RESULTS_DIR = results_dir
            return self

        def __exit__(self, *_):
            for k, v in self._orig.items():
                setattr(_mod, k, v)

    return _Ctx()


# -----------------------------------------------------------------------
# presenter_mode_active() tests
# -----------------------------------------------------------------------

# T1 — no sentinel → False
with tempfile.TemporaryDirectory() as td:
    s = Path(td) / "presenter-mode.sentinel"
    with _patch(sentinel=s):
        if not _mod.presenter_mode_active():
            ok("T1: no sentinel file → presenter_mode_active returns False")
        else:
            fail("T1: no sentinel", "got True when file absent")

# T2 — sentinel with FUTURE timestamp → True (presenter mode active)
with tempfile.TemporaryDirectory() as td:
    s = Path(td) / "presenter-mode.sentinel"
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    s.write_text(future)
    with _patch(sentinel=s):
        if _mod.presenter_mode_active():
            ok("T2: future-dated sentinel → presenter_mode_active returns True")
        else:
            fail("T2: future sentinel", f"got False for expiry={future}")

# T3 — sentinel with PAST timestamp → False (expired)
with tempfile.TemporaryDirectory() as td:
    s = Path(td) / "presenter-mode.sentinel"
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    s.write_text(past)
    with _patch(sentinel=s):
        if not _mod.presenter_mode_active():
            ok("T3: expired sentinel → presenter_mode_active returns False")
        else:
            fail("T3: expired sentinel", f"got True for past expiry={past}")

# T4 — malformed sentinel content (doesn't start with digit) → False (fail-closed)
with tempfile.TemporaryDirectory() as td:
    s = Path(td) / "presenter-mode.sentinel"
    s.write_text("garbage content not an ISO date")
    with _patch(sentinel=s):
        if not _mod.presenter_mode_active():
            ok("T4: malformed sentinel content → presenter_mode_active returns False (fail-closed)")
        else:
            fail("T4: malformed sentinel", "got True for non-ISO content")

# -----------------------------------------------------------------------
# get_waiting_questions() tests
# -----------------------------------------------------------------------

# T5 — no file → empty list
with tempfile.TemporaryDirectory() as td:
    pq = Path(td) / "pending-questions.md"
    with _patch(pq_file=pq):
        result = _mod.get_waiting_questions()
        if result == []:
            ok("T5: missing pending-questions.md → empty list")
        else:
            fail("T5: missing file", f"got {result}")

# T6 — single unanswered question
with tempfile.TemporaryDirectory() as td:
    pq = Path(td) / "pending-questions.md"
    pq.write_text(
        "# Pending Questions\n\n"
        "## Deploy timing for PR #1086\n"
        "- **Status:** unanswered\n"
        "- Asked: 2026-05-20\n"
    )
    with _patch(pq_file=pq):
        result = _mod.get_waiting_questions()
        if len(result) == 1 and "Deploy timing" in result[0]["title"]:
            ok("T6: single unanswered section → returned in list")
        else:
            fail("T6: single unanswered", f"got {result}")

# T7 — answered question NOT included
with tempfile.TemporaryDirectory() as td:
    pq = Path(td) / "pending-questions.md"
    pq.write_text(
        "## Resolved question\n"
        "- **Status:** answered\n"
        "- Answer: yes, ship it\n"
    )
    with _patch(pq_file=pq):
        result = _mod.get_waiting_questions()
        if result == []:
            ok("T7: answered question not included in results")
        else:
            fail("T7: answered excluded", f"got {result}")

# T8 — mixed: unanswered + answered → only unanswered returned
with tempfile.TemporaryDirectory() as td:
    pq = Path(td) / "pending-questions.md"
    pq.write_text(
        "# Pending Questions\n\n"
        "## Unanswered one\n"
        "- **Status:** unanswered\n\n"
        "## Already resolved\n"
        "- **Status:** answered\n\n"
        "## Also waiting\n"
        "- **Status:** Waiting on Bassil\n"
    )
    with _patch(pq_file=pq):
        result = _mod.get_waiting_questions()
        titles = [q["title"] for q in result]
        if len(result) == 2 and "Unanswered one" in titles and "Also waiting" in titles:
            ok("T8: mixed sections — only unanswered/waiting returned")
        else:
            fail("T8: mixed sections", f"titles={titles}")

# T9 — legacy Q1 — Title format (## Q1 — Title) is recognized
with tempfile.TemporaryDirectory() as td:
    pq = Path(td) / "pending-questions.md"
    pq.write_text(
        "## Q1 — Should we merge PR 968 first?\n"
        "- **Status:** unanswered\n"
    )
    with _patch(pq_file=pq):
        result = _mod.get_waiting_questions()
        if len(result) == 1 and "Q1 — Should we merge" in result[0]["title"]:
            ok("T9: legacy Q1 — Title format recognized")
        else:
            fail("T9: legacy format", f"got {result}")

# T10 — section with no **Status:** field → skipped
with tempfile.TemporaryDirectory() as td:
    pq = Path(td) / "pending-questions.md"
    pq.write_text(
        "## A question with no status field\n"
        "- Just some notes, no status line\n"
    )
    with _patch(pq_file=pq):
        result = _mod.get_waiting_questions()
        if result == []:
            ok("T10: section without **Status:** field skipped")
        else:
            fail("T10: no status field skipped", f"got {result}")

# -----------------------------------------------------------------------
# should_notify() tests
# -----------------------------------------------------------------------

# T11 — no .last-pq-notify file → should notify
with tempfile.TemporaryDirectory() as td:
    f = Path(td) / ".last-pq-notify"
    with _patch(last_notify=f):
        if _mod.should_notify():
            ok("T11: no .last-pq-notify → should_notify returns True")
        else:
            fail("T11: no file → notify", "got False when file absent")

# T12 — .last-pq-notify with mtime <1h ago → cooldown active → False
with tempfile.TemporaryDirectory() as td:
    f = Path(td) / ".last-pq-notify"
    f.write_text(str(int(time.time())))
    # mtime = now (definitely within 1h)
    with _patch(last_notify=f):
        if not _mod.should_notify():
            ok("T12: recent .last-pq-notify → cooldown active → should_notify False")
        else:
            fail("T12: cooldown active", "got True for recent notify file")

# T13 — .last-pq-notify with mtime >1h ago → should notify again
with tempfile.TemporaryDirectory() as td:
    f = Path(td) / ".last-pq-notify"
    f.write_text("old")
    # Set mtime to 2 hours ago
    old_time = time.time() - 7201
    os.utime(f, (old_time, old_time))
    with _patch(last_notify=f):
        if _mod.should_notify():
            ok("T13: .last-pq-notify older than 1h → should_notify True")
        else:
            fail("T13: expired cooldown", "got False for file >1h old")

# -----------------------------------------------------------------------
# voice_client_connected() tests
# -----------------------------------------------------------------------

# T14 — no voice log → False
with tempfile.TemporaryDirectory() as td:
    vl = Path(td) / "voice-agent.log"
    with _patch(voice_log=vl):
        if not _mod.voice_client_connected():
            ok("T14: no voice-agent.log → voice_client_connected returns False")
        else:
            fail("T14: no log file", "got True when file absent")

# T15 — log with [Health] client=true → True
with tempfile.TemporaryDirectory() as td:
    vl = Path(td) / "voice-agent.log"
    vl.write_text(
        "[2026-05-26T10:00:00Z] Voice agent started\n"
        "[2026-05-26T10:00:05Z] [Health] client=true streams=1\n"
    )
    with _patch(voice_log=vl):
        if _mod.voice_client_connected():
            ok("T15: log with [Health] client=true → voice_client_connected True")
        else:
            fail("T15: client=true", "got False")

# T16 — log with [Health] client=false → False
with tempfile.TemporaryDirectory() as td:
    vl = Path(td) / "voice-agent.log"
    vl.write_text(
        "[2026-05-26T10:00:00Z] [Health] client=false streams=0\n"
    )
    with _patch(voice_log=vl):
        if not _mod.voice_client_connected():
            ok("T16: log with [Health] client=false → voice_client_connected False")
        else:
            fail("T16: client=false", "got True")

# T17 — most recent [Health] line wins (last line overrides earlier True)
with tempfile.TemporaryDirectory() as td:
    vl = Path(td) / "voice-agent.log"
    vl.write_text(
        "[2026-05-26T10:00:00Z] [Health] client=true streams=1\n"
        "[2026-05-26T10:00:10Z] [Health] client=false streams=0\n"
    )
    with _patch(voice_log=vl):
        if not _mod.voice_client_connected():
            ok("T17: most recent [Health] line wins — last=false → False")
        else:
            fail("T17: most recent [Health] wins", "got True — stale earlier line used")

# T18 — log with no [Health] line → False
with tempfile.TemporaryDirectory() as td:
    vl = Path(td) / "voice-agent.log"
    vl.write_text("Voice agent started\nSome log output, no health line.\n")
    with _patch(voice_log=vl):
        if not _mod.voice_client_connected():
            ok("T18: log with no [Health] line → voice_client_connected False")
        else:
            fail("T18: no [Health] line", "got True")

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
print(f"\n{PASS}/{PASS + FAIL} tests passed")
if FAIL:
    sys.exit(1)
