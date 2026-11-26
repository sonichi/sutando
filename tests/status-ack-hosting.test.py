#!/usr/bin/env python3
"""Where the status-ack loop runs: sparrowd supervises it from the skill's own manifest declaration,
and it ships off.

  a) the shipped manifest declares a supervised_worker, and sparrowd skips it until its
     interpreter is configured (STATUS_ACK_PYTHON is empty in the manifest)
  b) with the interpreter configured, sparrowd supervises the skill's worker script
  c) the hosted loop while off: every pass reads and writes nothing, and it keeps running
     (a supervised worker that exits is restarted in a loop)
  d) the worker entry, started as sparrowd starts it, is still running and has written
     nothing after its first pass while off
  CONTROL (c): the same loop enabled publishes the line

Run: python3 tests/status-ack-hosting.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_env import isolated_child_env  # noqa: E402
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
from _helpers import proactive_lane as pl  # noqa: E402
from _helpers import status_ack_env as sa  # noqa: E402

ISO = pl.scratch("status-ack-hosting-")
check = pl.Checks()
SKILL = sa.REPO / "skills" / "status-ack"
TID = "task-1761300000001"


def sparrowd():
    spec = importlib.util.spec_from_file_location("sparrowd_for_status_ack", sa.REPO / "src" / "sparrowd.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tree(ws: Path) -> list:
    return sorted(str(p.relative_to(ws)) for p in ws.rglob("*"))


def main() -> int:
    manifest = json.loads((SKILL / "manifest.json").read_text())
    decl = manifest.get("supervised_worker") or {}
    check(decl.get("script") == "scripts/status_ack_worker.py" and manifest["config"].get("STATUS_ACK_PYTHON") == ""
          and manifest["config"]["STATUS_ACK_ENABLED"] == "0" and manifest["enabled"] is False,
          f"a) declared as a supervised worker, interpreter unset, off ({decl})")

    sd = sparrowd()
    saved = os.environ.pop("STATUS_ACK_PYTHON", None)
    try:
        specs, skipped = sd._skill_worker_specs()
        check(not any(s.name == "status-ack" for s in specs)
              and any(w.startswith("status-ack: no interpreter configured: set STATUS_ACK_PYTHON") for w in skipped),
              f"a) unconfigured: sparrowd skips it with the reason ({[w for w in skipped if 'status-ack' in w]})")
        os.environ["STATUS_ACK_PYTHON"] = sys.executable
        specs, _ = sd._skill_worker_specs()
        spec = next((s for s in specs if s.name == "status-ack"), None)
        check(spec is not None and spec.argv == [sys.executable, str(SKILL / "scripts" / "status_ack_worker.py")],
              f"b) configured: sparrowd supervises the skill's worker ({spec})")
    finally:
        os.environ.pop("STATUS_ACK_PYTHON", None)
        if saved is not None:
            os.environ["STATUS_ACK_PYTHON"] = saved

    mod = sa.runner()
    lane = pl.Lane(gb, ISO)
    sa.task(lane.ws, TID)
    before, reports, sleeps = tree(lane.ws), [], []
    off = mod.settings(environ={})
    mod.serve(lane.ws, lambda: off, passes=4, clock=lambda: sa.T0 + 10_000, sleep=sleeps.append,
              holders=sa.STANDBY, emit=reports.append)
    lane.drain()
    check(reports == [{"enabled": False}] * 4 and sleeps == [off.poll_s] * 4 and tree(lane.ws) == before
          and lane.posts == [], f"c) off: four passes, nothing read or written ({reports}, {sleeps})")
    mod.serve(lane.ws, lambda: sa.on(mod), passes=1, clock=lambda: sa.T0 + 10_000, sleep=lambda s: None,
              holders=sa.SESSION, emit=reports.append)
    lane.drain()
    check(len(sa.room_posts(lane)) == 1, "CONTROL (c): the same loop enabled publishes the line")

    spec = importlib.util.spec_from_file_location("status_ack_worker_under_test", SKILL / "scripts" / "status_ack_worker.py")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    served = []
    worker.status_ack.serve = served.append
    worker.resolve_workspace = lambda migrate=True: ISO / "resolved-ws"
    rc = (worker.main(["--workspace", str(ISO / "given-ws")]), worker.main([]))
    check(rc == (0, 0) and served == [ISO / "given-ws", ISO / "resolved-ws"],
          f"d) the worker serves the given workspace, else the resolved one ({served})")

    ws = ISO / "worker-ws"
    (ws / "tasks").mkdir(parents=True)
    sa.task(ws, TID)
    before = tree(ws)
    env = {k: v for k, v in isolated_child_env().items() if not k.startswith("STATUS_ACK_")}
    proc = subprocess.Popen([sys.executable, str(SKILL / "scripts" / "status_ack_worker.py"), "--workspace", str(ws)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        line, deadline = "", time.time() + 30
        while not line and time.time() < deadline and proc.poll() is None:
            line = proc.stdout.readline()
        running = proc.poll() is None
    finally:
        proc.kill()
        _, err = proc.communicate()
    check(running and json.loads(line or "{}") == {"enabled": False} and tree(ws) == before,
          f"d) the worker entry keeps running while off and writes nothing ({line!r}, {err[-300:]!r})")
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())
