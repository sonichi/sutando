#!/usr/bin/env python3
"""Regression guard: a logging failure must not stop delivery.

## The bug

`discord-bridge.py` line-buffers stdout, so EVERY `print()` flushes at the
newline. When the far end of stdout goes away (supervisor exits, pipe closed,
launcher reaped), that flush raises `BrokenPipeError` *inside whatever code was
logging*.

In `poll_results` the sequence is:

  1. `channel.send(...)`            — the DM actually goes out
  2. `_mark_delivered(task_id)`
  3. `print(f"  Replied: ...", flush=True)`   <-- raises
  4. `except Exception as e: print(f"  Reply failed: {e}", flush=True)`  <-- raises again,
     inside the handler, so it propagates
  5. `archive_file(result_file, ...)`  — sits AFTER the try, never reached

`poll_results` has no loop-level try/except, so the exception escapes
`while True` and the coroutine ends permanently. The process stays alive and
keeps receiving, so every cheap probe reports a healthy bridge while it can no
longer reply. The message had already been sent, but the result file was left on
disk and nothing was logged — which reads as "never delivered" and invites a
resend (that is exactly how a duplicate DM got sent that night).

## The fix, in two independent halves

- `_NeverFatalStream` wraps stdout/stderr and swallows `OSError` on
  write/flush, so logging can no longer raise at all.
- `_supervise_loop` restarts any `poll_*` coroutine that escapes its body, so a
  crash from ANY cause degrades to a 5s gap instead of permanent silence.

Both are asserted here. The first two tests exercise the ORIGINAL failure mode
(a stream whose writes raise EPIPE), not a happy path.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_bridge_symbols():
    """Import the two symbols under test without importing the whole bridge.

    `discord-bridge.py` needs `discord`, a token, and workspace state at import
    time, so the module is parsed and only the relevant definitions are
    executed. That keeps this test hermetic (bar: bridge tests must not read
    host config).
    """
    src = (REPO / "src" / "discord-bridge.py").read_text()
    tree = compile(
        _extract(src, ["class _NeverFatalStream:", "async def _supervise_loop("]),
        "<bridge-subset>",
        "exec",
    )
    ns: dict = {"asyncio": asyncio, "POLL_LOOP_RESTART_SEC": 0}
    exec(tree, ns)
    return ns


def _extract(src: str, headers: list[str]) -> str:
    """Return the source of each top-level block whose first line matches."""
    lines = src.splitlines()
    out: list[str] = []
    for header in headers:
        start = next(i for i, line in enumerate(lines) if line.startswith(header))
        end = start + 1
        while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()):
            end += 1
        out.extend(lines[start:end])
        out.append("")
    return "\n".join(out)


class BrokenStream:
    """Stands in for stdout after the pipe's read end is gone."""

    def __init__(self):
        self.attempts = 0

    def write(self, data):
        self.attempts += 1
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")

    encoding = "utf-8"


class LoggingNotFatalTest(unittest.TestCase):
    def setUp(self):
        self.ns = _load_bridge_symbols()

    def test_print_through_wrapper_survives_a_dead_pipe(self):
        """The exact original trigger: print() to a stream that raises EPIPE."""
        broken = BrokenStream()
        wrapped = self.ns["_NeverFatalStream"](broken)

        # Control first — the instrument must be able to produce the failure,
        # otherwise "no exception" below proves nothing (REVIEW.md criterion 9).
        with self.assertRaises(BrokenPipeError):
            print("  Replied: ...", file=broken, flush=True)

        # Same call through the wrapper must not raise.
        print("  Replied: ...", file=wrapped, flush=True)
        self.assertGreater(broken.attempts, 1, "wrapper should still attempt the write")

    def test_wrapper_does_not_mask_non_oserror(self):
        """Only the EPIPE/EBADF class is swallowed; real bugs still surface."""

        class Weird:
            def write(self, data):
                raise ValueError("not a pipe problem")

            def flush(self):
                pass

        wrapped = self.ns["_NeverFatalStream"](Weird())
        with self.assertRaises(ValueError):
            wrapped.write("x")

    def test_wrapper_delegates_unknown_attributes(self):
        wrapped = self.ns["_NeverFatalStream"](BrokenStream())
        self.assertEqual(wrapped.encoding, "utf-8")

    def test_supervisor_restarts_a_crashing_loop(self):
        """A loop that escapes its body is restarted, not silently dropped."""
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise BrokenPipeError(32, "Broken pipe")
            await asyncio.sleep(3600)  # third entry: behave like a live loop

        async def drive():
            task = asyncio.ensure_future(self.ns["_supervise_loop"](flaky, "flaky"))
            for _ in range(200):
                await asyncio.sleep(0)
                if calls["n"] >= 3:
                    break
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(drive())
        self.assertGreaterEqual(calls["n"], 3, "supervisor should re-enter after each crash")

    def test_supervisor_propagates_cancellation(self):
        """Shutdown must stay prompt — CancelledError is not a 'crash'."""

        async def sleeper():
            await asyncio.sleep(3600)

        async def drive():
            task = asyncio.ensure_future(self.ns["_supervise_loop"](sleeper, "sleeper"))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(drive())


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
