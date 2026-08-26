#!/usr/bin/env python3
"""A veto already established must survive a FAILED re-probe, and must reach
the --fix boundary through the quota consumers.

Two structured-state-to-flat-value defects, same shape:

  check_voice_transport  re-derives the veto from a second `_proc_lstarts`.
  That helper fails CLOSED to ([], {}) on TimeoutExpired/OSError, so a probe
  timeout is indistinguishable from "unpinned" -- the established veto is
  dropped and `_stuck_connecting` arms the kickstart the pin forbids.

  proxy_liveness_status flattens a pinned proxy to the string "stale". The
  --fix boundary reads check["restart_veto"], which a string cannot carry, so
  a pinned proxy's dependent quota checks reach that boundary unvetoed.
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

tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-veto-probe-"))
VETO = "pinned by #2604 witness — DO NOT RESTART"

log = tmp / "voice-agent.log"
log.write_text("\n".join(
    ["Sutando — Voice Interface",
     '[VoiceSession] Transport closed code=1011 reason="boom"']
    + ["[Health] state=CONNECTING"] * 25) + "\n")
hc._voice_log_path = lambda: log

# The SECOND probe fails exactly the way _proc_lstarts fails: closed, empty.
hc._proc_lstarts = lambda _p: ([], {})

voice_row = {"name": "voice-agent", "status": "warn", "restart_veto": VETO}
out = hc.check_voice_transport(voice_row)

check(out.get("status") == "fail",
      f"fixture reaches the stuck-CONNECTING arm (status={out.get('status')})")
check(out.get("restart_veto") == VETO,
      "established veto SURVIVES a failed re-probe")
check("_stuck_connecting" not in out,
      "kickstart is NOT armed while a veto stands")
check(VETO in (out.get("detail") or ""),
      "the veto is stated in the detail the owner reads")

# --- carry_proxy_veto: the structured half ---
rows = [
    ({"name": "quota-telemetry", "status": "warn"}, VETO, True,
     "a non-ok dependent check CARRIES the proxy veto"),
    ({"name": "quota-telemetry", "status": "ok"}, VETO, False,
     "an ok check is left alone (no veto to enforce)"),
    ({"name": "quota-telemetry", "status": "warn"}, None, False,
     "no pin -> no veto invented"),
]
for row, veto, want, label in rows:
    got = hc.carry_proxy_veto(dict(row), veto)
    check(("restart_veto" in got) is want, label)

# The producer must actually yield a veto for a pinned proxy, or the wiring
# above is a no-op with nothing upstream of it.
check(hc.proxy_restart_veto({"restart_veto": VETO}) == VETO,
      "proxy_restart_veto reads the structured field the string projection drops")
check(hc.proxy_restart_veto({"status": "warn", "live": True}) is None,
      "an unpinned proxy yields no veto")

sys.exit(fail)
