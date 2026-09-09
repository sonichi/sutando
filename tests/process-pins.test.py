#!/usr/bin/env python3
"""A restart pin suppresses `restart needed` ONLY for the exact process it names.

The stale probe compares source mtime against process start. A tree that moved
FORWARD and one that moved BACKWARD produce the identical signal with opposite
correct remedies: restart adopts newer code in the first case and discards
branch code that exists only in the running process in the second.

The pin is the second case, and every way it could quietly over-suppress is
pinned here: a reused pid, a dead pid, a missing expiry, a past expiry, and an
unreadable pin file. Each must NOT suppress, and each must SURFACE — a pin that
stopped matching means the thing it protected is already gone, and silence
there is worse than the warning it replaced.

Run: python3 tests/process-pins.test.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import process_pins as pp  # noqa: E402

NOW = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc).timestamp()
FUTURE = (datetime.fromtimestamp(NOW, timezone.utc) + timedelta(days=7)).isoformat()
PAST = (datetime.fromtimestamp(NOW, timezone.utc) - timedelta(days=1)).isoformat()
LSTART = "Sat Aug 23 12:24:57 2026"

failures: list = []


def check(label: str, cond: bool) -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        failures.append(label)


def pin(**kw) -> dict:
    base = {"service": "discord-bridge", "pid": 87258, "lstart": LSTART,
            "reason": "#2604 witness armed", "expires_at": FUTURE}
    base.update(kw)
    return base


LIVE = {"87258": LSTART}

# --- the one case that may suppress ----------------------------------------
r = pp.evaluate([pin()], "discord-bridge", LIVE, NOW)
check("exact (pid, lstart) match + unexpired -> ARMED", [v for v, _, _ in r] == [pp.ARMED])
check("armed detail says DO NOT RESTART and names the reason",
      "DO NOT RESTART" in (pp.armed_detail(r) or "") and "#2604" in (pp.armed_detail(r) or ""))

# --- every way it must NOT suppress ----------------------------------------
CASES = [
    ("pid reused by a later process", pin(), {"87258": "Sun Aug 24 08:00:00 2026"}, pp.MISMATCH),
    ("pinned pid no longer running", pin(), {"91000": LSTART}, pp.ORPHAN),
    ("expiry already passed", pin(expires_at=PAST), LIVE, pp.EXPIRED),
    ("no expiry declared", pin(expires_at=None), LIVE, pp.EXPIRED),
    ("expiry unparseable", pin(expires_at="whenever"), LIVE, pp.EXPIRED),
]
for label, p, live, want in CASES:
    r = pp.evaluate([p], "discord-bridge", live, NOW)
    check(f"{label} -> {want}", [v for v, _, _ in r] == [want])
    check(f"{label} -> does NOT suppress", pp.armed_detail(r) is None)
    check(f"{label} -> surfaces a detail", bool(r and r[0][2]))

# A pin for another service must not reach this one.
r = pp.evaluate([pin(service="telegram-bridge")], "discord-bridge", LIVE, NOW)
check("pin scoped to another service is invisible here", r == [])
check("...and suppresses nothing", pp.armed_detail(r) is None)

# --- the pin FILE fails open ------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    check("absent pin file -> no pins", pp.load_pins(tmp / "nope.json") == [])
    bad = tmp / "bad.json"
    bad.write_text("{not json")
    check("unreadable pin file -> no pins (never suppresses)", pp.load_pins(bad) == [])
    wrong = tmp / "wrong.json"
    wrong.write_text(json.dumps({"pins": "not-a-list"}))
    check("malformed pins key -> no pins", pp.load_pins(wrong) == [])
    good = tmp / "good.json"
    good.write_text(json.dumps({"pins": [pin(), "junk"]}))
    check("valid file loads only dict entries", len(pp.load_pins(good)) == 1)

# --- stale_verdict: the ONE decision both health-check call sites delegate to
r = pp.evaluate([pin()], "discord-bridge", LIVE, NOW)
st, det = pp.stale_verdict(r, 821)
check("stale_verdict armed -> warn, never stale", st == "warn")
check("stale_verdict armed -> keeps the age and the reason",
      "821 min" in det and "DO NOT RESTART" in det)

st, det = pp.stale_verdict([], 821)
check("stale_verdict no pins -> stale + restart needed",
      st == "stale" and "restart needed" in det)
check("stale_verdict no pins -> no bracketed note", "[" not in det)

st, det = pp.stale_verdict(pp.evaluate([pin(expires_at=PAST)], "discord-bridge", LIVE, NOW), 5)
check("stale_verdict expired -> still stale", st == "stale")
check("stale_verdict expired -> surfaces the lost pin", "expired" in det)

# An armed sibling changes the prescription, not the other pins' notes;
# orphan removal is manual, so armed-beside-stale is the ordinary case.
orphan = pin(pid=91000)
for order, label in (([orphan, pin()], "orphan first"),
                     ([pin(), orphan], "armed first")):
    r = pp.evaluate(order, "discord-bridge", LIVE, NOW)
    st, det = pp.stale_verdict(r, 821)
    check(f"stale_verdict armed+orphan ({label}) -> warn", st == "warn")
    check(f"stale_verdict armed+orphan ({label}) -> keeps the armed reason",
          "DO NOT RESTART" in det)
    check(f"stale_verdict armed+orphan ({label}) -> STILL surfaces the orphan",
          "no longer running" in det)

# A naive (tz-less) expiry must be read as UTC, not crash or read as eternal.
naive_past = datetime.fromtimestamp(NOW, timezone.utc).replace(tzinfo=None) - timedelta(days=1)
r = pp.evaluate([pin(expires_at=naive_past.isoformat())], "discord-bridge", LIVE, NOW)
check("naive expires_at is treated as UTC and expires", [v for v, _, _ in r] == [pp.EXPIRED])
naive_future = datetime.fromtimestamp(NOW, timezone.utc).replace(tzinfo=None) + timedelta(days=1)
r = pp.evaluate([pin(expires_at=naive_future.isoformat())], "discord-bridge", LIVE, NOW)
check("naive future expires_at still arms", [v for v, _, _ in r] == [pp.ARMED])

# --- controls: the evaluator can produce both polarities --------------------
check("control: ARMED is reachable",
      pp.armed_detail(pp.evaluate([pin()], "discord-bridge", LIVE, NOW)) is not None)
check("control: empty pin list is silent", pp.evaluate([], "discord-bridge", LIVE, NOW) == [])


# WIRING: a policy module nothing calls is a latent no-op, so this drives the
# real mark_stale_if_outdated over a stubbed process table, not the source text.
spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(hc)
except SystemExit:
    pass


def _drive(pins_on_disk, live_lstart):
    """Run the real mark_stale_if_outdated over a fabricated process table."""
    def fake_run(argv, **kw):
        out = ""
        if "/usr/bin/pgrep" in argv[0]:
            out = "\n".join(live_lstart) + "\n"
        elif "/bin/ps" in argv[0]:
            # A blank line and an unparseable one must be skipped, not crash and
            # not key a pin — ps output is not guaranteed clean.
            rows = ["", "   not a timestamp   "]
            rows += [f"{pid} {ls}" for pid, ls in live_lstart.items()]
            out = "\n".join(rows) + "\n"
        return types.SimpleNamespace(stdout=out, returncode=0)

    orig_run, orig_filter, orig_unchanged, orig_pins = (
        hc.subprocess.run, hc._filter_pids_this_checkout,
        hc._file_unchanged_since, hc._pin_verdicts)
    hc.subprocess.run = fake_run
    hc._filter_pids_this_checkout = lambda pids: pids
    hc._file_unchanged_since = lambda *a, **k: False
    hc._pin_verdicts = lambda service, lb: pp.evaluate(pins_on_disk, service, lb, NOW)
    chk = {"name": "discord-bridge", "status": "ok", "detail": ""}
    try:
        hc.mark_stale_if_outdated(chk, REPO / "src" / "health-check.py", "discord-bridge",
                                  threshold_sec=-10**9)
    finally:
        (hc.subprocess.run, hc._filter_pids_this_checkout,
         hc._file_unchanged_since, hc._pin_verdicts) = (
            orig_run, orig_filter, orig_unchanged, orig_pins)
    return chk


c = _drive([pin()], {"87258": LSTART})
check("wiring: armed pin -> status is NOT stale", c["status"] != "stale")
check("wiring: armed pin -> detail says DO NOT RESTART", "DO NOT RESTART" in c["detail"])

c = _drive([pin(expires_at=PAST)], {"87258": LSTART})
check("wiring: expired pin -> still stale", c["status"] == "stale")
check("wiring: expired pin -> restart needed still printed", "restart needed" in c["detail"])
check("wiring: expired pin -> the lost pin is SURFACED", "expired" in c["detail"])

# The real _pin_verdicts, reading the real path under a temp workspace — so the
# monkeypatch above cannot hide a wrong filename.
with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    (ws / "state").mkdir()
    # This case evaluates on the WALL CLOCK (real _pin_verdicts passes
    # time.time()), so its expiry must outlive the calendar, not the frozen NOW.
    _wall_future = (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat()
    (ws / "state" / "process-pins.json").write_text(
        json.dumps({"pins": [pin(expires_at=_wall_future)]}))
    orig_ws = hc.WORKSPACE_DIR
    hc.WORKSPACE_DIR = ws
    try:
        real = hc._pin_verdicts("discord-bridge", {"87258": LSTART})
    finally:
        hc.WORKSPACE_DIR = orig_ws
    check("real _pin_verdicts reads state/process-pins.json",
          [v for v, _, _ in real] == [pp.ARMED])
    check("no service name -> no pins read", hc._pin_verdicts("", {"87258": LSTART}) == [])

c = _drive([], {"87258": LSTART})
check("wiring: no pin -> unchanged stale prescription", c["status"] == "stale")
check("wiring: no pin -> no pin noise in the detail", "[" not in c["detail"])

# The ps parser takes `pid lstart` AND a bare lstart; only the first can key a
# pin, so a bare-lstart fixture fails toward no suppression.
c = _drive([pin()], {"87258": LSTART})
check("ps `pid lstart` shape keys the pin", "DO NOT RESTART" in c["detail"])

print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
