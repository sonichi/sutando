#!/usr/bin/env python3
"""room_ops `resolve_in_room`: the client half of the broker's `op: resolve_user`.

The broker resolves a query inside ONE room's roster, display names included,
and this is the first thing `mention` asks when the directory does not know a
handle. Pins the wire shape it sends and the answers it must read (verified
against matrix_ingest.py, 2026-09-10): a hit, "ambiguous: …" (a refusal that
carries the candidates — never a guess), "… not found" (a plain miss), and a
gateway that has no such op at all (HTTP 400 "unknown op", or a 404), which
must read as `unsupported` so the caller falls back to the member list instead
of reporting a miss nobody measured.
"""
import importlib.util
import io
import sys
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "agent-room-ops"))

spec = importlib.util.spec_from_file_location(
    "resolve", REPO / "skills" / "agent-room-ops" / "resolve.py")
rs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rs)

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


def _run(query, payload=None, *, raises=None, room=ROOM):
    """Drive resolve_in_room with the gateway stubbed at the http_json seam;
    returns (result, what was sent)."""
    saved_g, saved_h = rs.gateway, rs.http_json
    sent = {}
    try:
        rs.gateway = lambda: ("https://example.invalid", {})
        if raises is not None:
            def _boom(*a, **k):
                raise raises
            rs.http_json = _boom
        else:
            def _answer(method, url, headers, body):
                sent.update(method=method, url=url, payload=body)
                return 200, payload
            rs.http_json = _answer
        return rs.resolve_in_room(query, room), sent
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
check(r["ok"] is False and r["unsupported"] is True, "HTTP 404 (verb unimplemented) -> unsupported")
r, _ = _run("x", raises=_http_error(400, '{"error": "query required"}'))
check(r["ok"] is False and r["unsupported"] is False and "query required" in r["reason"],
      "another 400 is a rejected query, not a missing op")
r, _ = _run("x", raises=_http_error(403, '{"error": "not a member"}'))
check(r["ok"] is False and r["unsupported"] is False and "403" in r["reason"],
      "403 keeps the membership diagnosis and is not unsupported")

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

if fails:
    print(f"\n{len(fails)} FAILURE(S)")
    raise SystemExit(1)
print("\nALL PASS — resolve_in_room speaks op:resolve_user and reads all of its answers")
