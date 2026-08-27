#!/usr/bin/env python3
"""Regression guard: a logging failure must not stop delivery — or drop inbound.

## The bug

`discord-bridge.py` line-buffers stdout, so EVERY `print()` flushes at the
newline. When the far end of stdout goes away (supervisor exits, pipe closed,
launcher reaped), that flush raises `BrokenPipeError` *inside whatever code was
logging*, and nothing catches it.

Two halves, and the second is the one that loses data.

**Delivery.** In `poll_results`:

  1. `channel.send(...)`            — the DM actually goes out
  2. `_mark_delivered(task_id)`
  3. `print(f"  Replied: ...", flush=True)`   <-- raises
  4. `except Exception as e: print(...)`      <-- raises again, inside the handler
  5. `archive_file(result_file, ...)`  — sits AFTER the try, never reached

`poll_results` has no loop-level try/except, so the exception escapes
`while True` and the coroutine ends permanently. The process stays alive, so
every cheap probe reports a healthy bridge while it can no longer reply.

**Receive.** In `on_message` the DM checkpoint advances inside a `try` (it
survives), and the very next statement is an unguarded `print`. The allowlist
check, the tier stamp and the `tasks/task-*.txt` write all sit below it — so the
handler dies before the message becomes a task. Because the checkpoint already
advanced, the REST catch-up skips it too: the message is destroyed, not delayed.

## The fix, in two independent halves

- `_NeverFatalStream` wraps stdout/stderr and swallows `OSError` on
  write/flush, so logging can no longer raise at all — anywhere in the process.
- `_supervise_loop` restarts any `poll_*` coroutine that escapes its body, so a
  crash from ANY cause degrades to a short gap instead of permanent silence.

Both are asserted here against the REAL module (imported with `discord` stubbed
and `CLAUDE_CONFIG_DIR` isolated), not against a copy — an earlier draft exec'd
extracted source blocks, which passes but attributes no coverage to the file
under test and would let the shipped lines rot untested.
"""
from __future__ import annotations

import asyncio
import atexit
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# The bridge constructs a client at import; stub `discord` when it is absent.
try:  # pragma: no cover - depends on the runner's site-packages
    import discord  # noqa: F401
except ImportError:
    _d = types.ModuleType("discord")
    _d.Intents = type("I", (), {"default": staticmethod(lambda: type("X", (), {"message_content": False})())})
    _d.Client = type("C", (), {"__init__": lambda self, **k: None, "event": staticmethod(lambda fn: fn)})
    _d.File = type("F", (), {})
    _d.Message = type("M", (), {})
    _d.DMChannel = type("DM", (), {})
    _d.AllowedMentions = type("AM", (), {"__init__": lambda self, **k: None})
    _d.MessageType = type("MT", (), {"default": 0, "reply": 19})
    sys.modules["discord"] = _d

# The bridge resolves channel access at import and falls back to the real
# ~/.claude, so CLAUDE_CONFIG_DIR and HOME must be isolated BEFORE the import.
_CFG = tempfile.mkdtemp(prefix="ccd-logging-not-fatal-")
atexit.register(lambda: shutil.rmtree(_CFG, ignore_errors=True))
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
os.environ["HOME"] = _CFG
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
_chan = Path(_CFG) / "channels" / "discord"
_chan.mkdir(parents=True, exist_ok=True)
(_chan / "access.json").write_text('{"allowFrom": []}')


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "_logging_not_fatal_bridge", REPO / "src" / "discord-bridge.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    # ACCESS_BACKUP_FILE resolves under the LIVE workspace at import, so the temp
    # tree above is not isolation and the real durable file gets overwritten.
    mod.ACCESS_BACKUP_FILE = (
        Path(tempfile.mkdtemp(prefix="acl-bk-")) / "discord-access-backup.json")

    return mod


BRIDGE = _load_bridge()


class BrokenStream:
    """Stands in for stdout after the pipe's read end is gone."""

    def __init__(self):
        self.attempts = 0
        self.encoding = "utf-8"

    def write(self, data):
        self.attempts += 1
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


class TypeErrorStream(BrokenStream):
    """A stream whose failure is NOT the EPIPE class — must still propagate."""

    def write(self, data):
        raise TypeError("not a pipe problem")


class NeverFatalStreamTest(unittest.TestCase):
    def test_write_swallows_epipe_and_reports_accepted(self):
        raw = BrokenStream()
        s = BRIDGE._NeverFatalStream(raw)
        self.assertEqual(s.write("hello"), len("hello"))
        self.assertEqual(raw.attempts, 1)

    def test_flush_swallows_epipe(self):
        s = BRIDGE._NeverFatalStream(BrokenStream())
        s.flush()  # must not raise

    def test_print_through_wrapper_does_not_raise(self):
        """The original failure: print() flushing into a dead pipe."""
        raw = BrokenStream()
        saved = sys.stdout
        sys.stdout = BRIDGE._NeverFatalStream(raw)
        try:
            print("  Replied: task-123", flush=True)
        finally:
            sys.stdout = saved
        self.assertGreaterEqual(raw.attempts, 1)

    def test_non_oserror_still_propagates(self):
        s = BRIDGE._NeverFatalStream(TypeErrorStream())
        with self.assertRaises(TypeError):
            s.write("x")

    def test_getattr_delegates_to_wrapped_stream(self):
        s = BRIDGE._NeverFatalStream(BrokenStream())
        self.assertEqual(s.encoding, "utf-8")


class SuperviseLoopTest(unittest.TestCase):
    def test_crashing_loop_is_restarted(self):
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise BrokenPipeError(32, "Broken pipe")
            raise asyncio.CancelledError

        async def run():
            saved = BRIDGE.POLL_LOOP_RESTART_SEC
            BRIDGE.POLL_LOOP_RESTART_SEC = 0
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await BRIDGE._supervise_loop(flaky, "flaky")
            finally:
                BRIDGE.POLL_LOOP_RESTART_SEC = saved

        asyncio.run(run())
        self.assertEqual(len(calls), 3)

    def test_loop_returning_is_also_restarted(self):
        calls = []

        async def returns_early():
            calls.append(1)
            if len(calls) >= 2:
                raise asyncio.CancelledError
            return None

        async def run():
            saved = BRIDGE.POLL_LOOP_RESTART_SEC
            BRIDGE.POLL_LOOP_RESTART_SEC = 0
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await BRIDGE._supervise_loop(returns_early, "returns_early")
            finally:
                BRIDGE.POLL_LOOP_RESTART_SEC = saved

        asyncio.run(run())
        self.assertEqual(len(calls), 2)


class OnReadyStartsSupervisedLoopsTest(unittest.TestCase):
    """The poll loops must be started through the supervisor, and only once.

    `on_ready` fires again on every reconnect. Before the singleton guard, each
    reconnect leaked another copy of every poll loop; N reconnects meant N+1
    `poll_progress` loops all posting their own placeholder for the same task.
    """

    def _run_on_ready(self, created):
        # Neutralise everything in on_ready that touches the filesystem or the
        # network. _recover_orphan_sending_files() renames real results/*.sending
        # back to .txt — a test must never run it against a live workspace.
        saved = {
            "_recover_orphan_sending_files": BRIDGE._recover_orphan_sending_files,
            "_catchup_missed_dms": BRIDGE._catchup_missed_dms,
        }
        BRIDGE._recover_orphan_sending_files = lambda: 0

        async def _noop_catchup():
            return None

        BRIDGE._catchup_missed_dms = _noop_catchup

        class _Loop:
            @staticmethod
            def create_task(coro):
                # `_supervise_loop(fn, name)` has not started yet, so its frame
                # still holds the argument — that is the loop's identity.
                frame = getattr(coro, "cr_frame", None)
                created.append(frame.f_locals.get("name") if frame else None)
                coro.close()
                return None

        class _Client:
            user = "test-bot"
            loop = _Loop()

        saved_client = BRIDGE.client
        BRIDGE.client = _Client()
        try:
            asyncio.run(BRIDGE.on_ready())
        finally:
            BRIDGE.client = saved_client
            for k, v in saved.items():
                setattr(BRIDGE, k, v)

    def test_loops_start_once_and_are_supervised(self):
        BRIDGE._poll_loops_started = False
        created: list = []
        self._run_on_ready(created)
        for expected in ("poll_results", "poll_progress", "poll_approved",
                         "poll_proactive", "poll_dm_fallback"):
            self.assertIn(expected, created)

        # Second on_ready (a reconnect) must NOT start another set.
        again: list = []
        self._run_on_ready(again)
        self.assertEqual([c for c in again if c], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
