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


def test_401_keeps_the_token_diagnosis_even_with_a_structured_body():
    # bassilkhilo-ag2's blocker on #2375. The `except HTTPError` change reaches
    # EVERY status the caller can receive, not only the 404 it was written for.
    # A gateway 401 carrying a membership-flavoured body previously rendered as
    # a membership verdict, sending a debugger to room membership when the real
    # fault is the bearer token. degrade_reason() must stay authoritative here.
    _patch(lambda *a, **k: (_ for _ in ()).throw(
        _httperror(401, b'{"error": "denied - agent not a joined member"}')))
    res = D._call("prep_get", "!r:x", "@me:x", {}, {})
    reason = res.get("reason") or ""
    check(reason.startswith("auth failed"),
          "401 + membership-flavoured body -> still diagnoses the TOKEN")
    check("(server said: denied - agent not a joined member)" in reason,
          "401 -> the server's message is appended, not discarded")


def test_403_keeps_the_membership_diagnosis_even_with_a_structured_body():
    # The mirror case: a real membership denial worded like a missing doc must
    # not read as "not found", or an access problem looks like a content problem.
    _patch(lambda *a, **k: (_ for _ in ()).throw(
        _httperror(403, b'{"error": "roadmap/plan.md not found"}')))
    res = D._call("doc_get", "!r:x", "@me:x", {}, {"folder": "roadmap"})
    reason = res.get("reason") or ""
    check(reason.startswith("denied"),
          "403 + not-found-flavoured body -> still diagnoses MEMBERSHIP")
    check("(server said: roadmap/plan.md not found)" in reason,
          "403 -> the server's message is appended, not discarded")


def test_non_auth_statuses_still_prefer_the_server_message():
    # CALIBRATION. The two guards above assert that a body did NOT win; both
    # would also pass if the override had been deleted outright, which would
    # regress this PR's entire purpose. Pin that the override still works where
    # it is safe — 404 (the PR's target) and a generic 5xx, where degrade_reason
    # has no diagnosis to protect and "HTTP 500" is strictly less useful.
    _patch(lambda *a, **k: (_ for _ in ()).throw(
        _httperror(404, b'{"error": "roadmap/plan.md not found"}')))
    res = D._call("doc_get", "!r:x", "@me:x", {}, {"folder": "roadmap"})
    check(res.get("reason") == "roadmap/plan.md not found",
          "404 -> server message still wins (the PR's purpose is intact)")

    _patch(lambda *a, **k: (_ for _ in ()).throw(
        _httperror(500, b'{"error": "doc backend unavailable"}')))
    res = D._call("doc_get", "!r:x", "@me:x", {}, {})
    check(res.get("reason") == "doc backend unavailable",
          "500 -> server message still wins over the generic 'HTTP 500'")


def test_auth_status_without_a_body_is_unchanged():
    # No body to append: the reason must be exactly degrade_reason(), with no
    # dangling "(server said: )" fragment.
    _patch(lambda *a, **k: (_ for _ in ()).throw(_httperror(401, b"")))
    res = D._call("doc_get", "!r:x", "@me:x", {}, {})
    check(res.get("reason") == "auth failed — check the gateway bearer token (401)",
          "bodiless 401 -> plain degrade_reason, no empty 'server said' suffix")


if __name__ == "__main__":
    test_structured_not_found_is_surfaced()
    test_bodiless_404_falls_back_to_degrade_reason()
    test_non_json_404_falls_back()
    test_doc_get_wrapper_surfaces_reason()
    test_401_keeps_the_token_diagnosis_even_with_a_structured_body()
    test_403_keeps_the_membership_diagnosis_even_with_a_structured_body()
    test_non_auth_statuses_still_prefer_the_server_message()
    test_auth_status_without_a_body_is_unchanged()
    print(f"\n{'FAILED: ' + '; '.join(FAILS) if FAILS else 'all passed'}")
    sys.exit(1 if FAILS else 0)
