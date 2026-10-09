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
        if fail_edits[0] == "reject":
            return {"ok": False}
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

def card_edits_since(mark):
    return [p["body"] for _m, u, p in calls[mark:] if u == "/v1/room" and p.get("op") == "edit"]


# 6. Legacy upgrade through the whole retry beat: a claimless live publish_pending beside an
# archived kept_private. Nothing posts, nothing contradicts the archive, the card is restored.
for card_pending in (True, False):
    path, rid = new_review(f"task-legacy-{card_pending}")
    stale = json.loads(path.read_text())
    bridge._handle_review_decision(decision("Yes", rid, f"legacy-yes-{card_pending}"))
    archived = path.parent / "archive" / path.name
    stale.update({"status": "publish_pending", "resolved_at": 1001.0,
                  "decision": "false_positive", "card_resolution_pending": card_pending})
    bridge._atomic_private_json(path, stale)
    legacy_bytes = path.read_bytes()
    live_state, archive_state = frozen(path), frozen(archived)
    before, mark = len(shared_posts()), len(calls)
    fail_edits[0] = True if card_pending else "reject"
    bridge._retry_withheld_reviews()
    fail_edits[0] = False
    check(len(shared_posts()) == before and unchanged(path, live_state)
          and unchanged(archived, archive_state)
          and not any("False positive" in b for b in card_edits_since(mark)),
          f"legacy pending={card_pending}, card unreachable: record, archive and card untouched")
    check(any(path.stem in line and "left untouched" in line for line in logs),
          f"legacy pending={card_pending}: the deferral is logged for the owner")
    mark = len(calls)
    bridge._retry_withheld_reviews()
    bridge._retry_withheld_reviews()
    edits = [b for b in card_edits_since(mark) if rid in b]
    superseded = list((path.parent / "archive" / "superseded").glob(f"{path.stem}.*.json"))
    check(len(shared_posts()) == before, f"legacy pending={card_pending}: nothing posts")
    check(len(edits) == 1 and "Kept private" in edits[0],
          f"legacy pending={card_pending}: the card is restored to the archive once: {edits}")
    check(not path.exists() and unchanged(archived, archive_state)
          and any(p.read_bytes() == legacy_bytes for p in superseded),
          f"legacy pending={card_pending}: the live copy is retired; the archive is untouched")

# 6b. Each retry that touches a record or card defers on its own: the card retry reconciles
# a stale live "published", and a direct card resolution never shows "False positive".
for label in ("stale-published", "direct-resolve"):
    path, rid = new_review(f"task-defer-{label}")
    stale = json.loads(path.read_text())
    bridge._handle_review_decision(decision("Yes", rid, f"defer-{label}"))
    archived = path.parent / "archive" / path.name
    if label == "stale-published":
        stale.update({"status": "published", "decision": "false_positive",
                      "card_resolution_pending": False})
    else:
        stale.update({"status": "publish_pending", "decision": "false_positive",
                      "card_resolution_pending": True})
    bridge._atomic_private_json(path, stale)
    legacy_bytes, archive_state = path.read_bytes(), frozen(archived)
    mark = len(calls)
    if label == "stale-published":
        bridge._retry_withheld_reviews()
    else:
        check(bridge._resolve_review_card(path, dict(stale)) is False,
              "a card resolution beside an archived decision reports nothing resolved")
    edits = [b for b in card_edits_since(mark) if rid in b]
    superseded = list((path.parent / "archive" / "superseded").glob(f"{path.stem}.*.json"))
    check(len(edits) == 1 and "Kept private" in edits[0] and not path.exists()
          and unchanged(archived, archive_state)
          and any(p.read_bytes() == legacy_bytes for p in superseded),
          f"{label}: card restored to the archive and copy retired, archive untouched: {edits}")

# 7. Every non-awaiting or unproven state refuses a claim, a publication and the whole
# retry beat, and is not touched.
claim_id = "ab" * 16
base_record = {"review_id": "wr_0000000000000000", "status": "awaiting_owner",
               "dm_room_id": DM, "owner": OWNER, "withheld_body": "SECRET-BODY",
               "context": {"channel_id": SHARED}}
cases = {
    "legacy-publish_pending": {**base_record, **NO},
    "list-claim-publish_pending": {**base_record, **NO, "decision_claim_id": ["x"]},
    "short-claim-publish_pending": {**base_record, **NO, "decision_claim_id": "ab"},
    "nonhex32-claim-publish_pending": {**base_record, **NO, "decision_claim_id": "z" * 32},
    "unissued-hex32-claim-publish_pending": {**base_record, **NO, "decision_claim_id": claim_id},
    "claim-issued-for-another-review": {**base_record, **NO, "decision_claim_id": claim_id},
    "wrong-decision-publish_pending": {**base_record, **NO, "decision": "sensitive",
                                       "decision_claim_id": claim_id},
    "publish_failed": {**base_record, "status": "publish_failed",
                       "decision": "false_positive", "decision_claim_id": claim_id},
    "kept_private": {**base_record, **YES, "decision_claim_id": claim_id},
    "published": {**base_record, "status": "published", "decision": "false_positive",
                  "decision_claim_id": claim_id},
    "claimed-but-archived-kept_private": {**base_record, **NO, "decision_claim_id": claim_id},
    "awaiting-but-archived-kept_private": dict(base_record),
    "pending-but-archived-unreadable": {**base_record, **NO, "decision_claim_id": claim_id},
    "pending-but-archived-non-dict": {**base_record, **NO, "decision_claim_id": claim_id},
    "malformed-json": "{not json",
    "non-dict-json": ["SECRET-BODY"],
    "missing": None,
}
issued_here = ("wrong-decision", "publish_failed", "kept_private", "published", "claimed-but",
               "pending-but")
for label, content in cases.items():
    bridge._STATE = root / "states" / label
    directory = bridge._STATE / "withheld-team-results"
    directory.mkdir(parents=True)
    case_path = directory / "wr_0000000000000000.json"
    (directory / "claims").mkdir()
    if label.startswith(issued_here):
        (directory / "claims" / f"{case_path.stem}.{claim_id}").touch()
    if label == "claim-issued-for-another-review":
        (directory / "claims" / f"wr_1111111111111111.{claim_id}").touch()
    if "archived" in label:
        (directory / "archive").mkdir()
        (directory / "archive" / case_path.name).write_text(
            "{unreadable" if label.endswith("unreadable") else "[]" if label.endswith("non-dict")
            else json.dumps({**base_record, **YES}))
    if isinstance(content, str):
        case_path.write_text(content)
    elif content is not None:
        case_path.write_text(json.dumps(content))
    before_state = frozen(case_path)
    before = len(shared_posts())
    claims = [guard.claim_withheld_decision(case_path, dict(u)) for u in (YES, NO)]
    published = bridge._publish_review(case_path, {**base_record, "decision_claim_id": claim_id})
    bridge._retry_withheld_reviews()
    check(claims == [None, None] and published is False and len(shared_posts()) == before,
          f"{label}: claims {claims}, publish {published}, "
          f"{len(shared_posts()) - before} posts")
    # A decided record may be archived by the beat, as it always was: same bytes, moved.
    moved = directory / "archive" / case_path.name
    landed = moved if not case_path.exists() and "archived" not in label else case_path
    check(unchanged(landed, before_state), f"{label}: record bytes or mtime changed")
bridge._STATE = root / "state"

# 7b. Exact claim: both ids were issued for this review, but the caller's is not the one
# the live record carries, so it never publishes.
bridge._STATE = root / "states" / "other-claim-id"
directory = bridge._STATE / "withheld-team-results"
(directory / "claims").mkdir(parents=True)
case_path = directory / "wr_0000000000000000.json"
for issued in (claim_id, "cd" * 16):
    (directory / "claims" / f"{case_path.stem}.{issued}").touch()
case_path.write_text(json.dumps({**base_record, **NO, "decision_claim_id": "cd" * 16}))
before_state = frozen(case_path)
before = len(shared_posts())
check(not guard.withheld_claim_publishable(case_path, claim_id),
      "a different issued claim id is not the live record's release claim")
check(bridge._publish_review(case_path, {**base_record, **NO, "decision_claim_id": claim_id})
      is False and len(shared_posts()) == before,
      "a snapshot with another claim id must not post")
check(unchanged(case_path, before_state), "other-claim-id: record bytes or mtime changed")
bridge._STATE = root / "state"

# 8. Positive control: a release claimed by this writer, interrupted before its POST,
# is still published exactly once by the retry beat.
path, rid = new_review("task-claimed-retry")
claimed = guard.claim_withheld_decision(path, dict(NO))
check(claimed is not None
      and (path.parent / "claims" / f"{path.stem}.{claimed['decision_claim_id']}").is_file(),
      "a claim on an awaiting record is recorded in the claim ledger")
before = len(shared_posts())
bridge._retry_withheld_reviews()
bridge._retry_withheld_reviews()
check(len(shared_posts()) - before == 1, "the claimed release is published exactly once")
check(guard.retire_superseded_record(path) is False,
      "with no archived decision there is nothing to retire")

# 9. Delegation: the bridge acts on what the injected guard answers. Each fake returns an
# answer the real guard would not give for that state, so a bypass changes the outcome.
real = {name: getattr(guard, name) for name in (
    "claim_withheld_decision", "withheld_claim_publishable",
    "archived_withheld_decision", "retire_superseded_record")}
seen = []
path, rid = new_review("task-delegate-claim")
awaiting = path.read_bytes()
guard.claim_withheld_decision = lambda p, u: seen.append(("claim", p)) and None
bridge._handle_review_decision(decision("Yes", rid, "delegate-claim"))
check(("claim", path) in seen and path.read_bytes() == awaiting,
      "the bridge's decision is the injected guard's claim answer (refused: nothing written)")
SENTINEL = "5e" * 16
bridge._STATE = root / "states" / "delegate-publish"
directory = bridge._STATE / "withheld-team-results"
directory.mkdir(parents=True)
case_path = directory / "wr_0000000000000000.json"
legacy = {**base_record, **NO, "decision_claim_id": SENTINEL}
case_path.write_text(json.dumps(legacy))
guard.withheld_claim_publishable = lambda p, c: seen.append(("publish", c)) or c == SENTINEL
before = len(shared_posts())
bridge._publish_review(case_path, legacy)
check(("publish", SENTINEL) in seen and len(shared_posts()) == before + 1,
      "the bridge's publication gate is the injected guard's answer")
guard.archived_withheld_decision = lambda p: {**base_record, **YES, "decision": "sensitive",
                                              "dm_event_id": "$sentinel-card"}
guard.retire_superseded_record = lambda p: seen.append(("retire", p)) or True
mark = len(calls)
check(bridge._defers_to_archive(case_path) and ("retire", case_path) in seen
      and any(p.get("event_id") == "$sentinel-card" for _m, _u, p in calls[mark:]),
      "the bridge's archive authority and retirement are the injected guard's")
for name, function in real.items():
    setattr(guard, name, function)
bridge._STATE = root / "state"

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
