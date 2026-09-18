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
    check(r["status"] == "ok" and "not active" in r["detail"],
          "no outbox roots -> ok, honest about the window not being active")

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

    # A counter that PARSES to a non-dict must be unreadable-loud, not a crash
    # (null is the realistic shape: a truncated or half-written counter).
    for bad in ("null", "[]", '"str"', "3"):
        (root2 / "a-fallback-hits.json").write_text(bad)
        r = hc.check_a_fallback_hits(ws)
        check(r["status"] == "warn" and "unreadable" in r["detail"],
              f"non-dict counter {bad!r} -> unreadable warn, not an escape")

    # int() on a container/None count raised TypeError past the old except;
    # every garbage shape must land on the same unreadable warn.
    for badv in ('{"count": {}}', '{"count": [1]}', '{"count": null}',
                 '{"count": {"total": 3}}'):
        (root2 / "a-fallback-hits.json").write_text(badv)
        r = hc.check_a_fallback_hits(ws)
        check(r["status"] == "warn" and "unreadable" in r["detail"],
              f"non-int count value {badv} -> unreadable warn, not a raise")

    # last_hit_ts domain garbage passes isinstance but raises inside
    # fromtimestamp — and only on the hit branch, the gate path itself.
    for badts in ("1e+30", "-1e+30", "NaN", "null", '"x"'):
        (root2 / "a-fallback-hits.json").write_text(
            '{"count": 2, "last_hit_ts": %s, "last_item": "task-q"}' % badts)
        r = hc.check_a_fallback_hits(ws)
        check(r["status"] == "warn" and "2 hit(s)" in r["detail"]
              and "at ?" in r["detail"],
              f"hit with last_hit_ts={badts} -> reported with '?', no raise")
    (root2 / "a-fallback-hits.json").write_text('{"count": 0}')

    # The gate must not pass on an ABSENT instrument: a migrated root with no
    # counter means dual_read never ran there, which is not a measured zero.
    root3 = ws / "results" / ".outbox-gateway"
    root3.mkdir()
    (root3 / ".items-migrated").mkdir()
    r = hc.check_a_fallback_hits(ws)
    check("never ran" in r["detail"] and root3.name in r["detail"],
          "migrated root without counter -> instrument-absence warn, named")
    (root3 / "a-fallback-hits.json").write_text(json.dumps({"count": 0}))
    (root2 / "a-fallback-hits.json").write_text(json.dumps({"count": 0}))
    (root / "a-fallback-hits.json").write_text(json.dumps({"count": 0}))
    r = hc.check_a_fallback_hits(ws)
    check(r["status"] == "ok" and "measured zero" in r["detail"],
          "migrated root with a zero counter -> ok as a MEASURED zero")

    # Shared validator (writer + probe): a malformed count must BLOCK the
    # gate, never read as a clean zero. int() accepted -5 as measured.
    for bad in ('{"count": -5}', '{"count": true}', '{"count": "7"}',
                '{"count": 10000000000000}'):
        (root / "a-fallback-hits.json").write_text(bad)
        r = hc.check_a_fallback_hits(ws)
        check(r["status"] == "warn" and "malformed" in r["detail"],
              f"malformed counter {bad} -> warn (gate blocked), not zero")
    (root / "a-fallback-hits.json").write_text(json.dumps({"count": 0}))
    r = hc.check_a_fallback_hits(ws)
    check(r["status"] == "ok", "control: valid zero still measures ok")

    # A SYMLINK to external valid-zero bytes passes is_file() but is not
    # writer state; it must block the gate, not read as a measured zero.
    ext = ws / "external-zero.json"
    ext.write_text(json.dumps({"count": 0}))
    (root / "a-fallback-hits.json").unlink()
    (root / "a-fallback-hits.json").symlink_to(ext)
    r = hc.check_a_fallback_hits(ws)
    check(r["status"] == "warn" and "malformed" in r["detail"],
          "symlinked valid-zero counter -> warn (gate blocked), not zero")
    (root / "a-fallback-hits.json").unlink()
    (root / "a-fallback-hits.json").write_text(json.dumps({"count": 0}))
    r = hc.check_a_fallback_hits(ws)
    check(r["status"] == "ok", "control: replacing the symlink restores ok")

    # An UNAVAILABLE validator (import failure) must fail closed to warn,
    # never let the gate read a zero it could not validate.
    import sys as _sys
    import types as _types
    _saved = {k: _sys.modules.get(k) for k in
              ("ag2_sparrow", "ag2_sparrow.delivery_core",
               "ag2_sparrow.delivery_core.migration")}
    try:
        for k in _saved:
            _sys.modules[k] = _types.ModuleType(k)   # no read_fallback_counter
        r = hc.check_a_fallback_hits(ws)
        check(r["status"] == "warn" and "malformed" in r["detail"],
              "validator import failure -> warn (fail closed), never ok")
    finally:
        for k, v in _saved.items():
            if v is None:
                _sys.modules.pop(k, None)
            else:
                _sys.modules[k] = v
    r = hc.check_a_fallback_hits(ws)
    check(r["status"] == "ok", "control: validator restored -> ok again")

    # TOCTOU on the detail re-read: validated count stands, missing detail
    # fields degrade to '?' — the warn must still fire, never crash.
    (root / "a-fallback-hits.json").write_text(
        json.dumps({"count": 2, "last_item": "it-9", "last_hit_ts": 1.0}))
    from pathlib import Path as _P
    _orig_read = _P.read_text
    _n = {"c": 0}
    _target = root / "a-fallback-hits.json"
    def _flaky(self, *a, **kw):
        if self == _target:
            _n["c"] += 1
            if _n["c"] > 1:              # validator's read passes, detail read fails
                raise OSError("vanished between reads")
        return _orig_read(self, *a, **kw)
    _P.read_text = _flaky
    try:
        r = hc.check_a_fallback_hits(ws)
    finally:
        _P.read_text = _orig_read
    check(r["status"] == "warn" and "2 hit(s)" in r["detail"]
          and "?" in r["detail"],
          f"detail re-read failure degrades to '?' but the warn still fires ({r['detail'][-90:]})")
    (root / "a-fallback-hits.json").write_text(json.dumps({"count": 0}))

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
