#!/usr/bin/env python3
"""Tests for skills/agent-room-ops/doc.py — a 4xx that carries a structured
{"error": ...} body must surface THAT error, not be flattened to the generic
"verb unimplemented (404)" by degrade_reason().

Regression: on 2026-07-28 a `doc get` for a genuinely-absent plan doc returned
"verb unimplemented (404)" while the raw gateway op:prep_get returned
`{"error": "roadmap/<name> not found"}`. doc._call caught the HTTPError and
called degrade_reason(e.code), discarding the server's body — so a missing doc
read as a dead doc backend, masking that prep_get was in fact working.

Pure, no network: http_json is monkeypatched to raise a controllable HTTPError.
Lives under tests/ so CI auto-discovers it (find tests -name '*.test.py').
Run: python3 tests/agent-room-ops-doc-404-legibility.test.py  (exit 0 pass / 1 fail)
"""
import io
import os
import sys
from urllib.error import HTTPError

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "skills", "agent-room-ops"))
import doc as D

FAILS = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _httperror(code, body_bytes):
    return HTTPError("https://gw/v1/room", code, "err", {}, io.BytesIO(body_bytes))


def _patch(raiser):
    """Neutralise the gate + gateway plumbing so _call reaches http_json, then
    make http_json raise `raiser`. Exercises the SHIPPED _call, not a copy."""
    D.gate_allows = lambda *a, **k: True
    D.load_gate = lambda *a, **k: {}
    D.gateway = lambda *a, **k: ("https://gw", {})
    D.http_json = raiser


def test_structured_not_found_is_surfaced():
    _patch(lambda *a, **k: (_ for _ in ()).throw(
        _httperror(404, b'{"error": "roadmap/plan.md not found"}')))
    res = D._call("prep_get", "!r:x", "@me:x", {},
                  {"folder": "roadmap", "filename": "plan.md"})
    check(res.get("reason") == "roadmap/plan.md not found",
          "404 with {\"error\":...} -> server's error surfaced, not 'verb unimplemented'")
    check(res.get("ok") is False, "structured not-found -> ok is False")
    check(res.get("folder") == "roadmap" and res.get("name") == "plan.md",
          "structured not-found -> folder/name echoed for the caller")


def test_bodiless_404_falls_back_to_degrade_reason():
    _patch(lambda *a, **k: (_ for _ in ()).throw(_httperror(404, b"")))
    res = D._call("prep_get", "!r:x", "@me:x", {}, {"folder": "roadmap"})
    check(res.get("reason") == "verb unimplemented (404)",
          "bodiless 404 -> degrade_reason fallback ('verb unimplemented (404)')")


def test_non_json_404_falls_back():
    _patch(lambda *a, **k: (_ for _ in ()).throw(
        _httperror(404, b"<html>404 Not Found</html>")))
    res = D._call("prep_get", "!r:x", "@me:x", {}, {})
    check(res.get("reason") == "verb unimplemented (404)",
          "non-JSON 404 body -> degrade_reason fallback (not a crash)")


def test_doc_get_wrapper_surfaces_reason():
    # The shipped public entrypoint, not just _call.
    _patch(lambda *a, **k: (_ for _ in ()).throw(
        _httperror(404, b'{"error": "roadmap/plan.md not found"}')))
    res = D.doc_get("!r:x", folder="roadmap", name="plan.md", agent_mxid="@me:x")
    check(res.get("reason") == "roadmap/plan.md not found",
          "doc_get() -> surfaces the structured not-found through the wrapper")


if __name__ == "__main__":
    test_structured_not_found_is_surfaced()
    test_bodiless_404_falls_back_to_degrade_reason()
    test_non_json_404_falls_back()
    test_doc_get_wrapper_surfaces_reason()
    print(f"\n{'FAILED: ' + '; '.join(FAILS) if FAILS else 'all passed'}")
    sys.exit(1 if FAILS else 0)
