#!/usr/bin/env python3
"""A gateway lane never claims a proactive addressed to another homeserver's room.

Measured 2026-09-11T03:11Z on a three-lane host (prod ag2.space, dev, local-hs):
`results/proactive-1789096305.txt` carried `[channel: !oZUw…:ag2.space]`. The
local-hs lane (agent `@…:ag2space.local`, gateway localhost:9995) claimed it,
got HTTP 502 six times and PARKED it to results/undelivered/ — "it will NOT be
re-sent" — while the prod lane, the only one that could deliver it, never saw
it. Deliverable owner-facing work, lost to the wrong lane, silently.

The lane's own homeserver is derivable from its enrolled identity, so no fence
needs configuring: a `[channel: !room:server]` whose server is not the lane's
own is (a) never claimed at the peek, and (b) if seen only post-claim, handed
back rather than parked — same predicate, both sides.

  a) foreign-server room, send would fail    -> not claimed, not parked, untouched
  b) own-server room                         -> delivered, exactly one post (control)
  c) two lanes, one file                     -> exactly one delivery, no park
  d) identity unknown                        -> today's behaviour (claims), pinned
  e) foreign server seen only post-claim     -> released to .txt, never parked

Run: python3 tests/gateway-proactive-foreign-homeserver.test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
_ISOLATED = tempfile.mkdtemp(prefix="gw-foreign-hs-cc-")
os.environ["CLAUDE_CONFIG_DIR"] = _ISOLATED
os.environ["REMOTE_PROACTIVE_ROOM"] = ""

from ag2_sparrow import remote_gateway_bridge as gb  # noqa: E402

PROD_ROOM = "!oZUwTNaWEKAnsxPtkd:ag2.space"
PROD_AGENT = "@sutando-qingyun-001:ag2.space"
LOCAL_AGENT = "@qingyun-oss.agent:ag2space.local"
BODY = f"[channel: {PROD_ROOM}]\nSudoo progress — the guard rewrite is done.\n"

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _gateway_502() -> urllib.error.HTTPError:
    """The production shape: the drain catches urllib's HTTPError, nothing else."""
    return urllib.error.HTTPError("http://localhost:9995/relay/v1/room", 502, "Bad Gateway", {}, None)


def lane(identity: str, tmp: Path, posts: list, fail_post: bool = False):
    """Run ONE _post_proactive pass as a lane whose enrolled identity is `identity`.
    Every POST attempt is recorded BEFORE it is allowed to fail: a lane that claims
    a room it cannot deliver to shows up here as an attempt, not as silence."""
    def _fake_req(method, path, payload=None, timeout=None):
        if method == "POST":
            posts.append({"path": path, "payload": payload, "identity": identity})
            if fail_post:
                raise _gateway_502()
        return {"ok": True, "event_id": "$evt"}

    saved = (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.UNDELIVERABLE_RESULTS_DIR,
             gb.PROACTIVE_ROOM, gb._req, gb.PROACTIVE_CLAIM_GATE, os.environ.get("AGENT_MXID"))
    gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR = tmp, tmp / "archive"
    gb.UNDELIVERABLE_RESULTS_DIR = tmp / "undelivered"
    gb.PROACTIVE_ROOM, gb._req, gb.PROACTIVE_CLAIM_GATE = "", _fake_req, None
    gb._ROUTING.update(owner_dm="", loaded=True, next=gb.time.time() + 3600)
    os.environ["AGENT_MXID"] = identity
    try:
        gb._post_proactive()
    finally:
        (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.UNDELIVERABLE_RESULTS_DIR,
         gb.PROACTIVE_ROOM, gb._req, gb.PROACTIVE_CLAIM_GATE, prev) = saved
        if prev is None:
            os.environ.pop("AGENT_MXID", None)
        else:
            os.environ["AGENT_MXID"] = prev


def fresh() -> Path:
    gb._PROACTIVE_ATTEMPTS.clear()   # one retry budget per scenario, like one live process
    tmp = Path(tempfile.mkdtemp(prefix="gw-foreign-hs-"))
    (tmp / "archive").mkdir()
    (tmp / "proactive-1.txt").write_text(BODY, encoding="utf-8")
    return tmp


def names(tmp: Path) -> list[str]:
    return sorted(p.relative_to(tmp).as_posix() for p in tmp.rglob("*") if p.is_file())


def main() -> int:
    # a) the local-hs lane must leave the prod room alone even when its send would 502
    tmp = fresh(); posts: list = []
    for _ in range(7):  # more passes than the retry budget: nothing may accumulate
        lane(LOCAL_AGENT, tmp, posts, fail_post=True)
    check(posts == [], "a) foreign-server room: no post attempted")
    check(names(tmp) == ["proactive-1.txt"], f"a) file untouched, nothing parked: {names(tmp)}")

    # b) control: the prod lane delivers the same file
    tmp = fresh(); posts = []
    lane(PROD_AGENT, tmp, posts)
    check(len(posts) == 1 and posts[0]["payload"].get("room_id") == PROD_ROOM,
          f"b) own-server room delivered exactly once to {PROD_ROOM}: {posts}")
    check(not (tmp / "proactive-1.txt").exists() and not (tmp / "undelivered").exists(),
          "b) delivered file archived, nothing parked")

    # c) two lanes, one file: the foreign lane passes first and repeatedly
    tmp = fresh(); posts = []
    for _ in range(3):
        lane(LOCAL_AGENT, tmp, posts, fail_post=True)
    lane(PROD_AGENT, tmp, posts)
    lane(LOCAL_AGENT, tmp, posts, fail_post=True)
    check(len(posts) == 1 and posts[0]["identity"] == PROD_AGENT,
          f"c) two lanes, one file: exactly one attempt, by the prod lane ({[p['identity'] for p in posts]})")
    check(not (tmp / "undelivered").exists(), "c) no lane parked it")

    # d) identity unknown: today's behaviour is kept, and this pin says so
    tmp = fresh(); posts = []
    lane("", tmp, posts)
    check(len(posts) == 1, "d) unknown identity still claims and delivers (unchanged)")

    # e) foreign server seen only post-claim: released, never parked
    tmp = fresh(); posts = []
    real_route = gb._proactive_route
    calls = {"n": 0}

    def flip(body):
        calls["n"] += 1
        route, room, text = real_route(body)
        if calls["n"] == 1:               # the peek sees a room this lane may claim
            return route, PROD_ROOM.replace(":ag2.space", ":ag2space.local"), text
        return route, room, text          # post-claim: the real, foreign room
    gb._proactive_route = flip
    try:
        lane(LOCAL_AGENT, tmp, posts, fail_post=True)
    finally:
        gb._proactive_route = real_route
    check(posts == [], f"e) post-claim foreign server: no post attempted ({len(posts)})")
    check(names(tmp) == ["proactive-1.txt"],
          f"e) post-claim foreign server: handed back as .txt, not parked: {names(tmp)}")

    # the predicate itself — guarded, so a tree WITHOUT the fix reports the
    # behavioural failures above instead of aborting here on the missing symbol
    has_predicate = hasattr(gb, "_room_is_deliverable_here") and hasattr(gb, "_own_homeserver")
    check(has_predicate, "predicate: _room_is_deliverable_here and _own_homeserver exist")
    if has_predicate:
        os.environ["AGENT_MXID"] = LOCAL_AGENT
        check(gb._room_is_deliverable_here("!x:ag2.space") is False, "predicate: prod room not deliverable from local-hs")
        check(gb._room_is_deliverable_here("!x:ag2space.local") is True, "predicate: own room deliverable")
        os.environ["AGENT_MXID"] = ""
        check(gb._own_homeserver() == "", "predicate: no identity -> no homeserver")
        os.environ.pop("AGENT_MXID", None)

    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}")
        return 1
    print("\nPASS — a lane claims only rooms on its own homeserver; a wrong claim releases, never parks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
