#!/usr/bin/env python3
"""BEHAVIOURAL: drive `poll_results` over a genuinely empty result file.

The sibling suite (`result-router-empty-result-bound.test.py`) proves the POLICY
and asserts, by reading the source, that both bridges delegate to it. That is a
grep — it cannot tell whether the counter actually increments, whether the notice
actually prints, or whether it fires once. This drives the real loop.

ASYMMETRY, STATED RATHER THAN IMPLIED. Only Discord is covered behaviourally here.
Its wiring lives in `async def poll_results()`, which a patched `asyncio.sleep`
turns into a clean single-iteration driver. Telegram's sits inside `main()`
(lines 555-1045 — bot construction, long-poll, the lot), so driving it would mean
a harness larger than the change. Telegram stays source-asserted in the sibling
suite, and this docstring says so instead of letting "both bridges tested" be
inferred.

HERMETIC: `CLAUDE_CONFIG_DIR` and `RESULTS_DIR` are tmpdirs, `discord` is stubbed,
`client.is_ready()` is False so no heartbeat is written. The last case asserts the
operator's real results/ was untouched rather than trusting the redirect.
"""
from __future__ import annotations

import asyncio
import builtins
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


_CFG = tempfile.mkdtemp(prefix="empty-wire-ccd-")
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
_d = Path(_CFG) / "channels" / "discord"
_d.mkdir(parents=True, exist_ok=True)
(_d / "access.json").write_text(json.dumps({"allowFrom": ["4242"]}))

try:  # pragma: no cover - present in dev, absent in clean CI
    import discord  # noqa: F401
except Exception:
    _st = types.ModuleType("discord")
    _st.Intents = type("Intents", (), {"default": staticmethod(
        lambda: type("I", (), {"message_content": False})())})
    _st.Client = type("Client", (), {"__init__": lambda self, **kw: None,
                                     "event": staticmethod(lambda fn: fn)})
    _st.File = type("File", (), {"__init__": lambda self, *a, **kw: None})
    _st.Message = type("Message", (), {})
    _st.DMChannel = type("DMChannel", (), {})
    sys.modules["discord"] = _st

_spec = importlib.util.spec_from_file_location("db_wire", REPO / "src" / "discord-bridge.py")
db = importlib.util.module_from_spec(_spec)
sys.modules["db_wire"] = db
_spec.loader.exec_module(db)

_LIVE = Path(db.RESULTS_DIR)
_live_before = sorted(p.name for p in _LIVE.iterdir()) if _LIVE.exists() else None


class _Tick(Exception):
    """Ends one poll_results iteration at its trailing sleep."""


def _drive(box: Path, task_id: str, iterations: int) -> list[str]:
    """Run `iterations` real poll passes; return everything the loop printed."""
    db.RESULTS_DIR = box
    # ARCHIVE_RESULTS_DIR / ARCHIVE_TASKS_DIR are SEPARATE module constants,
    # computed at import from the real RESULTS_DIR — rebinding RESULTS_DIR alone
    # does nothing for them, and `archive_path()` then mkdir -p's into the
    # OPERATOR's live results/. The hermeticity assertion at the end of this file
    # caught exactly that (`[] -> ['archive']`) before it shipped; without it this
    # test would have been the pollution class that has blocked three of my PRs.
    db.ARCHIVE_RESULTS_DIR = box / "archive"
    db.ARCHIVE_TASKS_DIR = box / "archive-tasks"
    db.TASKS_DIR = box / "tasks"
    db.TASKS_DIR.mkdir(parents=True, exist_ok=True)
    db.client = type("C", (), {"is_ready": lambda self: False})()
    out: list[str] = []

    async def _sleep(_s):
        raise _Tick()

    orig_sleep, orig_print = db.asyncio.sleep, builtins.print
    db.asyncio.sleep = _sleep
    builtins.print = lambda *a, **k: out.append(" ".join(str(x) for x in a))
    db.pending_replies[task_id] = object()   # seed ONCE — re-seeding each pass
    # made the TERMINAL assertion true by construction and let the counter
    # restart after the bound cleared it. The harness, not the code.
    try:
        for _ in range(iterations):
            try:
                asyncio.run(db.poll_results())
            except _Tick:
                pass
            except Exception:      # noqa: BLE001 — surfaced via `out`, not hidden
                pass
    finally:
        db.asyncio.sleep, builtins.print = orig_sleep, orig_print
    return out


def main() -> int:
    print("discord-bridge empty-result wiring (behavioural):")
    T = db.result_router.EMPTY_RESULT_POLL_THRESHOLD

    # --- a persistently empty result announces exactly once ----------------
    box = Path(tempfile.mkdtemp(prefix="empty-wire-"))
    (box / "task-STUCK.txt").write_text("")
    db._empty_result_polls.clear()
    out = _drive(box, "task-STUCK", T + 5)
    # Count FIRES, not MENTIONS. `"PRESENT BUT EMPTY" in line` reported 2 — the
    # notice itself, plus the §9.3 delivery-failure line which quotes the notice
    # as its error text. The code fired once (probed: iteration 20 only); the
    # predicate was counting the echo. Anchor on the notice's own prefix.
    fired = [line for line in out if line.lstrip().startswith("[result] ")]

    # The counter is CLEARED at the bound (terminal disposition), so the
    # post-condition is absence, not T+5. Asserting T+5 was asserting the
    # pre-fix behaviour.
    check("the counter reached the bound and was then cleared",
          db._empty_result_polls.get("task-STUCK") is None,
          f"counter={db._empty_result_polls.get('task-STUCK')} — should be cleared "
          f"once the task is terminated")
    check("the notice fires EXACTLY once", len(fired) == 1,
          f"fired {len(fired)}x — 0 means the wiring is dead, >1 means a 3s nag")
    check("  ...naming the task", bool(fired) and "task-STUCK" in fired[0],
          fired[0] if fired else "<nothing>")
    check("  ...and the empty body was NOT delivered",
          not any("Sent" in line for line in out), "an empty reply went out")

    # --- §9.3: OWNER-VISIBLE and TERMINAL, not merely logged ---------------
    # @john-the-dev's blocker on 175173c3: printing to bridge stdout changes
    # nothing the owner experiences. These four assert the things a log line
    # cannot give you — that the task STOPS being pending, the result is
    # archived, and the owner-notification path is actually entered.
    check("TERMINAL: the task is dropped from pending_replies",
          "task-STUCK" not in db.pending_replies,
          "still pending — it will be re-read every 3s until the 7-day age-out, "
          "which is the defect, now with a log line")
    check("  ...the result file is archived, not left to re-poll",
          not (box / "task-STUCK.txt").exists(),
          "left in results/ — the next poll finds it empty all over again")
    check("  ...the owner-notification path was ENTERED",
          any("delivery-failure" in line for line in out),
          "no owner DM attempted; §9.3 requires one for any delivery failure")
    check("  ...and the counter is cleared so a resend starts fresh",
          db._empty_result_polls.get("task-STUCK") is None,
          f"left at {db._empty_result_polls.get('task-STUCK')}")

    # --- the near-miss: filled BEFORE the bound stays silent ---------------
    # This is the partial-write case. If it ever announces, the bound is too
    # tight and the fix becomes the noise it was meant to avoid.
    box2 = Path(tempfile.mkdtemp(prefix="empty-wire-ok-"))
    (box2 / "task-FILLS.txt").write_text("")
    db._empty_result_polls.clear()
    out2 = _drive(box2, "task-FILLS", 3)
    (box2 / "task-FILLS.txt").write_text("the real body")
    out2 += _drive(box2, "task-FILLS", 1)
    check("a file filled BEFORE the bound never announces",
          not any("PRESENT BUT EMPTY" in line for line in out2),
          "would fire on every normal partial write")
    check("  ...and the counter is CLEARED once it delivers",
          db._empty_result_polls.get("task-FILLS") is None,
          f"counter left at {db._empty_result_polls.get('task-FILLS')} — a stale "
          f"count would announce a healthy task after a few transient empties")

    live_after = sorted(p.name for p in _LIVE.iterdir()) if _LIVE.exists() else None
    check("HERMETIC: operator's real results/ untouched", live_after == _live_before,
          f"{_live_before} -> {live_after}")

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All empty-result wiring checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
