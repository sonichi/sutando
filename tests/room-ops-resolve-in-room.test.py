#!/usr/bin/env python3
"""room_ops `resolve_in_room`: the client half of the broker's `op: resolve_user`.

The broker resolves a query inside ONE room's roster, display names included,
and this is the first thing `mention` asks when the directory does not know a
handle. Pins the wire shape it sends and the answers it must read (verified
against matrix_ingest.py, 2026-09-10): a hit, "ambiguous: …" (a refusal that
carries the candidates — never a guess), "… not found" (a plain miss), and a
gateway that has no such op at all (HTTP 400 "unknown op", or a BARE 404/405),
which must read as `unsupported` so the caller falls back to the member list
instead of reporting a miss nobody measured.

2026-09-11, measured live: the broker signals a MISS with HTTP 404 and a JSON
body `{"error": "no member matching 'x' in this room — not found"}`. Every 404
used to read as `unsupported`, so `mention`'s `slug_handle` retry never ran on
a real miss and the flag misreported the broker. A 404 that carries JSON is
now the op answering; only a bare 404/405, or an "unknown op" error, is the
op missing. The request's User-Agent is pinned too: Cloudflare answers
urllib's default UA with 403 (error 1010), so the skill's own must go out.
"""
import importlib.util
import io
import json
import os
import sys
import urllib.error
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "agent-room-ops"))

spec = importlib.util.spec_from_file_location(
    "resolve", REPO / "skills" / "agent-room-ops" / "resolve.py")
rs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rs)
# `mention` must bind THIS resolve module (its `from resolve import …`), so the
# slug-retry check below drives the real resolve_in_room through it.
sys.modules["resolve"] = rs
spec_m = importlib.util.spec_from_file_location(
    "mention", REPO / "skills" / "agent-room-ops" / "mention.py")
mn = importlib.util.module_from_spec(spec_m)
spec_m.loader.exec_module(mn)
REAL_GATEWAY = rs.gateway

fails = []


def check(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        fails.append(msg)


ROOM = "!r:ag2.space"
BASSIL_AGENT = "@bassil-bassil-s-sutando.agent:ag2.space"
SONICHI = "@sutando-sonichi:ag2.space"


def _http_error(code, body):
    return urllib.error.HTTPError("https://example.invalid/v1/room", code, "x", {},
                                  io.BytesIO(body.encode()))


def _run(query, payload=None, *, raises=None, room=ROOM, headers=None):
    """Drive resolve_in_room with the gateway stubbed at the http_json seam;
    returns (result, what was sent)."""
    saved_g, saved_h = rs.gateway, rs.http_json
    sent = {}
    try:
        rs.gateway = lambda: ("https://example.invalid", dict(headers or {}))
        if raises is not None:
            def _boom(*a, **k):
                raise raises
            rs.http_json = _boom
        else:
            def _answer(method, url, headers, body):
                sent.update(method=method, url=url, headers=headers, payload=body)
                return 200, payload
            rs.http_json = _answer
        return rs.resolve_in_room(query, room), sent
    finally:
        rs.gateway, rs.http_json = saved_g, saved_h


def _drive(handle, answers, roster):
    """mention._resolve_from_room over the REAL resolve_in_room, the gateway
    answering each call in turn (an HTTPError is raised, anything else is a
    200 body); returns (result, the queries sent). `roster` is handed in so
    the members module is never imported (it would reach the network)."""
    saved_g, saved_h = rs.gateway, rs.http_json
    asked = []
    try:
        rs.gateway = lambda: ("https://example.invalid", {})

        def _answer(method, url, headers, body):
            asked.append(body["query"])
            a = answers[len(asked) - 1]
            if isinstance(a, Exception):
                raise a
            return 200, a
        rs.http_json = _answer
        return mn._resolve_from_room(handle, ROOM, "@me:ag2.space", roster=roster), asked
    finally:
        rs.gateway, rs.http_json = saved_g, saved_h


print("1. wire shape")
r, sent = _run("Bassil's Sutando", {"mxid": BASSIL_AGENT, "display_name": "Bassil's Sutando"})
check(sent["method"] == "POST" and sent["url"].endswith("/v1/room"), "POST /v1/room")
check(sent["payload"] == {"op": "resolve_user", "room_id": ROOM, "query": "Bassil's Sutando"},
      "payload is exactly {op, room_id, query}; the query goes as given (the broker "
      "matches case-insensitively, and the slug retry is the caller's)")

print("2. a hit")
check(r["ok"] is True and r["mxid"] == BASSIL_AGENT, "hit -> ok + mxid")
check(r["display_name"] == "Bassil's Sutando", "hit carries the display name")
check(r["candidates"] == [] and r["reason"] is None and r["unsupported"] is False,
      "hit has no candidates, no reason, and is not unsupported")
r, _ = _run("x", {"mxid": f"  {SONICHI}  "})
check(r["mxid"] == SONICHI and r["display_name"] == "", "mxid is stripped; a missing name is ''")

print("3. ambiguous -> a refusal carrying the candidates")
r, _ = _run("sutando", {"error": f"ambiguous: {SONICHI}, {BASSIL_AGENT}"})
check(r["ok"] is False and r["mxid"] is None, "ambiguous is not a hit")
check(r["candidates"] == [SONICHI, BASSIL_AGENT],
      "the mxids after 'ambiguous:' are the candidates, in the broker's order")
check(rs.is_ambiguous(r), "is_ambiguous reads it")
check(r["unsupported"] is False, "ambiguous is an answer, not a missing op")
check("ambiguous" in r["reason"], "the server's own text is the reason")

print("4. not found -> a plain miss")
r, _ = _run("nobody", {"error": "user 'nobody' not found in room"})
check(r["ok"] is False and r["candidates"] == [] and not rs.is_ambiguous(r),
      "not found: ok false, no candidates, not ambiguous")
check("not found" in r["reason"], "the server's own text is the reason")
check(r["unsupported"] is False, "a miss is not an unsupported op")

print("5. a gateway without the op -> unsupported")
r, _ = _run("x", raises=_http_error(400, '{"error": "unknown op resolve_user"}'))
check(r["ok"] is False and r["unsupported"] is True, "HTTP 400 'unknown op' -> unsupported")
check("unknown op" in r["reason"], "...and the reason says so")
r, _ = _run("x", raises=_http_error(404, ""))
check(r["ok"] is False and r["unsupported"] is True, "a BARE HTTP 404 (no body) -> unsupported")
r, _ = _run("x", raises=urllib.error.HTTPError("https://example.invalid/v1/room", 404, "x", {}, None))
check(r["ok"] is False and r["unsupported"] is True, "a 404 with no body object at all -> unsupported")
r, _ = _run("x", raises=_http_error(404, "<html>not found</html>"))
check(r["ok"] is False and r["unsupported"] is True, "a 404 with a non-JSON body -> unsupported")
r, _ = _run("x", raises=_http_error(405, ""))
check(r["ok"] is False and r["unsupported"] is True, "a bare HTTP 405 -> unsupported")
r, _ = _run("x", raises=_http_error(404, '{"error": "unknown op resolve_user"}'))
check(r["ok"] is False and r["unsupported"] is True and "unknown op" in r["reason"],
      "a 404 whose error names an unknown op -> unsupported, with the server's text")
r, _ = _run("x", raises=_http_error(400, '{"error": "query required"}'))
check(r["ok"] is False and r["unsupported"] is False and "query required" in r["reason"],
      "another 400 is a rejected query, not a missing op")
r, _ = _run("x", raises=_http_error(403, '{"error": "not a member"}'))
check(r["ok"] is False and r["unsupported"] is False and "403" in r["reason"],
      "403 keeps the membership diagnosis and is not unsupported")

print("5b. the live broker's 404 is a MISS, not a missing op (measured 2026-09-11)")
NOT_FOUND = json.dumps({"error": "no member matching 'Owner's Sutando' in this room — not found"})
r, _ = _run("Owner's Sutando", raises=_http_error(404, NOT_FOUND))
check(r["ok"] is False and r["mxid"] is None, "404 + not-found JSON: ok false, no mxid")
check(r["unsupported"] is False, "...and NOT unsupported — the op answered")
check(r["reason"] == json.loads(NOT_FOUND)["error"], "...the server's own text is the reason")
check(r["candidates"] == [] and not rs.is_ambiguous(r), "...a plain miss, not ambiguous")
r, _ = _run("sutando", raises=_http_error(404, f'{{"error": "ambiguous: {SONICHI}, {BASSIL_AGENT}"}}'))
check(r["candidates"] == [SONICHI, BASSIL_AGENT] and r["unsupported"] is False,
      "an 'ambiguous' body under a 404 still carries its candidates")
r, _ = _run("x", raises=_http_error(404, '{"detail": "gone"}'))
check(r["ok"] is False and r["unsupported"] is False and bool(r["reason"]),
      "a 404 with JSON but no error text is a miss with a reason, not a missing op")
r, _ = _run("x", raises=_http_error(404, f'{{"mxid": "{SONICHI}"}}'))
check(r["ok"] is False and r["mxid"] is None and "malformed" in r["reason"],
      "an mxid under an error status is a contradiction, never a resolve")
r, _ = _run("x", raises=_http_error(405, '{"error": "method not allowed"}'))
check(r["ok"] is False and r["unsupported"] is False and r["reason"] == "method not allowed",
      "a 405 with an error body is a refusal with the server's text, not a missing op")
for code, parsed, reason, want in ((404, None, "verb unimplemented (404)", True),
                                   (405, None, "HTTP 405", True),
                                   (404, {"error": "x not found"}, "x not found", False),
                                   (404, {}, "verb unimplemented (404)", False),
                                   (400, None, "unknown op resolve_user", True),
                                   (400, {"error": "query required"}, "query required", False),
                                   (403, None, "denied (403)", False)):
    check(rs.op_unsupported(code, parsed, reason) is want,
          f"op_unsupported({code}, {parsed!r}, {reason!r}) is {want}")

print("5c. the slug retry runs on a 404 miss — and not on a bare 404")
SUSAN = "@liususan091219-susan-s-bot.agent:ag2.space"
r, asked = _drive("Susan's bot", [_http_error(404, NOT_FOUND), {"mxid": SUSAN}], roster=[])
check(asked == ["Susan's bot", "susan-s-bot"], "the name misses with a 404 body, so the slug is tried")
check(r is not None and r["ok"] and r["mxid"] == SUSAN and r["resolved_by"] == "broker",
      "...and the slug hit resolves via the broker")
r, asked = _drive("Susan's bot", [_http_error(404, NOT_FOUND), _http_error(404, NOT_FOUND)],
                  roster=[{"user_id": SUSAN, "display_name": "Susan's bot"}])
check(asked == ["Susan's bot", "susan-s-bot"] and r["resolved_by"] == "room",
      "two 404 misses fall through to the roster in hand")
r, asked = _drive("Susan's bot", [_http_error(404, "")],
                  roster=[{"user_id": SUSAN, "display_name": "Susan's bot"}])
check(asked == ["Susan's bot"], "a BARE 404 is a missing op: no slug retry")
check(r is not None and r["ok"] and r["resolved_by"] == "room", "...and the roster answers")
r, asked = _drive("Susan's bot", [_http_error(404, NOT_FOUND), _http_error(404, NOT_FOUND)], roster=None)
check(asked == ["Susan's bot", "susan-s-bot"] and r is None,
      "an unreadable roster (None) is not re-read: nobody could answer")

print("6. junk bodies and transport failures never raise and never resolve")
for junk in ("not a dict", {}, [], {"mxid": "nobody"}, {"mxid": 7}, {"ok": True}, None):
    r, _ = _run("x", junk)
    check(r["ok"] is False and r["mxid"] is None and bool(r["reason"]),
          f"junk body {junk!r} -> ok false with a reason")
r, _ = _run("x", raises=TimeoutError("slow"))
check(r["ok"] is False and "network" in r["reason"], "timeout -> network reason")
r, _ = _run("x", raises=urllib.error.URLError("dns"))
check(r["ok"] is False and "network" in r["reason"], "URLError -> network reason")
r, _ = _run("x", raises=ValueError("not json"))
check(r["ok"] is False and "parse" in r["reason"], "a non-JSON 200 -> parse reason")

print("7. a declined answer without an `error` key")
r, _ = _run("x", {"ok": False, "reason": "rate limited"})
check(r["ok"] is False and r["reason"] == "rate limited" and not rs.is_ambiguous(r),
      "ok:false + reason -> the reason is relayed, not read as a hit")
r, _ = _run("x", {"ok": False})
check(r["ok"] is False and r["reason"] == "gateway declined",
      "ok:false with nothing else -> 'gateway declined'")

print("8. refusals before the network")
calls = []
saved_g, saved_h = rs.gateway, rs.http_json
try:
    rs.gateway = lambda: ("", {})
    rs.http_json = lambda *a, **k: calls.append(a) or (200, {})
    check(rs.resolve_in_room("x", ROOM)["reason"] == "no gateway configured",
          "no gateway configured is named, not a crash")
    rs.gateway = lambda: ("https://example.invalid", {})
    check(rs.resolve_in_room("", ROOM)["reason"] == "empty query", "empty query is refused")
    check(rs.resolve_in_room("   ", ROOM)["reason"] == "empty query", "blank query is refused")
    check(rs.resolve_in_room("x", "")["reason"] == "room_id required", "missing room is refused")
    check(calls == [], "none of those touched the network")
finally:
    rs.gateway, rs.http_json = saved_g, saved_h

print("9. the request carries the skill's User-Agent (Cloudflare 403s urllib's default)")
r, sent = _run("x", {"mxid": SONICHI}, headers={"User-Agent": "sutando-room-ops/1", "Authorization": "Bearer t"})
check(sent["headers"].get("User-Agent") == "sutando-room-ops/1",
      "resolve_in_room sends the gateway's headers as given, User-Agent included")
with mock.patch.dict(os.environ, {"GATEWAY_URL": "https://gw.example", "GATEWAY_TOKEN": "t"}):
    _base, real_headers = REAL_GATEWAY()
check(real_headers.get("User-Agent") == "sutando-room-ops/1",
      "gateway() itself sets User-Agent: sutando-room-ops/1")

if fails:
    print(f"\n{len(fails)} FAILURE(S)")
    raise SystemExit(1)
print("\nALL PASS — resolve_in_room speaks op:resolve_user and reads all of its answers")
