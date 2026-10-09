#!/usr/bin/env python3
"""A durable owner decision on a withheld Team result is final: a later, stale or
racing Yes/No reply to the same review never publishes the body or flips the
archived decision. Drives the production guard + bridge functions with a fake relay."""
from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile
import threading

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow import remote_gateway_bridge as bridge  # noqa: E402
from ag2_sparrow import team_result_guard as guard  # noqa: E402

SHARED = "!shared:example"
DM = "!owner-dm:example"
OWNER = "@owner:example"
failures = []


def check(ok, message):
    if not ok:
        failures.append(message)
        print(f"FAIL: {message}")


root = pathlib.Path(tempfile.mkdtemp())
originals = {name: getattr(bridge, name) for name in (
    "_STATE", "_WITHHELD_DM_CACHE", "_WITHHELD_CONTROL_DIR", "_GATEWAY_OWNER_DM_HINT",
    "_reenroll_identity", "_tier_for", "_req", "_match_review_decision", "_log")}
bridge._STATE = root / "state"
bridge._WITHHELD_DM_CACHE = bridge._STATE / "withheld-review-dm.json"
bridge._WITHHELD_CONTROL_DIR = bridge._STATE / "withheld-review-control-results"
bridge._GATEWAY_OWNER_DM_HINT = ""
bridge._reenroll_identity = lambda: "@agent:example"
bridge._tier_for = lambda *_args: "owner"
logs = []
bridge._log = logs.append
lock = threading.Lock()
calls = []
fail_edits = [False]


def fake_req(method, path, payload=None, timeout=35):
    with lock:
        calls.append((method, path, payload))
        n = len(calls)
    if (method, path) == ("GET", "/v1/agents"):
        return {"agents": [{"id": "@agent:example", "owner": OWNER, "owner_dm_room": DM}]}
    if path == "/v1/room" and payload.get("op") == "message":
        return {"ok": True, "event_id": f"$event-{n}"}
    if path == "/v1/room" and payload.get("op") == "edit":
        if fail_edits[0]:
            raise TimeoutError("card edit unavailable")
        return {"ok": True, "event_id": f"$edit-{n}"}
    if path == "/v1/results":
        return {"ok": True}
    raise AssertionError((method, path, payload))


bridge._req = fake_req
leak = guard.TeamResultVerdict(guard.VERDICT_LEAK, guard.TEAM_LEAK_RESULT, "possible leak")


def new_review(task_id):
    guard.materialize_withheld_verdict(
        leak, "SECRET-BODY", bridge._STATE, task_id,
        {"source": "ag2space", "channel_id": SHARED, "user_id": "@team:example"},
        "@agent:example", now=1000)
    path = guard.withheld_review_path(bridge._STATE, task_id)
    bridge._route_withheld_review(path)
    return path, json.loads(path.read_text())["review_id"]


def decision(answer, rid, task_id):
    return {"id": task_id, "task": f"{answer} {rid}", "source": "ag2space",
            "channel_id": DM, "user_id": OWNER, "access_tier": "owner",
            "reply_to_event": ""}


def shared_posts():
    return [p for _m, u, p in calls
            if u == "/v1/room" and p.get("op") == "message" and p.get("room_id") == SHARED]


def final_record(path):
    archived = path.parent / "archive" / path.name
    live = path.is_file()
    return live, json.loads((path if live else archived).read_text())


def stale_schedule(label, first, second, edits_fail):
    """Match both replies against the same awaiting_owner snapshot, then handle in order."""
    path, rid = new_review(f"task-{label}")
    tasks = {a: decision(a, rid, f"{label}-{a}") for a in (first, second)}
    matched = {t["id"]: originals["_match_review_decision"](t) for t in tasks.values()}
    bridge._match_review_decision = lambda task: matched.get(task["id"])
    before = len(shared_posts())
    fail_edits[0] = edits_fail
    try:
        outcome = {a: bridge._handle_review_decision(tasks[a]) for a in (first, second)}
    finally:
        fail_edits[0] = False
        bridge._match_review_decision = originals["_match_review_decision"]
    live, record = final_record(path)
    posts = shared_posts()[before:]
    print(f"{label} outcome {outcome} | live {live} | status {record['status']} "
          f"| shared-room posts {[(p['room_id'], p['body']) for p in posts]}")
    return outcome, record, posts, tasks


# 1. The issue's schedule: Yes completes and archives, then the stale No resumes.
outcome, record, posts, tasks = stale_schedule("yes-then-stale-no", "Yes", "No", False)
check(posts == [], "a stale No after a durable keep-private must not post the body")
check(record["status"] == "kept_private" and record["decision"] == "sensitive",
      "the archived keep-private decision must not flip")
check(all(outcome.values()), "both replies are consumed, never re-dispatched as chat tasks")
check(bridge._control_result_path(tasks["No"]["id"]).is_file(),
      "the ignored reply still gets its [no-send] control result")
check(any("already decided" in line for line in logs), "the ignored reply is logged")

# 2. Same, but the card edit fails so the kept-private record is still live.
outcome, record, posts, _ = stale_schedule("yes-live-then-stale-no", "Yes", "No", True)
check(posts == [] and record["status"] == "kept_private",
      "a stale No on a live kept-private record must not post or flip it")

# 3. The reverse: No publishes, then a stale Yes cannot rewrite the outcome.
outcome, record, posts, _ = stale_schedule("no-then-stale-yes", "No", "Yes", False)
check(len(posts) == 1 and record["status"] == "published"
      and record["decision"] == "false_positive",
      "a stale Yes after publication must not rewrite the published decision")

# 4. Publication revalidates the on-disk claim, not the caller's snapshot.
path, rid = new_review("task-revalidate")
snapshot = json.loads(path.read_text())
bridge._handle_review_decision(decision("Yes", rid, "revalidate-yes"))
before = len(shared_posts())
bridge._publish_review(path, {**snapshot, "status": "publish_pending"})
check(len(shared_posts()) == before, "a stale publish_pending snapshot must not post")

# 5. Race: both replies matched, then released together; exactly one decision wins.
for round_no in range(25):
    path, rid = new_review(f"task-race-{round_no}")
    tasks = [decision(a, rid, f"race-{round_no}-{a}") for a in ("Yes", "No")]
    barrier = threading.Barrier(2)

    def matched_then_wait(task):
        found = originals["_match_review_decision"](task)
        barrier.wait(timeout=10)
        return found

    bridge._match_review_decision = matched_then_wait
    before = len(shared_posts())
    threads = [threading.Thread(target=bridge._handle_review_decision, args=(t,))
               for t in tasks]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    bridge._match_review_decision = originals["_match_review_decision"]
    _live, record = final_record(path)
    posts = shared_posts()[before:]
    winner_published = record["status"] == "published"
    kept_edits = [p for _m, u, p in calls if u == "/v1/room" and p.get("op") == "edit"
                  and rid in p.get("body", "") and "Kept private" in p.get("body", "")]
    losers = [line for line in logs if path.stem in line and "already decided" in line]
    check(len(posts) == (1 if winner_published else 0)
          and record["status"] in ("published", "kept_private")
          and (not winner_published or not kept_edits) and len(losers) == 1,
          f"race round {round_no}: status {record['status']}, {len(posts)} posts, "
          f"{len(kept_edits)} keep-private edits, {len(losers)} ignored replies")

for name, value in originals.items():
    setattr(bridge, name, value)
shutil.rmtree(root, ignore_errors=True)

if failures:
    print(f"FAILED: {len(failures)} check(s)")
    sys.exit(1)
print("PASS: a durable owner decision is final; stale or racing replies change nothing.")
