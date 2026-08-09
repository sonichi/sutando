#!/usr/bin/env python3
"""One authoritative severity-tagged verdict + severity gate (runtime-health.py).

Design: docs/design-core-health-verdict.md (items #1 + #2 of the watchdog
systematic-fix analysis, owner-approved 2026-08-02).

The load-bearing property: a merely slow / idle / mis-probed BUT ALIVE core is
NEVER restarted. Restart (`act`) requires a `critical` verdict that persisted
>= confirm_min cycles AND has >= 2 independent signals agreeing it is down AND
is not freshly booted. Everything softer is `report`/`escalate`/`none`. This is
the fixture wall against the false-positive-restart class that produced ~15
separate historical fixes (#2114 idle->hung, #2072 bad-PATH->crashed, etc.).
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("rh", REPO / "src" / "runtime-health.py")
rh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rh)

failures = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ── severity_of: every derive() state maps to the intended bucket ────────────
check("working -> ok", rh.severity_of("working") == "ok")
check("idle -> ok", rh.severity_of("idle") == "ok")
check("needs_login -> escalate", rh.severity_of("needs_login") == "escalate")
check("unknown(wedged) -> critical", rh.severity_of("unknown") == "critical")
check("offline -> critical", rh.severity_of("offline") == "critical")
check("degraded -> warn (reserved)", rh.severity_of("degraded") == "warn")
check("unmapped -> critical (fail toward noticing)", rh.severity_of("weird") == "critical")


# ── severity_gate: the action decision ───────────────────────────────────────
check("ok -> none", rh.severity_gate({"severity": "ok"}) == "none")
check("warn -> report", rh.severity_gate({"severity": "warn"}) == "report")

# needs_login NEVER auto-restarts — it is a human-only blocker.
check("escalate -> escalate (never act)",
      rh.severity_gate({"severity": "escalate", "confirm": 99,
                        "signals": {"process": False, "status_fresh": False, "gateway": False}}) == "escalate")

# critical, fully corroborated + persisted + not fresh -> act
act_v = {"severity": "critical", "confirm": 2,
         "signals": {"process": False, "status_fresh": False, "gateway": True}}
check("critical + confirm>=2 + 2 down-votes -> act", rh.severity_gate(act_v) == "act")

# qingyun CR on #2527: a genuinely offline core with a LINGERING gateway. derive()
# yields process=False, gateway=True, status_fresh=None — but the heartbeat has
# stopped (dead), so heartbeat_fresh=False gives the 2nd independent down-vote.
# Must reach `act` (a surviving gateway must not make a dead core unrecoverable).
offline_lingering_gw = {"health": "offline", "severity": "critical", "confirm": 2,
                        "signals": {"process": False, "gateway": True,
                                    "status_fresh": None, "heartbeat_fresh": False}}
check("offline + lingering gateway + heartbeat stale -> act (#2527 CR)",
      rh.severity_gate(offline_lingering_gw) == "act")

# critical but only ONE signal down (a single mis-probe) -> report, NOT act
one_probe = {"severity": "critical", "confirm": 9,
             "signals": {"process": True, "status_fresh": False, "gateway": True}}
check("critical + only 1 down-vote -> report (single mis-probe can't kill)",
      rh.severity_gate(one_probe) == "report")

# critical + corroborated but only ONE cycle (a blip) -> report, NOT act
blip = {"severity": "critical", "confirm": 1,
        "signals": {"process": False, "status_fresh": False, "gateway": False}}
check("critical + confirm=1 (blip) -> report", rh.severity_gate(blip) == "report")

# freshly booted -> report even if critical + confirmed + all down
fresh = {"severity": "critical", "confirm": 9,
         "signals": {"process": False, "status_fresh": False, "gateway": False}}
check("critical but freshly_booted -> report",
      rh.severity_gate(fresh, freshly_booted=True) == "report")


# ── "healthy-but-looks-dead" fixtures: must NEVER reach `act` ─────────────────
# An IDLE core routinely reads status_fresh=None (idle writes status rarely) and
# is classified idle -> ok. It must be `none`, never a restart.
idle_v = {"health": "idle", "severity": rh.severity_of("idle"),
          "confirm": 50, "signals": {"process": True, "status_fresh": None, "gateway": True}}
check("idle core (looks quiet) -> none", rh.severity_gate(idle_v) == "none")

# A stale-but-ALIVE core: status_fresh False but process + gateway up AND the
# heartbeat still beats (only ONE down-vote) -> report, never act.
stale_alive = {"health": "unknown", "severity": "critical", "confirm": 100,
               "signals": {"process": True, "status_fresh": False, "gateway": True,
                           "heartbeat_fresh": True}}
check("stale-but-alive (process+gateway up, still beating) -> report, never act",
      rh.severity_gate(stale_alive) == "report")

# A single bad-PATH mis-probe: process reads False on a LIVE core, but the
# independent heartbeat still beats (fresh) and the gateway serves — only one
# down-vote -> report, never act. This is the case that must stay protected even
# though it shares process=False/gateway=True with a real offline; the heartbeat
# is what distinguishes them.
badpath = {"health": "offline", "severity": "critical", "confirm": 100,
           "signals": {"process": False, "status_fresh": True, "gateway": True,
                       "heartbeat_fresh": True}}
check("bad-PATH mis-probe (live core still beating) -> report, never act",
      rh.severity_gate(badpath) == "report")


# ── derive() return shape carries the verdict fields ─────────────────────────
v = rh.derive()
check("derive() includes severity", "severity" in v and v["severity"] in
      {"ok", "warn", "escalate", "critical"}, repr(v.get("severity")))
check("derive() includes signals block",
      isinstance(v.get("signals"), dict) and
      {"process", "gateway", "status_fresh", "pane_login", "heartbeat_fresh"} <= set(v["signals"]),
      repr(v.get("signals")))
check("derive() keeps legacy health key", "health" in v)


# ── _confirm_count: increments on repeat, resets on change ───────────────────
with tempfile.TemporaryDirectory() as d:
    # no prior file -> 1
    check("confirm: fresh -> 1", rh._confirm_count(d, "offline", "critical") == 1)
    json.dump({"health": "offline", "severity": "critical", "confirm": 3},
              open(os.path.join(d, "core-verdict.json"), "w"))
    check("confirm: same (health,sev) -> +1",
          rh._confirm_count(d, "offline", "critical") == 4)
    check("confirm: health changed -> reset 1",
          rh._confirm_count(d, "working", "ok") == 1)
    # corrupt prior file -> reset 1 (fail toward re-confirming)
    open(os.path.join(d, "core-verdict.json"), "w").write("{not json")
    check("confirm: corrupt prior -> 1", rh._confirm_count(d, "offline", "critical") == 1)
    # valid JSON but NOT an object (a list) must not raise AttributeError out of
    # main() — the OSError/ValueError guard doesn't catch it (bassil CR on #2527).
    json.dump([], open(os.path.join(d, "core-verdict.json"), "w"))
    check("confirm: non-dict prior ([]) -> 1 (no crash)",
          rh._confirm_count(d, "offline", "critical") == 1)
    json.dump("a string", open(os.path.join(d, "core-verdict.json"), "w"))
    check("confirm: non-dict prior (str) -> 1 (no crash)",
          rh._confirm_count(d, "offline", "critical") == 1)


# ── _host_label_safe + _heartbeat_fresh: cover the defensive branches ─────────
sys.path.insert(0, str(REPO / "src"))  # so `from util_paths import _host_label` resolves
check("_host_label_safe returns a label", bool(rh._host_label_safe()))

# both resolution paths fail -> None
_saved = sys.modules.get("util_paths")
_orig_gethostname = rh.socket.gethostname
try:
    sys.modules["util_paths"] = None  # makes `from util_paths import ...` raise
    def _boom():
        raise OSError("no hostname")
    rh.socket.gethostname = _boom
    check("_host_label_safe both-paths-fail -> None", rh._host_label_safe() is None)
finally:
    rh.socket.gethostname = _orig_gethostname
    if _saved is not None:
        sys.modules["util_paths"] = _saved
    else:
        sys.modules.pop("util_paths", None)

# _heartbeat_fresh: host unresolved -> None
_orig_hls = rh._host_label_safe
try:
    rh._host_label_safe = lambda: None
    check("_heartbeat_fresh unresolved host -> None", rh._heartbeat_fresh("/tmp") is None)
finally:
    rh._host_label_safe = _orig_hls

# _heartbeat_fresh: fresh / stale / missing .alive
with tempfile.TemporaryDirectory() as ws:
    _host = rh._host_label_safe() or "h"
    _orig_hls2 = rh._host_label_safe
    rh._host_label_safe = lambda: _host
    try:
        cores = os.path.join(ws, "state", "cores")
        os.makedirs(cores)
        alive = os.path.join(cores, _host + ".alive")
        with open(alive, "w") as f:
            f.write("{}")
        check("_heartbeat_fresh fresh .alive -> True", rh._heartbeat_fresh(ws) is True)
        old = os.path.getmtime(alive) - (rh.HEARTBEAT_STALE_SECONDS + 30)
        os.utime(alive, (old, old))
        check("_heartbeat_fresh stale .alive -> False", rh._heartbeat_fresh(ws) is False)
        os.remove(alive)
        check("_heartbeat_fresh missing .alive -> False", rh._heartbeat_fresh(ws) is False)
    finally:
        rh._host_label_safe = _orig_hls2


# ── P1-1 (qingyun CR on #2527): probe-unavailable must be UNKNOWN, never a
#    down-vote. Exercised through the REAL probe boundary — patch subprocess.run
#    so the real _run/_core_running/_gateway_running exception path executes. ────
_orig_run = rh.subprocess.run
# These blocks exercise _gateway_running's PROBE logic, which only runs when the
# gateway is configured on this host (bassil CR on #2527). Force configured=True
# so the probe path is what's under test here; the not-configured short-circuit
# is covered separately below.
_orig_gwc = rh._gateway_configured
rh._gateway_configured = lambda: True


def _raise_missing(*a, **k):
    raise FileNotFoundError("probe binary missing")


try:
    rh.subprocess.run = _raise_missing
    check("_run -> (None, '') when the command cannot execute", rh._run(["nope"]) == (None, ""))
    check("_core_running -> None when the probe is unavailable", rh._core_running() is None)
    check("_gateway_running -> None when BOTH probes are unavailable", rh._gateway_running() is None)

    # Full pipeline under a total probe outage + a fresh heartbeat — qingyun's
    # exact attack. derive() must yield an UNKNOWN verdict whose signals carry no
    # False, so even at confirm=2 the gate can only report.
    _orig_hb = rh._heartbeat_fresh
    rh._heartbeat_fresh = lambda ws: True
    try:
        v = rh.derive()
    finally:
        rh._heartbeat_fresh = _orig_hb
    check("derive() probe outage -> health 'unknown'", v["health"] == "unknown", repr(v["health"]))
    check("derive() probe outage -> process signal None", v["signals"]["process"] is None)
    check("derive() probe outage -> gateway signal None", v["signals"]["gateway"] is None)
    v_confirmed = dict(v)
    v_confirmed["confirm"] = 2
    _gate = rh.severity_gate(v_confirmed)
    check("correlated probe outage + fresh heartbeat -> report, NEVER act (qingyun P1-1)",
          _gate == "report", f"gate={_gate!r}")
finally:
    rh.subprocess.run = _orig_run

# A probe that RAN and genuinely found nothing still reads False (a real miss is
# not the same as an unavailable probe).
_orig_run2 = rh._run
try:
    rh._run = lambda cmd: (1, "")
    check("_core_running -> False when has-session ran and missed", rh._core_running() is False)
    check("_gateway_running -> False when probes ran but found nothing", rh._gateway_running() is False)
    rh._run = lambda cmd: (0, "") if cmd and cmd[0] == "pgrep" else (1, "")
    check("_gateway_running -> True via pgrep", rh._gateway_running() is True)
    rh._run = lambda cmd: (1, "") if cmd and cmd[0] == "pgrep" else (0, "gateway\n")
    check("_gateway_running -> True via tmux window fallback", rh._gateway_running() is True)
finally:
    rh._run = _orig_run2
    rh._gateway_configured = _orig_gwc


# ── P1 (bassil CR on #2527): a host with NO gateway configured must read the
#    absent bridge as not-applicable (None), never a down-vote — else the gate
#    restarts a live core just for lacking an optional component. The short-circuit
#    must fire WITHOUT ever probing. ────────────────────────────────────────────
_orig_run3 = rh._run
_orig_gwc2 = rh._gateway_configured
try:
    # If the probe were reached it would say "running" — assert we still get None,
    # proving _gateway_running short-circuited on not-configured.
    rh._run = lambda cmd: (0, "gateway\n")
    rh._gateway_configured = lambda: False
    check("_gateway_running -> None when gateway not configured (no down-vote)",
          rh._gateway_running() is None)
    rh._gateway_configured = lambda: None
    check("_gateway_running -> None when gateway config can't be determined",
          rh._gateway_running() is None)
finally:
    rh._run = _orig_run3
    rh._gateway_configured = _orig_gwc2

# _gateway_configured itself: env token, .env token, none.
_env_keys = ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN", "CLAUDE_CONFIG_DIR")
_saved_env = {k: os.environ.get(k) for k in _env_keys}
try:
    for k in _env_keys:
        os.environ.pop(k, None)
    os.environ["REMOTE_TASK_TOKEN"] = "x"
    check("_gateway_configured: env token -> True", rh._gateway_configured() is True)
    os.environ.pop("REMOTE_TASK_TOKEN")
    check("_gateway_configured: no env, no CLAUDE_CONFIG_DIR -> None",
          rh._gateway_configured() is None)
    with tempfile.TemporaryDirectory() as cfg:
        os.environ["CLAUDE_CONFIG_DIR"] = cfg
        check("_gateway_configured: config dir, no .env -> None",
              rh._gateway_configured() is None)
        ch = os.path.join(cfg, "channels", "ag2space")
        os.makedirs(ch)
        open(os.path.join(ch, ".env"), "w").write("OTHER=1\n")
        check("_gateway_configured: .env without token -> False",
              rh._gateway_configured() is False)
        open(os.path.join(ch, ".env"), "w").write("REMOTE_TASK_TOKEN=abc\n")
        check("_gateway_configured: .env with token -> True",
              rh._gateway_configured() is True)
finally:
    for k, val in _saved_env.items():
        if val is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = val


# ── severity_gate is defined, exported, and tested but has NO production caller
#    yet: slice 1 is additive by design (Mini cold-review on #2527). Pin the
#    unwired state so the day someone wires it this test fails and forces a
#    deliberate update, rather than the wiring landing unremarked and the gate
#    quietly turning into a live action path no test noticed. ──────────────────
_src_dir = REPO / "src"
_gate_callers = []
for _py in sorted(_src_dir.rglob("*.py")):
    for _i, _line in enumerate(_py.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if _line.lstrip().startswith("#"):
            continue  # a comment mentioning it is not a call
        if "def severity_gate(" in _line:
            continue  # the definition itself
        if "severity_gate(" in _line:
            _gate_callers.append(f"{_py.relative_to(REPO)}:{_i}")
check("severity_gate has NO production call site under src/ (slice 1 additive; Mini #2527)",
      not _gate_callers, f"unexpected callers: {_gate_callers}")


if failures:
    print(f"\n{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("\nall passed")
