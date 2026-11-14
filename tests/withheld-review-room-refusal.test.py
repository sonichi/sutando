#!/usr/bin/env python3
"""A private review DM whose room refuses the agent (HTTP 403 "sender is not in this room")
gets send_failure_policy.ROOM_REFUSAL_ATTEMPTS spaced sends, then is sent once to a new private room
and the result is settled instead of re-sent on every 1 s scan; if that room refuses too, the
review is reported as dropped (#5054)."""
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


    sent, refusal, refusing = [], [NOT_IN_ROOM], {LEFT}
    created = [FRESH]

    def fake_req(method, path, payload=None, timeout=35):
        if path == "/v1/room" and payload.get("op") == "create":
            return {"ok": True, "room_id": created[0]}
        if path == "/v1/room" and payload.get("op") == "message":
            sent.append(payload["room_id"])
            if payload["room_id"] in refusing:
                raise forbidden(refusal[0])
            return {"ok": True, "event_id": f"$e{len(sent)}"}
        raise AssertionError((method, path, payload))

    def scan_for_a_minute(tid):
        clock, got = [1000.0], []
        with mock.patch.object(m.time, "monotonic", lambda: clock[0]):
            for _ in range(60):  # one scan a second, as the result drain does
                got.append(m._guarded_result_body(tid, BODY))
                clock[0] += 1.0
        return got

    def record_of(tid):
        return json.loads(m.team_result_guard.withheld_review_artifact(m._STATE, tid, BODY).read_text())

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

        # --- the room refuses the sender: bounded sends, then THIS review goes to a new room
        sent.clear()
        refusal[0] = NOT_IN_ROOM
        cap = m.ROOM_REFUSAL_ATTEMPTS if hasattr(m, "ROOM_REFUSAL_ATTEMPTS") else 5
        write_task(tasks, "task-refused")
        got = scan_for_a_minute("task-refused")
        check(sent.count(LEFT) == cap, f"the refused review DM is sent {cap} times in 60 scans "
                                       f"(sent {sent.count(LEFT)})")
        check(sent.count(FRESH) == 2, "then the same review is sent once, to a new private room "
                                      f"(its 2 parts; sent {sent.count(FRESH)})")
        settled = [g for g in got if g[0] is not None]
        check(bool(settled) and all(g == settled[0] for g in settled) and got[-1] == settled[0],
              "once settled, every later scan gets the same verdict")
        check((settled[0][0] if settled else None) == "[no-send]",
              "the withheld result is still suppressed from the shared room")
        record = record_of("task-refused")
        check((record.get("status"), record.get("dm_room_id")) == ("awaiting_owner", FRESH),
              f"the owner's review is open in the new room ({record.get('status')})")
        cache = json.loads(m._WITHHELD_DM_CACHE.read_text())
        check((cache.get("room_id"), cache.get("private")) == (FRESH, True),
              "the new room is the cached private review room")

        # --- the next withheld result goes straight to the new room
        sent.clear()
        write_task(tasks, "task-next")
        body, _ = m._guarded_result_body("task-next", BODY)
        check(body == "[no-send]" and sent == [FRESH, FRESH], "the next review goes to the new room")

        # --- every room refuses: settled, and the log and reason say the review was dropped
        sent.clear()
        refusing.update({FRESH, "!fresh2:ag2.space"})
        created[0] = "!fresh2:ag2.space"
        logged = []
        with mock.patch.object(m, "_log", logged.append):
            write_task(tasks, "task-dropped")
            got = scan_for_a_minute("task-dropped")
        check(sent.count(FRESH) == cap and sent.count("!fresh2:ag2.space") == 1,
              f"bounded sends, then one to a new room ({sent})")
        settled = [g for g in got if g[0] is not None]
        reason = settled[0][1] if settled else ""
        check(bool(settled) and "dropped" in reason, "the reason says the owner review was dropped")
        check(any("DROPPED" in line for line in logged), "and so does the log line")
        check(record_of("task-dropped").get("status") == "pending_dm", "the record is kept on disk")

        def refuse_then(tid, room, on_fifth):
            """The cached room refuses the agent; `on_fifth(record path)` runs as the fifth refusal is raised."""
            sent.clear()
            refusing.clear()
            refusing.add(LEFT)
            created[0] = room
            m._WITHHELD_DM_CACHE.write_text(json.dumps({"owner": OWNER, "room_id": LEFT, "private": True}))
            write_task(tasks, tid)
            record = []

            def req(method, path, payload=None, timeout=35):
                if payload.get("op") == "message" and payload["room_id"] == LEFT:
                    record[:] = record or [m.team_result_guard.withheld_review_artifact(m._STATE, tid, BODY)]
                    if sent.count(LEFT) == cap - 1:
                        on_fifth(record[0])
                return fake_req(method, path, payload, timeout)
            return req

        # --- the new room fails for another reason: kept for the next pass, not reported dropped
        flaky, failed_once = "!fresh3:ag2.space", []

        def flaky_once():
            def fail_once(method, path, payload=None, timeout=35):
                if payload.get("op") == "message" and payload["room_id"] == flaky and not failed_once:
                    failed_once.append(1)
                    sent.append(flaky)
                    raise urllib.error.HTTPError("https://gw/v1/room", 500, "Server Error", {},
                                                 io.BytesIO(b"{}"))
                return refused(method, path, payload, timeout)
            return fail_once

        refused = refuse_then("task-flaky", flaky, lambda _: None)
        m._req = flaky_once()
        logged = []
        with mock.patch.object(m, "_log", logged.append):
            got = scan_for_a_minute("task-flaky")
        check(any(b is None and "failed in its new room" in (r or "") for b, r in got),
              "a non-refusal error in the new room keeps the result for the next pass")
        check(sent == [LEFT] * cap + [flaky] * 3,
              f"and the next pass sends the review to the new room ({sent})")
        check(got[-1][0] == "[no-send]" and "dropped" not in (got[-1][1] or ""),
              "where it settles, not reported as dropped")
        check(not any("DROPPED" in line for line in logged), "and nothing logs it as dropped")
        record = record_of("task-flaky")
        check((record.get("status"), record.get("dm_room_id")) == ("awaiting_owner", flaky),
              f"the owner's review is open in the new room ({record.get('status')})")

        # --- the record is unreadable when the new room would be tried: kept for the next pass,
        # which re-issues it and sends it to the new room; never reported dropped
        unreadable = "!fresh4:ag2.space"
        m._req = refuse_then("task-unreadable", unreadable,
                             lambda path: path.write_text("{not json", encoding="utf-8"))
        logged = []
        with mock.patch.object(m, "_log", logged.append):
            got = scan_for_a_minute("task-unreadable")
        check(any(b is None and "failed in its new room" in (r or "") for b, r in got),
              "an unreadable record at the re-route keeps the result for the next pass")
        check(sent == [LEFT] * cap + [unreadable] * 2,
              f"and the next pass sends the review to the new room ({sent})")
        check(got[-1][0] == "[no-send]" and not any("DROPPED" in line for line in logged),
              "where it settles, and nothing logs it as dropped")

        # --- the new room fails without an HTTP answer (a timeout on create or on the send):
        # the scan still returns, keeps the result for the next pass, and never reports it dropped
        for n, (op, error) in enumerate((
                ("create", urllib.error.URLError("timed out")),
                ("message", TimeoutError("The read operation timed out")))):
            new_room, raised = f"!fresh{5 + n}:ag2.space", []

            def times_out_once(method, path, payload=None, timeout=35, new_room=new_room, op=op, error=error):
                if payload.get("op") == op and not raised and (op == "create" or payload["room_id"] == new_room):
                    raised.append(op)
                    raise error
                return refusing_left(method, path, payload, timeout)

            refusing_left = refuse_then(f"task-timeout-{n}", new_room, lambda _: None)
            m._req = times_out_once
            logged, got, escaped = [], [], []
            clock = [1000.0]
            with mock.patch.object(m, "_log", logged.append), \
                    mock.patch.object(m.time, "monotonic", lambda: clock[0]):
                for _ in range(60):
                    try:
                        got.append(m._guarded_result_body(f"task-timeout-{n}", BODY))
                    except Exception as exc:  # noqa: BLE001 - what escaped is what the poll round sees
                        escaped.append(exc)
                    clock[0] += 1.0
            label = type(error).__name__
            check(not escaped and raised == [op],
                  f"a {label} on the new room's {op} stays inside the scan (escaped: {escaped})")
            check(any(b is None and "failed in its new room" in (r or "") for b, r in got),
                  "and keeps the result for the next pass")
            check(bool(got) and got[-1][0] == "[no-send]" and not any("DROPPED" in line for line in logged),
                  "where it settles, and nothing logs it as dropped")
            check(record_of(f"task-timeout-{n}").get("dm_room_id") == new_room,
                  "with the owner's review open in the new room")
    finally:
        for n, v in saved.items():
            setattr(m, n, v)
        m._WITHHELD_TASK_OUTPUT.clear()
        m._WITHHELD_TASK_OUTPUT.update(saved_output)
        getattr(m, "_REVIEW_ROOM_REFUSALS", {}).clear()

print("PASS: 0 failure(s)" if not fail else "FAIL: withheld review room refusal")
sys.exit(fail)
