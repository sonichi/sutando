#!/usr/bin/env python3
"""The done flag has ONE writer, and it runs before the result it attributes.

`pool_delivery.mark_done` is that writer; its readers are `pool_delivery.residue`
and `src/result_claimant.py`, which the gateway consults to stamp an outbound
reply with the worker that produced it. The contract this suite pins:

    ordering    flag, THEN result. A result the drain can see always has its
                attribution beside it; the reverse (flag, no result) is the
                recoverable state, and `residue` maps it to "finished".
    atomicity   temp file + rename in the same directory, so a concurrent
                reader sees the name absent or complete, never half-written.
    isolation   two finishers racing on different tasks do not clobber each
                other, and a repeat of the same finish is idempotent.
    bounds      a recipient or task id outside the pool's own grammar is
                rejected, so no caller can write outside its own claim tree.

Ordering is asserted through the SHIPPED watcher (`src/watch-tasks-stream.sh`,
whose `publish_terminal_failure` is the worker-side path that publishes a result
into `results/`) and the SHIPPED drain resolver — not a re-enactment of either.

Run: python3 tests/pool-done-flag-writer.test.py
"""
from __future__ import annotations

import importlib.util
import multiprocessing
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []

sys.path.insert(0, str(REPO / "src"))
import pool_delivery  # noqa: E402
import result_claimant  # noqa: E402

# The watcher harness is imported, never restated: a copy drifts from the script.
_spec = importlib.util.spec_from_file_location(
    "_reap_harness", REPO / "tests" / "watch-tasks-stream-dead-worker-reap.test.py")
_reap = importlib.util.module_from_spec(_spec)
sys.modules["_reap_harness"] = _reap
_spec.loader.exec_module(_reap)
Harness, wait_for, names = _reap.Harness, _reap.wait_for, _reap.names

WORKER = "worker-1"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _finish(args) -> str:
    """One finisher, in its own process — the production writer and nothing else."""
    ws, worker, task_id = args
    return str(pool_delivery.mark_done(Path(ws), worker, task_id))


def scenario_the_drain_never_sees_a_result_before_its_flag() -> None:
    """A worker's watcher publishes a result; an observer polling `results/` as
    fast as it can must find the attribution already there, every time."""
    print("\nscenario: flag-before-result, through the shipped watcher and drain")
    h = Harness()
    h.extra_env["SUTANDO_INSTANCE_ID"] = WORKER
    tid = "task-ordering"
    result = h.ws / "results" / f"{tid}.txt"
    seen: dict = {}

    def observe() -> None:
        # The drain's own question, asked the instant the result becomes visible.
        while not seen.get("stop"):
            if result.exists():
                seen["claimant"] = result_claimant.resolve_claimant(h.ws / "state", tid)
                seen["size"] = result.stat().st_size
                return
            time.sleep(0.001)

    watcher = threading.Thread(target=observe, daemon=True)
    try:
        h.task(f"{tid}.txt")
        h.start()
        if not wait_for(lambda: names(h.dispatch() and h.dispatch() / "running")):
            check("the task reached a handler worker", False, str(h.dispatch()))
            return
        watcher.start()
        h.kill_workers()
        h.deliver("task-nudge.txt")  # arrival is what drives the reap
        published = wait_for(lambda: result.is_file() and result.stat().st_size > 0)
        check("the worker's watcher published a result", published)
        seen["stop"] = True
        watcher.join(10.0)
        check("the observer caught the result appearing", "claimant" in seen,
              f"never observed {result}")
        check("and its flag was ALREADY there when it did",
              seen.get("claimant") == WORKER,
              f"result was visible with claimant={seen.get('claimant')!r}")
        flag = pool_delivery.done_flag(h.ws, WORKER, tid)
        check("the flag the writer left is a regular file", pool_delivery.is_done_flag(flag))
        check("and the drain resolves it to this worker after the fact",
              result_claimant.resolve_claimant(h.ws / "state", tid) == WORKER)
        beside = [p.name for p in flag.parent.iterdir()] if flag.parent.is_dir() else []
        check("no temp file is left in the flag's directory",
              beside == [flag.name], str(beside))
    finally:
        seen["stop"] = True
        h.stop()


def scenario_racing_finishers_do_not_clobber_each_other() -> None:
    """Eight processes finishing eight tasks at once: every flag lands, each
    names its own worker, and nothing half-written is left behind."""
    print("\nscenario: concurrent finishers, production writer")
    ws = Path(tempfile.mkdtemp(prefix="mark-done-race-"))
    jobs = [(str(ws), f"worker-{i}", f"task-race{i}") for i in range(4)]
    jobs += [(str(ws), f"worker-{i}", "task-shared") for i in range(4)]
    with multiprocessing.Pool(len(jobs)) as pool:
        written = pool.map(_finish, jobs)
    check("every finisher wrote its own flag", len(set(written)) == len(jobs),
          f"{len(set(written))} distinct of {len(jobs)}")
    per_task = all(result_claimant.resolve_claimant(ws / "state", f"task-race{i}")
                   == f"worker-{i}" for i in range(4))
    check("each task resolves to the worker that finished it", per_task)
    # The shared id is the clobber probe: four writers, one name each, all kept.
    check("four workers claiming one task are all visible to the drain",
          result_claimant.claimants(ws / "state", "task-shared")
          == [f"worker-{i}" for i in range(4)])
    leftovers = [str(p) for p in (ws / "state" / "workers").rglob(".*.tmp")]
    check("no partially written flag is left behind", not leftovers, str(leftovers))


def scenario_a_repeated_finish_is_idempotent() -> None:
    """A retried finish must not double-count or leave a second name."""
    print("\nscenario: the same finish, twice")
    ws = Path(tempfile.mkdtemp(prefix="mark-done-twice-"))
    jobs = [(str(ws), WORKER, "task-twice")] * 6
    with multiprocessing.Pool(3) as pool:
        pool.map(_finish, jobs)
    check("the tree still names exactly one claimant",
          result_claimant.claimants(ws / "state", "task-twice") == [WORKER])
    check("and the drain resolves it",
          result_claimant.resolve_claimant(ws / "state", "task-twice") == WORKER)


def scenario_a_crash_after_the_flag_is_the_recoverable_half() -> None:
    """The one asymmetry the ordering buys: a flag without a result is a state
    the sweep knows how to finish, while a result without a flag is a reply
    already delivered with nobody's name on it — unrecoverable by then."""
    print("\nscenario: crash between the flag and the result")
    ws = Path(tempfile.mkdtemp(prefix="mark-done-crash-"))
    (ws / "results").mkdir(parents=True)
    tid = "task-crashed"
    pool_delivery.mark_done(ws, WORKER, tid)
    check("no result was published", not pool_delivery.result_path(ws, tid).exists())
    check("the flag still attributes the task", result_claimant.resolve_claimant(ws / "state", tid) == WORKER)
    check("and residue calls that state recoverable, not lost",
          pool_delivery.residue(ws, WORKER, tid) == "finished")


def scenario_the_writer_refuses_a_name_outside_the_pools_grammar() -> None:
    """Bounds are the writer's, not the caller's: the flag path is built from
    two ids, so an unchecked one writes into somebody else's claim tree."""
    print("\nscenario: writer bounds")
    ws = Path(tempfile.mkdtemp(prefix="mark-done-bounds-"))
    for recipient in ("../escape", "Worker-1", ""):
        try:
            pool_delivery.mark_done(ws, recipient, "task-x")
            check(f"rejects recipient {recipient!r}", False, "it was accepted")
        except ValueError:
            check(f"rejects recipient {recipient!r}", True)
    for task_id in ("../../escape", "notataskid", "task-a/b"):
        try:
            pool_delivery.mark_done(ws, WORKER, task_id)
            check(f"rejects task id {task_id!r}", False, "it was accepted")
        except ValueError:
            check(f"rejects task id {task_id!r}", True)


def main() -> int:
    scenario_the_drain_never_sees_a_result_before_its_flag()
    scenario_racing_finishers_do_not_clobber_each_other()
    scenario_a_repeated_finish_is_idempotent()
    scenario_a_crash_after_the_flag_is_the_recoverable_half()
    scenario_the_writer_refuses_a_name_outside_the_pools_grammar()
    print("\n" + ("FAILURES: " + ", ".join(FAILURES) if FAILURES else "all checks passed"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
