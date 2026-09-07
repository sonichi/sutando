#!/usr/bin/env python3
"""A proactive file naming its own Matrix room delivers without PROACTIVE_ROOM.

`_proactive_route` has always resolved `[channel: !room:server]` to that room, and
the send already reads `room_override or PROACTIVE_ROOM`. But `_post_proactive`
returned at its first line when PROACTIVE_ROOM was unset, so the per-message target
was unreachable on any host without a global default room — two different settings,
and the absence of the default suppressed the explicit one.

Measured live before this change: the Discord bridge correctly released a
Matrix-addressed file (#2997), nothing consumed it, and it cycled
released -> re-claimed -> released ~21x/min, unbounded and undelivered.

  a) explicit room + no default   -> delivered to the NAMED room
  b) explicit room + a default    -> still the named room, default not consulted
  c) no target + no default       -> skipped WITHOUT claiming (a claim would spin)
  d) no target + a default        -> delivered to the default (unchanged)
  e) foreign target + no default  -> left for its own bridge, never claimed
  f) RESTART: a dead-pid claim recovers to .txt and then delivers to its named room
  g) a LIVE pid's in-flight claim is still never stolen
  h) a target present at PEEK but gone POST-CLAIM is handed back, not posted

Run: python3 tests/gateway-proactive-explicit-room-without-default.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
# Import-time config resolution must not read the developer's real channel dir.
_ISOLATED = tempfile.mkdtemp(prefix="gw-proactive-cc-")
os.environ["CLAUDE_CONFIG_DIR"] = _ISOLATED
os.environ["REMOTE_PROACTIVE_ROOM"] = ""

from ag2_sparrow import remote_gateway_bridge as gb  # noqa: E402

ROOM = "!TargetRoomAbCdEf:ag2.space"
DEFAULT_ROOM = "!DefaultRoomXyZ:ag2.space"
DISCORD_ID = "1530802402603700415"

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _dead_pid() -> int:
    """A pid that is certainly not running: spawn one and reap it."""
    import subprocess as sp
    pr = sp.Popen([sys.executable, "-c", "pass"])
    pr.wait()
    return pr.pid


def drain(body: str, default_room: str):
    """Run one _post_proactive pass over a single file. Returns (posts, leftovers)."""
    tmp = Path(tempfile.mkdtemp(prefix="gw-proactive-"))
    (tmp / "archive").mkdir()
    src = tmp / "proactive-1.txt"
    src.write_text(body, encoding="utf-8")
    posts: list[dict] = []

    def _fake_req(method, path, payload=None, timeout=None):
        if method == "POST":  # the routing lookup is a GET, not a send
            posts.append({"path": path, "payload": payload})
        return {"ok": True, "event_id": "$evt"}

    saved = (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.PROACTIVE_ROOM,
             gb._req, gb.PROACTIVE_CLAIM_GATE)
    gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR = tmp, tmp / "archive"
    gb.PROACTIVE_ROOM, gb._req, gb.PROACTIVE_CLAIM_GATE = default_room, _fake_req, None
    try:
        gb._post_proactive()
    finally:
        (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.PROACTIVE_ROOM,
         gb._req, gb.PROACTIVE_CLAIM_GATE) = saved
    return posts, sorted(p.name for p in tmp.iterdir() if p.is_file())


def main() -> int:
    # a) THE GAP: named room, no default.
    posts, left = drain(f"[channel: {ROOM}]\nhello room\n", "")
    check(len(posts) == 1, f"a) explicit room + no default -> one post, got {len(posts)}")
    check(posts and posts[0]["payload"].get("room_id") == ROOM,
          f"a) delivered to the NAMED room, got {posts[0]['payload'].get('room_id') if posts else None}")
    check(posts and "channel:" not in posts[0]["payload"].get("body", ""),
          "a) the marker is stripped from the delivered body")
    check(left == [], f"a) the file is retired, got {left}")

    # b) A default must not win over an explicit target.
    posts, _ = drain(f"[channel: {ROOM}]\nhello room\n", DEFAULT_ROOM)
    check(posts and posts[0]["payload"].get("room_id") == ROOM,
          f"b) explicit beats default, got {posts[0]['payload'].get('room_id') if posts else None}")

    # c) Skip BEFORE the claim: claiming a file with nowhere to go means
    #    claim -> hand back -> re-claim, every pass, forever.
    posts, left = drain("no target here\n", "")
    check(posts == [], f"c) no target + no default -> nothing sent, got {len(posts)}")
    check(left == ["proactive-1.txt"],
          f"c) and left UNCLAIMED as .txt (no .sending), got {left}")

    # d) Pre-existing behaviour is unchanged.
    posts, left = drain("no target here\n", DEFAULT_ROOM)
    check(posts and posts[0]["payload"].get("room_id") == DEFAULT_ROOM,
          f"d) no target + default -> the default, got {posts[0]['payload'].get('room_id') if posts else None}")
    check(left == [], f"d) and retired, got {left}")

    # e) Removing the early return must not make this bridge start eating another
    #    bridge's files — the foreign check now runs on hosts it never ran on.
    posts, left = drain(f"[channel: {DISCORD_ID}]\nfor discord\n", "")
    check(posts == [], f"e) foreign target -> nothing sent, got {len(posts)}")
    check(left == ["proactive-1.txt"], f"e) and left for its own bridge, got {left}")

    # f) RESTART PATH. This PR also removed the no-default early return from
    #    _recover_orphan_proactive, and only the drain was covered until now.
    tmp = Path(tempfile.mkdtemp(prefix="gw-proactive-recover-"))
    (tmp / "archive").mkdir()
    dead = _dead_pid()
    claim = tmp / f"proactive-9.sending.{dead}"
    claim.write_text(f"[channel: {ROOM}]\nrecovered body\n", encoding="utf-8")
    posts: list = []

    saved = (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.PROACTIVE_ROOM,
             gb._req, gb.PROACTIVE_CLAIM_GATE)
    gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR = tmp, tmp / "archive"
    gb.PROACTIVE_ROOM = ""
    gb._req = lambda m, path, payload=None, timeout=None: (
        posts.append(payload) or {"ok": True, "event_id": "$evt"})
    gb.PROACTIVE_CLAIM_GATE = None
    try:
        gb._recover_orphan_proactive()
        recovered = (tmp / "proactive-9.txt").exists()
        gb._post_proactive()
    finally:
        (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.PROACTIVE_ROOM,
         gb._req, gb.PROACTIVE_CLAIM_GATE) = saved

    check(recovered, "f) a dead-pid claim recovers to .txt with no default room")
    check(len(posts) == 1, f"f) and the next drain delivers it, got {len(posts)} post(s)")
    check(posts and posts[0].get("room_id") == ROOM,
          f"f) to its NAMED room, got {posts[0].get('room_id') if posts else None}")
    check(sorted(q.name for q in tmp.iterdir() if q.is_file()) == [],
          "f) nothing left behind in results/")

    # g) A LIVE pid's claim is still never stolen — the guard this PR did not touch,
    #    asserted here because f) is the first test to exercise this function at all.
    tmp2 = Path(tempfile.mkdtemp(prefix="gw-proactive-live-"))
    live = tmp2 / f"proactive-10.sending.{os.getpid()}"
    live.write_text(f"[channel: {ROOM}]\nin flight\n", encoding="utf-8")
    saved_dir = gb.RESULTS_DIR
    gb.RESULTS_DIR = tmp2
    try:
        gb._recover_orphan_proactive()
    finally:
        gb.RESULTS_DIR = saved_dir
    check(live.exists() and not (tmp2 / "proactive-10.txt").exists(),
          "g) a LIVE pid's in-flight claim is left alone")

    # h) `_proactive_route` runs twice per file (peek, then post-claim), so a
    #    target can be present at peek and gone after. a-g never make them differ.
    tmp3 = Path(tempfile.mkdtemp(prefix="gw-proactive-vanish-"))
    (tmp3 / "archive").mkdir()
    (tmp3 / "proactive-11.txt").write_text(
        f"[channel: {ROOM}]\nvanishing target\n", encoding="utf-8")
    posts3: list = []
    calls = {"n": 0}

    def _vanishing_route(body):
        calls["n"] += 1
        # 1st = peek (has a room), 2nd = post-claim (target gone).
        return ("send", ROOM if calls["n"] == 1 else None, "vanishing target")

    saved = (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.PROACTIVE_ROOM,
             gb._req, gb.PROACTIVE_CLAIM_GATE, gb._proactive_route)
    gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR = tmp3, tmp3 / "archive"
    gb.PROACTIVE_ROOM = ""
    gb._req = lambda m, path, payload=None, timeout=None: (
        posts3.append(payload) or {"ok": True, "event_id": "$evt"})
    gb.PROACTIVE_CLAIM_GATE = None
    gb._proactive_route = _vanishing_route
    try:
        gb._post_proactive()
    finally:
        (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.PROACTIVE_ROOM,
         gb._req, gb.PROACTIVE_CLAIM_GATE, gb._proactive_route) = saved

    check(calls["n"] == 2, f"h) the file is routed twice (peek + post-claim), got {calls['n']}")
    check(posts3 == [], f"h) target vanished post-claim -> nothing sent, got {len(posts3)}")
    check((tmp3 / "proactive-11.txt").exists(),
          "h) the file is HANDED BACK as .txt rather than posted to room_id=None")
    check(not list(tmp3.glob("proactive-11.sending*")),
          "h) and no claim is left holding it")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
        return 1
    print("PASS — an explicitly addressed proactive needs no default room")
    return 0


if __name__ == "__main__":
    sys.exit(main())
