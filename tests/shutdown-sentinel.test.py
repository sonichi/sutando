#!/usr/bin/env python3
"""Graceful-shutdown sentinel helper (owner ask 2026-07-17).

The sentinel is the durable "shutting down on purpose, not crashing" signal the
core loop / bridges check to exit cleanly. Guards the mark/clear/check/info
lifecycle + the CLI used by restart.sh and startup.sh.

Run: python3 tests/shutdown-sentinel.test.py   (exit 0/1)
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import contextlib
import io
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("shutdown_mod", REPO / "src" / "shutdown.py")
sd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sd)

# Cover the REAL _sentinel_path() once (it's replaced with a temp path below).
_real_path = sd._sentinel_path()
assert str(_real_path).endswith("state/shutdown.sentinel"), _real_path

# Redirect the sentinel to a temp path (never touch the real state dir).
_tmp = Path(tempfile.mkdtemp(prefix="shutdown-")) / "shutdown.sentinel"
sd._sentinel_path = lambda: _tmp

failures: list[str] = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


# clean slate
check("not shutting down initially", sd.is_shutting_down() is False)
check("info is None when absent", sd.shutdown_info() is None)

# mark
sd.mark_shutdown("restart.sh --stop-only")
check("is_shutting_down after mark", sd.is_shutting_down() is True)
info = sd.shutdown_info()
check("info carries reason", info and info.get("reason") == "restart.sh --stop-only")
check("info carries a timestamp", info and isinstance(info.get("ts"), int) and info["ts"] > 0)

# mark is idempotent (overwrite, still exactly one sentinel)
sd.mark_shutdown("again")
check("mark overwrites reason", sd.shutdown_info().get("reason") == "again")

# clear
sd.clear_shutdown()
check("cleared → not shutting down", sd.is_shutting_down() is False)
check("clear is idempotent (no raise when absent)", (sd.clear_shutdown() or True))

# corrupt sentinel → info degrades, doesn't raise
_tmp.write_text("{not json")
check("corrupt sentinel still reads as shutting-down", sd.is_shutting_down() is True)
check("corrupt sentinel info degrades gracefully", sd.shutdown_info().get("reason") == "unknown")
_tmp.unlink()

# ── CLI dispatch (in-process so coverage counts; uses the patched temp path) ──
sd.clear_shutdown()
check("main('check') → 1 when not shutting down", sd.main(["shutdown.py", "check"]) == 1)
check("main('mark') → 0 and writes sentinel",
      sd.main(["shutdown.py", "mark", "cli-test"]) == 0 and sd.is_shutting_down())
check("main('mark') recorded the reason", sd.shutdown_info().get("reason") == "cli-test")
check("main('check') → 0 when shutting down", sd.main(["shutdown.py", "check"]) == 0)
check("main('clear') → 0 and removes sentinel",
      sd.main(["shutdown.py", "clear"]) == 0 and not sd.is_shutting_down())
check("main() with no arg defaults to check", sd.main(["shutdown.py"]) == 1)
check("main('bogus') → 2 usage", sd.main(["shutdown.py", "bogus"]) == 2)

# ── the instance gate: what the default core scope marks ──
# Derived from the SAME redirected resolver, so it lands in the temp dir too.
_inst = sd._gate_path("instance")
check("the instance gate is a different file in the same state dir",
      _inst != _tmp and _inst.parent == _tmp.parent and _inst.name.endswith(".shutdown.sentinel"))
sd.clear_shutdown()
sd.mark_shutdown("restart.sh", gate="instance")
check("mark --gate instance writes the instance gate, not the workspace one",
      _inst.exists() and not _tmp.exists())
check("is_shutting_down sees the instance gate", sd.is_shutting_down() is True)
check("info comes from the instance gate", sd.shutdown_info().get("reason") == "restart.sh")
sd.clear_shutdown(gate="workspace")
check("clear --gate workspace leaves the instance gate alone", _inst.exists())
sd.clear_shutdown(gate="instance")
check("clear --gate instance removes it", not _inst.exists() and sd.is_shutting_down() is False)
sd.mark_shutdown("a", gate="instance"); sd.mark_shutdown("b")
sd.clear_shutdown()
check("a bare clear (launcher boot) removes BOTH gates", not _inst.exists() and not _tmp.exists())
try:
    sd._gate_path("bogus")
    check("an unknown gate raises, never resolves to a guess", False)
except ValueError:
    check("an unknown gate raises, never resolves to a guess", True)
# An identity that cannot be resolved must not turn a reader into a crash: the
# workspace-wide gate still answers.
_real_igp = sd.instance_shutdown_gate_path
sd.instance_shutdown_gate_path = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no runtime-api"))
try:
    sd.mark_shutdown("ws-only")
    check("with an unresolvable instance the workspace gate still reads as shutting down",
          sd.is_shutting_down() is True)
    check("...and a bare clear still clears the workspace gate", (sd.clear_shutdown() or True)
          and sd.is_shutting_down() is False)
finally:
    sd.instance_shutdown_gate_path = _real_igp
_out = io.StringIO()
with contextlib.redirect_stdout(_out):
    _rc = sd.main(["shutdown.py", "mark", "restart.sh --stop-only", "--gate", "instance"])
check("main('mark --gate instance') → 0 and writes the instance gate", _rc == 0 and _inst.exists())
check("main('check') → 0 on an instance gate alone", sd.main(["shutdown.py", "check"]) == 0)
_out = io.StringIO()
with contextlib.redirect_stdout(_out):
    _rc = sd.main(["shutdown.py", "path", "--gate", "instance"])
check("main('path --gate instance') prints the instance gate", _rc == 0 and _out.getvalue().strip() == str(_inst))
check("main('clear --gate instance') → 0 and removes it",
      sd.main(["shutdown.py", "clear", "--gate", "instance"]) == 0 and not _inst.exists())
_out = io.StringIO()
with contextlib.redirect_stdout(_out):
    _rc = sd.main(["shutdown.py", "path", "--state-dir", str(_tmp.parent / "elsewhere")])
check("main('path --state-dir D') resolves under D, not the ambient workspace",
      _rc == 0 and _out.getvalue().strip() == str(_tmp.parent / "elsewhere" / "shutdown.sentinel"))
check("main('mark --gate bogus') → 2 usage", sd.main(["shutdown.py", "mark", "x", "--gate", "bogus"]) == 2)

# `path` is a contract the shell launchers depend on: they stash/restore the
# sentinel around a launch and must not re-derive its location themselves.
_out = io.StringIO()
with contextlib.redirect_stdout(_out):
    _rc = sd.main(["shutdown.py", "path"])
check("main('path') → 0", _rc == 0)
check("main('path') prints the resolved sentinel path, not a re-derived one",
      _out.getvalue().strip() == str(sd._sentinel_path()))

# Drives the model gate with the REAL is_shutting_down(), not a reimplementation:
# that is what makes this a before/after proof instead of restating the fix.
def _intake_accepts_new_task() -> bool:
    """The decision shared by the watcher emit + the loop's top-of-pass check:
    accept/surface a NEW task ONLY when not shutting down."""
    return not sd.is_shutting_down()


sd.clear_shutdown()
_before = _intake_accepts_new_task()          # BEFORE: no sentinel → task flows to the core
sd.mark_shutdown("restart.sh --stop-only")
_after = _intake_accepts_new_task()           # AFTER: sentinel → deferred, no mid-shutdown orphan
check("BEFORE shutdown — a new task is accepted (flows to the core loop)", _before is True)
check("AFTER shutdown — a new task is deferred, not handed to a dying core", _after is False)
sd.clear_shutdown()
check("after clear on boot — task intake resumes (no permanent suppression)",
      _intake_accepts_new_task() is True)

print()
if failures:
    print(f"FAIL — {len(failures)}: {failures}")
    sys.exit(1)
print("PASS — shutdown sentinel")
