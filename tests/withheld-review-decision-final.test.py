#!/usr/bin/env python3
"""A durable owner decision on a withheld Team result is final: a later, stale or
racing Yes/No reply to the same review never publishes the body or flips the
archived decision. Drives the production guard + bridge functions with a fake relay."""
from __future__ import annotations

import builtins
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading

REPO = pathlib.Path(__file__).resolve().parent.parent
REAL_HOME = pathlib.Path(os.path.expanduser("~")).resolve()
root = pathlib.Path(tempfile.mkdtemp()).resolve()
# Hermetic import: no host token (env, channel .env or vault), home, config or workspace.
os.environ.pop("AG2_DEVICE_ENV", None)
os.environ.pop("AG2_REMOTE_TOKEN", None)
os.environ["CLAUDE_CONFIG_DIR"] = str(root / "claude-config")
os.environ.update({
    "HOME": str(root / "home"), "REMOTE_TASK_TOKEN": "dummy-test-token",
    "AGENT_CONNECT_TASK_DIR": str(root / "workspace" / "tasks"),
    "AGENT_CONNECT_RESULT_DIR": str(root / "workspace" / "results"),
    "AGENT_CONNECT_STATE_DIR": str(root / "workspace" / "state"),
})
host_opens = []


def _outside_sandbox(target):
    try:
        resolved = pathlib.Path(os.fsdecode(target)).resolve()
    except (TypeError, ValueError, OSError):
        return False
    inside = lambda base: resolved == base or base in resolved.parents  # noqa: E731
    return inside(REAL_HOME) and not inside(REPO) and not inside(root)


def _spy(real):
    def wrapper(target, *args, **kwargs):
        if isinstance(target, (str, bytes, os.PathLike)) and _outside_sandbox(target):
            host_opens.append(os.fsdecode(target))
        return real(target, *args, **kwargs)
    return wrapper


_real_open, _real_io_open, _real_os_open = builtins.open, io.open, os.open
builtins.open, io.open, os.open = _spy(_real_open), _spy(_real_io_open), _spy(_real_os_open)
try:  # positive control: the spy records a host path before the open fails
    open(REAL_HOME / ".withheld-review-test-sentinel-absent")
except OSError:
    pass
sentinel_seen = host_opens == [str(REAL_HOME / ".withheld-review-test-sentinel-absent")]
host_opens.clear()
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
sys.path.insert(0, str(REPO / "src"))

from ag2_sparrow import remote_gateway_bridge as bridge  # noqa: E402
import policy.egress.result as guard  # noqa: E402  the canonical writer, injected below

SHARED = "!shared:example"
DM = "!owner-dm:example"
OWNER = "@owner:example"
failures = []


def check(ok, message):
    if not ok:
        failures.append(message)
        print(f"FAIL: {message}")


vendored = REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "team_result_guard.py"
check(vendored.read_bytes() == (REPO / "src" / "policy" / "egress" / "result.py").read_bytes(),
      "the vendored guard is byte-identical to the canonical writer under test")
originals = {name: getattr(bridge, name) for name in (
    "_STATE", "_WITHHELD_DM_CACHE", "_WITHHELD_CONTROL_DIR", "_GATEWAY_OWNER_DM_HINT",
    "_reenroll_identity", "_tier_for", "_req", "_match_review_decision", "_log",
    "team_result_guard")}
spy_calls = {"claim_withheld_decision": 0, "withheld_claim_publishable": 0}


def spied(name):
    real = getattr(guard, name)

    def wrapper(*args, **kwargs):
        spy_calls[name] += 1
        return real(*args, **kwargs)
    return wrapper


for _name in spy_calls:
    setattr(guard, _name, spied(_name))
bridge.team_result_guard = guard
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


def frozen(path):
    """Pin mtime to a known past value and return (bytes, mtime_ns) or None if absent."""
    if not path.exists():
        return None
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    return path.read_bytes(), path.stat().st_mtime_ns


def unchanged(path, before):
    after = None if not path.exists() else (path.read_bytes(), path.stat().st_mtime_ns)
    return after == before


YES = {"status": "kept_private", "decision": "sensitive", "card_resolution_pending": True}
NO = {"status": "publish_pending", "decision": "false_positive",
      "card_resolution_pending": True}

# 6. Legacy upgrade: an older writer left a live publish_pending (no claim id) beside
# an archived kept_private, and stopped before its POST; the retry loop must not post.
path, rid = new_review("task-legacy")
stale = json.loads(path.read_text())
bridge._handle_review_decision(decision("Yes", rid, "legacy-yes"))
stale.update({"status": "publish_pending", "resolved_at": 1001.0,
              "decision": "false_positive", "card_resolution_pending": True})
bridge._atomic_private_json(path, stale)
before_state = frozen(path)
before = len(shared_posts())
bridge._retry_pending_publications()
check(len(shared_posts()) == before, "a legacy publish_pending beside kept_private must not post")
check(unchanged(path, before_state), "the refused legacy record is left byte-for-byte")
check(json.loads((path.parent / "archive" / path.name).read_text())["status"] == "kept_private",
      "the archived keep-private decision survives the retry")
check(any(path.stem in line and "publication refused" in line for line in logs),
      "the refused publication is logged for the owner")

# 7. Every non-awaiting state refuses both a claim and a publication, and is not touched.
claim_id = "ab" * 16
base_record = {"review_id": "wr_0000000000000000", "status": "awaiting_owner",
               "dm_room_id": DM, "owner": OWNER, "withheld_body": "SECRET-BODY",
               "context": {"channel_id": SHARED}}
cases = {
    "legacy-publish_pending": {**base_record, **NO},
    "malformed-claim-publish_pending": {**base_record, **NO, "decision_claim_id": ["x"]},
    "wrong-decision-publish_pending": {**base_record, **NO, "decision": "sensitive",
                                       "decision_claim_id": claim_id},
    "publish_failed": {**base_record, "status": "publish_failed",
                       "decision": "false_positive", "decision_claim_id": claim_id},
    "kept_private": {**base_record, **YES, "decision_claim_id": claim_id},
    "published": {**base_record, "status": "published", "decision": "false_positive",
                  "decision_claim_id": claim_id},
    "claimed-but-archived-kept_private": {**base_record, **NO, "decision_claim_id": claim_id},
    "awaiting-but-archived-kept_private": dict(base_record),
    "malformed-json": "{not json",
    "non-dict-json": ["SECRET-BODY"],
    "missing": None,
}
for label, content in cases.items():
    bridge._STATE = root / "states" / label
    directory = bridge._STATE / "withheld-team-results"
    directory.mkdir(parents=True)
    case_path = directory / "wr_0000000000000000.json"
    if "archived" in label:
        (directory / "archive").mkdir()
        (directory / "archive" / case_path.name).write_text(
            json.dumps({**base_record, **YES}))
    if isinstance(content, str):
        case_path.write_text(content)
    elif content is not None:
        case_path.write_text(json.dumps(content))
    before_state = frozen(case_path)
    before = len(shared_posts())
    claims = [guard.claim_withheld_decision(case_path, dict(u)) for u in (YES, NO)]
    published = bridge._publish_review(case_path, {**base_record, "decision_claim_id": claim_id})
    bridge._retry_pending_publications()
    check(claims == [None, None] and published is False and len(shared_posts()) == before,
          f"{label}: claims {claims}, publish {published}, "
          f"{len(shared_posts()) - before} posts")
    check(unchanged(case_path, before_state), f"{label}: record bytes or mtime changed")
bridge._STATE = root / "state"

# 7b. A caller holding one well-formed claim id never publishes a record that carries another.
bridge._STATE = root / "states" / "other-claim-id"
directory = bridge._STATE / "withheld-team-results"
directory.mkdir(parents=True)
case_path = directory / "wr_0000000000000000.json"
case_path.write_text(json.dumps({**base_record, **NO, "decision_claim_id": "cd" * 16}))
before_state = frozen(case_path)
before = len(shared_posts())
check(not guard.withheld_claim_publishable(case_path, claim_id),
      "a different well-formed claim id is not the release claim")
check(bridge._publish_review(case_path, {**base_record, **NO, "decision_claim_id": claim_id})
      is False and len(shared_posts()) == before,
      "a snapshot with another claim id must not post")
check(unchanged(case_path, before_state), "other-claim-id: record bytes or mtime changed")
bridge._STATE = root / "state"

# 8. Positive control: a release claimed by this writer, interrupted before its POST,
# is still published exactly once by the retry loop.
path, rid = new_review("task-claimed-retry")
claimed = guard.claim_withheld_decision(path, dict(NO))
check(claimed is not None and len(claimed["decision_claim_id"]) == 32,
      "a claim on an awaiting record carries a fresh claim id")
before = len(shared_posts())
bridge._retry_pending_publications()
bridge._retry_pending_publications()
check(len(shared_posts()) - before == 1, "the claimed release is published exactly once")

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

check(all(count > 0 for count in spy_calls.values()),
      f"the bridge delegates claims and publication checks to the guard: {spy_calls}")

builtins.open, io.open, os.open = _real_open, _real_io_open, _real_os_open
check(bridge.TOKEN == "dummy-test-token", "the bridge took the test token, not a host one")
check(sentinel_seen, "the host-path spy records an open under the real home")
check(host_opens == [], f"the test opened host paths outside its sandbox: {host_opens[:5]}")

for name, value in originals.items():
    setattr(bridge, name, value)
shutil.rmtree(root, ignore_errors=True)

if failures:
    print(f"FAILED: {len(failures)} check(s)")
    sys.exit(1)
print("PASS: a durable owner decision is final; stale or racing replies change nothing.")
