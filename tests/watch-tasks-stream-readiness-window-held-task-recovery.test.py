"""Held-task retry, replay and admission cases for the readiness-window
contract: a held task must survive a busy stream of unrelated events, must
not be re-admitted by its own later Created/Updated events (Linux semantics,
driven deterministically here), must never be replayed mid-shutdown, and a
retry after a foreign claim releases must process the task exactly once.

Split from the original combined readiness-window file (#4627: that file was
the single heaviest suite in the python corpus and set the CI floor for every
PR) -- this piece and its siblings (…-decision-instant, …-unreadable-config)
share tests/fixtures/readiness_window_helpers.py. No scenario's behavior
changed in the split.

Run: python3 tests/watch-tasks-stream-readiness-window-held-task-recovery.test.py
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from readiness_window_helpers import (  # noqa: E402
    watcher_env, handlers, workspace, publish, write_task, start, pump,
    stop, wait_ready, feed_start,
)

FAILURES: list[str] = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def scenario_held_task_under_a_busy_stream():
    """A held task must be retried on an elapsed deadline even when unrelated
    events arrive continuously (each one restarts the read timeout)."""
    tmp, ws, b = workspace("ready-busy-")
    cfg = ws / "state" / "task-event-handler.json"
    log = tmp / "handler.log"
    h = handlers(tmp, log, (("hC", 4),))
    publish(cfg, h["hC"])
    os.chmod(cfg, 0)
    env = watcher_env(tmp, ws, b, {"SUTANDO_HELD_RETRY_INTERVAL": "2"})
    p = start(ws, env)
    out: list[str] = []
    try:
        wait_ready(ws)
        write_task(ws, "task-team")
        pump(p, out, lambda: log.exists() or any("task-team" in ln for ln in out), timeout=3, settle=0.3)
        held_log = log.read_text().split() if log.exists() else []
        t0 = time.time()
        dispatched_at = None
        n = 0
        while time.time() - t0 < 8:
            (ws / "state" / f"noise-{n}").write_text("x")  # an ignored event every 150 ms
            n += 1
            if time.time() - t0 >= 1.0 and (cfg.stat().st_mode & 0o777) == 0:
                os.chmod(cfg, 0o644)
            if dispatched_at is None and log.exists():
                dispatched_at = time.time() - t0
                break
            time.sleep(0.15)
        time.sleep(1.0)
        try:
            os.set_blocking(p.stdout.fileno(), False)
            c = p.stdout.read()
            if c:
                out.extend(c.splitlines())
        except (BlockingIOError, TypeError):
            pass
        handled = log.read_text().split() if log.exists() else []
        return held_log, out, handled, dispatched_at
    finally:
        try:
            os.chmod(cfg, 0o644)
        except OSError:
            pass
        stop(p)


print("a held task under a stream of ignored events every 150 ms; the config becomes readable at 1 s:")
held_log, out, handled, at = scenario_held_task_under_a_busy_stream()
check("setup: the task was held first", held_log == [], f"handler log={held_log!r}")
check("the held task was dispatched within the retry deadline (2 s) despite the busy stream, once, never announced",
      at is not None and at <= 4.5 and handled == ["probe-hC", "handle-hC"]
      and not any(ln.startswith("TASK_FILE: task-team") for ln in out),
      f"dispatched_at={at} stdout={out!r} handler log={handled!r}")


def scenario_held_task_events_after_recovery_feed():
    """Linux semantics, driven deterministically: after a held task is
    re-dispatched on recovery, its own Created and Updated events arrive; it
    must not be admitted again. The test feeds fswatch's output itself."""
    tmp, ws, b = workspace("ready-feed-")
    feed = tmp / "feed"
    feed.write_text("")
    (b / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {feed}\n")
    (b / "fswatch").chmod(0o755)
    cfg = ws / "state" / "task-event-handler.json"
    log = tmp / "handler.log"
    h = handlers(tmp, log, (("hO", 0),))
    publish(cfg, h["hO"])
    os.chmod(cfg, 0)
    env = watcher_env(tmp, ws, b, {"SUTANDO_HELD_RETRY_INTERVAL": "1"})
    p = start(ws, env)
    out: list[str] = []
    real_tasks = Path(os.path.realpath(ws / "tasks"))

    def emit(path):
        with open(feed, "a") as fh:
            fh.write(f"{path}\n")

    try:
        # readiness: echo the probe the watcher writes into its own inbox
        t0 = time.time()
        probe = None
        while time.time() - t0 < 10:
            probes = list((ws / "tasks").glob(".ready-*"))
            if probes:
                probe = probes[0]
                break
            time.sleep(0.05)
        assert probe is not None, "no readiness probe appeared"
        emit(real_tasks / probe.name)
        wait_ready(ws)
        write_task(ws, "task-held")
        emit(real_tasks / "task-held.txt")
        pump(p, out, lambda: log.exists() or any("task-held" in ln for ln in out), timeout=4, settle=0.3)
        held_log = log.read_text().split() if log.exists() else []
        os.chmod(cfg, 0o644)
        emit(ws / "state" / "noise-1")  # any event: the retry deadline is checked after it
        pump(p, out, lambda: log.exists(), timeout=8, settle=1.0)
        first = log.read_text().split() if log.exists() else []
        emit(real_tasks / "task-held.txt")  # Created
        emit(real_tasks / "task-held.txt")  # Updated
        time.sleep(3.0)
        handled = log.read_text().split() if log.exists() else []
        return held_log, first, out, handled
    finally:
        try:
            os.chmod(cfg, 0o644)
        except OSError:
            pass
        stop(p)


print("a held task is re-dispatched on recovery, then its own Created and Updated events arrive (feed-driven):")
held_log, first, out, handled = scenario_held_task_events_after_recovery_feed()
check("setup: the task was held first", held_log == [], f"handler log={held_log!r}")
check("recovery dispatched it once", first == ["probe-hO", "handle-hO"], f"handler log={first!r}")
check("its later Created and Updated events did not admit it again",
      handled == ["probe-hO", "handle-hO"] and not any(ln.startswith("TASK_FILE: task-held") for ln in out),
      f"stdout={out!r} handler log={handled!r}")


def scenario_held_task_during_shutdown():
    """A task held on a broken config, then the shutdown sentinel is written and
    the config recovers: the held task must not be replayed by the config's own
    event, the only replay path there is; it stays in the inbox."""
    tmp, ws, b = workspace("ready-shutdown-")
    cfg = ws / "state" / "task-event-handler.json"
    log = tmp / "handler.log"
    h = handlers(tmp, log, (("hC", 4),))
    publish(cfg, h["hC"])
    os.chmod(cfg, 0)
    env = watcher_env(tmp, ws, b, {"SUTANDO_HELD_RETRY_INTERVAL": "60"})
    p, emit, real_tasks = feed_start(tmp, ws, b, env)
    out: list[str] = []
    try:
        write_task(ws, "task-team")
        emit(real_tasks / "task-team.txt")
        pump(p, out, lambda: log.exists() or any("task-team" in ln for ln in out), timeout=3, settle=0.3)
        held_log = log.read_text().split() if log.exists() else []
        (ws / "state" / "shutdown.sentinel").write_text("")
        os.chmod(cfg, 0o644)
        emit(os.path.realpath(cfg))  # the config's own event runs the reload and the held replay
        time.sleep(4.0)
        try:
            os.set_blocking(p.stdout.fileno(), False)
            c = p.stdout.read()
            if c:
                out.extend(c.splitlines())
        except (BlockingIOError, TypeError):
            pass
        handled = log.read_text().split() if log.exists() else []
        still_there = (ws / "tasks" / "task-team.txt").exists()
        return held_log, out, handled, still_there
    finally:
        try:
            os.chmod(cfg, 0o644)
        except OSError:
            pass
        stop(p)


print("a held task, then the shutdown sentinel, then the config recovers via its own event:")
held_log, out, handled, still_there = scenario_held_task_during_shutdown()
check("setup: the task was held first", held_log == [], f"handler log={held_log!r}")
check("the held task was NOT replayed mid-shutdown and remains in the inbox",
      handled == [] and not any("task-team" in ln for ln in out) and still_there,
      f"stdout={out!r} handler log={handled!r} still_there={still_there}")


def scenario_foreign_claim_then_retry():
    """A live foreign claim holds the task at its first decision, so nothing
    runs; once the claim is released, a retry event for the same unchanged file
    must process the task exactly once."""
    tmp, ws, b = workspace("ready-claim-")
    cfg = ws / "state" / "task-event-handler.json"
    log = tmp / "handler.log"
    h = handlers(tmp, log, (("hC", 4),))
    publish(cfg, h["hC"])
    claims = ws / "state" / "task-event-handler-claims"
    claims.mkdir()
    write_task(ws, "task-team")
    task_path = os.path.realpath(ws / "tasks" / "task-team.txt")
    # run_handler_now's claim record: owner pid, watcher id, payload, disposition
    (claims / "task-team.txt").write_text(f"{os.getpid()}\nforeign-1\n{task_path}\nmust-handle\n")
    env = watcher_env(tmp, ws, b)
    p, emit, real_tasks = feed_start(tmp, ws, b, env)
    out: list[str] = []
    try:
        emit(real_tasks / "task-team.txt")
        time.sleep(3.0)
        first = log.read_text().split() if log.exists() else []
        (claims / "task-team.txt").unlink()
        emit(real_tasks / "task-team.txt")  # the retry event for the same unchanged file
        pump(p, out, lambda: log.exists(), timeout=8, settle=2.0)
        handled = log.read_text().split() if log.exists() else []
        return first, out, handled
    finally:
        stop(p)


print("a live foreign claim holds the task, then the claim is released and a retry event arrives:")
first, out, handled = scenario_foreign_claim_then_retry()
check("setup: with the foreign claim live the handler ran nothing beyond the probe and nothing was announced",
      "handle-hC" not in first and not any("task-team" in ln for ln in out[:0] + [ln for ln in out if ln.startswith("TASK_FILE")]),
      f"handler log={first!r} stdout={out!r}")
check("after the release, the retry event processed the task exactly once",
      handled.count("handle-hC") == 1 and not any(ln.startswith("TASK_FILE: task-team") for ln in out),
      f"stdout={out!r} handler log={handled!r}")

print(("FAILED — " + ", ".join(FAILURES)) if FAILURES else
      "PASS — held-task retry, replay-suppression and shutdown-gate cases")
sys.exit(1 if FAILURES else 0)
