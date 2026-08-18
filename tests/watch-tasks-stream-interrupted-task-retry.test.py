#!/usr/bin/env python3
"""Characterize what a core restart does to an in-flight required-Team task.

The retry machinery for such a task already exists end to end and nothing here
adds it: the watcher never removes a task from `tasks/` (every `TASKS_DIR` use is
resolve/mkdir/glob/watch), so the initial sweep re-dispatches whatever is still
there on the next start, re-probing rc=4 back to `must-handle`.

What consumes that retry is the refusal the shutdown path publishes. A result
makes the task deliverable, the delivering bridge archives the task after the
result (`discord-bridge.py`, whose comment says the task would otherwise "sit in
tasks/ forever"), and an archived task is out of the sweep's reach.

These scenarios pin both halves as they behave TODAY, so that a change to the
shutdown path shows up here as a deliberate behaviour flip rather than as a
silent one. They assert the mechanism, not the wording: `REFUSAL_MARK` is the
shared prefix of every terminal-failure body, and `INTERRUPTED` is the reason
word this path passes.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []
REFUSAL_MARK = "could not safely process"
INTERRUPTED = "was interrupted"

# Import the sibling suite's harness (import-safe) rather than restating it:
# a copied harness drifts from the script it drives.
_spec = importlib.util.spec_from_file_location(
    "_reap_harness", REPO / "tests" / "watch-tasks-stream-dead-worker-reap.test.py")
_reap = importlib.util.module_from_spec(_spec)
sys.modules["_reap_harness"] = _reap
_spec.loader.exec_module(_reap)
Harness, wait_for = _reap.Harness, _reap.wait_for


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def claims_dir(h) -> Path:
    return h.ws / "state" / "task-event-handler-claims"


def worker_running(h) -> bool:
    d = h.dispatch()
    return bool(d and (d / "workers").is_dir() and any((d / "workers").iterdir()))


def scenario_interrupted_task_is_refused_but_left_in_tasks() -> None:
    """A restart with a required-Team handler outstanding publishes a refusal —
    and leaves the task file exactly where the next sweep would find it."""
    print("\nscenario: shutdown with an outstanding required handler")
    h = Harness()
    try:
        h.start()
        h.deliver("task-interrupted.txt")
        # The stub handler probes rc=4 (must-handle) then sleeps forever, so the
        # task is genuinely in flight when the watcher goes down.
        check("a handler worker is running before the shutdown",
              wait_for(lambda: worker_running(h), 30.0))

        h.stop(graceful=True)  # SIGTERM -> the trap runs fallback_outstanding_handlers()

        result = h.ws / "results" / "task-interrupted.txt"
        check("the shutdown publishes a terminal refusal", wait_for(result.is_file, 20.0))
        body = result.read_text() if result.is_file() else ""
        check("and its reason word is the interrupted one, not the failed one",
              REFUSAL_MARK in body and INTERRUPTED in body, repr(body[:120]))

        # The half that matters for retry: nothing moved the task.
        task = h.ws / "tasks" / "task-interrupted.txt"
        check("the task file is STILL in tasks/ after the shutdown", task.is_file())
        check("the watcher archived nothing itself",
              not list((h.ws / "tasks").glob("archive/**/*.txt")))
    finally:
        h.stop()


def scenario_a_restarted_watcher_redispatches_what_is_left_in_tasks() -> None:
    """The retry path, demonstrated across two processes — one watcher cannot
    show this about itself. The task left behind above is picked up again."""
    print("\nscenario: a second watcher over the same workspace re-dispatches it")
    h = Harness()
    try:
        h.start()
        h.deliver("task-survives.txt")
        check("first watcher takes the task", wait_for(lambda: worker_running(h), 30.0))
        h.stop(graceful=True)

        task = h.ws / "tasks" / "task-survives.txt"
        check("task still queued on disk between the two watchers", task.is_file())
        # Its claim is released by the shutdown, so a fresh watcher may take it.
        check("the interrupted task's claim is not held by the dead watcher",
              wait_for(lambda: not (claims_dir(h) / "task-survives.txt").exists(), 20.0))

        second = Harness.attach(h.ws, h.tmp)
        try:
            second.start()
            # No delivery: the initial sweep alone must find it in tasks/.
            check("the restarted watcher re-dispatches it from the sweep alone",
                  wait_for(lambda: worker_running(second), 30.0))
            check("and it is claimed again as required-Team work",
                  wait_for(lambda: (claims_dir(h) / "task-survives.txt").exists(), 20.0))
        finally:
            second.stop()
    finally:
        h.stop()


def main() -> int:
    scenario_interrupted_task_is_refused_but_left_in_tasks()
    scenario_a_restarted_watcher_redispatches_what_is_left_in_tasks()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nPASS — an interrupted required-Team task is refused, yet stays in tasks/ "
          "and is re-dispatched by the next watcher")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
