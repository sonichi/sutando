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
