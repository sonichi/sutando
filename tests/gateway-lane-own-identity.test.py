#!/usr/bin/env python3
"""A secondary lane delivers its own homeserver's proactive messages, judged by its own identity.

Measured on a three-lane host: the two secondary lanes had no AGENT_MXID of their own, so
`agent_identity.resolve` fell through to the host's binding, the primary lane's agent on another
homeserver. Every lane then judged every `[channel: !room:<secondary server>]` file as another
lane's and skipped it without a log line; ten owner messages sat for eight hours.

  a) resolve, secondary lane: never the host binding; its relay's whoami, recorded and reused
  b) resolve, primary lane: the host binding still answers (unchanged)
  c) drain, secondary lane with no env identity: its own server's room is delivered
  d) drain, secondary lane whose identity is unknown: held, and the log says so (backed off)
  e) drain, a room on another server: left, and the log says so once per backoff step
  f) health check: a proactive file unclaimed for 10 minutes fails the probe; a fresh one does not

Run: python3 tests/gateway-lane-own-identity.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
sys.path.insert(0, str(REPO / "src"))
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="gw-lane-id-cc-")
os.environ["REMOTE_PROACTIVE_ROOM"] = ""

from ag2_sparrow import agent_identity  # noqa: E402
from ag2_sparrow import remote_gateway_bridge as gb  # noqa: E402

HOST_AGENT = "@agent-a:ag2.space"
LANE_AGENT = "@agent-a:s.space"
LANE_ROOM = "!roomS:s.space"
OTHER_ROOM = "!roomD:dev.s.space"

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def state_with_binding() -> Path:
    state = Path(tempfile.mkdtemp(prefix="gw-lane-id-state-"))
    (state / "auth").mkdir()
    (state / "auth" / "ag2space.json").write_text(json.dumps({"agent_id": HOST_AGENT}))
    return state


def no_channel(_key):
    return ""


def resolve_cases() -> None:
    state = state_with_binding()
    asked: list[int] = []

    def whoami():
        asked.append(1)
        return LANE_AGENT

    got = agent_identity.resolve(state, no_channel, environ={}, instance="s-space")
    check(got == "", f"a) no whoami: unknown, never the host binding ({got!r})")
    got = agent_identity.resolve(state, no_channel, environ={}, instance="s-space", whoami=lambda: "")
    check(got == "", f"a) whoami gave nothing: unknown ({got!r})")
    got = agent_identity.resolve(state, no_channel, environ={}, instance="s-space", whoami=lambda: 1 / 0)
    check(got == "", f"a) whoami raised: unknown ({got!r})")
    got = agent_identity.resolve(state, no_channel, environ={}, instance="s-space",
                                 whoami=lambda: "not an mxid")
    check(got == "", f"a) whoami gave a malformed id: unknown ({got!r})")
    got = agent_identity.resolve(state, no_channel, environ={}, instance="s-space", whoami=whoami)
    check(got == LANE_AGENT and len(asked) == 1, f"a) whoami answers the lane ({got!r})")
    rec = json.loads((state / "auth" / "lane-agent.s-space.json").read_text())
    check(rec.get("agent_id") == LANE_AGENT, "a) the answer is recorded per lane")
    got = agent_identity.resolve(state, no_channel, environ={}, instance="s-space", whoami=whoami)
    check(got == LANE_AGENT and len(asked) == 1, "a) the record answers next time, no second whoami")
    got = agent_identity.resolve(state, no_channel, environ={}, instance="dev-s-space")
    check(got == "", "a) another lane does not read this lane's record")
    got = agent_identity.resolve(state, no_channel, environ={"AGENT_MXID": "@x:dev.s.space"},
                                 instance="s-space", whoami=whoami)
    check(got == "@x:dev.s.space", "a) an env identity still wins for a secondary lane")
    (state / "auth" / "lane-agent.dev-s-space.json").write_text("{not json")
    got = agent_identity.resolve(state, no_channel, environ={}, instance="dev-s-space",
                                 whoami=lambda: "@agent-a:dev.s.space")
    check(got == "@agent-a:dev.s.space", "a) an unreadable record is asked again")
    check(agent_identity.lane_agent(None, "s-space", whoami) == "", "a) no state dir: unknown")

    got = agent_identity.resolve(state, no_channel, environ={})
    check(got == HOST_AGENT, f"b) the primary lane keeps the host binding ({got!r})")


def drain(state: Path, results: Path, instance: str, whoami_answer, posts: list, logs: list) -> None:
    def fake_req(method, path, payload=None, timeout=None, **_kw):
        if method == "GET" and path == "/v1/whoami":
            if isinstance(whoami_answer, Exception):
                raise whoami_answer
            return {"agent_mxid": whoami_answer}
        if method == "POST":
            posts.append(payload)
        return {"ok": True, "event_id": "$evt"}

    saved = (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.UNDELIVERABLE_RESULTS_DIR, gb.PROACTIVE_ROOM,
             gb._req, gb.PROACTIVE_CLAIM_GATE, gb._STATE, gb.GATEWAY_INSTANCE, gb._log)
    gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR = results, results / "archive"
    gb.UNDELIVERABLE_RESULTS_DIR = results / "undelivered"
    gb.PROACTIVE_ROOM, gb._req, gb.PROACTIVE_CLAIM_GATE = "", fake_req, None
    gb._STATE, gb.GATEWAY_INSTANCE, gb._log = state, instance, logs.append
    gb._ROUTING.update(owner_dm="", loaded=True, next=gb.time.time() + 3600)
    try:
        gb._post_proactive()
    finally:
        (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.UNDELIVERABLE_RESULTS_DIR, gb.PROACTIVE_ROOM,
         gb._req, gb.PROACTIVE_CLAIM_GATE, gb._STATE, gb.GATEWAY_INSTANCE, gb._log) = saved


def results_with(room: str) -> Path:
    results = Path(tempfile.mkdtemp(prefix="gw-lane-id-results-"))
    (results / "archive").mkdir()
    (results / "proactive-1.txt").write_text(f"[channel: {room}]\nThe guard rewrite is done.\n")
    return results


def drain_cases() -> None:
    os.environ.pop("AGENT_MXID", None)

    state, results, posts, logs = state_with_binding(), results_with(LANE_ROOM), [], []
    drain(state, results, "s-space", LANE_AGENT, posts, logs)
    check(len(posts) == 1 and posts[0].get("room_id") == LANE_ROOM,
          f"c) the secondary lane delivers its own server's room ({posts})")
    check(not (results / "proactive-1.txt").exists(), "c) the delivered file left results/")

    gb._LOG_COUNTS.clear()
    state, results, posts, logs = state_with_binding(), results_with(LANE_ROOM), [], []
    for _ in range(4):
        drain(state, results, "s-space", OSError("relay down"), posts, logs)
    held = [m for m in logs if "own identity is unknown" in m]
    check(posts == [] and (results / "proactive-1.txt").exists(), "d) unknown identity: held, not sent")
    check(len(held) == 3 and "seen 4 times" in held[-1],
          f"d) the hold is logged on the 1st, 2nd and 4th pass ({len(held)}: {held[-1:]})")
    check(any("whoami failed" in m for m in logs), "d) the failed whoami is logged")

    gb._LOG_COUNTS.clear()
    state, results, posts, logs = state_with_binding(), results_with(OTHER_ROOM), [], []
    for _ in range(2):
        drain(state, results, "s-space", LANE_AGENT, posts, logs)
    left = [m for m in logs if "left for another lane" in m]
    check(posts == [] and (results / "proactive-1.txt").exists(), "e) another server's room is not sent")
    check(len(left) == 2 and "dev.s.space" in left[0] and "this lane is s.space" in left[0],
          f"e) the skip names both servers ({left[:1]})")


def health_cases() -> None:
    spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    results = Path(tempfile.mkdtemp(prefix="gw-lane-id-hc-"))
    now = time.time()
    got = hc.check_proactive_undelivered(results, now)
    check(got["status"] == "ok", f"f) empty results/: ok ({got['detail']})")
    fresh = results / "proactive-2.txt"
    fresh.write_text("x")
    os.utime(fresh, (now - 60, now - 60))
    check(hc.check_proactive_undelivered(results, now)["status"] == "ok", "f) a fresh file: ok")
    stuck = results / "proactive-ask-1.txt"
    stuck.write_text("x")
    os.utime(stuck, (now - 1800, now - 1800))
    got = hc.check_proactive_undelivered(results, now)
    check(got["status"] == "fail" and "1 proactive" in got["detail"] and "30 min" in got["detail"],
          f"f) a file unclaimed for 30 min fails ({got['detail']})")
    got = hc.check_proactive_undelivered(results / "missing", now)
    check(got["status"] == "ok", "f) a missing results/ is no reading, not a failure")


def main() -> int:
    resolve_cases()
    drain_cases()
    health_cases()
    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}")
        return 1
    print("\nPASS: a secondary lane judges rooms by its own identity, and a stuck message is never silent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
