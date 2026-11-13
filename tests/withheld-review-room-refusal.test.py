#!/usr/bin/env python3
"""A private review DM whose room refuses the agent (HTTP 403 "sender is not in this room")
gets send_failure_policy.ROOM_REFUSAL_ATTEMPTS spaced sends, then the withheld result is settled
instead of re-sent on every 1 s scan, and the next withheld result reaches the owner through a
fresh private room (#5054)."""
import io
import json
import pathlib
import sys
import tempfile
import urllib.error
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow import remote_gateway_bridge as m  # noqa: E402

fail = 0
AGENT, OWNER = "@agent:ag2.space", "@owner:ag2.space"
LEFT, FRESH = "!left:ag2.space", "!fresh:ag2.space"
NOT_IN_ROOM = b'{"errcode": "M_FORBIDDEN", "error": "sender is not in this room"}'
BODY = "[channel: !elsewhere:ag2.space]\nrouted output\n"


def check(cond, label):
    global fail
    print(("PASS: " if cond else "FAIL: ") + label)
    if not cond:
        fail = 1


def forbidden(body):
    err = urllib.error.HTTPError("https://gw/v1/room", 403, "Forbidden", {}, io.BytesIO(body))
    err.sparrow_body = io.BytesIO(body)  # what _req keeps on every HTTPError it raises
    return err


def write_task(tasks, tid):
    (tasks / f"{tid}.txt").write_text(
        f"id: {tid}\nsource: ag2space\nchannel_id: !shared:ag2.space\n"
        "task: hello\naccess_tier: team\n", encoding="utf-8")


with tempfile.TemporaryDirectory() as td:
    root = pathlib.Path(td)
    names = ("TASKS_DIR", "_STATE", "_WITHHELD_DM_CACHE", "_WITHHELD_CONTROL_DIR", "_LOG_DIR",
             "_LOG_FILE", "_LOCK_WS", "_GATEWAY_OWNER_DM_HINT", "_reenroll_identity",
             "_gateway_owner", "_req")
    saved = {n: getattr(m, n) for n in names if hasattr(m, n)}
    saved_output = dict(m._WITHHELD_TASK_OUTPUT)
    tasks = root / "tasks"
    tasks.mkdir()
    m.TASKS_DIR, m._STATE = tasks, root / "state"
    m._WITHHELD_DM_CACHE = m._STATE / "withheld-review-dm.json"
    m._WITHHELD_CONTROL_DIR = m._STATE / "withheld-review-control-results"
    m._LOG_DIR, m._LOG_FILE, m._LOCK_WS = root / "logs", root / "logs" / "gateway-bridge.log", root
    m._GATEWAY_OWNER_DM_HINT = ""
    m._reenroll_identity = lambda: AGENT
    m._gateway_owner = lambda: OWNER
    m._WITHHELD_TASK_OUTPUT.clear()
    m._STATE.mkdir(parents=True)
    m._WITHHELD_DM_CACHE.write_text(json.dumps({"owner": OWNER, "room_id": LEFT, "private": True}))

    sent, refusal = [], [NOT_IN_ROOM]

    def fake_req(method, path, payload=None, timeout=35):
        if path == "/v1/room" and payload.get("op") == "create":
            return {"ok": True, "room_id": FRESH}
        if path == "/v1/room" and payload.get("op") == "message":
            sent.append(payload["room_id"])
            if payload["room_id"] == LEFT:
                raise forbidden(refusal[0])
            return {"ok": True, "event_id": f"$e{len(sent)}"}
        raise AssertionError((method, path, payload))

    m._req = fake_req
    try:
        # --- a 403 that is not the room's verdict: left for the next pass, as before
        for n, other in enumerate((b"<html>edge</html>",
                                   b'{"errcode": "M_FORBIDDEN", "error": "Invalid access token"}',
                                   b'{"errcode": "M_FORBIDDEN", "error": "sender is not a member of '
                                   b'this workspace"}')):
            sent.clear()
            refusal[0] = other
            write_task(tasks, f"task-other-{n}")
            got = [m._guarded_result_body(f"task-other-{n}", BODY) for _ in range(3)]
            check(all(body is None for body, _ in got), f"403 {other[:40]!r} stays for retry")
            check(sent.count(LEFT) == 3, "and is re-sent on each pass (unchanged behaviour)")
            check(json.loads(m._WITHHELD_DM_CACHE.read_text()).get("room_id") == LEFT,
                  "and the private review room stays cached")

        # --- the room refuses the sender: a bounded, spaced set of sends, then settled
        sent.clear()
        refusal[0] = NOT_IN_ROOM
        write_task(tasks, "task-refused")
        cap = m.ROOM_REFUSAL_ATTEMPTS if hasattr(m, "ROOM_REFUSAL_ATTEMPTS") else 5
        clock = [1000.0]
        got = []
        with mock.patch.object(m.time, "monotonic", lambda: clock[0]):
            for _ in range(60):  # one scan a second for a minute, as the result drain does
                got.append(m._guarded_result_body("task-refused", BODY))
                clock[0] += 1.0
        check(sent.count(LEFT) == cap, f"the refused review DM is sent {cap} times in 60 scans "
                                       f"(sent {sent.count(LEFT)})")
        settled = [g for g in got if g[0] is not None]
        check(bool(settled) and all(g == settled[0] for g in settled) and got[-1] == settled[0],
              "once settled, every later scan gets the same verdict")
        body, reason = settled[0] if settled else (None, None)
        check(body == "[no-send]", "the withheld result is still suppressed from the shared room")
        check("refused" in (reason or "") and "not in" in (reason or ""),
              "the reason names the room's refusal")
        record = json.loads(m.team_result_guard.withheld_review_artifact(
            m._STATE, "task-refused", BODY).read_text())
        check(record.get("status") == "pending_dm", "the review record is kept for the owner")
        check(json.loads(m._WITHHELD_DM_CACHE.read_text()).get("private") is not True,
              "the refusing room is no longer trusted as the private review room")

        # --- the next withheld result is not blocked: it reaches the owner in a new room
        sent.clear()
        write_task(tasks, "task-next")
        body, reason = m._guarded_result_body("task-next", BODY)
        check(body == "[no-send]" and LEFT not in sent and FRESH in sent,
              "the next review goes to a fresh private room")
    finally:
        for n, v in saved.items():
            setattr(m, n, v)
        m._WITHHELD_TASK_OUTPUT.clear()
        m._WITHHELD_TASK_OUTPUT.update(saved_output)
        getattr(m, "_REVIEW_ROOM_REFUSALS", {}).clear()

print("PASS: 0 failure(s)" if not fail else "FAIL: withheld review room refusal")
sys.exit(fail)
