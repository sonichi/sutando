#!/usr/bin/env python3
"""The done flag has ONE writer, and it runs before the result it attributes.

`pool_delivery.mark_done` is that writer; its readers are `pool_delivery.residue`
and `src/result_claimant.py`, which the gateway consults to stamp an outbound
reply with the worker that produced it. The contract this suite pins:

    ordering    `.pending`, THEN result, THEN `.flag`. A result the drain can
                see always has its attribution beside it — on the handler's
                success path as much as on the reaper's failure path; the
                reverse (`.pending`, no result) is the recoverable state, and
                a sweep releases it rather than retiring it.
    atomicity   temp file + rename in the same directory, so a concurrent
                reader sees the name absent or complete, never half-written.
    isolation   two finishers racing on different tasks do not clobber each
                other, and a repeat of the same finish is idempotent.
    bounds      a recipient or task id outside the pool's own grammar is
                rejected, so no caller can write outside its own claim tree.

Ordering is asserted through the SHIPPED watcher (`src/watch-tasks-stream.sh`:
its handler runner and `finish_handler_task` on success, `publish_terminal_failure`
on failure) and the SHIPPED drain resolver — not a re-enactment of either.

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
    return str(pool_delivery.mark_done(Path(ws), worker, task_id, published=True))


def _witness(result: Path, ws: Path, tid: str, seen: dict, also=None) -> threading.Thread:
    """The drain's answer, recorded the instant `result` appears. It looks before
    it honours `stop`: the main thread's own sighting never closes its window."""
    def observe() -> None:
        try:
            while True:
                if result.exists():
                    seen["claimant"] = result_claimant.resolve_claimant(ws / "state", tid)
                    if also is not None:
                        seen.update(also())
                    return
                if seen.get("stop"):
                    return
                time.sleep(0.001)
        except Exception as exc:  # noqa: BLE001 — a dead witness must say why
            seen["error"] = repr(exc)
    return threading.Thread(target=observe, name="observe", daemon=True)


def _witnessed(seen: dict) -> bool:
    return "claimant" in seen or "error" in seen


def scenario_the_drain_never_sees_a_result_before_its_flag() -> None:
    """A worker's watcher publishes a result; an observer polling `results/` as
    fast as it can must find the attribution already there, every time."""
    print("\nscenario: flag-before-result, through the shipped watcher and drain")
    h = Harness()
    h.extra_env["SUTANDO_INSTANCE_ID"] = WORKER
    tid = "task-ordering"
    result = h.ws / "results" / f"{tid}.txt"
    seen: dict = {}
    watcher = _witness(result, h.ws, tid, seen, lambda: {"size": result.stat().st_size})
    try:
        h.task(f"{tid}.txt")
        h.start()
        if not wait_for(lambda: names(h.dispatch() and h.dispatch() / "running")):
            check("the task reached a handler worker", False, str(h.dispatch()))
            return
        watcher.start()
        h.kill_workers()
        h.deliver("task-nudge.txt")  # arrival is what drives the reap
        # The witness is what the test waits on; the file alone decides nothing.
        wait_for(lambda: _witnessed(seen))
        published = result.is_file() and result.stat().st_size > 0
        check("the worker's watcher published a result", published)
        seen["stop"] = True
        watcher.join(10.0)
        check("the observer caught the result appearing", "claimant" in seen,
              seen.get("error") or f"never observed {result}")
        check("and its flag was ALREADY there when it did",
              seen.get("claimant") == WORKER,
              f"result was visible with claimant={seen.get('claimant')!r}")
        flag = pool_delivery.done_flag(h.ws, WORKER, tid)
        check("the record is promoted to `.flag` once the result is out",
              wait_for(lambda: pool_delivery.is_done_flag(flag)), str(flag))
        check("and the drain resolves it to this worker after the fact",
              result_claimant.resolve_claimant(h.ws / "state", tid) == WORKER)
        # `done/` is per WORKER: the nudge that drove the reap is still in its
        # handler, so its own `.pending` is a live record, not this task's residue.
        beside = _records_of(flag.parent, tid)
        check("neither a temp file nor the spent `.pending` is left beside it",
              beside == [flag.name], str(beside))
    finally:
        seen["stop"] = True
        h.stop()


def scenario_a_successful_handler_is_attributed_before_its_result_is_visible() -> None:
    """The handler, not the watcher, publishes a successful result — so the
    watcher's finisher runs only after the drain could already have seen it.
    The name must be down before the handler is even started, and the record
    must be promoted once the finisher runs."""
    print("\nscenario: handler success path, through the shipped watcher and drain")
    h = Harness()
    h.extra_env["SUTANDO_INSTANCE_ID"] = WORKER
    # A handler that answers: the probe says must-handle, the run publishes.
    h.handler.write_text(
        '#!/bin/sh\nfor a in "$@"; do [ "$a" = "--probe" ] && exit 4; done\n'
        'while [ $# -gt 0 ]; do case "$1" in --task-file) tf="$2"; shift;;'
        ' --results-dir) rd="$2"; shift;; esac; shift; done\n'
        'n="$(basename "$tf")"\nsleep 0.3\n'
        'printf "answered\\n" > "$rd/.$n.tmp" && mv "$rd/.$n.tmp" "$rd/$n"\n')
    tid = "task-success"
    result = h.ws / "results" / f"{tid}.txt"
    seen: dict = {}
    watcher = _witness(result, h.ws, tid, seen,
                       lambda: {"stage": pool_delivery.flag_stage(h.ws, WORKER, tid)})
    try:
        h.deliver(f"{tid}.txt")
        h.start()
        watcher.start()
        # The witness is what the test waits on; the file alone decides nothing.
        wait_for(lambda: _witnessed(seen))
        published = result.is_file() and result.stat().st_size > 0
        check("the handler published a result", published)
        seen["stop"] = True
        watcher.join(10.0)
        # The reviewer's probe, printed so a control run shows what it measured.
        print(f"  probe: result_ready={published} stage_at_visibility={seen.get('stage')!r} "
              f"claimant={seen.get('claimant')!r}")
        check("the observer caught the result appearing", "claimant" in seen,
              seen.get("error") or str(result))
        check("and this worker's name was ALREADY on it when it did",
              seen.get("claimant") == WORKER,
              f"result was visible with claimant={seen.get('claimant')!r}")
        done = pool_delivery.done_flag(h.ws, WORKER, tid)
        check("the finisher then promotes the record to `.flag`",
              wait_for(lambda: pool_delivery.is_done_flag(done)), str(done))
        check("and nothing but the flag is left in the record's directory",
              [p.name for p in done.parent.iterdir()] == [done.name],
              str([p.name for p in done.parent.iterdir()]))
        check("a drain after the fact still names this worker",
              result_claimant.resolve_claimant(h.ws / "state", tid) == WORKER)
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
    # A retried finisher laying `pending` after the promote must not reopen it.
    late = pool_delivery.mark_done(ws, WORKER, "task-twice", published=False)
    check("a late `pending` never demotes a promoted record",
          late == pool_delivery.done_flag(ws, WORKER, "task-twice")
          and pool_delivery.flag_stage(ws, WORKER, "task-twice") == "done"
          and not pool_delivery.pending_flag(ws, WORKER, "task-twice").exists(),
          f"stage={pool_delivery.flag_stage(ws, WORKER, 'task-twice')!r}")


def scenario_a_crash_after_the_record_is_the_recoverable_half() -> None:
    """The asymmetry the ordering buys: a record without a result is a state
    the sweep hands back, while a result without a record is a reply already
    delivered with nobody's name on it — unrecoverable by then. Run THROUGH the
    sweep: a label alone proved nothing when the sweep then unlinked the sentinel."""
    print("\nscenario: crash between the record and the result, through the sweep")
    ws = Path(tempfile.mkdtemp(prefix="mark-done-crash-"))
    (ws / "results").mkdir(parents=True)
    (ws / "tasks").mkdir()
    tid = "task-crashed"
    (ws / "tasks" / f"{tid}.txt").write_text("task: probe\n")
    d = pool_delivery.deliveries_dir(ws, WORKER)
    d.mkdir(parents=True)
    accepted = pool_delivery.accept(_sentinel(d, tid))
    pool_delivery.mark_done(ws, WORKER, tid, published=False)   # ...then the crash
    check("no result was published", not pool_delivery.result_path(ws, tid).exists())
    check("the record still attributes the task",
          result_claimant.resolve_claimant(ws / "state", tid) == WORKER)
    before = pool_delivery.residue(ws, WORKER, tid)
    check("residue calls that state died-mid-work, not finished", before == "died-mid-work", before)
    out = pool_delivery.sweep(ws, WORKER)
    print(f"  probe: before={before!r} retired={out['retired']} released={out['released']} "
          f"sentinel_after={pool_delivery.find(ws, WORKER, tid) is not None} "
          f"result_after={pool_delivery.result_path(ws, tid).exists()}")
    check("the sweep hands the work back instead of retiring it",
          out["released"] == [tid] and out["retired"] == [], str(out))
    check("the sentinel survives the sweep, back under its pending name",
          (d / f"{tid}.txt").is_file() and not accepted.exists())
    check("and the record survives with it, so the retry's reply is still attributed",
          pool_delivery.flag_stage(ws, WORKER, tid) == "pending"
          and result_claimant.resolve_claimant(ws / "state", tid) == WORKER)
    # The other half: a `done` record with no result is a DRAINED reply, retired.
    pool_delivery.mark_done(ws, WORKER, tid, published=True)
    check("a promoted record with no result reads as finished (drained), not died",
          pool_delivery.residue(ws, WORKER, tid) == "finished")


def _records_of(d: Path, tid: str) -> list[str]:
    """Every name in `d` that belongs to `tid`: its stages and their temp files."""
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.name.lstrip(".").startswith(tid + "."))


def _sentinel(d: Path, tid: str) -> Path:
    p = d / f"{tid}{pool_delivery.PENDING_SUFFIX}"
    p.touch()
    return p


def scenario_the_writer_refuses_a_name_outside_the_pools_grammar() -> None:
    """Bounds are the writer's, not the caller's: the flag path is built from
    two ids, so an unchecked one writes into somebody else's claim tree."""
    print("\nscenario: writer bounds")
    ws = Path(tempfile.mkdtemp(prefix="mark-done-bounds-"))
    for recipient in ("../escape", "Worker-1", ""):
        try:
            pool_delivery.mark_done(ws, recipient, "task-x", published=False)
            check(f"rejects recipient {recipient!r}", False, "it was accepted")
        except ValueError:
            check(f"rejects recipient {recipient!r}", True)
    for task_id in ("../../escape", "notataskid", "task-a/b"):
        try:
            pool_delivery.mark_done(ws, WORKER, task_id, published=False)
            check(f"rejects task id {task_id!r}", False, "it was accepted")
        except ValueError:
            check(f"rejects task id {task_id!r}", True)


def scenario_a_failed_rename_leaves_nothing_behind() -> None:
    """The flag is either there or not there. A writer that half-published and
    left its temp file would hand the next reader a name it cannot classify."""
    print("\nscenario: the rename fails")
    ws = Path(tempfile.mkdtemp(prefix="mark-done-fail-"))
    tid = "task-blocked"
    # A directory squatting the flag's own name: rename onto it cannot succeed.
    blocked = pool_delivery.done_flag(ws, WORKER, tid)
    blocked.mkdir(parents=True)
    raised = False
    try:
        pool_delivery.mark_done(ws, WORKER, tid, published=True)
    except OSError:
        raised = True
    check("the writer raises rather than reporting a finish", raised)
    beside = sorted(p.name for p in blocked.parent.iterdir())
    check("and no temp file survives the failure", beside == [blocked.name], str(beside))
    # The reader's half of the same state: malformed, so it names nobody.
    try:
        result_claimant.resolve_claimant(ws / "state", tid)
        check("the drain refuses to attribute the squatted name", False, "it returned a worker")
    except result_claimant.Unattributable as exc:
        check("the drain refuses to attribute the squatted name",
              "not a regular file" in str(exc), str(exc))


def scenario_the_cli_entry_point_writes_the_same_flag() -> None:
    """The watcher reaches the writer through this argv, so it is covered here
    in-process — a subprocess run would exercise it and measure nothing."""
    print("\nscenario: pool_delivery.py mark-done")
    ws = Path(tempfile.mkdtemp(prefix="mark-done-cli-"))
    rc = pool_delivery.main(["--workspace", str(ws), "--recipient", WORKER,
                             "mark-done", "--task-id", "task-viacli", "--stage", "pending"])
    check("the CLI reports success", rc == 0)
    check("and the drain resolves the record it wrote",
          result_claimant.resolve_claimant(ws / "state", "task-viacli") == WORKER)
    check("which is the pending stage", pool_delivery.flag_stage(ws, WORKER, "task-viacli") == "pending")
    rc = pool_delivery.main(["--workspace", str(ws), "--recipient", WORKER,
                             "mark-done", "--task-id", "task-viacli", "--stage", "done"])
    check("--stage done promotes it", rc == 0 and pool_delivery.flag_stage(ws, WORKER, "task-viacli") == "done")
    for argv in (["mark-done", "--stage", "pending"], ["mark-done", "--task-id", "task-viacli"]):
        try:
            pool_delivery.main(["--workspace", str(ws), "--recipient", WORKER] + argv)
            check(f"{' '.join(argv)!r} is rejected, not defaulted", False, "it was accepted")
        except SystemExit as exc:
            check(f"{' '.join(argv)!r} is rejected, not defaulted", exc.code != 0, str(exc.code))


def main() -> int:
    scenario_the_drain_never_sees_a_result_before_its_flag()
    scenario_a_successful_handler_is_attributed_before_its_result_is_visible()
    scenario_racing_finishers_do_not_clobber_each_other()
    scenario_a_repeated_finish_is_idempotent()
    scenario_a_crash_after_the_record_is_the_recoverable_half()
    scenario_the_writer_refuses_a_name_outside_the_pools_grammar()
    scenario_a_failed_rename_leaves_nothing_behind()
    scenario_the_cli_entry_point_writes_the_same_flag()
    print("\n" + ("FAILURES: " + ", ".join(FAILURES) if FAILURES else "all checks passed"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
