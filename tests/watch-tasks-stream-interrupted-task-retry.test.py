#!/usr/bin/env python3
"""Characterize what a core restart does to an in-flight required-Team task.

The retry machinery for such a task already exists end to end and nothing here
adds it: the watcher never removes a task from `tasks/` (every `TASKS_DIR` use is
resolve/mkdir/glob/watch), so the initial sweep re-dispatches whatever is still
there on the next start, re-probing rc=4 back to `must-handle`.

The shutdown path used to publish a refusal for the interrupted task. A result
makes the task deliverable, the delivering bridge archives the task after the
result (`discord-bridge.py`, whose comment says the task would otherwise "sit in
tasks/ forever"), and an archived task is out of the sweep's reach -- so that
refusal CONSUMED the retry, and for an optional handler the matching fallback
announce ran the task twice (#4816). Now the shutdown publishes nothing and
announces nothing: the claim stays behind, still naming the dead watcher's pid,
and the next watcher retires it in prepare_handler_state() and re-dispatches the
task from its sweep. These scenarios pin that flip: `REFUSAL_MARK` is the shared
prefix of every terminal-failure body, and it must NOT appear.

The async dispatch pipeline (`--handler-runner`/the old `fallback_outstanding_
handlers`) that used to publish this on shutdown was retired -- run_handler_now()
runs the handler synchronously, and a SIGTERM landing mid-call interrupts bash's
`wait` on it directly rather than deferring, so run_handler_now() itself never
resumes to settle the claim. `settle_own_claims_on_shutdown()` (called from
cleanup()) restores the equivalent: disposition-aware settlement of any claim
this watcher still owns, operating on CLAIMS_DIR directly rather than depending
on where execution was interrupted -- same shape, same wording, as before.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []
REFUSAL_MARK = "could not safely process"

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
    # DISPATCH_DIR/workers/ is retired (see module docstring); a claim is the
    # compatible replacement signal -- run_handler_now() takes it synchronously.
    d = claims_dir(h)
    return d.is_dir() and any(d.glob("task-*.txt"))


def scenario_interrupted_task_is_left_in_tasks_unrefused() -> None:
    """A restart with a required-Team handler outstanding publishes nothing —
    and leaves the task file exactly where the next sweep will find it."""
    print("\nscenario: shutdown with an outstanding required handler")
    h = Harness()
    try:
        h.start()
        h.deliver("task-interrupted.txt")
        # The stub handler probes rc=4 (must-handle) then sleeps forever, so the
        # task is genuinely in flight when the watcher goes down.
        check("a handler worker is running before the shutdown",
              wait_for(lambda: worker_running(h), 30.0))

        h.stop(graceful=True)  # SIGTERM -> settle_own_claims_on_shutdown()

        result = h.ws / "results" / "task-interrupted.txt"
        time.sleep(2.0)  # long enough for the old path's refusal to have landed
        check("the shutdown publishes NO terminal refusal", not result.is_file(),
              result.read_text()[:120] if result.is_file() else "")
        check("nothing at all was published for it",
              not list((h.ws / "results").glob("*task-interrupted*")))

        # The half that matters for retry: nothing moved the task.
        task = h.ws / "tasks" / "task-interrupted.txt"
        check("the task file is STILL in tasks/ after the shutdown", task.is_file())
        check("the watcher archived nothing itself",
              not list((h.ws / "tasks").glob("archive/**/*.txt")))
        claim = claims_dir(h) / "task-interrupted.txt"
        check("the claim is left behind, naming the dead watcher's pid",
              claim.is_file() and claim.read_text().splitlines()[0] == str(h.proc.pid),
              claim.read_text()[:80] if claim.is_file() else "no claim")
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
        # The shutdown leaves the claim; only its owner pid is dead, which is
        # what the next watcher's prepare_handler_state() retires it on.
        claim = claims_dir(h) / "task-survives.txt"
        dead_pid = claim.read_text().splitlines()[0] if claim.is_file() else ""
        check("the interrupted task's claim still names the dead watcher",
              dead_pid == str(h.proc.pid), dead_pid)

        second = Harness.attach(h.ws, h.tmp)
        try:
            second.start()
            # No delivery: the initial sweep alone must find it in tasks/.
            check("the restarted watcher re-dispatches it from the sweep alone",
                  wait_for(lambda: worker_running(second), 30.0))
            check("and it is claimed again as required-Team work, by the NEW watcher",
                  wait_for(lambda: claim.is_file()
                           and claim.read_text().splitlines()[0] == str(second.proc.pid), 20.0),
                  claim.read_text()[:80] if claim.is_file() else "no claim")
        finally:
            second.stop()
    finally:
        h.stop()


def main() -> int:
    scenario_interrupted_task_is_left_in_tasks_unrefused()
    scenario_a_restarted_watcher_redispatches_what_is_left_in_tasks()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nPASS — an interrupted required-Team task is neither refused nor announced; "
          "it stays in tasks/ and is re-dispatched by the next watcher")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
