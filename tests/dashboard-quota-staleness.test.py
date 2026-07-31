#!/usr/bin/env python3
"""The dashboard quota panel must disclose how old its reading is.

`quota-state.json` is written by the credential proxy. When the proxy is not in
the boot path (sonichi#2211) nothing rewrites it, and the panel kept rendering
the last snapshot as if current: Chi found it showing "4% used, resets 16:40
Jul 17" from a file **332 hours** old.

The asymmetry is the point. A MISSING file already degrades honestly — no
numbers, `available: True`. A STALE file is confidently wrong, and confidently
wrong is the worse failure because nothing prompts a second look.

These pin that the age travels with the data and that the label never implies a
freshness the reader cannot vouch for.
"""
import importlib.util
import json
import pathlib
import sys
import tempfile
import time
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("dash", REPO / "src" / "dashboard.py")
dash = importlib.util.module_from_spec(spec)
sys.modules["dash"] = dash
spec.loader.exec_module(dash)

failures = []


def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + label + ("" if cond else f" — {detail}"))
    if not cond:
        failures.append(label)


def write_state(tmp, hours_ago=None, last_checked="use-age", extra=None):
    """A quota-state.json whose reading is `hours_ago` old."""
    p = pathlib.Path(tmp) / "quota-state.json"
    body = {"available": True, "headers": {
        "anthropic-ratelimit-unified-5h-utilization": "0.04",
        "anthropic-ratelimit-unified-5h-reset": "1784331600",
    }}
    if last_checked == "use-age" and hours_ago is not None:
        ts = datetime.now(timezone.utc).timestamp() - hours_ago * 3600
        body["last_checked"] = datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")
    elif last_checked not in (None, "use-age"):
        body["last_checked"] = last_checked
    if extra:
        body.update(extra)
    p.write_text(json.dumps(body))
    if hours_ago is not None:
        old = time.time() - hours_ago * 3600
        import os
        os.utime(p, (old, old))
    return p


with tempfile.TemporaryDirectory() as tmp:
    # --- the live failure: a 332h-old reading -------------------------------
    p = write_state(tmp, hours_ago=331.7)
    data = json.loads(p.read_text())
    fresh = dash._quota_freshness(data, p)
    # NB: .get() not [] — a pre-fix module returns {} and must FAIL these
    # checks, not raise KeyError and abort the suite before the rest run.
    check("A1 a 332h-old reading is flagged stale", fresh.get("stale") is True, str(fresh))
    check("A2 its age is reported, not hidden",
          (fresh.get("age_h") or 0) > 300, str(fresh))
    check("A3 the label says STALE and gives the age in days",
          "STALE" in dash._quota_age_label({**data, **fresh}) and "d old" in dash._quota_age_label({**data, **fresh}),
          dash._quota_age_label({**data, **fresh}))

    # --- controls: a fresh reading must NOT be cried wolf on ----------------
    p = write_state(tmp, hours_ago=0.05)
    data = json.loads(p.read_text())
    fresh = dash._quota_freshness(data, p)
    check("B1 a 3-minute-old reading is NOT stale", fresh.get("stale") is False, str(fresh))
    check("B2 fresh readings still show their age (absence trains the eye to ignore it)",
          "ago" in dash._quota_age_label({**data, **fresh}),
          dash._quota_age_label({**data, **fresh}))

    p = write_state(tmp, hours_ago=5.5)
    data = json.loads(p.read_text())
    check("B3 just under the 6h threshold is still fresh",
          dash._quota_freshness(data, p).get("stale") is False)
    p = write_state(tmp, hours_ago=6.5)
    data = json.loads(p.read_text())
    check("B4 just over the 6h threshold is stale",
          dash._quota_freshness(data, p).get("stale") is True)

    # --- unknown age must fail CLOSED --------------------------------------
    p = write_state(tmp, hours_ago=0.1, last_checked="not-a-timestamp")
    data = json.loads(p.read_text())
    fresh = dash._quota_freshness(data, p)
    check("C1 an unparseable last_checked falls back to mtime, not a crash",
          fresh.get("age_h") is not None, str(fresh))

    # last_checked is the WRITER's observation; mtime only says when the file
    # was touched. A rewrite carrying an old reading must still read old.
    p = write_state(tmp, hours_ago=None, last_checked=(
        datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - 40 * 3600, timezone.utc)
        .isoformat().replace("+00:00", "Z")))
    data = json.loads(p.read_text())
    fresh = dash._quota_freshness(data, p)
    check("C2 a FRESH mtime with an OLD last_checked still reads stale",
          fresh.get("stale") is True and (fresh.get("age_h") or 0) > 30, str(fresh))

    check("D1 no data at all → 'no data', not a fake age",
          dash._quota_age_label({"available": True}) == "no data")
    check("D2 age unknown → said so explicitly",
          dash._quota_age_label({"headers": {"x": "1"}, "age_h": None}) == "age unknown")

    # --- the label's middle branches ---------------------------------------
    # Between "minutes ago" and "days old" sit two bands that the cases above
    # jump over. They are the ones a reader sees most often in normal operation.
    check("D3 stale but under a day → hours, not a fraction of a day",
          dash._quota_age_label({"headers": {"x": "1"}, "age_h": 8.0, "stale": True})
          == "STALE 8.0h old")
    check("D4 fresh and over an hour → hours ago",
          dash._quota_age_label({"headers": {"x": "1"}, "age_h": 2.0, "stale": False})
          == "2.0h ago")

    # --- get_quota_status() end-to-end -------------------------------------
    # The helpers can be right while the function that calls them never does.
    p = write_state(tmp, hours_ago=400.0)
    orig_ws = dash.WORKSPACE_DIR
    try:
        dash.WORKSPACE_DIR = pathlib.Path(tmp)
        q = dash.get_quota_status()
        check("F1 get_quota_status attaches freshness to the returned data",
              q.get("stale") is True and (q.get("age_h") or 0) > 390, str(q)[:120])
        check("F2 it still returns the payload it always did",
              "headers" in q, str(q)[:120])
    finally:
        dash.WORKSPACE_DIR = orig_ws

    # --- unreadable file: fail CLOSED --------------------------------------
    # If neither last_checked nor mtime can be obtained, the honest answer is
    # "unknown age, treat as stale" — never "fresh".
    class Unstatable:
        def stat(self):
            raise OSError("simulated: file vanished between read and stat")

    fresh = dash._quota_freshness({"last_checked": None}, Unstatable())
    check("F3 an unstatable file reports unknown age and fails CLOSED to stale",
          fresh == {"age_h": None, "stale": True}, str(fresh))

    # --- the rendered panel ------------------------------------------------
    # The helpers being right is not the fix; the fix is what the page SHOWS.
    # render_dashboard() takes no arguments and pulls its own data, so stub the
    # collaborators and drive the real template.
    def render_with(quota):
        orig = {n: getattr(dash, n) for n in
                ("get_health", "get_activity", "get_pending_count", "get_score",
                 "get_system_stats")}
        dash.get_health = lambda: {}
        dash.get_activity = lambda n=5: []
        dash.get_pending_count = lambda: {"open": 0, "done": 0}
        dash.get_score = lambda: "?"
        dash.get_system_stats = lambda: {
            "quota": quota, "disk_free": "1G", "battery": "50%",
            "charging": False, "cpu": "1%", "mem": "1G", "uptime": "1d",
        }
        try:
            return dash.render_dashboard()
        finally:
            for n, f in orig.items():
                setattr(dash, n, f)

    try:
        stale_html = render_with({"available": True, "headers": {"x": "1"},
                                  "age_h": 336.5, "stale": True})
        check("E1 a stale panel renders the warning glyph, not a check",
              "⚠" in stale_html)
        check("E2 a stale panel renders the age next to it",
              "STALE 14.0d old" in stale_html, "age badge missing")
        check("E3 a stale panel is coloured as a warning",
              "#b45309" in stale_html)

        fresh_html = render_with({"available": True, "headers": {"x": "1"},
                                  "age_h": 0.2, "stale": False})
        check("E4 a fresh panel does NOT cry wolf", "⚠" not in fresh_html)
        check("E5 a fresh panel still states its age",
              "12m ago" in fresh_html, "fresh age missing")
    except Exception as exc:  # a template break must fail loudly, not skip
        check(f"E* render_dashboard raised: {type(exc).__name__}: {exc}", False)

print()
if failures:
    print(f"FAIL — {len(failures)} check(s) failed")
    sys.exit(1)
print("PASS — dashboard quota staleness tests")
