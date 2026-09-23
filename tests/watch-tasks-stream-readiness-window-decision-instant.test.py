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
to route. A fifth replaces the config between its parse and its stamp. A
sixth pins the file identity to one usable line even when `stat` behaves like
GNU coreutils.

Split from the original combined readiness-window file (#4627: that file was
the single heaviest suite in the python corpus and set the CI floor for every
PR) -- this piece and its siblings (…-unreadable-config, …-held-task-recovery)
share tests/fixtures/readiness_window_helpers.py. No scenario's behavior
changed in the split.

Run: python3 tests/watch-tasks-stream-readiness-window-decision-instant.test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from readiness_window_helpers import (  # noqa: E402
    REPO, watcher_env, handlers, workspace, publish, write_task, start,
    pump, stop, wait_ready,
)

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

        def write_task_local(name):
            final = ws / "tasks" / f"{name}.txt"
            t = final.with_name(f".{name}.tmp")
            t.write_text(f"id: {name}\naccess_tier: team\ntask: restricted\n")
            t.replace(final)

        os.set_blocking(p.stdout.fileno(), False)

        def pump_local(until, timeout=12):
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
            write_task_local("task-team")
            pump_local(lambda: log.exists() or any("task-team" in ln for ln in out))
        else:
            write_task_local("task-team")
            pump_local(lambda: any("TASK_FILE: task-team" in ln for ln in out))
            publish_config()
            time.sleep(0.5)
            write_task_local("task-next")
            pump_local(lambda: log.exists() or any("task-next" in ln for ln in out))
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
    handlers_local = {}
    for name, rc in (("hA", 3), ("hB", 3), ("hC", 4)):
        h = tmp / f"{name}.sh"
        h.write_text('#!/bin/sh\n'
                     f'for a in "$@"; do [ "$a" = "--probe" ] && {{ echo probe-{name} >> {log}; exit {rc}; }}; done\n'
                     f'echo handle-{name} >> {log}\nexit {rc}\n')
        h.chmod(0o755)
        handlers_local[name] = h
    fixed = 1_700_000_000  # one mtime for every replacement

    def publish_local(name):
        t = cfg.with_name(f".cfg-{name}.tmp")
        t.write_text(json.dumps({"handler": str(handlers_local[name])}))
        os.utime(t, (fixed, fixed))
        t.replace(cfg)

    publish_local("hA")
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
        publish_local("hB")
        sizes.add(cfg.stat().st_size)
        publish_local("hC")
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
    handlers_local = {}
    for name, rc in (("hA", 3), ("hC", 4)):
        h = tmp / f"{name}.sh"
        h.write_text('#!/bin/sh\n'
                     f'for a in "$@"; do [ "$a" = "--probe" ] && {{ echo probe-{name} >> {log}; exit {rc}; }}; done\n'
                     f'echo handle-{name} >> {log}\nexit {rc}\n')
        h.chmod(0o755)
        handlers_local[name] = h
    cfg.write_text(json.dumps({"handler": str(handlers_local["hA"])}))
    (ws / "tasks" / "task-team.txt").write_text("id: task-team\naccess_tier: team\ntask: restricted\n")
    real_py = shutil.which("python3")
    swapped = tmp / "swapped"
    cfg_c = tmp / "cfg-c.json"
    cfg_c.write_text(json.dumps({"handler": str(handlers_local["hC"])}))
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


def scenario_gnu_stat_shim_two_tasks():
    """A `stat` that behaves like GNU coreutils (`-f` prints a multi-line
    filesystem dump and exits 1; `-c '%i'` works): two distinct tasks must both
    be admitted, and the recorded identity must be one line."""
    tmp, ws, b = workspace("ready-gnustat-")
    (b / "stat").write_text(
        '#!/bin/bash\n'
        'case "$1" in\n'
        '  -f) printf "  File: \\"%s\\"\\n    ID: 1 Namelen: 255 Type: apfs\\nBlock size: 4096  Fundamental block size: 4096\\n'
        'Blocks: Total: 1 Free: 1 Available: 1\\nInodes: Total: 1 Free: 1\\n" "$2"; exit 1 ;;\n'
        '  -c) printf "%s\\n" "$(ls -di -- "$3" | awk \'{print $1}\')" ;;\n'
        '  *) exit 1 ;;\n'
        'esac\n')
    (b / "stat").chmod(0o755)
    env = watcher_env(tmp, ws, b)
    errf = tmp / "watcher.err"
    with open(errf, "w") as fh:
        p = start(ws, env, stderr=fh)
    out: list[str] = []
    try:
        wait_ready(ws)
        write_task(ws, "task-one")
        pump(p, out, lambda: any("task-one" in ln for ln in out), timeout=6, settle=0.3)
        write_task(ws, "task-two")
        pump(p, out, lambda: any("task-two" in ln for ln in out), timeout=6, settle=0.5)
    finally:
        stop(p)
    err = errf.read_text()
    return out, err


print("a GNU-like stat on PATH (`-f` dumps the filesystem and exits 1):")
out, err = scenario_gnu_stat_shim_two_tasks()
check("two distinct tasks were both admitted, once each",
      sum(ln.startswith("TASK_FILE: task-one") for ln in out) == 1
      and sum(ln.startswith("TASK_FILE: task-two") for ln in out) == 1,
      f"stdout={out!r}")
check("no task was dispatched without an identity (the identity was one usable line)",
      "no usable file identity" not in err, f"stderr={err!r}")

print(("FAILED — " + ", ".join(FAILURES)) if FAILURES else
      "PASS — decision-instant, config-staleness and identity cases")
sys.exit(1 if FAILURES else 0)
