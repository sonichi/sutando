"""Shared subprocess-management helpers for the readiness-window suite
(split across watch-tasks-stream-readiness-window-*.test.py so no single
file pins a CI leg — see #4627). Behavior is copied verbatim from the
original combined file; nothing here changes what any scenario does.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from clean_watcher_env import clean_env  # noqa: E402


def watcher_env(tmp, ws, b, extra=None):
    # clean_env(), not dict(os.environ): a live pool worker's real SUTANDO_*
    # state (SUTANDO_INBOX_RESOLVER, ...) otherwise leaks into the watcher
    # subprocess under test. See #4649.
    env = clean_env()
    env["PATH"] = f"{b}:{env['PATH']}"
    env["TMPDIR"] = str(tmp)
    env["SUTANDO_RESULTS_DIR"] = str(ws / "results")
    env["SUTANDO_WORKSPACE_DIR"] = str(ws)
    env["SUTANDO_STANDBY_STOP_TIMEOUT"] = "1"
    env.update(extra or {})
    return env


def handlers(tmp, log, spec):
    out = {}
    for name, rc in spec:
        h = tmp / f"{name}.sh"
        h.write_text('#!/bin/sh\n'
                     f'for a in "$@"; do [ "$a" = "--probe" ] && {{ echo probe-{name} >> {log}; exit {rc}; }}; done\n'
                     f'echo handle-{name} >> {log}\nexit {rc}\n')
        h.chmod(0o755)
        out[name] = h
    return out


def workspace(prefix):
    import tempfile
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


def publish(cfg, handler):
    t = cfg.with_name(".cfg.tmp")
    t.write_text(json.dumps({"handler": str(handler)}))
    t.replace(cfg)


def write_task(ws, name):
    final = ws / "tasks" / f"{name}.txt"
    t = final.with_name(f".{name}.tmp")
    t.write_text(f"id: {name}\naccess_tier: team\ntask: restricted\n")
    t.replace(final)


def start(ws, env, stderr=subprocess.DEVNULL):
    return subprocess.Popen(
        ["bash", "src/watch-tasks-stream.sh", str(ws / "tasks"),
         "--role", "session", "--inbox", str(ws / "tasks")],
        cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=stderr,
        text=True, start_new_session=True)


def pump(p, out, until, timeout=12, settle=1.5):
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


def stop(p):
    try:
        os.killpg(p.pid, 15)
    except (ProcessLookupError, PermissionError):
        p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()


def wait_ready(ws, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout and not list((ws / "state").glob("*.pid")):
        time.sleep(0.1)
    time.sleep(1.5)


def feed_start(tmp, ws, b, env):
    """A feed-driven fswatch stand-in: the test emits event lines itself.
    Returns (process, emit, real_tasks_dir); readiness is already proven."""
    feed = tmp / "feed"
    feed.write_text("")
    (b / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {feed}\n")
    (b / "fswatch").chmod(0o755)
    p = start(ws, env)
    real_tasks = Path(os.path.realpath(ws / "tasks"))

    def emit(path):
        with open(feed, "a") as fh:
            fh.write(f"{path}\n")

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
    return p, emit, real_tasks
