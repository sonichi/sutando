#!/usr/bin/env python3
"""Unit test for src/runtime-api/rundir.py — the ONE run-dir/socket policy the
daemon and CLI share. Pins the documented resolution order on every branch
(in-process, so the coverage recorder sees the module the E2E daemon
subprocess hides):

  SUTANDO_RUN_DIR > darwin app-support run dir > $XDG_RUNTIME_DIR/sutando >
  ~/.sutando/run;  socket = SUTANDO_RUNTIME_SOCKET or
  <run_dir>/<(agent, instance) key>/runtime.sock (legacy flat socket honored
  only for the undeclared actor)

Run: python3 tests/runtime-api-rundir.test.py   (stdlib only)
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "rundir", REPO / "src" / "runtime-api" / "rundir.py")
rd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rd)

FAILS: list = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _clear():
    # The actor chain reads env AND the enrolled record, so a hermetic socket
    # assertion has to clear both halves — not just the run dir.
    for k in ("SUTANDO_RUN_DIR", "SUTANDO_RUNTIME_SOCKET", "XDG_RUNTIME_DIR",
              "SUTANDO_RUNTIME_STATE", "SUTANDO_AGENT_ID", "AGENT_MXID",
              "AGENT_ID", "SUTANDO_INSTANCE_ID"):
        os.environ.pop(k, None)


def main() -> int:
    saved = {k: os.environ.get(k) for k in
             ("SUTANDO_RUN_DIR", "SUTANDO_RUNTIME_SOCKET", "XDG_RUNTIME_DIR",
              "SUTANDO_RUNTIME_STATE", "SUTANDO_AGENT_ID", "AGENT_MXID",
              "AGENT_ID", "SUTANDO_INSTANCE_ID")}
    try:
        # 1. explicit override always wins, on any platform
        _clear()
        os.environ["SUTANDO_RUN_DIR"] = "/tmp/rt-override"
        check(rd.run_dir() == Path("/tmp/rt-override"),
              "SUTANDO_RUN_DIR override wins")

        # 2. darwin default
        _clear()
        rd.sys.platform = "darwin"
        check(rd.run_dir() == Path.home() / "Library" / "Application Support"
              / "space.ag2.app" / "run",
              "darwin → app-support run dir")

        # 3. linux + XDG_RUNTIME_DIR
        rd.sys.platform = "linux"
        os.environ["XDG_RUNTIME_DIR"] = "/run/user/501"
        check(rd.run_dir() == Path("/run/user/501") / "sutando",
              "linux + XDG_RUNTIME_DIR → $XDG_RUNTIME_DIR/sutando")

        # 4. last-resort portable fallback
        os.environ.pop("XDG_RUNTIME_DIR", None)
        check(rd.run_dir() == Path.home() / ".sutando" / "run",
              "no darwin, no XDG → ~/.sutando/run fallback")

        # 5. socket: env override wins; else derived from run_dir
        os.environ["SUTANDO_RUNTIME_SOCKET"] = "/tmp/x.sock"
        check(rd.socket_path() == "/tmp/x.sock",
              "SUTANDO_RUNTIME_SOCKET override wins")
        os.environ.pop("SUTANDO_RUNTIME_SOCKET", None)
        import tempfile
        rt2 = tempfile.mkdtemp(prefix="rt2-")
        os.environ["SUTANDO_RUN_DIR"] = rt2
        os.environ["SUTANDO_RUNTIME_STATE"] = rt2  # no enrolled record here
        check(rd.socket_path() == f"{rt2}/{rd.DEFAULT_ACTOR}/runtime.sock",
              "default socket = <run_dir>/<(actor, instance) key>/runtime.sock")
        # pre-M2 daemons/clients: flat legacy socket still honored for default
        legacy = Path(rt2) / "sutando-runtime.sock"
        legacy.touch()
        check(rd.socket_path() == str(legacy),
              "existing flat legacy socket wins for the default instance")
    finally:
        rd.sys.platform = __import__("sys").platform
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print(f"\n{'PASS — rundir policy green' if not FAILS else f'FAILED ({len(FAILS)})'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
