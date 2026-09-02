#!/usr/bin/env python3
"""BEHAVIOURAL: drives the real `poll_results`. Discord is the ONLY bridge carrying this
bound — Telegram and Slack do not reference it, so nothing here or in the sibling covers them."""
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


OWNER_ID = "424242424242"      # hermetic owner for the recipient proof
SENT: list = []               # (recipient_id, text) actually written to a DM


class _Tick(Exception):
    """Ends one poll_results iteration at its trailing sleep."""


def _drive(box: Path, task_id: str, iterations: int) -> list[str]:
    """Run `iterations` real poll passes; return everything the loop printed."""
    db.RESULTS_DIR = box
    # ARCHIVE_* are separate module constants computed at import, so rebinding
    # RESULTS_DIR alone lets archive_path() mkdir into the operator's live results/.
    db.ARCHIVE_RESULTS_DIR = box / "archive"
    db.ARCHIVE_TASKS_DIR = box / "archive-tasks"
    db.TASKS_DIR = box / "tasks"
    db.TASKS_DIR.mkdir(parents=True, exist_ok=True)

    # The stub needs a real fetch_user -> create_dm -> send chain that records the
    # recipient: a log-line assertion passes even when no DM is attempted.
    db.ACCESS_FILE = box / "access.json"
    db.ACCESS_FILE.write_text(json.dumps({"allowFrom": [OWNER_ID]}))
    SENT.clear()

    class _DM:
        def __init__(self, uid):
            self.id = f"dm-{uid}"
            self.recipient_id = str(uid)

        async def send(self, text):
            SENT.append((self.recipient_id, str(text)))

    class _User:
        def __init__(self, uid):
            self.id = str(uid)
            self.bot = False

        async def create_dm(self):
            return _DM(self.id)

    class _Client:
        def is_ready(self):
            return False

        async def fetch_user(self, uid):
            return _User(uid)

    db.client = _Client()
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
    # Preseed the source task: without it "absent afterwards" is vacuously true and
    # passes against code that never archives.
    db_tasks = box / "tasks"
    db_tasks.mkdir(parents=True, exist_ok=True)
    (db_tasks / "task-STUCK.txt").write_text("id: task-STUCK\ntask: something\n")
    # Seed the other task-scoped maps so "cleared" is a state CHANGE, not an
    # absence that was already there.
    db.pending_reply_anchors["task-STUCK"] = 123456789
    db._progress_msgs["task-STUCK"] = {"message_id": 1, "chan": 2}
    out = _drive(box, "task-STUCK", T + 5)
    # Count fires, not mentions: the delivery-failure line quotes the notice, so a
    # substring match double-counts. Anchor on the notice's own prefix.
    fired = [line for line in out if line.lstrip().startswith("[result] ")]

    # The counter is cleared at the bound, so the post-condition is absence, not T+5.
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

    # Assert what a log line cannot: the task stops being pending, the result is
    # archived, and the owner-notification path is entered.
    check("TERMINAL: the task is dropped from pending_replies",
          "task-STUCK" not in db.pending_replies,
          "still pending — it will be re-read every 1s until the 7-day age-out, "
          "which is the defect, now with a log line")
    check("  ...the result file is archived, not left to re-poll",
          not (box / "task-STUCK.txt").exists(),
          "left in results/ — the next poll finds it empty all over again")
    # Recipient proof, not a log-line count: the code prints "delivery-failure" from
    # inside its own except branch when the DM never happens. Assert the write.
    check("OWNER-VISIBLE: exactly one DM was actually sent",
          len(SENT) == 1,
          f"{len(SENT)} DMs sent — 0 means nobody was notified and the previous "
          f"string assertion was passing on the failure path itself")
    check("  ...addressed to the resolved OWNER, not just 'someone'",
          bool(SENT) and SENT[0][0] == OWNER_ID,
          f"went to {SENT[0][0] if SENT else '<nobody>'}, expected {OWNER_ID}")
    check("  ...and the body names the task, so the owner can act on it",
          bool(SENT) and "task-STUCK" in SENT[0][1],
          SENT[0][1][:120] if SENT else "<nothing sent>")

    # LIFECYCLE: every task-scoped map, and the SOURCE TASK, not just the result.
    check("TERMINAL: the source task is archived out of tasks/",
          not (box / "tasks" / "task-STUCK.txt").exists(),
          "left in tasks/ — queue health and task discovery still see it as "
          "pending work while this branch claims the task is finished")
    check("  ...the reply anchor is cleared",
          "task-STUCK" not in db.pending_reply_anchors,
          f"stale anchor {db.pending_reply_anchors.get('task-STUCK')} leaks")
    check("  ...the progress placeholder state is cleared",
          "task-STUCK" not in db._progress_msgs,
          "placeholder tracking leaks; the ⏳ line never clears")
    check("  ...and the counter is cleared so a resend starts fresh",
          db._empty_result_polls.get("task-STUCK") is None,
          f"left at {db._empty_result_polls.get('task-STUCK')}")

    # CONSECUTIVE means consecutive: without a reset on the absent branch, a writer
    # that removes and recreates its result is terminalized when it reappears.
    box_gap = Path(tempfile.mkdtemp(prefix="empty-wire-gap-"))
    (box_gap / "task-GAP.txt").write_text("")
    db._empty_result_polls.clear()
    (box_gap / "tasks").mkdir(parents=True, exist_ok=True)
    (box_gap / "tasks" / "task-GAP.txt").write_text("id: task-GAP\ntask: something\n")
    _drive(box_gap, "task-GAP", T - 1)
    before_missing = db._empty_result_polls.get("task-GAP")
    check("precondition: the counter is one below the bound",
          before_missing == T - 1, f"counter={before_missing}, expected {T - 1}")

    (box_gap / "task-GAP.txt").unlink()              # the writer retries
    out_missing = _drive(box_gap, "task-GAP", 1)
    check("a poll with the file ABSENT clears the counter",
          db._empty_result_polls.get("task-GAP") is None,
          f"counter={db._empty_result_polls.get('task-GAP')} — survived the gap, so the "
          f"next present-and-empty poll reaches the bound and terminalizes a live task")
    check("  ...and the absent poll announces nothing",
          not [l for l in out_missing if l.lstrip().startswith("[result] ")], str(out_missing))

    (box_gap / "task-GAP.txt").write_text("")        # reappears, still empty
    out_again = _drive(box_gap, "task-GAP", 1)
    check("  ...so the reappearance does NOT fire the terminal notice",
          not [l for l in out_again if l.lstrip().startswith("[result] ")],
          "fired on the first poll after the file came back — the count carried over")
    check("  ...and the task is still pending, not archived",
          "task-GAP" in db.pending_replies,
          "a retried writer was falsely terminalized")

    # --- the near-miss: filled BEFORE the bound stays silent ---------------
    # The partial-write case. If it ever announces, the bound is too tight.
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
