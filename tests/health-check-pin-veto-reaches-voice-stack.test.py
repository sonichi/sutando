#!/usr/bin/env python3
"""The veto must come from the PRODUCER, not from a hand-built fixture.

`check_voice_watchers` and `check_voice_transport` withdraw their restart
prescription when the voice row carries `restart_veto`. That is only worth
anything if the row a real pinned voice-agent produces actually carries it --
and `mark_stale_if_outdated` used to write status and detail alone.

So the whole exception was reachable from tests and unreachable in production:
the natural stale row read `warn + DO NOT RESTART` with `restart_veto=None`,
both voice checks took their early return, and the two findings left `issues`
entirely -- taking `--quiet` from exit 1 to exit 0.

This drives the real producer end to end. It fails on a build where the veto
is fabricated by the test rather than carried by the code.
"""
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import time

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

tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-producer-veto-"))
(tmp / "state").mkdir(parents=True, exist_ok=True)
hc.WORKSPACE_DIR = tmp
pins = tmp / "state" / "process-pins.json"

PID, LSTART = 4242, "Mon Aug 25 12:00:00 2026"
# The process started an hour ago; the source is NOW -> naturally stale.
proc_start = time.time() - 3600
src = tmp / "voice-agent.ts"
src.write_text("// changed after the process started\n")
os.utime(src, (time.time(), time.time()))

hc._proc_lstarts = lambda _p: ([proc_start], {str(PID): LSTART})
hc._file_unchanged_since = lambda _f, _s: False   # a real content change

log = tmp / "voice-agent.log"
log.write_text("Sutando — Voice Interface\nWatching for results\n")
hc._voice_log_path = lambda: log


def arm(on):
    pins.write_text(json.dumps({"pins": [{
        "service": "voice-agent", "pid": PID, "lstart": LSTART,
        "reason": "witness armed in this process",
        "pinned_at": "2026-08-25T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
    }]} if on else {"pins": []}))


def natural_voice_row(armed):
    """The row a real pinned voice-agent produces -- via the real producer."""
    arm(armed)
    row = {"name": "voice-agent", "status": "ok", "detail": "port 8788"}
    hc.mark_stale_if_outdated(row, src, "voice-agent[.]ts", service="voice-agent")
    return row


def quiet_exit(rows):
    orig_all, orig_argv = hc.run_all_checks, sys.argv
    hc.run_all_checks = lambda: rows
    sys.argv = ["health-check.py", "--quiet"]
    try:
        hc.main(); return 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 0
    finally:
        hc.run_all_checks, sys.argv = orig_all, orig_argv


for armed in (False, True):
    row = natural_voice_row(armed)
    w = hc.check_voice_watchers(dict(row))
    t = hc.check_voice_transport(dict(row))
    rows = [row, w, t]
    issues = [c for c in rows if hc.is_issue(c)]
    tag = "ARMED " if armed else "UNPIN "
    print(f"  {tag} voice={row['status']} veto={row.get('restart_veto') is not None} "
          f"statuses={[c['status'] for c in rows]} issues={len(issues)} rc={quiet_exit(rows)}")

    if armed:
        check(row.get("restart_veto"),
              "the PRODUCER attaches the veto (not the test)")
        check(w["status"] == "fail" and "missing watcher(s)" in w["detail"],
              f"the watcher DIAGNOSIS survives ({w['status']}: {w['detail'][:40]})")
        check("restart voice-agent" not in w["detail"],
              "and its restart REMEDY is withdrawn")
        # The voice row itself is `warn` (benign, excluded from issues) -- which
        # is exactly why the watcher finding has to carry the outage instead.
        check(any(c is w or c["name"] == w["name"] for c in issues),
              f"the watcher finding stays in `issues` ({[c['name'] for c in issues]})")
        check(quiet_exit(rows) == 1, "so --quiet still exits 1")
        check(quiet_exit([row]) == 0,
              "control: the voice row ALONE exits 0 -- the watcher row is load-bearing")
    else:
        # Control: without a pin nothing is vetoed and the remedy stands.
        check(not row.get("restart_veto"), "control: unpinned row carries NO veto")
        check(row["status"] == "stale", f"control: and is plainly stale ({row['status']})")

# ---- HEALTHY pinned process: reaches NO staleness arm at all --------------
hc._file_unchanged_since = lambda _f, _s: True      # nothing is stale

# Hermetic: drive the REAL resolver with an explicit env, so the check does not
# depend on this host carrying a voice credential.
VENV = {"GEMINI_API_KEY": "test-only-not-a-real-key"}
VENV_PATH = tmp / "absent.env"
assert hc.resolve_voice_health_config(env=VENV, env_path=VENV_PATH)["enabled"] is True, (
    "fixture invalid: voice must be ENABLED or check_voice_stack takes its "
    "disabled early return and the code under test never runs")

for armed in (False, True):
    arm(armed)
    hc.check_port = lambda *a, **k: {"name": "voice-agent", "status": "ok",
                                     "detail": "port 9900"}
    hc.check_bodhi_dist = lambda: {"name": "bodhi-dist", "status": "ok", "detail": "-"}
    rows = {c["name"]: c for c in hc.check_voice_stack(env=VENV, env_path=VENV_PATH)}
    # Disabled mode returns all four rows `ok`; reaching the composition is what
    # makes every assertion below meaningful.
    check(rows["voice-watchers"]["status"] != "ok",
          "hermetic: the composition ran (not the disabled early return)")
    v, w = rows["voice-agent"], rows["voice-watchers"]
    tag = "ARMED " if armed else "UNPIN "
    print(f"  {tag} voice={v['status']} veto={v.get('restart_veto') is not None} "
          f"watcher={w['status']} | {w['detail'][:52]}")
    if armed:
        check(v.get("restart_veto"),
              "healthy pinned voice row carries the veto (no staleness arm fires)")
        check("restart voice-agent" not in w["detail"],
              "so the watcher remedy is withdrawn on a HEALTHY pinned process")
        check(w["status"] == "fail" and "missing watcher(s)" in w["detail"],
              "while the watcher finding itself survives")
    else:
        check(not v.get("restart_veto"), "control: healthy UNPINNED row has no veto")
        check("restart voice-agent" in w["detail"],
              "control: and its remedy still stands")

if fail:
    print("FAIL: the veto does not reach the voice stack from the producer")
    sys.exit(1)
print("PASS: the production producer carries the veto into the voice stack.")
