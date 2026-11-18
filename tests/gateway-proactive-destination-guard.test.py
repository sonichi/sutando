#!/usr/bin/env python3
"""The AG2 gateway guards a proactive body redirected to a room other than the owner
DM at a non-owner tier, through the shared guard; the owner DM is unchanged (#4971).

  a) [channel: room] + an internal error / local path -> the room gets the standard notice
  b) the same body with no redirect                    -> the owner DM gets it unchanged
  c) [channel: <owner DM>] + the same body             -> unchanged (it is the owner's DM)
  d) [channel: room] + clean prose                     -> posted unchanged
  e) the drain delegates to guard_proactive_for_destination, not a copy

Imports the vendored module with an isolated env; never execs the wrapper.
Run: python3 tests/gateway-proactive-destination-guard.test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
_ISO = Path(tempfile.mkdtemp(prefix="gw-proactive-dest-guard-"))
for _k in ("task", "result", "state"):
    (_ISO / _k).mkdir()
os.environ.update({
    "HOME": str(_ISO / "home"),
    "CLAUDE_CONFIG_DIR": str(_ISO / "claude"),
    "REMOTE_TASK_TOKEN": "https://gw.example/relay|test-token",
    "REMOTE_PROACTIVE_ROOM": "",
    "AGENT_CONNECT_TASK_DIR": str(_ISO / "task"),
    "AGENT_CONNECT_RESULT_DIR": str(_ISO / "result"),
    "AGENT_CONNECT_STATE_DIR": str(_ISO / "state"),
})

from ag2_sparrow import remote_gateway_bridge as gb  # noqa: E402

ROOM = "!TargetRoomAbCdEf:ag2.space"
OWNER_DM = "!OwnerDmRoomXyZ:ag2.space"
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def drain(body: str):
    """One _post_proactive pass over one file; returns (sent payloads, log lines)."""
    tmp = Path(tempfile.mkdtemp(dir=_ISO, prefix="drain-"))
    (tmp / "archive").mkdir()
    (tmp / "proactive-1.txt").write_text(body, encoding="utf-8")
    posts: list[dict] = []
    logs: list[str] = []

    def _fake_req(method, path, payload=None, timeout=None, **_kw):
        if method == "POST":
            posts.append(payload)
        return {"ok": True, "event_id": "$evt"}

    saved = (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.PROACTIVE_ROOM,
             gb._req, gb.PROACTIVE_CLAIM_GATE, gb._log)
    gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR = tmp, tmp / "archive"
    gb.PROACTIVE_ROOM, gb._req, gb.PROACTIVE_CLAIM_GATE = OWNER_DM, _fake_req, None
    gb._log = logs.append
    gb._ROUTING.update(owner_dm=OWNER_DM, loaded=True, next=gb.time.time() + 3600)
    try:
        gb._post_proactive()
    finally:
        (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.PROACTIVE_ROOM,
         gb._req, gb.PROACTIVE_CLAIM_GATE, gb._log) = saved
        gb._ROUTING.update(owner_dm="", loaded=False, next=0.0)
    return posts, logs


def main() -> int:
    notice = gb.team_result_guard.TEAM_LEAK_RESULT
    for label, leak in (("errno", "copy failed: [Errno 2] No such file"),
                        ("home path", "wrote /Users/someone/notes/plan.md"),
                        ("windows path", "wrote C:\\Users\\someone\\plan.md"),
                        ("tmp path", "see /tmp/sutando-run/out.log")):
        posts, logs = drain(f"[channel: {ROOM}]\n{leak}\n")
        check([(p.get("room_id"), p.get("body")) for p in posts] == [(ROOM, notice)],
              f"a) {label}: the room gets only the standard notice; got {posts}")
        check(any("withheld proactive" in m for m in logs), f"a) {label}: the withhold is logged")
        posts, _ = drain(f"{leak}\n")
        check([(p.get("room_id"), p.get("body")) for p in posts] == [(OWNER_DM, leak)],
              f"b) {label}: the owner DM gets it unchanged; got {posts}")
        posts, _ = drain(f"[channel: {OWNER_DM}]\n{leak}\n")
        check([(p.get("room_id"), p.get("body")) for p in posts] == [(OWNER_DM, leak)],
              f"c) {label}: a redirect to the owner DM is unchanged; got {posts}")

    posts, _ = drain(f"[channel: {ROOM}]\nthe weekly summary is ready\n")
    check([(p.get("room_id"), p.get("body")) for p in posts] == [(ROOM, "the weekly summary is ready")],
          f"d) clean prose is posted unchanged; got {posts}")

    source = (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "remote_gateway_bridge.py").read_text()
    check("team_result_guard.guard_proactive_for_destination(" in source,
          "e) the drain delegates to the shared guard")

    print(f"\n{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
