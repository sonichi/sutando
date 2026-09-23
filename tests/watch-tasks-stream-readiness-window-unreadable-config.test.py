"""Handler-config staleness and unreadability cases for the readiness-window
contract: a resolver publishing a replacement mid-decision, a runtime dir that
can't take a snapshot, a failing checksum tool, no config at all, and a config
that appears but can't be read (mode 000, or a dangling symlink) -- each must
be held (never silently routed on stale/absent state) and recovered once
readable.

Split from the original combined readiness-window file (#4627: that file was
the single heaviest suite in the python corpus and set the CI floor for every
PR) -- this piece and its siblings (…-decision-instant, …-held-task-recovery)
share tests/fixtures/readiness_window_helpers.py. No scenario's behavior
changed in the split.

Run: python3 tests/watch-tasks-stream-readiness-window-unreadable-config.test.py
"""
import json
import os
import subprocess
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


def scenario_resolver_publishes_between_refresh_and_decision():
    """The inbox resolver, which runs between the start of dispatch_task and the
    routing decision, atomically publishes must-handle C over fallback B."""
    tmp, ws, b = workspace("ready-resolver-")
    cfg = ws / "state" / "task-event-handler.json"
    log = tmp / "handler.log"
    h = handlers(tmp, log, (("hB", 3), ("hC", 4)))
    publish(cfg, h["hB"])
    cfg_c = tmp / "cfg-c.json"
    cfg_c.write_text(json.dumps({"handler": str(h["hC"])}))
    resolver = tmp / "resolver.sh"
    resolver.write_text('#!/bin/bash\n'
                        f'cp {cfg_c} {cfg}.tmp && mv {cfg}.tmp {cfg}\n'
                        'printf \'%s\\n\' "$1"\n')
    resolver.chmod(0o755)
    env = watcher_env(tmp, ws, b, {"SUTANDO_INBOX_RESOLVER": str(resolver)})
    p = start(ws, env)
    out: list[str] = []
    try:
        wait_ready(ws)
        write_task(ws, "task-team")
        pump(p, out, lambda: log.exists() or any("task-team" in ln for ln in out))
        handled = log.read_text().split() if log.exists() else []
        return out, handled
    finally:
        stop(p)


def scenario_runtime_dir_unwritable():
    """A must-handle config is published while the watcher's runtime dir cannot
    take the snapshot copy: the task is held, never announced, and handled once
    the dir is writable again and the config's next event runs the replay.
    No tick exists: the feed emits the events, and the poll interval is far
    beyond the test so only events can move the held task."""
    tmp, ws, b = workspace("ready-broken-")
    cfg = ws / "state" / "task-event-handler.json"
    log = tmp / "handler.log"
    h = handlers(tmp, log, (("hB", 3), ("hC", 4)))
    publish(cfg, h["hB"])
    env = watcher_env(tmp, ws, b, {"SUTANDO_HANDLER_POLL_INTERVAL": "60"})
    p, emit, real_tasks = feed_start(tmp, ws, b, env)
    out: list[str] = []
    runtime = None
    try:
        runtime = next(iter(tmp.glob("sutando-task-watch.*")))
        os.chmod(runtime, 0o500)
        publish(cfg, h["hC"])
        emit(os.path.realpath(cfg))
        time.sleep(0.5)
        write_task(ws, "task-team")
        emit(real_tasks / "task-team.txt")
        pump(p, out, lambda: log.exists() or any("task-team" in ln for ln in out), timeout=6, settle=0.5)
        held_out = list(out)
        held_log = log.read_text().split() if log.exists() else []
        os.chmod(runtime, 0o700)
        emit(os.path.realpath(cfg))  # the config's own event: reload succeeds now, held tasks replay
        pump(p, out, lambda: log.exists() or any("task-team" in ln for ln in out), timeout=12)
        handled = log.read_text().split() if log.exists() else []
        return held_out, held_log, out, handled
    finally:
        if runtime is not None:
            os.chmod(runtime, 0o700)
        stop(p)


def scenario_cksum_fails_at_first():
    """The checksum tool fails on its first two calls; a config replacement
    made while it was failing must still route the task."""
    tmp, ws, b = workspace("ready-cksum-")
    cfg = ws / "state" / "task-event-handler.json"
    log = tmp / "handler.log"
    h = handlers(tmp, log, (("hA", 3), ("hC", 4)))
    publish(cfg, h["hA"])
    import shutil
    real = shutil.which("cksum")
    counter = tmp / "cksum-calls"
    (b / "cksum").write_text('#!/bin/bash\n'
                             f'n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}\n'
                             '[ "$n" -le 2 ] && exit 1\n'
                             f'exec {real} "$@"\n')
    (b / "cksum").chmod(0o755)
    env = watcher_env(tmp, ws, b)
    p = start(ws, env)
    out: list[str] = []
    try:
        wait_ready(ws)
        publish(cfg, h["hC"])
        time.sleep(0.5)
        write_task(ws, "task-team")
        pump(p, out, lambda: log.exists() or any("task-team" in ln for ln in out))
        handled = log.read_text().split() if log.exists() else []
        calls = int(counter.read_text() or 0) if counter.exists() else 0
        return out, handled, calls
    finally:
        stop(p)


def scenario_missing_config_stderr():
    """No config at all: the task reaches the core and the watcher's stderr
    carries only its own announce line, no error from the stamp's read."""
    tmp, ws, b = workspace("ready-nocfg-")
    env = watcher_env(tmp, ws, b)
    errf = tmp / "watcher.err"
    with open(errf, "w") as fh:
        p = start(ws, env, stderr=fh)
    out: list[str] = []
    try:
        wait_ready(ws)
        write_task(ws, "task-owner")
        pump(p, out, lambda: any("task-owner" in ln for ln in out))
    finally:
        stop(p)
    err = [ln for ln in errf.read_text().splitlines() if ln.strip()]
    return out, err


print("the inbox resolver publishes must-handle C between the start of dispatch and the decision:")
out, handled = scenario_resolver_publishes_between_refresh_and_decision()
check("the task was routed by C, the config on disk at the decision: never announced, handled once",
      not any(ln.startswith("TASK_FILE: task-team") for ln in out) and handled == ["probe-hC", "handle-hC"],
      f"stdout={out!r} handler log={handled!r}")

print("the runtime dir cannot take the snapshot while a must-handle config is published:")
held_out, held_log, out, handled = scenario_runtime_dir_unwritable()
check("while the config could not be read the task was held: no announcement, no handler run",
      not any("task-team" in ln for ln in held_out) and held_log == [],
      f"stdout={held_out!r} handler log={held_log!r}")
check("once the dir was writable again the held task was handled once by C, still never announced",
      not any(ln.startswith("TASK_FILE: task-team") for ln in out) and handled == ["probe-hC", "handle-hC"],
      f"stdout={out!r} handler log={handled!r}")

print("the checksum tool fails on its first calls:")
out, handled, calls = scenario_cksum_fails_at_first()
print(f"  (cksum calls seen on the routing path: {calls})")
check("a replacement made while the checksum failed still routed the task: never announced, handled once by C",
      not any(ln.startswith("TASK_FILE: task-team") for ln in out) and handled == ["probe-hC", "handle-hC"],
      f"stdout={out!r} handler log={handled!r}")

print("no config at all:")
out, err = scenario_missing_config_stderr()
check("the task reached the core once", sum(ln.startswith("TASK_FILE: task-owner") for ln in out) == 1, f"stdout={out!r}")
check("stderr holds only the watcher's own announce line",
      len(err) == 1 and err[0].startswith("watch-tasks-stream: role=session"), f"stderr={err!r}")


def scenario_absent_then_unreadable_config_appears():
    """No config at start. A must-handle config appears but cannot be read
    (mode 000), with the task's event delivered before the config's. The task
    must be held, then handled once the config is readable and its own event
    runs the replay. The feed orders the events; no tick exists."""
    tmp, ws, b = workspace("ready-unread-")
    cfg = ws / "state" / "task-event-handler.json"
    log = tmp / "handler.log"
    h = handlers(tmp, log, (("hC", 4),))
    env = watcher_env(tmp, ws, b, {"SUTANDO_HANDLER_POLL_INTERVAL": "60"})
    p, emit, real_tasks = feed_start(tmp, ws, b, env)
    out: list[str] = []
    try:
        write_task(ws, "task-team")
        publish(cfg, h["hC"])
        os.chmod(cfg, 0)
        emit(real_tasks / "task-team.txt")  # the task's event first
        emit(os.path.realpath(cfg))                           # then the (unreadable) config's
        pump(p, out, lambda: log.exists() or any("task-team" in ln for ln in out), timeout=6, settle=0.5)
        held_out = list(out)
        held_log = log.read_text().split() if log.exists() else []
        os.chmod(cfg, 0o644)
        emit(os.path.realpath(cfg))  # the config's own event once readable: reload and replay
        pump(p, out, lambda: log.exists() or any("task-team" in ln for ln in out), timeout=12)
        handled = log.read_text().split() if log.exists() else []
        return held_out, held_log, out, handled
    finally:
        try:
            os.chmod(cfg, 0o644)
        except OSError:
            pass
        stop(p)


def scenario_held_task_own_event_after_recovery():
    """A task held while the config was unreadable is replayed by the config's
    own event once it is readable again, then receives its own file event with
    unchanged content: it must be dispatched exactly once. Needs the host's
    fswatch (an Updated event on the same name)."""
    import shutil
    if not shutil.which("fswatch"):
        return None
    tmp, ws, b = workspace("ready-held-")
    (b / "fswatch").unlink()
    cfg = ws / "state" / "task-event-handler.json"
    log = tmp / "handler.log"
    h = handlers(tmp, log, (("hO", 0),))  # optional handler: accepts and runs, writes no result
    publish(cfg, h["hO"])
    os.chmod(cfg, 0)
    env = watcher_env(tmp, ws, b, {"SUTANDO_HANDLER_POLL_INTERVAL": "60"})
    p = start(ws, env)
    out: list[str] = []
    try:
        wait_ready(ws)
        write_task(ws, "task-held")
        pump(p, out, lambda: log.exists() or any("task-held" in ln for ln in out), timeout=5, settle=0.5)
        held_log = log.read_text().split() if log.exists() else []
        os.chmod(cfg, 0o644)  # fswatch reports this as the config's Updated event: reload and replay
        pump(p, out, lambda: log.exists(), timeout=12, settle=1.0)
        task_path = ws / "tasks" / "task-held.txt"
        body = task_path.read_text()
        with open(task_path, "w") as fh:  # the held task's own Updated event, content unchanged
            fh.write(body)
        pump(p, out, lambda: False, timeout=4, settle=0.0)
        handled = log.read_text().split() if log.exists() else []
        return held_log, out, handled
    finally:
        try:
            os.chmod(cfg, 0o644)
        except OSError:
            pass
        stop(p)


print("no config, then an unreadable must-handle config appears with the task's event first:")
held_out, held_log, out, handled = scenario_absent_then_unreadable_config_appears()
check("the task was held while the new config could not be read: no announcement, no handler run",
      not any("task-team" in ln for ln in held_out) and held_log == [],
      f"stdout={held_out!r} handler log={held_log!r}")
check("once readable, the held task was handled once by C, never announced",
      not any(ln.startswith("TASK_FILE: task-team") for ln in out) and handled == ["probe-hC", "handle-hC"],
      f"stdout={out!r} handler log={handled!r}")

print("a held task's own event arrives after the config is readable again:")
r = scenario_held_task_own_event_after_recovery()
if r is None:
    print("  skip: no fswatch on this host (the case needs an Updated event on the same name)")
else:
    held_log, out, handled = r
    check("setup: the task was held first", held_log == [] and not any("task-held" in ln for ln in out[:1]),
          f"handler log={held_log!r}")
    check("the task was dispatched exactly once: one probe, one run, no announcement",
          handled == ["probe-hO", "handle-hO"] and not any(ln.startswith("TASK_FILE: task-held") for ln in out),
          f"stdout={out!r} handler log={handled!r}")


def scenario_dangling_symlink_config():
    """The config path is a dangling symlink: a config that exists and cannot
    be read, so the task is held, never announced. The target appearing
    outside the watched dir is no watched event, so the task stays held; the
    link being re-pointed in place is one, and the task replays exactly once."""
    tmp, ws, b = workspace("ready-dangling-")
    cfg = ws / "state" / "task-event-handler.json"
    cfg_event = Path(os.path.realpath(cfg.parent)) / cfg.name
    log = tmp / "handler.log"
    h = handlers(tmp, log, (("hC", 4),))
    target = tmp / "real-config.json"
    os.symlink(target, cfg)  # dangling: the target does not exist yet
    env = watcher_env(tmp, ws, b, {"SUTANDO_HANDLER_POLL_INTERVAL": "60"})
    p, emit, real_tasks = feed_start(tmp, ws, b, env)
    out: list[str] = []
    try:
        write_task(ws, "task-team")
        emit(real_tasks / "task-team.txt")
        pump(p, out, lambda: log.exists() or any("task-team" in ln for ln in out), timeout=4, settle=0.3)
        held_out = list(out)
        held_log = log.read_text().split() if log.exists() else []
        target.write_text(json.dumps({"handler": str(h["hC"])}))  # no watched event
        pump(p, out, lambda: False, timeout=3, settle=0.0)
        still_out = list(out)
        still_log = log.read_text().split() if log.exists() else []
        link = cfg.with_name(".cfg.link")
        os.symlink(target, link)
        os.replace(link, cfg)  # the link re-pointed in place: a genuine watched event
        emit(cfg_event)
        pump(p, out, lambda: log.exists() or any("task-team" in ln for ln in out), timeout=10)
        handled = log.read_text().split() if log.exists() else []
        return held_out, held_log, still_out, still_log, out, handled
    finally:
        stop(p)


print("the config path is a dangling symlink:")
held_out, held_log, still_out, still_log, out, handled = scenario_dangling_symlink_config()
check("the task was held (a symlink that resolves nowhere is broken, not absent): no announcement, no handler run",
      not any("task-team" in ln for ln in held_out) and held_log == [],
      f"stdout={held_out!r} handler log={held_log!r}")
check("the target appearing outside the watched dir is no event: the task stays held",
      not any("task-team" in ln for ln in still_out) and still_log == [],
      f"stdout={still_out!r} handler log={still_log!r}")
check("once the link was re-pointed in place, the held task was handled once by C, never announced",
      not any(ln.startswith("TASK_FILE: task-team") for ln in out) and handled == ["probe-hC", "handle-hC"],
      f"stdout={out!r} handler log={handled!r}")

print(("FAILED — " + ", ".join(FAILURES)) if FAILURES else
      "PASS — unreadable/absent/stale handler-config cases")
sys.exit(1 if FAILURES else 0)
