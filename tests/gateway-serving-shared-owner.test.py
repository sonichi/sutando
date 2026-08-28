#!/usr/bin/env python3
"""The gateway-status.json serving verdict has ONE owner, and all three readers
delegate to it.

Two halves, and both are needed:
  * CONTRACT — gateway_serving decides freshness and serving.
  * DELEGATION — health-check, core-input-watch and services_status agree with it
    on the never-polled record. That is the half a per-reader test cannot cover:
    the original bug was three copies drifting, not one copy being wrong.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
sys.path.insert(0, str(SRC))


def load(name, fname):
    spec = importlib.util.spec_from_file_location(name, SRC / fname)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


import gateway_serving as gs  # noqa: E402

NOW = time.time()
NAN, INF = float("nan"), float("inf")
HUGE = 10 ** 400  # valid JSON, unrepresentable as a float
failures = []

# ---------------------------------------------------------------- contract
CONTRACT = [
    ("connected + real last_ok_ts is serving", {"ts": NOW, "connected": True, "last_ok_ts": NOW - 5}, True),
    ("connected + null last_ok_ts is NOT serving", {"ts": NOW, "connected": True, "last_ok_ts": None}, False),
    ("connected + missing last_ok_ts is NOT serving", {"ts": NOW, "connected": True}, False),
    ("connected + bool last_ok_ts is NOT serving", {"ts": NOW, "connected": True, "last_ok_ts": True}, False),
    ("not connected is NOT serving", {"ts": NOW, "connected": False, "last_ok_ts": NOW - 9}, False),
]
for name, rec, want in CONTRACT:
    v = gs.verdict_from_record(rec, now=NOW, max_age=180)
    if v is None or v.serving != want:
        failures.append(f"contract: {name}: want serving={want}, got {v and v.serving}")

# No opinion, rather than a False that would look like a real outage.
NO_OPINION = [
    ("stale record", {"ts": NOW - 9999, "connected": True, "last_ok_ts": NOW}),
    ("bool ts", {"ts": True, "connected": True, "last_ok_ts": NOW}),
    ("missing ts", {"connected": True, "last_ok_ts": NOW}),
    ("not a mapping", ["nope"]),
]
for name, rec in NO_OPINION:
    if gs.verdict_from_record(rec, now=NOW, max_age=180) is not None:
        failures.append(f"contract: {name}: want None (no opinion)")

if gs.read_verdict(Path(tempfile.gettempdir()) / "definitely-absent-3471.json", now=NOW, max_age=180) is not None:
    failures.append("contract: an absent file must be no opinion")

# -------------------------------------------------------------- delegation
# The record from the field: a lane asserting connection that never polled.
NEVER_POLLED = {"ts": NOW, "connected": True, "last_ok_ts": None}
d = tempfile.mkdtemp()
p = Path(d) / "gateway-status.json"
p.write_text(json.dumps(NEVER_POLLED))

hc = load("hc_shared", "health-check.py")
if hc._gateway_serving(p, NOW) is not False:
    failures.append("delegation: health-check._gateway_serving must be False on never-polled")

ciw = load("ciw_shared", "core-input-watch.py")
if ciw._gateway_status(d) is not False:
    failures.append("delegation: core-input-watch._gateway_status must be False on never-polled")

ss = load("ss_shared", "services_status.py")
state, detail, since = ss.probe_gateway(p, "no-such-pattern", NOW, lambda *a, **k: None)
if state != "offline" or since is not None:
    failures.append(f"delegation: services_status.probe_gateway must be offline/None, got {(state, detail, since)}")

# Each reader must actually CALL the owner, not re-derive an agreeing answer.
for mod, name in ((hc, "health-check"), (ciw, "core-input-watch"), (ss, "services_status")):
    if getattr(mod, "read_gateway_verdict", None) is not gs.read_verdict:
        failures.append(f"delegation: {name} must bind gateway_serving.read_verdict")

# ------------------------------------------ malformed records never read healthy

# Each of these returned serving=True before validation landed. A corrupt record
# must yield no opinion or non-serving, never a false green.
MALFORMED = [
    ("connected is the string 'false'", {"ts": NOW, "connected": "false", "last_ok_ts": NOW}, None),
    ("connected is 1, not True",        {"ts": NOW, "connected": 1, "last_ok_ts": NOW}, None),
    ("ts is in the future",             {"ts": NOW + 3600, "connected": True, "last_ok_ts": NOW}, None),
    ("ts is negative",                  {"ts": -1, "connected": True, "last_ok_ts": NOW}, None),
    ("last_ok_ts is -1",                {"ts": NOW, "connected": True, "last_ok_ts": -1}, False),
    ("last_ok_ts is in the future",     {"ts": NOW, "connected": True, "last_ok_ts": NOW + 3600}, False),
    # json.loads parses NaN/Infinity, so these are reachable sidecar values.
    # NaN defeats both bounds silently: NaN > max_age and NaN < -skew are both False.
    ("ts is NaN",                       {"ts": NAN, "connected": True, "last_ok_ts": NOW}, None),
    ("ts is Infinity",                  {"ts": INF, "connected": True, "last_ok_ts": NOW}, None),
    ("last_ok_ts is NaN",               {"ts": NOW, "connected": True, "last_ok_ts": NAN}, False),
    # json parses ints of any size; float() raises OverflowError, which is an
    # ArithmeticError and so escapes read_verdict's ValueError/TypeError catch.
    ("ts is a huge int",                {"ts": HUGE, "connected": True, "last_ok_ts": NOW}, None),
    ("last_ok_ts is a huge int",        {"ts": NOW, "connected": True, "last_ok_ts": HUGE}, False),
]
malformed_checked = len(MALFORMED)
for _label, _rec, _want in MALFORMED:
    _v = gs.verdict_from_record(_rec, now=NOW, max_age=300)
    _got = None if _v is None else _v.serving
    if _got is not _want:
        failures.append(f"malformed: {_label}: want {_want}, got {_got}")

# Control: the skew window must still ACCEPT a record a hair in the future, or
# it is a stricter one-sided window rather than a two-sided one.
_v = gs.verdict_from_record({"ts": NOW + 1, "connected": True, "last_ok_ts": NOW}, now=NOW, max_age=300)
if _v is None or _v.serving is not True:
    failures.append("malformed: a record within the skew tolerance must still be serving")
malformed_checked += 1

# A bad backoff_s must not poison the serving verdict it has no part in.
_v = gs.verdict_from_record({"ts": NOW, "connected": True, "last_ok_ts": NOW, "backoff_s": -5}, now=NOW, max_age=300)
if _v is None or _v.serving is not True or _v.backoff_s is not None:
    failures.append("malformed: a negative backoff_s must be dropped, not change serving")
malformed_checked += 1

for _bad in (-5, HUGE, NAN):
    _v = gs.verdict_from_record({"ts": NOW, "connected": True, "last_ok_ts": NOW, "backoff_s": _bad}, now=NOW, max_age=300)
    if _v is None or _v.serving is not True or _v.backoff_s is not None:
        failures.append(f"malformed: backoff_s={_bad!r} must be dropped without changing serving")
    malformed_checked += 1

# qingyun ran the overflow control through the FILE path, so pin that path too.
_p = Path(tempfile.mkdtemp()) / "gw.json"
_p.write_text('{"ts": ' + "1" + "0" * 400 + ', "connected": true, "last_ok_ts": 1}')
if gs.read_verdict(_p, now=NOW, max_age=300) is not None:
    failures.append("malformed: a huge int via read_verdict must be no opinion, not a raise")
malformed_checked += 1

# ------------------------------------------ the guard must not over-reject

# Every rule above rejects something; each needs its nearest ACCEPTED neighbour,
# or a guard that refuses everything would pass the malformed suite unchanged.
ACCEPTED = [
    ("backoff_s = 0.0 is a value, not absence", {"ts": NOW, "connected": True, "last_ok_ts": NOW, "backoff_s": 0.0},
     lambda v: v.backoff_s == 0.0),
    ("backoff_s underflows to 0.0",             {"ts": NOW, "connected": True, "last_ok_ts": NOW, "backoff_s": json.loads("1e-400")},
     lambda v: v.backoff_s == 0.0),
    ("last_ok_ts = 0.0 is non-negative",        {"ts": NOW, "connected": True, "last_ok_ts": 0.0},
     lambda v: v.last_ok_ts == 0.0 and v.serving is True),
    ("ts exactly at the skew boundary",         {"ts": NOW + gs.FUTURE_SKEW_S, "connected": True, "last_ok_ts": NOW},
     lambda v: v.serving is True),
    ("ts exactly at max_age",                   {"ts": NOW - 300, "connected": True, "last_ok_ts": NOW},
     lambda v: v.serving is True),
    # The adjacent pair at float representability: 2**1023 converts, 2**1024
    # does not. A guard one value too tight rejects the first and passes the rest.
    ("backoff_s = 2**1023, the largest int that converts", {"ts": NOW, "connected": True, "last_ok_ts": NOW, "backoff_s": 2 ** 1023},
     lambda v: v.backoff_s == float(2 ** 1023)),
    ("last_ok_ts exactly at the skew boundary",  {"ts": NOW, "connected": True, "last_ok_ts": NOW + gs.FUTURE_SKEW_S},
     lambda v: v.last_ok_ts is not None and v.serving is True),
]
for _label, _rec, _ok in ACCEPTED:
    _v = gs.verdict_from_record(_rec, now=NOW, max_age=300)
    if _v is None or not _ok(_v):
        failures.append(f"over-reject: {_label}: got {_v}")

# ------------------------------------------ reader-specific edges preserved

# core-input-watch keeps its reconnect grace: a lane that HAS served and is
# backing off stays alive. Delegation must not flatten it into the shared rule.
p.write_text(json.dumps({"ts": NOW, "connected": False, "last_ok_ts": NOW - 1, "backoff_s": 5}))
if ciw._gateway_status(d) is not True:
    failures.append("edge: core-input-watch must keep the reconnect grace for a recently-served lane")
# ...but a never-polled lane has no success to age, so the grace must not apply.
p.write_text(json.dumps({"ts": NOW, "connected": False, "last_ok_ts": None, "backoff_s": 5}))
if ciw._gateway_status(d) is not False:
    failures.append("edge: the reconnect grace must not rescue a lane that never polled")

# services_status keeps its pgrep fallback when the sidecar has no opinion.
p.write_text(json.dumps({"ts": NOW - 9999, "connected": True, "last_ok_ts": NOW}))
if ss.probe_gateway(p, "pat", NOW, lambda *a, **k: "1234")[0] != "running":
    failures.append("edge: services_status must fall back to pgrep on a stale sidecar")

if failures:
    for f in failures:
        print(f"FAIL: {f}")
    sys.exit(1)
print(f"ok - {len(CONTRACT)} contract + {len(NO_OPINION) + 1} no-opinion + {malformed_checked} malformed + {len(ACCEPTED)} accepted cases, "
      f"3 readers delegating, 3 reader-specific edges preserved")
