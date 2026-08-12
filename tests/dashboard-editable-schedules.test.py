#!/usr/bin/env python3
"""Editable schedules: crons.json read/write/validate + merge-by-name (owner ask).

The dashboard now edits this host's crons.json from the UI (add / edit cron /
delete). These guard the backend that the POST/DELETE handlers call: validation
(cron shape, mutually-exclusive prompt/prompt_skill), atomic round-trip, and the
merge-onto-existing-by-name path an inline cron-only edit relies on.

Run: python3 tests/dashboard-editable-schedules.test.py   (exit 0/1)
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("dashboard_es", REPO / "src" / "dashboard.py")
dash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dash)

# Exercise the REAL _crons_path() once, then redirect to a temp file (never
# touch the real per-host cron set).
_real_crons_path = dash._crons_path()
_tmp = Path(tempfile.mkdtemp(prefix="dash-es-")) / "crons.json"
dash._crons_path = lambda: _tmp

failures: list[str] = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ── _html_attr + real _crons_path ─────────────────────────────────────────────
check("_html_attr escapes quote/angle/amp",
      dash._html_attr('a"<b>&') == "a&quot;&lt;b&gt;&amp;")
check("_crons_path ends with hosts/<host>/crons.json",
      str(_real_crons_path).endswith("crons.json") and "hosts" in str(_real_crons_path))

# ── validation ────────────────────────────────────────────────────────────────
check("valid job passes", dash._validate_job(
    {"name": "x", "cron": "*/10 * * * *", "prompt_skill": "morning-briefing"}) is None)
check("non-dict job rejected", dash._validate_job("not a dict") == "job must be an object")
check("missing name rejected", dash._validate_job({"name": "", "cron": "* * * * *", "prompt": "y"}))
check("bad cron (4 fields) rejected", dash._validate_job({"name": "x", "cron": "* * * *", "prompt": "y"}))
# CR #2164: a malformed 5-field cron (right token count, garbage fields) must be
# rejected — _cron_next_run returns None (not raises), so it can't be the gate.
check("malformed 5-field cron rejected", dash._validate_job(
    {"name": "x", "cron": "foo bar baz qux quux", "prompt": "y"}))
check("out-of-range minute rejected", dash._validate_job(
    {"name": "x", "cron": "99 * * * *", "prompt": "y"}))
check("out-of-range month rejected", dash._validate_job(
    {"name": "x", "cron": "0 0 1 13 *", "prompt": "y"}))
check("inverted range rejected", dash._validate_job(
    {"name": "x", "cron": "0 0 1 * 5-1", "prompt": "y"}))
check("bad step rejected", dash._validate_job(
    {"name": "x", "cron": "*/0 * * * *", "prompt": "y"}))
# A syntactically-valid but rare cron (Feb 29 — no run for years) must still be
# ACCEPTED — validity is per-field syntax/range, not presence in the scan horizon.
check("valid-but-rare cron accepted (Feb 29)", dash._validate_job(
    {"name": "x", "cron": "0 0 29 2 *", "prompt": "y"}) is None)
check("valid ranges/lists/steps accepted", dash._validate_job(
    {"name": "x", "cron": "0,30 9-17 * * 1-5", "prompt_skill": "s"}) is None)
# The bad cron is also rejected end-to-end through the persisting handler.
_bad_code, _bad_obj = dash.upsert_schedule({"name": "bad", "cron": "foo bar baz qux quux", "prompt": "y"})
check("upsert rejects a malformed 5-field cron (not persisted)", _bad_code == 400, str(_bad_obj))
# Direct helper unit — the accepted/rejected token forms.
check("_cron_field_valid accepts *, */N, A-B, N, lists",
      all(dash._cron_field_valid(s, 0, 59) for s in ("*", "*/5", "0-59", "30", "0,15,30,45")))
check("_cron_field_valid rejects garbage / OOR / empty",
      not any(dash._cron_field_valid(s, 0, 59) for s in ("foo", "60", "*/x", "", "10-5")))
check("both prompt+skill rejected", dash._validate_job(
    {"name": "x", "cron": "* * * * *", "prompt": "y", "prompt_skill": "z"}))
check("neither prompt nor skill rejected", dash._validate_job({"name": "x", "cron": "* * * * *"}))

# ── atomic round-trip ─────────────────────────────────────────────────────────
dash._write_crons([{"name": "a", "cron": "0 9 * * *", "prompt_skill": "morning-briefing"}])
check("write+read round-trips", dash._read_crons()[0]["name"] == "a")
check("write is atomic (no leftover tmp)", not _tmp.with_suffix(".json.tmp").exists())
check("read of missing file → []", (lambda: (_tmp.unlink(), dash._read_crons() == [])[1])())

# ── merge-by-name (simulate the POST handler's merge logic) ───────────────────
def _upsert(jobs, body):
    """Mirror do_POST's merge — cron-only edit inherits existing fields."""
    name = body["name"].strip()
    existing = next((j for j in jobs if j.get("name") == name), None)
    merged = dict(existing) if existing else {}
    merged["name"] = name
    for k in ("cron", "prompt", "prompt_skill", "description"):
        if k in body and str(body.get(k)).strip():
            merged[k] = str(body[k]).strip()
    if (body.get("prompt_skill") or "").strip():
        merged.pop("prompt", None)
    elif (body.get("prompt") or "").strip():
        merged.pop("prompt_skill", None)
    assert dash._validate_job(merged) is None, dash._validate_job(merged)
    jobs = [j for j in jobs if j.get("name") != name]
    jobs.append(merged)
    return jobs


jobs = [{"name": "briefing", "cron": "57 6 * * *", "prompt_skill": "morning-briefing"}]
# inline cron-only edit — must inherit prompt_skill, not fail validation
jobs = _upsert(jobs, {"name": "briefing", "cron": "30 7 * * *"})
j = next(x for x in jobs if x["name"] == "briefing")
check("cron-only edit updates cron", j["cron"] == "30 7 * * *")
check("cron-only edit preserves prompt_skill", j.get("prompt_skill") == "morning-briefing")
check("replace-by-name (no duplicate)", len([x for x in jobs if x["name"] == "briefing"]) == 1)
# switching type: supplying prompt drops prompt_skill
jobs = _upsert(jobs, {"name": "briefing", "cron": "30 7 * * *", "prompt": "Run: echo hi"})
j = next(x for x in jobs if x["name"] == "briefing")
check("switching to prompt drops prompt_skill", "prompt_skill" not in j and j.get("prompt") == "Run: echo hi")

# ── pure upsert_schedule / delete_schedule (what the HTTP handlers call) ───────
dash._write_crons([])
code, obj = dash.upsert_schedule({"name": "n1", "cron": "*/5 * * * *", "prompt_skill": "morning-briefing"})
check("upsert add → 200", code == 200 and obj.get("ok"))
check("upsert persisted the job", dash._read_crons()[0]["name"] == "n1")
code, obj = dash.upsert_schedule({"name": "n1", "cron": "0 9 * * *"})  # cron-only edit
check("upsert cron-only edit → 200 (inherits skill)", code == 200)
check("edit kept prompt_skill", dash._read_crons()[0].get("prompt_skill") == "morning-briefing")
code, obj = dash.upsert_schedule({"name": "", "cron": "* * * * *", "prompt": "x"})
check("upsert missing name → 400", code == 400)
code, obj = dash.upsert_schedule({"name": "bad", "cron": "nope", "prompt": "x"})
check("upsert bad cron → 400", code == 400)
code, obj = dash.upsert_schedule("not a dict")
check("upsert non-dict → 400", code == 400)
# CR #2164: non-string scalar fields must be rejected with a 400, not crash on
# `.strip()`. A JSON like {"name": 123} previously raised AttributeError and
# closed the request with no response.
for bad in (
    {"name": 123, "cron": "0 9 * * *", "prompt": "x"},
    {"name": "x", "cron": True, "prompt": "x"},
    {"name": "x", "cron": "0 9 * * *", "prompt": 5},
    {"name": "x", "cron": "0 9 * * *", "prompt_skill": ["a"]},
    {"name": "x", "cron": "0 9 * * *", "description": {"a": 1}, "prompt": "x"},
):
    c, o = dash.upsert_schedule(bad)
    check(f"upsert rejects non-string field → 400 ({list(bad)})", c == 400 and "must be a string" in o.get("error", ""), str((c, o)))
# A valid all-string body still passes.
c, o = dash.upsert_schedule({"name": "okjob", "cron": "0 9 * * *", "prompt": "Run: echo hi"})
check("upsert accepts all-string body", c == 200, str((c, o)))
dash.delete_schedule("okjob")
code, obj = dash.delete_schedule("n1")
check("delete existing → 200", code == 200 and obj.get("deleted") == "n1")
code, obj = dash.delete_schedule("ghost")
check("delete missing → 404", code == 404)

# ── explicit BEFORE/AFTER state transitions for add / edit / delete (CR #2164) ──
# Show the persisted crons.json content before and after each operation — the
# add/edit/delete evidence, driving the pure handlers the HTTP routes call.
import json as _json
dash._write_crons([])  # start from a clean slate

# ADD
before_add = dash._read_crons()
dash.upsert_schedule({"name": "briefing", "cron": "0 9 * * *", "prompt_skill": "morning-briefing"})
after_add = dash._read_crons()
print("ADD    before:", _json.dumps(before_add), "→ after:", _json.dumps(after_add))
check("ADD: crons.json goes [] → one job named 'briefing'",
      before_add == [] and len(after_add) == 1 and after_add[0]["name"] == "briefing")

# EDIT (cron-only, by name — must not duplicate, must keep prompt_skill)
before_edit = dash._read_crons()
dash.upsert_schedule({"name": "briefing", "cron": "30 7 * * *"})
after_edit = dash._read_crons()
print("EDIT   before:", _json.dumps(before_edit), "→ after:", _json.dumps(after_edit))
check("EDIT: still one job, cron '0 9 * * *' → '30 7 * * *', prompt_skill preserved",
      len(after_edit) == 1 and after_edit[0]["cron"] == "30 7 * * *"
      and after_edit[0].get("prompt_skill") == "morning-briefing")

# DELETE
before_del = dash._read_crons()
dash.delete_schedule("briefing")
after_del = dash._read_crons()
print("DELETE before:", _json.dumps(before_del), "→ after:", _json.dumps(after_del))
check("DELETE: crons.json goes one job → []",
      len(before_del) == 1 and after_del == [])

# ── scheduler-specific fields survive an edit (CR #2164 — the field-drop bug) ──
# A job configured directly in crons.json with scheduler-specific keys, then
# edited via the dashboard (cron-only), must KEEP those keys — a prior version
# rebuilt a name/cron/prompt/description whitelist and silently dropped them,
# which could disable a Codex job or detach its room.
dash._write_crons([{
    "name": "codexjob", "cron": "0 6 * * *", "prompt_skill": "morning-briefing",
    "execution": "codex-task", "delivery": "proactive", "retry_minutes": 20,
    "timezone": "America/Los_Angeles", "room": "!room:ag2.space", "room_id": "!room:ag2.space",
    "launchd": True,
}])
code, _ = dash.upsert_schedule({"name": "codexjob", "cron": "30 6 * * *"})  # cron-only edit
saved = dash._read_crons()[0]
check("edit → 200", code == 200)
check("edit applied the cron change", saved.get("cron") == "30 6 * * *")
check("edit preserves execution (Codex job not disabled)", saved.get("execution") == "codex-task")
check("edit preserves delivery", saved.get("delivery") == "proactive")
check("edit preserves retry_minutes", saved.get("retry_minutes") == 20)
check("edit preserves timezone", saved.get("timezone") == "America/Los_Angeles")
check("edit preserves room + room_id (not detached)",
      saved.get("room") == "!room:ag2.space" and saved.get("room_id") == "!room:ag2.space")
check("edit preserves launchd flag", saved.get("launchd") is True)
check("edit still preserves prompt_skill", saved.get("prompt_skill") == "morning-briefing")

# ── no wildcard CORS on the dashboard handler (CR #2164) ──────────────────────
# The dashboard is same-origin; a wildcard Access-Control-Allow-Origin while
# advertising POST/DELETE let a cross-origin tab mutate loopback schedules.
_dash_src = (REPO / "src" / "dashboard.py").read_text()
check("no Access-Control-Allow-Origin header is emitted (send_header call absent)",
      'send_header("Access-Control-Allow-Origin"' not in _dash_src
      and "send_header('Access-Control-Allow-Origin'" not in _dash_src)
check("handler no longer advertises cross-origin write methods via CORS",
      "Access-Control-Allow-Methods" not in _dash_src)

# ── concurrent mutations are linearizable (CR #2164) ──────────────────────────
# dashboard runs under ThreadingHTTPServer, so overlapping upsert/delete requests
# must not lose an acknowledged update or crash on a shared temp path. Fire N
# distinct upserts concurrently (barrier-synchronized, with a widened read→write
# window) and assert every acknowledged job survives. Pre-fix (unlocked
# read-modify-write + shared crons.json.tmp) this dropped updates and could raise
# FileNotFoundError off the shared .tmp; the transaction lock serializes them.
import threading as _threading
import time as _time

dash._write_crons([])
_N = 24
_barrier = _threading.Barrier(_N)
_codes: dict[str, int] = {}
_errs: list[str] = []

# Widen the read→write window so an unlocked implementation reliably races; under
# the real lock this sleep simply runs inside the critical section (still serial).
#
# Patch the SCHEDULES module, not dash: the read→merge→write transaction moved to
# dashboard_schedules, so patching dash._read_crons (a thin wrapper the transaction
# no longer calls) leaves this widening inert. The race is still detected without
# it — measured 1/24 persisted under a removed lock — but only incidentally, on
# this filesystem and scheduler. Patching here keeps the detection *engineered*
# rather than luck, which is what the original author wired it up for.
import dashboard_schedules as _ds

_orig_read = _ds.read_crons


def _slow_read(path):
    r = _orig_read(path)
    _time.sleep(0.003)
    return r


_ds.read_crons = _slow_read


def _worker(i):
    name = f"job{i:02d}"
    try:
        _barrier.wait(timeout=10)
        code, _ = dash.upsert_schedule(
            {"name": name, "cron": "*/5 * * * *", "prompt_skill": "morning-briefing"})
        _codes[name] = code
    except Exception as e:  # e.g. a shared-temp FileNotFoundError
        _errs.append(f"{name}: {type(e).__name__}: {e}")


_threads = [_threading.Thread(target=_worker, args=(i,)) for i in range(_N)]
for _t in _threads:
    _t.start()
for _t in _threads:
    _t.join(timeout=15)
_ds.read_crons = _orig_read

_final = {j["name"] for j in dash._read_crons()}
check("concurrent upserts: no writer raised (no shared-temp FileNotFoundError)",
      not _errs, "; ".join(_errs))
check("concurrent upserts: every request acknowledged 200",
      len(_codes) == _N and all(c == 200 for c in _codes.values()), str(_codes))
check("concurrent upserts: all N acknowledged jobs persisted (no lost update)",
      _final == {f"job{i:02d}" for i in range(_N)},
      f"persisted {len(_final)}/{_N}: {sorted(_final)}")

# Upsert racing a delete of a different pre-existing job: both must serialize —
# the add survives, the victim is gone, the untouched job is intact.
dash._write_crons([
    {"name": "keep", "cron": "*/5 * * * *", "prompt_skill": "morning-briefing"},
    {"name": "victim", "cron": "*/5 * * * *", "prompt_skill": "morning-briefing"}])
_b2 = _threading.Barrier(2)


def _do_upsert():
    _b2.wait(timeout=10)
    dash.upsert_schedule({"name": "added", "cron": "0 9 * * *", "prompt_skill": "morning-briefing"})


def _do_delete():
    _b2.wait(timeout=10)
    dash.delete_schedule("victim")


_tu = _threading.Thread(target=_do_upsert)
_td = _threading.Thread(target=_do_delete)
_tu.start()
_td.start()
_tu.join(timeout=15)
_td.join(timeout=15)
_names2 = {j["name"] for j in dash._read_crons()}
check("upsert||delete serialize: added survives, victim gone, keep intact",
      _names2 == {"keep", "added"}, str(sorted(_names2)))

# ── _write_crons write-failure cleanup (CR #2164 defense-in-depth) ────────────
# A failed os.replace must remove the per-writer temp (no orphan) and re-raise;
# a double-fault (temp already gone) must be swallowed by the inner except so the
# write still raises cleanly rather than masking with a FileNotFoundError.
dash._write_crons([{"name": "seed", "cron": "*/5 * * * *", "prompt_skill": "morning-briefing"}])
_seed_bytes = _tmp.read_text()
_orig_replace = dash.os.replace


def _boom(*a, **k):
    raise OSError("disk full")


# (a) replace fails, temp cleanup succeeds → raises, no orphan .tmp, file intact.
dash.os.replace = _boom
_raised = False
try:
    dash._write_crons([{"name": "new", "cron": "*/5 * * * *", "prompt_skill": "morning-briefing"}])
except OSError:
    _raised = True
finally:
    dash.os.replace = _orig_replace
check("write failure re-raises OSError", _raised)
check("write failure leaves no orphan .tmp",
      not list(_tmp.parent.glob("*.tmp")), str(list(_tmp.parent.glob("*.tmp"))))
check("write failure leaves crons.json intact", _tmp.read_text() == _seed_bytes)


# (b) double-fault: replace fails AND the temp is already gone, so the code's
#     own tmp.unlink() raises — the inner except must swallow it and the write
#     still re-raises the original OSError.
def _boom_del(src, dst, *a, **k):
    Path(src).unlink()  # remove temp so the except's tmp.unlink() raises
    raise OSError("disk full")


dash.os.replace = _boom_del
_raised2 = False
try:
    dash._write_crons([{"name": "new2", "cron": "*/5 * * * *", "prompt_skill": "morning-briefing"}])
except OSError:
    _raised2 = True
finally:
    dash.os.replace = _orig_replace
check("double-fault (temp already gone) still re-raises, no masking", _raised2)
check("double-fault leaves crons.json intact", _tmp.read_text() == _seed_bytes)

print()
if failures:
    print(f"FAIL — {len(failures)}: {failures}")
    sys.exit(1)
print("PASS — editable schedules backend")
