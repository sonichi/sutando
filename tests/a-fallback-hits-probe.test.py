#!/usr/bin/env python3
"""The dual_read fallback counter is Design A's deletion release gate; the
probe must warn on any hit, stay green on zero/absent, fail soft on garbage."""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(hc)
except SystemExit:
    pass

failures = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    (ws / "results").mkdir()
    r = hc.check_a_fallback_hits(ws)
    check(r["status"] == "ok" and "no migration-window" in r["detail"],
          "no outbox roots -> ok")

    root = ws / "results" / ".outbox-discord-proactive"
    root.mkdir()
    r = hc.check_a_fallback_hits(ws)
    check(r["status"] == "ok", "root without counter file -> ok (no migration ran)")

    (root / "a-fallback-hits.json").write_text(json.dumps(
        {"count": 0, "last_hit_ts": 0, "last_item": None}))
    r = hc.check_a_fallback_hits(ws)
    check(r["status"] == "ok", "counter at zero -> ok")

    (root / "a-fallback-hits.json").write_text(json.dumps(
        {"count": 3, "last_hit_ts": 1787370000.0, "last_item": "task-abc"}))
    r = hc.check_a_fallback_hits(ws)
    check(r["status"] == "warn", "any hit -> warn (a hit is a FINDING)")
    check("task-abc" in r["detail"] and "3 hit(s)" in r["detail"],
          "warn names the count and the last-hit item (diagnosable)")

    root2 = ws / "results" / ".outbox"
    root2.mkdir()
    (root2 / "a-fallback-hits.json").write_text("{torn")
    r = hc.check_a_fallback_hits(ws)
    check(r["status"] == "warn" and "unreadable" in r["detail"],
          "unreadable counter fails LOUD (warn), never silently green")
    check("task-abc" in r["detail"],
          "and the readable root's hit is still reported alongside")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
