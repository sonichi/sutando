#!/usr/bin/env python3
"""core_heartbeat: `.alive` must report the CORE's pid, and stop beating when the core is gone.

Regression for 2026-08-01. `write_beat` wrote `os.getpid()` — the *writer's* pid — while the
module docstring already claimed the field was the core's. The writer is started detached by
startup.sh (PPID 1), is never killed by restart.sh, and is only started `if ! pgrep`, so it
outlives every core restart. Consequence: `.alive` kept a FRESH mtime carrying a pid that was
never the core's, and a DEAD core read as healthy. Confirmed on two hosts (Pro: writer ELAPSED
2-01:49 spanning several core restarts).

Why it is urgent rather than tidy: #2333's review asks to prove a restarted service "publishes a
healthy heartbeat". Against the pre-fix instrument that clause CANNOT FAIL, so restart evidence
could come back green for the wrong reason.

Hermetic by construction — `_alive_path` is redirected into a tmpdir, so nothing here can touch a
real workspace (`$SUTANDO_WORKSPACE` is no longer honored as of v0.8, so setting it would NOT
isolate; the resolved path is what matters).
"""
import importlib, json, os, subprocess, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
ch = importlib.import_module("core_heartbeat")
_REAL_CORE_PID = ch.core_pid   # kept: the monkeypatches below would otherwise
                              # make the "real function" tests exercise a fake

_fails = []
def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        _fails.append(name)


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    alive = tmp / "testhost.alive"
    ch.CORES_DIR = tmp
    ch._alive_path = lambda: alive
    # Prove the redirect actually took, or every assertion below is vacuous.
    check("ISOLATION: resolved .alive path is inside the tmpdir",
          str(ch._alive_path()).startswith(str(tmp)))

    # --- payload: pid is the CORE's, writer's pid kept separately ------------
    ch.core_pid = lambda socket_path=None: 424242
    ch.write_beat()
    rec = json.loads(alive.read_text())
    check("pid is the CORE's pane pid (424242)", rec.get("pid") == 424242)
    check("pid is NOT the writer's pid (the pre-fix behaviour)", rec.get("pid") != os.getpid())
    check("heartbeat_pid records the writer", rec.get("heartbeat_pid") == os.getpid())
    check("pid and heartbeat_pid are different processes", rec["pid"] != rec["heartbeat_pid"])
    check("schema_version advertises the contract change", rec.get("schema_version", 0) >= 3)
    check("socket is still recorded (unchanged contract)", "socket" in rec)

    # --- fail-open: tmux unusable must not blank the field or stop reporting -
    ch.core_pid = lambda socket_path=None: None
    alive.unlink()
    ch.write_beat()
    rec2 = json.loads(alive.read_text())
    check("fail-open: a beat is still written when the core pid is unknown", alive.exists())
    check("fail-open: pid falls back to the writer's own", rec2.get("pid") == os.getpid())

    # --- the loop stops once an OBSERVED core disappears ---------------------
    seen = {"n": 0}
    def vanishing(socket_path=None):
        seen["n"] += 1
        return 999999 if seen["n"] <= 2 else None
    ch.core_pid = vanishing
    ch.write_beat = lambda status="running": None
    alive.write_text("{}")
    rc = ch.run_forever(interval=0.01)
    check("run_forever returns 0 when the core vanishes", rc == 0)
    check("it removed .alive so readers see the core leave", not alive.exists())

    # --- CONTROL: a core that stays PRESENT must NOT trigger the vanish path -
    # Without this, "stops beating when the core is gone" would also pass if the
    # loop simply stopped unconditionally — the assertion has to be able to fail
    # in the opposite direction too.
    calls = {"n": 0}
    def live(socket_path=None):
        calls["n"] += 1
        if calls["n"] > 3:          # let it beat a few times, then ask it to stop
            ch._SHUTDOWN_REQUESTED = True
        return 111111
    ch.core_pid = live
    ch._SHUTDOWN_REQUESTED = False
    alive.write_text("{}")
    rc2 = ch.run_forever(interval=0.01)
    check("CONTROL: a live core exits via the shutdown flag, not the vanish path", rc2 == 0)
    check("CONTROL: .alive SURVIVES while the core is present", alive.exists())

# --- core_pid() itself: real subprocess, fake tmux on PATH ------------------
ch.core_pid = _REAL_CORE_PID   # undo the monkeypatches; without this these two
                               # assertions would call the stub and pass vacuously
with tempfile.TemporaryDirectory() as td2:
    b = Path(td2) / "bin"; b.mkdir()
    (b / "tmux").write_text("#!/bin/sh\necho 31337\n"); (b / "tmux").chmod(0o755)
    os.environ["PATH"] = f"{b}:{os.environ['PATH']}"
    check("core_pid() parses the pane pid from tmux", _REAL_CORE_PID("/tmp/x.sock") == 31337)
    (b / "tmux").write_text("#!/bin/sh\nexit 1\n")
    check("core_pid() returns None when tmux fails (no server / socket gone)",
          _REAL_CORE_PID("/tmp/x.sock") is None)

print()
if _fails:
    print(f"{len(_fails)} test(s) FAILED: {_fails}")
    sys.exit(1)
print("all tests passed — .alive reports the core's pid and stops when the core is gone")
