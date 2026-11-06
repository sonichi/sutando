#!/usr/bin/env python3
"""A pin vetoes the watcher REMEDY -- and must not suppress the DIAGNOSIS.

Two failures lived here, on adjacent arms:

1. `check_voice_watchers` prescribed "-- restart voice-agent" identically
   whether or not a pin was armed. That is the remedy a pin exists to forbid.

2. Both voice checks return early when the voice row is not "ok". On the
   natural pinned path the row is non-ok, so the watcher parse never ran and
   the finding vanished entirely -- the pin suppressing the DIAGNOSIS, which
   is the inverse of what it is for. A pinned process is RUNNING, so the parse
   still holds.
"""
import importlib.util
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
fail = 0


def check(cond, label):
    global fail
    print(("PASS: " if cond else "FAIL: ") + label)
    if not cond:
        fail = 1


spec = importlib.util.spec_from_loader("hc", loader=None)
hc = importlib.util.module_from_spec(spec)
hc.__file__ = str(REPO / "src" / "health-check.py")
sys.path.insert(0, str(REPO / "src"))
exec(compile((REPO / "src" / "health-check.py").read_text(),
             str(REPO / "src" / "health-check.py"), "exec"), hc.__dict__)

tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-watcher-veto-"))
log = tmp / "voice-agent.log"
# A boot banner with ONE watcher registered: two are genuinely missing.
log.write_text("Sutando — Voice Interface\nWatching for results\n")
hc._voice_log_path = lambda: log

VETO = "DO NOT RESTART voice-agent pid 4242 — witness armed in this process"

# --- CONTROL: ok + unpinned -> the remedy must still be prescribed ----------
plain = hc.check_voice_watchers({"name": "voice-agent", "status": "ok"})
check(plain["status"] == "fail", f"control: missing watchers detected ({plain['status']})")
check("restart voice-agent" in plain["detail"], "control: remedy IS prescribed")
check(not plain.get("restart_veto"), "control: carries no veto")

# --- ARMED + ok -> finding stays, remedy withdrawn -------------------------
armed = hc.check_voice_watchers(
    {"name": "voice-agent", "status": "ok", "restart_veto": VETO})
check(armed["status"] == "fail", f"armed: the finding SURVIVES ({armed['status']})")
check("missing watcher(s)" in armed["detail"], "armed: and names what is missing")
check("restart voice-agent" not in armed["detail"], "armed: the remedy is withdrawn")
check(armed.get("restart_veto") == VETO, "armed: veto carried to the action boundary")

# --- ARMED + non-ok row -> the parse must STILL run ------------------------
# This is the natural pinned path, and the one that used to return early.
pinned_stale = hc.check_voice_watchers(
    {"name": "voice-agent", "status": "stale", "restart_veto": VETO})
check(pinned_stale["status"] == "fail",
      f"pinned+stale: diagnosis is NOT suppressed (got {pinned_stale['status']})")
check("missing watcher(s)" in pinned_stale["detail"],
      "pinned+stale: the watcher finding is still reported")

# --- CONTROL: non-ok and UNPINNED still short-circuits, as designed --------
dep = hc.check_voice_watchers({"name": "voice-agent", "status": "stale"})
check(dep["status"] == "warn",
      f"control: unpinned non-ok still defers to the dependency ({dep['status']})")

if fail:
    print("FAIL: the pin does not veto the watcher remedy correctly")
    sys.exit(1)
print("PASS: the pin vetoes the watcher remedy and preserves the diagnosis.")
