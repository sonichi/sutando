"""A session watcher dispatches a task admitted after readiness with the handler
config fswatch delivered before that task's event, the same guarantee the live
loop gives.

The readiness probe buffers every event fswatch delivers before the probe comes
back. A handler config and a task that both land in that window must be
handled in fswatch's order: config first means the task goes through the
handler (a must-handle task is never announced to the live core); task first
means the task may reach the core, as it would on the live loop, and the
config applies to the next task.

A third case distinguishes a per-decision refresh from a reload taken once
before the sweep: the config changes between two swept items. A fourth makes
two same-second, equal-size atomic replacements and requires the second one
to route. A fifth replaces the config between its parse and its stamp.

Run: python3 tests/watch-tasks-stream-readiness-window-honours-handler-config.test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def scenario(config_first: bool):
    """Start a session watcher whose fswatch delivers events only after a delay
    (a wide readiness window). config_first: publish a must-handle config and a
    Team task inside the window. Otherwise: the task inside the window, the
    config only after the task was announced, then a second task.
    Returns (stdout lines, handler log, result names)."""
    tmp = Path(tempfile.mkdtemp(prefix="ready-window-"))
    ws = tmp / "ws"
    (ws / "tasks").mkdir(parents=True)
    (ws / "results" / "archive").mkdir(parents=True)
    (ws / "state").mkdir()
    b = tmp / "bin"
    b.mkdir()
    # Subscribes at once, delivers 2 s late: the probe, the config and the task
    # all stay buffered, so the window is wide and the test controls the order.
    (b / "fswatch").write_text(
        "#!/bin/bash\nbash "
        f"{REPO / 'tests' / 'fixtures' / 'fswatch-poll-stub.sh'} \"$@\" | {{ sleep 2; exec cat; }}\n")
    (b / "fswatch").chmod(0o755)
    log = tmp / "handler.log"
    handler = tmp / "handler.sh"
    handler.write_text(
        '#!/bin/sh\n'
        f'for a in "$@"; do [ "$a" = "--probe" ] && {{ echo probe >> {log}; exit 4; }}; done\n'
        f'echo handle >> {log}\nexit 4\n')
    handler.chmod(0o755)
    cfg = ws / "state" / "task-event-handler.json"

    env = dict(os.environ)
    env["PATH"] = f"{b}:{env['PATH']}"
    env["TMPDIR"] = str(tmp)
    env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
    env["SUTANDO_WORKSPACE_DIR"] = str(ws)
    env["SUTANDO_STANDBY_STOP_TIMEOUT"] = "1"
    env.pop("SUTANDO_INSTANCE_ID", None)
    env.pop("SUTANDO_TASK_EVENT_HANDLER", None)
    p = subprocess.Popen(
        ["bash", "src/watch-tasks-stream.sh", str(ws / "tasks"),
         "--role", "session", "--inbox", str(ws / "tasks")],
        cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, start_new_session=True)
    out: list[str] = []
    try:
        # The probe is written first and fswatch's 2 s delay holds it back, so the
        # probe, the config and the task are all buffered when readiness returns.
        time.sleep(0.6)

        def publish_config():
            t = cfg.with_name(".cfg.tmp")
            t.write_text(json.dumps({"handler": str(handler)}))
            t.replace(cfg)

        def write_task(name):
            final = ws / "tasks" / f"{name}.txt"
            t = final.with_name(f".{name}.tmp")
            t.write_text(f"id: {name}\naccess_tier: team\ntask: restricted\n")
            t.replace(final)

        os.set_blocking(p.stdout.fileno(), False)

        def pump(until, timeout=12):
            t0 = time.time()
            while time.time() - t0 < timeout:
                time.sleep(0.3)
                try:
                    c = p.stdout.read()
                except (BlockingIOError, TypeError):
                    c = None
                if c:
                    out.extend(c.splitlines())
                if until():
                    time.sleep(1.0)
                    try:
                        c = p.stdout.read()
                    except (BlockingIOError, TypeError):
                        c = None
                    if c:
                        out.extend(c.splitlines())
                    return

        if config_first:
            publish_config()
            time.sleep(0.3)
            write_task("task-team")
            pump(lambda: log.exists() or any("task-team" in ln for ln in out))
        else:
            write_task("task-team")
            pump(lambda: any("TASK_FILE: task-team" in ln for ln in out))
            publish_config()
            time.sleep(0.5)
            write_task("task-next")
            pump(lambda: log.exists() or any("task-next" in ln for ln in out))
        handled = log.read_text().split() if log.exists() else []
        results = sorted(f.name for f in (ws / "results").glob("task-*.txt"))
        return out, handled, results
    finally:
        try:
            os.killpg(p.pid, 15)
        except (ProcessLookupError, PermissionError):
            p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


print("config then task, both inside the readiness window:")
out, handled, results = scenario(config_first=True)
leaked = any(ln.startswith("TASK_FILE: task-team") for ln in out)
check("the Team task is NOT announced to the live core", not leaked,
      f"stdout={out!r}")
check("the Team task went through the handler exactly once (one probe, one run)",
      handled.count("probe") == 1 and handled.count("handle") == 1,
      f"handler log={handled!r} results={results!r}")
check("the handler's must-handle verdict published a terminal result", results != [],
      f"results={results!r}")

print("task inside the window, config only after it was announced, then a second task:")
out, handled, results = scenario(config_first=False)
check("the earlier task reached the core exactly once (no config existed at its decision)",
      sum(ln.startswith("TASK_FILE: task-team") for ln in out) == 1,
      f"stdout={out!r}")
check("the later config applies to the next task: not announced, handled once",
      not any(ln.startswith("TASK_FILE: task-next") for ln in out)
      and handled.count("probe") == 1 and handled.count("handle") == 1,
      f"stdout={out!r} handler log={handled!r} results={results!r}")


def scenario_config_changes_between_swept_items():
    """Two tasks pending before the watcher starts. The declared handler, probed
    for the first task, re-declares a must-handle handler and declines; the
    second swept task must then be routed by the config as it is on disk at ITS
    decision, not by a reload taken once before the sweep."""
    tmp = Path(tempfile.mkdtemp(prefix="ready-sweep-"))
    ws = tmp / "ws"
    (ws / "tasks").mkdir(parents=True)
    (ws / "results" / "archive").mkdir(parents=True)
    (ws / "state").mkdir()
    b = tmp / "bin"
    b.mkdir()
    (b / "fswatch").write_text(
        f"#!/bin/bash\nexec bash {REPO / 'tests' / 'fixtures' / 'fswatch-poll-stub.sh'} \"$@\"\n")
    (b / "fswatch").chmod(0o755)
    cfg = ws / "state" / "task-event-handler.json"
    log = tmp / "handler.log"
    strict = tmp / "strict.sh"
    strict.write_text(
        '#!/bin/sh\n'
        f'for a in "$@"; do [ "$a" = "--probe" ] && {{ echo probe >> {log}; exit 4; }}; done\n'
        f'echo handle >> {log}\nexit 4\n')
    strict.chmod(0o755)
    first = tmp / "first.sh"
    first.write_text(
        '#!/bin/sh\n'
        f'printf \'{{"handler": "{strict}"}}\' > {cfg}.tmp && mv {cfg}.tmp {cfg}\n'
        'exit 3\n')
    first.chmod(0o755)
    cfg.write_text(json.dumps({"handler": str(first)}))
    for name in ("task-a", "task-b"):
        (ws / "tasks" / f"{name}.txt").write_text(f"id: {name}\naccess_tier: team\ntask: restricted\n")
        time.sleep(0.05)

    env = dict(os.environ)
    env["PATH"] = f"{b}:{env['PATH']}"
    env["TMPDIR"] = str(tmp)
    env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
    env["SUTANDO_WORKSPACE_DIR"] = str(ws)
    env["SUTANDO_STANDBY_STOP_TIMEOUT"] = "1"
    env.pop("SUTANDO_INSTANCE_ID", None)
    env.pop("SUTANDO_TASK_EVENT_HANDLER", None)
    p = subprocess.Popen(
        ["bash", "src/watch-tasks-stream.sh", str(ws / "tasks"),
         "--role", "session", "--inbox", str(ws / "tasks")],
        cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, start_new_session=True)
    out: list[str] = []
    try:
        os.set_blocking(p.stdout.fileno(), False)
        t0 = time.time()
        while time.time() - t0 < 12:
            time.sleep(0.3)
            try:
                c = p.stdout.read()
            except (BlockingIOError, TypeError):
                c = None
            if c:
                out.extend(c.splitlines())
            if log.exists() or any("task-b" in ln for ln in out):
                time.sleep(1.5)
                try:
                    c = p.stdout.read()
                except (BlockingIOError, TypeError):
                    c = None
                if c:
                    out.extend(c.splitlines())
                break
        handled = log.read_text().split() if log.exists() else []
        results = sorted(f.name for f in (ws / "results").glob("task-*.txt"))
        return out, handled, results
    finally:
        try:
            os.killpg(p.pid, 15)
        except (ProcessLookupError, PermissionError):
            p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


print("two swept tasks; the first one's probe re-declares a must-handle handler:")
out, handled, results = scenario_config_changes_between_swept_items()
check("the first task, declined by the handler declared at its decision, reached the core once",
      sum(ln.startswith("TASK_FILE: task-a") for ln in out) == 1, f"stdout={out!r}")
check("the second task was routed by the config on disk at ITS decision: never announced, handled once",
      not any(ln.startswith("TASK_FILE: task-b") for ln in out)
      and handled.count("probe") == 1 and handled.count("handle") == 1,
      f"stdout={out!r} handler log={handled!r} results={results!r}")


def scenario_same_second_equal_size_replacements():
    """The config is replaced twice, atomically, with equal-size contents and
    identical mtimes, while fswatch's delivery is held; a task written in the same
    instant is then delivered before the config's event. Its routing decision
    must read the second replacement, not a stamp-equal stale handler."""
    tmp = Path(tempfile.mkdtemp(prefix="ready-stamp-"))
    ws = tmp / "ws"
    (ws / "tasks").mkdir(parents=True)
    (ws / "results" / "archive").mkdir(parents=True)
    (ws / "state").mkdir()
    b = tmp / "bin"
    b.mkdir()
    (b / "fswatch").write_text(
        f"#!/bin/bash\nexec bash {REPO / 'tests' / 'fixtures' / 'fswatch-poll-stub.sh'} \"$@\"\n")
    (b / "fswatch").chmod(0o755)
    cfg = ws / "state" / "task-event-handler.json"
    log = tmp / "handler.log"
    handlers = {}
    for name, rc in (("hA", 3), ("hB", 3), ("hC", 4)):
        h = tmp / f"{name}.sh"
        h.write_text('#!/bin/sh\n'
                     f'for a in "$@"; do [ "$a" = "--probe" ] && {{ echo probe-{name} >> {log}; exit {rc}; }}; done\n'
                     f'echo handle-{name} >> {log}\nexit {rc}\n')
        h.chmod(0o755)
        handlers[name] = h
    fixed = 1_700_000_000  # one mtime for every replacement

    def publish(name):
        t = cfg.with_name(f".cfg-{name}.tmp")
        t.write_text(json.dumps({"handler": str(handlers[name])}))
        os.utime(t, (fixed, fixed))
        t.replace(cfg)

    publish("hA")
    sizes = {cfg.stat().st_size}
    env = dict(os.environ)
    env["PATH"] = f"{b}:{env['PATH']}"
    env["TMPDIR"] = str(tmp)
    env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
    env["SUTANDO_WORKSPACE_DIR"] = str(ws)
    env["SUTANDO_STANDBY_STOP_TIMEOUT"] = "1"
    env.pop("SUTANDO_INSTANCE_ID", None)
    env.pop("SUTANDO_TASK_EVENT_HANDLER", None)
    p = subprocess.Popen(
        ["bash", "src/watch-tasks-stream.sh", str(ws / "tasks"),
         "--role", "session", "--inbox", str(ws / "tasks")],
        cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, start_new_session=True)
    out: list[str] = []
    stub = None
    try:
        t0 = time.time()
        while time.time() - t0 < 15 and not list((ws / "state").glob("*.pid")):
            time.sleep(0.1)
        time.sleep(1.5)  # past the standby wait and the sweep
        # Hold delivery: the stub is the watcher's fswatch child.
        kids = subprocess.run(["pgrep", "-P", str(p.pid)], capture_output=True, text=True).stdout.split()
        stub = [k for k in kids if "fswatch" in subprocess.run(["ps", "-o", "command=", "-p", k],
                                                                capture_output=True, text=True).stdout]
        for k in stub:
            os.kill(int(k), 19)  # SIGSTOP
        publish("hB")
        sizes.add(cfg.stat().st_size)
        publish("hC")
        sizes.add(cfg.stat().st_size)
        final = ws / "tasks" / "task-team.txt"
        tt = final.with_name(".task-team.tmp")
        tt.write_text("id: task-team\naccess_tier: team\ntask: restricted\n")
        tt.replace(final)
        mtime = int(cfg.stat().st_mtime)
        for k in stub:
            os.kill(int(k), 18)  # SIGCONT
        stub = None
        os.set_blocking(p.stdout.fileno(), False)
        t0 = time.time()
        while time.time() - t0 < 12:
            time.sleep(0.3)
            try:
                c = p.stdout.read()
            except (BlockingIOError, TypeError):
                c = None
            if c:
                out.extend(c.splitlines())
            if log.exists() or any("task-team" in ln for ln in out):
                time.sleep(1.5)
                try:
                    c = p.stdout.read()
                except (BlockingIOError, TypeError):
                    c = None
                if c:
                    out.extend(c.splitlines())
                break
        handled = log.read_text().split() if log.exists() else []
        return out, handled, sizes, mtime
    finally:
        for k in stub or []:
            os.kill(int(k), 18)
        try:
            os.killpg(p.pid, 15)
        except (ProcessLookupError, PermissionError):
            p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


print("two same-second, equal-size atomic config replacements before the task's decision:")
out, handled, sizes, mtime = scenario_same_second_equal_size_replacements()
check("setup: every replacement had the same size and the same mtime",
      len(sizes) == 1 and mtime == 1_700_000_000, f"sizes={sizes!r} mtime={mtime}")
check("the task was routed by the SECOND replacement: never announced, handled once by hC",
      not any(ln.startswith("TASK_FILE: task-team") for ln in out)
      and handled == ["probe-hC", "handle-hC"],
      f"stdout={out!r} handler log={handled!r}")


def scenario_replacement_between_parse_and_stamp():
    """The parser is interposed: after it has parsed config A, and before any
    freshness stamp can be taken, the config is replaced atomically by C
    (must-handle), with no fswatch event for it. A task pending before start is
    then decided: it must be routed by C, never announced to the core."""
    import shutil
    tmp = Path(tempfile.mkdtemp(prefix="ready-parse-"))
    ws = tmp / "ws"
    (ws / "tasks").mkdir(parents=True)
    (ws / "results" / "archive").mkdir(parents=True)
    (ws / "state").mkdir()
    b = tmp / "bin"
    b.mkdir()
    (b / "fswatch").write_text(
        f"#!/bin/bash\nexec bash {REPO / 'tests' / 'fixtures' / 'fswatch-poll-stub.sh'} \"$@\"\n")
    (b / "fswatch").chmod(0o755)
    cfg = ws / "state" / "task-event-handler.json"
    log = tmp / "handler.log"
    handlers = {}
    for name, rc in (("hA", 3), ("hC", 4)):
        h = tmp / f"{name}.sh"
        h.write_text('#!/bin/sh\n'
                     f'for a in "$@"; do [ "$a" = "--probe" ] && {{ echo probe-{name} >> {log}; exit {rc}; }}; done\n'
                     f'echo handle-{name} >> {log}\nexit {rc}\n')
        h.chmod(0o755)
        handlers[name] = h
    cfg.write_text(json.dumps({"handler": str(handlers["hA"])}))
    (ws / "tasks" / "task-team.txt").write_text("id: task-team\naccess_tier: team\ntask: restricted\n")
    real_py = shutil.which("python3")
    swapped = tmp / "swapped"
    cfg_c = tmp / "cfg-c.json"
    cfg_c.write_text(json.dumps({"handler": str(handlers["hC"])}))
    # The interpreter the watcher is told to use: the real python, except that the
    # first config parse is followed by the atomic replacement A -> C.
    shim = tmp / "py-shim.sh"
    shim.write_text(
        '#!/bin/bash\n'
        'out="$(' + real_py + ' "$@")"; rc=$?\n'
        'case "$*" in *json*handler*)\n'
        f'  if [ ! -e {swapped} ]; then touch {swapped}; cp {cfg_c} {cfg}.tmp && mv {cfg}.tmp {cfg}; fi ;;\n'
        'esac\n'
        'printf \'%s\\n\' "$out"; exit $rc\n')
    shim.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{b}:{env['PATH']}"
    env["TMPDIR"] = str(tmp)
    env["SUTANDO_PY"] = str(shim)
    env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
    env["SUTANDO_WORKSPACE_DIR"] = str(ws)
    env["SUTANDO_STANDBY_STOP_TIMEOUT"] = "1"
    env.pop("SUTANDO_INSTANCE_ID", None)
    env.pop("SUTANDO_TASK_EVENT_HANDLER", None)
    p = subprocess.Popen(
        ["bash", "src/watch-tasks-stream.sh", str(ws / "tasks"),
         "--role", "session", "--inbox", str(ws / "tasks")],
        cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, start_new_session=True)
    out: list[str] = []
    try:
        os.set_blocking(p.stdout.fileno(), False)
        t0 = time.time()
        while time.time() - t0 < 15:
            time.sleep(0.3)
            try:
                c = p.stdout.read()
            except (BlockingIOError, TypeError):
                c = None
            if c:
                out.extend(c.splitlines())
            if log.exists() or any("task-team" in ln for ln in out):
                time.sleep(1.5)
                try:
                    c = p.stdout.read()
                except (BlockingIOError, TypeError):
                    c = None
                if c:
                    out.extend(c.splitlines())
                break
        handled = log.read_text().split() if log.exists() else []
        return out, handled, swapped.exists()
    finally:
        try:
            os.killpg(p.pid, 15)
        except (ProcessLookupError, PermissionError):
            p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


print("config replaced atomically after it was parsed and before its stamp was taken:")
out, handled, swapped = scenario_replacement_between_parse_and_stamp()
check("setup: the parser hook replaced the config once", swapped)
check("the pending task was routed by the replacement: never announced, handled once by hC",
      not any(ln.startswith("TASK_FILE: task-team") for ln in out)
      and handled == ["probe-hC", "handle-hC"],
      f"stdout={out!r} handler log={handled!r}")


def _watcher_env(tmp, ws, b, extra=None):
    env = dict(os.environ)
    env["PATH"] = f"{b}:{env['PATH']}"
    env["TMPDIR"] = str(tmp)
    env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
    env["SUTANDO_WORKSPACE_DIR"] = str(ws)
    env["SUTANDO_STANDBY_STOP_TIMEOUT"] = "1"
    env.pop("SUTANDO_INSTANCE_ID", None)
    env.pop("SUTANDO_TASK_EVENT_HANDLER", None)
    env.update(extra or {})
    return env


def _handlers(tmp, log, spec):
    out = {}
    for name, rc in spec:
        h = tmp / f"{name}.sh"
        h.write_text('#!/bin/sh\n'
                     f'for a in "$@"; do [ "$a" = "--probe" ] && {{ echo probe-{name} >> {log}; exit {rc}; }}; done\n'
                     f'echo handle-{name} >> {log}\nexit {rc}\n')
        h.chmod(0o755)
        out[name] = h
    return out


def _workspace(prefix):
    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    ws = tmp / "ws"
    (ws / "tasks").mkdir(parents=True)
    (ws / "results" / "archive").mkdir(parents=True)
    (ws / "state").mkdir()
    b = tmp / "bin"
    b.mkdir()
    (b / "fswatch").write_text(
        f"#!/bin/bash\nexec bash {REPO / 'tests' / 'fixtures' / 'fswatch-poll-stub.sh'} \"$@\"\n")
    (b / "fswatch").chmod(0o755)
    return tmp, ws, b


def _publish(cfg, handler):
    t = cfg.with_name(".cfg.tmp")
    t.write_text(json.dumps({"handler": str(handler)}))
    t.replace(cfg)


def _write_task(ws, name):
    final = ws / "tasks" / f"{name}.txt"
    t = final.with_name(f".{name}.tmp")
    t.write_text(f"id: {name}\naccess_tier: team\ntask: restricted\n")
    t.replace(final)


def _start(ws, env, stderr=subprocess.DEVNULL):
    return subprocess.Popen(
        ["bash", "src/watch-tasks-stream.sh", str(ws / "tasks"),
         "--role", "session", "--inbox", str(ws / "tasks")],
        cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=stderr,
        text=True, start_new_session=True)


def _pump(p, out, until, timeout=12, settle=1.5):
    os.set_blocking(p.stdout.fileno(), False)
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.3)
        try:
            c = p.stdout.read()
        except (BlockingIOError, TypeError):
            c = None
        if c:
            out.extend(c.splitlines())
        if until():
            time.sleep(settle)
            try:
                c = p.stdout.read()
            except (BlockingIOError, TypeError):
                c = None
            if c:
                out.extend(c.splitlines())
            return True
    return False


def _stop(p):
    try:
        os.killpg(p.pid, 15)
    except (ProcessLookupError, PermissionError):
        p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()


def _wait_ready(ws, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout and not list((ws / "state").glob("*.pid")):
        time.sleep(0.1)
    time.sleep(1.5)


def scenario_resolver_publishes_between_refresh_and_decision():
    """The inbox resolver, which runs between the start of dispatch_task and the
    routing decision, atomically publishes must-handle C over fallback B."""
    tmp, ws, b = _workspace("ready-resolver-")
    cfg = ws / "state" / "task-event-handler.json"
    log = tmp / "handler.log"
    h = _handlers(tmp, log, (("hB", 3), ("hC", 4)))
    _publish(cfg, h["hB"])
    cfg_c = tmp / "cfg-c.json"
    cfg_c.write_text(json.dumps({"handler": str(h["hC"])}))
    resolver = tmp / "resolver.sh"
    resolver.write_text('#!/bin/bash\n'
                        f'cp {cfg_c} {cfg}.tmp && mv {cfg}.tmp {cfg}\n'
                        'printf \'%s\\n\' "$1"\n')
    resolver.chmod(0o755)
    env = _watcher_env(tmp, ws, b, {"SUTANDO_INBOX_RESOLVER": str(resolver)})
    p = _start(ws, env)
    out: list[str] = []
    try:
        _wait_ready(ws)
        _write_task(ws, "task-team")
        _pump(p, out, lambda: log.exists() or any("task-team" in ln for ln in out))
        handled = log.read_text().split() if log.exists() else []
        return out, handled
    finally:
        _stop(p)


def scenario_runtime_dir_unwritable():
    """A must-handle config is published while the watcher's runtime dir cannot
    take the snapshot copy: the task is held, never announced, and handled once
    the dir is writable again (the read-timeout tick retries the reload)."""
    tmp, ws, b = _workspace("ready-broken-")
    cfg = ws / "state" / "task-event-handler.json"
    log = tmp / "handler.log"
    h = _handlers(tmp, log, (("hB", 3), ("hC", 4)))
    _publish(cfg, h["hB"])
    env = _watcher_env(tmp, ws, b, {"SUTANDO_HANDLER_POLL_INTERVAL": "1"})
    p = _start(ws, env)
    out: list[str] = []
    runtime = None
    try:
        _wait_ready(ws)
        runtime = next(iter(tmp.glob("sutando-task-watch.*")))
        os.chmod(runtime, 0o500)
        _publish(cfg, h["hC"])
        time.sleep(0.5)
        _write_task(ws, "task-team")
        _pump(p, out, lambda: log.exists() or any("task-team" in ln for ln in out), timeout=6, settle=0.5)
        held_out = list(out)
        held_log = log.read_text().split() if log.exists() else []
        os.chmod(runtime, 0o700)
        _pump(p, out, lambda: log.exists() or any("task-team" in ln for ln in out), timeout=12)
        handled = log.read_text().split() if log.exists() else []
        return held_out, held_log, out, handled
    finally:
        if runtime is not None:
            os.chmod(runtime, 0o700)
        _stop(p)


def scenario_cksum_fails_at_first():
    """The checksum tool fails on its first two calls; a config replacement
    made while it was failing must still route the task."""
    tmp, ws, b = _workspace("ready-cksum-")
    cfg = ws / "state" / "task-event-handler.json"
    log = tmp / "handler.log"
    h = _handlers(tmp, log, (("hA", 3), ("hC", 4)))
    _publish(cfg, h["hA"])
    import shutil
    real = shutil.which("cksum")
    counter = tmp / "cksum-calls"
    (b / "cksum").write_text('#!/bin/bash\n'
                             f'n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}\n'
                             '[ "$n" -le 2 ] && exit 1\n'
                             f'exec {real} "$@"\n')
    (b / "cksum").chmod(0o755)
    env = _watcher_env(tmp, ws, b)
    p = _start(ws, env)
    out: list[str] = []
    try:
        _wait_ready(ws)
        _publish(cfg, h["hC"])
        time.sleep(0.5)
        _write_task(ws, "task-team")
        _pump(p, out, lambda: log.exists() or any("task-team" in ln for ln in out))
        handled = log.read_text().split() if log.exists() else []
        calls = int(counter.read_text() or 0) if counter.exists() else 0
        return out, handled, calls
    finally:
        _stop(p)


def scenario_missing_config_stderr():
    """No config at all: the task reaches the core and the watcher's stderr
    carries only its own announce line, no error from the stamp's read."""
    tmp, ws, b = _workspace("ready-nocfg-")
    env = _watcher_env(tmp, ws, b)
    errf = tmp / "watcher.err"
    with open(errf, "w") as fh:
        p = _start(ws, env, stderr=fh)
    out: list[str] = []
    try:
        _wait_ready(ws)
        _write_task(ws, "task-owner")
        _pump(p, out, lambda: any("task-owner" in ln for ln in out))
    finally:
        _stop(p)
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
    must be held, then handled once the config is readable."""
    tmp, ws, b = _workspace("ready-unread-")
    cfg = ws / "state" / "task-event-handler.json"
    log = tmp / "handler.log"
    h = _handlers(tmp, log, (("hC", 4),))
    env = _watcher_env(tmp, ws, b, {"SUTANDO_HANDLER_POLL_INTERVAL": "1"})
    p = _start(ws, env)
    out: list[str] = []
    stub = []
    try:
        _wait_ready(ws)
        kids = subprocess.run(["pgrep", "-P", str(p.pid)], capture_output=True, text=True).stdout.split()
        stub = [k for k in kids if "fswatch" in subprocess.run(["ps", "-o", "command=", "-p", k],
                                                                capture_output=True, text=True).stdout]
        for k in stub:
            os.kill(int(k), 19)
        _write_task(ws, "task-team")
        _publish(cfg, h["hC"])
        os.chmod(cfg, 0)
        for k in stub:
            os.kill(int(k), 18)
        stub = []
        _pump(p, out, lambda: log.exists() or any("task-team" in ln for ln in out), timeout=6, settle=0.5)
        held_out = list(out)
        held_log = log.read_text().split() if log.exists() else []
        os.chmod(cfg, 0o644)
        _pump(p, out, lambda: log.exists() or any("task-team" in ln for ln in out), timeout=12)
        handled = log.read_text().split() if log.exists() else []
        return held_out, held_log, out, handled
    finally:
        for k in stub:
            os.kill(int(k), 18)
        try:
            os.chmod(cfg, 0o644)
        except OSError:
            pass
        _stop(p)


def scenario_held_task_own_event_after_recovery():
    """A task held while the config was unreadable receives its own file event
    once the config is readable again: it must be dispatched exactly once.
    Needs the host's fswatch (an Updated event on the same name)."""
    import shutil
    if not shutil.which("fswatch"):
        return None
    tmp, ws, b = _workspace("ready-held-")
    (b / "fswatch").unlink()
    cfg = ws / "state" / "task-event-handler.json"
    log = tmp / "handler.log"
    h = _handlers(tmp, log, (("hO", 0),))  # optional handler: accepts and runs, writes no result
    _publish(cfg, h["hO"])
    os.chmod(cfg, 0)
    env = _watcher_env(tmp, ws, b, {"SUTANDO_HANDLER_POLL_INTERVAL": "60"})
    p = _start(ws, env)
    out: list[str] = []
    try:
        _wait_ready(ws)
        _write_task(ws, "task-held")
        _pump(p, out, lambda: log.exists() or any("task-held" in ln for ln in out), timeout=5, settle=0.5)
        held_log = log.read_text().split() if log.exists() else []
        os.chmod(cfg, 0o644)
        time.sleep(1.0)
        with open(ws / "tasks" / "task-held.txt", "a") as fh:  # the held task's own Updated event
            fh.write("note: touched\n")
        _pump(p, out, lambda: log.exists(), timeout=12, settle=3.0)
        handled = log.read_text().split() if log.exists() else []
        return held_log, out, handled
    finally:
        try:
            os.chmod(cfg, 0o644)
        except OSError:
            pass
        _stop(p)


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


def scenario_held_task_under_a_busy_stream():
    """A held task must be retried on an elapsed deadline even when unrelated
    events arrive continuously (each one restarts the read timeout)."""
    tmp, ws, b = _workspace("ready-busy-")
    cfg = ws / "state" / "task-event-handler.json"
    log = tmp / "handler.log"
    h = _handlers(tmp, log, (("hC", 4),))
    _publish(cfg, h["hC"])
    os.chmod(cfg, 0)
    env = _watcher_env(tmp, ws, b, {"SUTANDO_HANDLER_POLL_INTERVAL": "30", "SUTANDO_HELD_RETRY_INTERVAL": "2"})
    p = _start(ws, env)
    out: list[str] = []
    try:
        _wait_ready(ws)
        _write_task(ws, "task-team")
        _pump(p, out, lambda: log.exists() or any("task-team" in ln for ln in out), timeout=3, settle=0.3)
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
        _stop(p)


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
    tmp, ws, b = _workspace("ready-feed-")
    feed = tmp / "feed"
    feed.write_text("")
    (b / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {feed}\n")
    (b / "fswatch").chmod(0o755)
    cfg = ws / "state" / "task-event-handler.json"
    log = tmp / "handler.log"
    h = _handlers(tmp, log, (("hO", 0),))
    _publish(cfg, h["hO"])
    os.chmod(cfg, 0)
    env = _watcher_env(tmp, ws, b, {"SUTANDO_HANDLER_POLL_INTERVAL": "60", "SUTANDO_HELD_RETRY_INTERVAL": "1"})
    p = _start(ws, env)
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
        _wait_ready(ws)
        _write_task(ws, "task-held")
        emit(real_tasks / "task-held.txt")
        _pump(p, out, lambda: log.exists() or any("task-held" in ln for ln in out), timeout=4, settle=0.3)
        held_log = log.read_text().split() if log.exists() else []
        os.chmod(cfg, 0o644)
        emit(ws / "state" / "noise-1")  # any event: the retry deadline is checked after it
        _pump(p, out, lambda: log.exists(), timeout=8, settle=1.0)
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
        _stop(p)


print("a held task is re-dispatched on recovery, then its own Created and Updated events arrive (feed-driven):")
held_log, first, out, handled = scenario_held_task_events_after_recovery_feed()
check("setup: the task was held first", held_log == [], f"handler log={held_log!r}")
check("recovery dispatched it once", first == ["probe-hO", "handle-hO"], f"handler log={first!r}")
check("its later Created and Updated events did not admit it again",
      handled == ["probe-hO", "handle-hO"] and not any(ln.startswith("TASK_FILE: task-held") for ln in out),
      f"stdout={out!r} handler log={handled!r}")

print(("FAILED — " + ", ".join(FAILURES)) if FAILURES else
      "PASS — a task admitted after readiness sees the handler config fswatch delivered before it")
sys.exit(1 if FAILURES else 0)
