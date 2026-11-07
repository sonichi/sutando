#!/usr/bin/env python3
"""watcher_rearm: the inbox and re-arm command a session owes, and when the hint fires.

Run: python3 tests/watcher-rearm.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("watcher_rearm", REPO / "src" / "watcher_rearm.py")
wr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wr)

failures = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f": {detail}"))
    if not cond:
        failures.append(name)


def fake_run(stdout=None, raises=None):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if raises:
            raise raises
        return SimpleNamespace(stdout=stdout, returncode=0)
    return run, calls


real_run = subprocess.run
try:
    inbox, cmd = wr.target("/R", "/W", None)
    check("core inbox is <ws>/tasks", inbox == "/W/tasks", inbox)
    check("core command is absolute and tagged",
          cmd == 'bash "/R/src/watch-tasks-stream.sh" --role session --inbox "/W/tasks"', cmd)
    inbox, cmd = wr.target("/R", "/W", "w1")
    check("worker inbox is its deliveries dir", inbox == "/W/deliveries/w1", inbox)
    check("worker command is the launcher's", cmd == wr.WORKER_REARM, cmd)

    for out, want in (("no\n", "no"), ("yes extra\n", "yes"), ("maybe\n", "unknown"), ("", "unknown"), (None, "unknown")):
        subprocess.run, calls = fake_run(stdout=out)
        got = wr.session_verdict("/W/tasks", "/W/state")
        check(f"verdict {out!r} -> {want}", got == want, got)
    check("verdict asks role-present session for the inbox",
          calls and calls[0][2:] == ["role-present", "session", "--inbox", "/W/tasks", "--ready", "/W/state"], calls)
    subprocess.run, _ = fake_run(raises=subprocess.TimeoutExpired("x", 15))
    check("a probe that cannot run is unknown", wr.session_verdict("/W/tasks", "/W/state") == "unknown")

    subprocess.run, _ = fake_run(stdout="yes\n")
    check("held inbox: no hint", wr.session_start_context("/R", "/W", None) is None)
    subprocess.run, _ = fake_run(stdout="unknown\n")
    check("unknown: no hint", wr.session_start_context("/R", "/W", None) is None)
    subprocess.run, _ = fake_run(stdout="no\n")
    ctx = wr.session_start_context("/R", "/W", None)
    text = (ctx or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
    check("unwatched: SessionStart context", (ctx or {}).get("hookSpecificOutput", {}).get("hookEventName") == "SessionStart", ctx)
    check("hint names the exact command and timeout",
          'bash "/R/src/watch-tasks-stream.sh" --role session --inbox "/W/tasks"' in text and "1800000" in text, text)

    os.environ.pop("SUTANDO_INSTANCE_ID", None)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = wr.main(["target", "--repo", "/R", "--workspace", "/W"])
    check("main target prints inbox then command",
          rc == 0 and out.getvalue().splitlines() == ["/W/tasks", 'bash "/R/src/watch-tasks-stream.sh" --role session --inbox "/W/tasks"'],
          out.getvalue())
    out = io.StringIO()
    with redirect_stdout(out):
        rc = wr.main(["session-start", "--workspace", "/W", "--repo", "/R"])
    check("main session-start emits hook JSON", rc == 0 and json.loads(out.getvalue())["hookSpecificOutput"]["hookEventName"] == "SessionStart", out.getvalue())
    subprocess.run, _ = fake_run(stdout="yes\n")
    out = io.StringIO()
    with redirect_stdout(out):
        rc = wr.main(["session-start", "--repo", "/R", "--workspace", "/W"])
    check("main session-start is silent when held", rc == 0 and out.getvalue() == "", out.getvalue())
    subprocess.run, _ = fake_run(stdout="no\n")
    os.environ["SUTANDO_TASK_WATCH_MOD"] = "1"
    out = io.StringIO()
    with redirect_stdout(out):
        rc = wr.main(["session-start", "--repo", "/R", "--workspace", "/W"])
    os.environ.pop("SUTANDO_TASK_WATCH_MOD", None)
    check("a Mod-marked session with an unwatched inbox is still asked for a Monitor",
          rc == 0 and "Re-arm it NOW" in out.getvalue(), out.getvalue())

    # mod-ready: yes only on the marker AND a ready session watcher; a refused or absent Mod is no.
    naps = []
    subprocess.run, calls = fake_run(stdout="yes\n")
    check("mod-ready without the marker is no, and probes nothing",
          wr.mod_ready("/R", "/W", None, env={}, sleep=naps.append) == "no" and not calls)
    check("mod-ready with the marker and a ready watcher is yes",
          wr.mod_ready("/R", "/W", None, env={"SUTANDO_TASK_WATCH_MOD": "1"}, sleep=naps.append) == "yes"
          and calls[0][2:] == ["role-present", "session", "--inbox", "/W/tasks", "--ready", "/W/state"], calls)
    for verdict in ("no\n", "unknown\n"):
        naps.clear()
        subprocess.run, calls = fake_run(stdout=verdict)
        got = wr.mod_ready("/R", "/W", "w1", env={"SUTANDO_TASK_WATCH_MOD": "1"}, wait_s=30, sleep=naps.append)
        check(f"a Mod that refused or never came up ({verdict.strip()}) is no after the 30 s wait",
              got == "no" and len(naps) == 30 and calls[0][5] == "/W/deliveries/w1", (got, len(naps)))
    os.environ["SUTANDO_TASK_WATCH_MOD"] = "1"
    subprocess.run, _ = fake_run(stdout="yes\n")
    out = io.StringIO()
    with redirect_stdout(out):
        rc = wr.main(["mod-ready", "--repo", "/R", "--workspace", "/W"])
    os.environ.pop("SUTANDO_TASK_WATCH_MOD", None)
    check("main mod-ready prints the word", rc == 0 and out.getvalue() == "yes\n", out.getvalue())

    check("core watch-argv is the target's command, one argument each",
          wr.watch_argv("/R", "/W", None, {}) == ["bash", "/R/src/watch-tasks-stream.sh", "/W/tasks", "--role", "session", "--inbox", "/W/tasks"])
    check("worker watch-argv takes the launcher's command and the target's inbox",
          wr.watch_argv("/R", "/W", "w1", {"SUTANDO_WATCHER_CMD": "/X/w.sh"})
          == ["bash", "/X/w.sh", "/W/deliveries/w1", "--role", "session", "--inbox", "/W/deliveries/w1"])
    try:
        wr.watch_argv("/R", "/W", "w1", {})
        check("a worker without SUTANDO_WATCHER_CMD is refused", False)
    except ValueError:
        check("a worker without SUTANDO_WATCHER_CMD is refused", True)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = wr.main(["watch-argv", "--repo", "/R", "--workspace", "/W"])
    check("main watch-argv prints one argument per line", rc == 0 and out.getvalue().splitlines()[1:3] == ["/R/src/watch-tasks-stream.sh", "/W/tasks"], out.getvalue())
    os.environ["SUTANDO_INSTANCE_ID"] = "w1"
    os.environ.pop("SUTANDO_WATCHER_CMD", None)
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        rc = wr.main(["watch-argv", "--repo", "/R", "--workspace", "/W"])
    os.environ.pop("SUTANDO_INSTANCE_ID", None)
    check("main watch-argv fails loudly for a worker with no command", rc == 1 and "SUTANDO_WATCHER_CMD" in err.getvalue(), err.getvalue())
    for bad in ([], ["bogus", "--repo", "/R", "--workspace", "/W"], ["target", "--repo", "/R"],
                ["target", "--repo"], ["target", "--nope", "x", "--workspace", "/W"]):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = wr.main(bad)
        check(f"usage error {bad!r} -> 64", rc == 64 and "usage" in err.getvalue(), (rc, err.getvalue()))
finally:
    subprocess.run = real_run

print("PASS" if not failures else f"FAIL ({len(failures)})")
sys.exit(1 if failures else 0)
