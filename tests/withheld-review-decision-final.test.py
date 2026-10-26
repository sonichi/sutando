#!/usr/bin/env python3
"""A durable owner decision on a withheld Team result is final: a later, stale or
racing Yes/No reply to the same review never publishes the body or flips the
archived decision. Drives the production guard + bridge functions with a fake relay."""
from __future__ import annotations

import builtins
import importlib
import contextlib
import io
import json
import os
import pathlib
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.error

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

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
check(str(bridge.RESULTS_DIR).startswith(str(root)), f"the outbox lives in the sandbox: {bridge.RESULTS_DIR}")
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


def fake_req(method, path, payload=None, timeout=35, **_kw):
    with lock:
        calls.append((method, path, payload))
        n = len(calls)
    if (method, path) == ("GET", "/v1/agents"):
        return {"agents": [{"id": "@agent:example", "owner": OWNER, "owner_dm_room": DM}]}
    if path == "/v1/room" and payload.get("op") == "members":
        return {"members": [{"user_id": "@agent:example"}, {"user_id": OWNER}]}
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

# 6c. The reconcile table, every row: only a proven archived (status, decision) pair
# restores the card and retires the copy; any other archive freezes the copy in conflicts/.
PROVEN = {"kept_private/sensitive": "kept_private", "published/false_positive": "published",
          "publish_failed/false_positive": "publish_failed"}
EXPECTED_CARD = {"kept_private": "Kept private", "published": "Published to the original room",
                 "publish_failed": "publication failed"}
STATUSES = ("kept_private", "published", "publish_failed", "publish_pending",
            "awaiting_owner", "pending_dm", None, 7, ["x"])
DECISIONS = ("sensitive", "false_positive", None, ["x"])
rows = [(f"{st}/{de}", {"status": st, "decision": de}) for st in STATUSES for de in DECISIONS]
rows += [("non-dict", []), ("unreadable", None)]
card = {"review_id": "wr_0000000000000000", "dm_room_id": DM, "dm_event_id": "$table-card",
        "owner": OWNER, "withheld_body": "SECRET-BODY", "context": {"channel_id": SHARED}}
for label, archived_fields in rows:
    bridge._STATE = root / "table" / label.replace("/", "-").replace("'", "")
    directory = bridge._STATE / "withheld-team-results"
    (directory / "archive").mkdir(parents=True)
    archived = directory / "archive" / "wr_0000000000000000.json"
    if archived_fields is None:
        archived.write_text("{unreadable")
    elif isinstance(archived_fields, list):
        archived.write_text(json.dumps(archived_fields))
    else:
        archived.write_text(json.dumps({**card, **archived_fields}))
    live = directory / "wr_0000000000000000.json"
    live.write_text(json.dumps({**card, **NO}))
    legacy_bytes, live_state, archive_state = live.read_bytes(), frozen(live), frozen(archived)
    expected = PROVEN.get(label)
    before, mark = len(shared_posts()), len(calls)
    bridge._retry_withheld_reviews()
    bridge._retry_withheld_reviews()
    edits = [p["body"] for _m, u, p in calls[mark:]
             if u == "/v1/room" and p.get("op") == "edit" and p.get("event_id") == "$table-card"]
    superseded = list((directory / "archive" / "superseded").glob("wr_0000000000000000.*.json"))
    check(guard.archived_outcome(json.loads(archived.read_text()) if archived_fields is not None
                                 else {}) == expected, f"table {label}: outcome {expected}")
    check(len(shared_posts()) == before and unchanged(archived, archive_state),
          f"table {label}: nothing posts and the archive is untouched")
    if expected:
        check(len(edits) == 1 and EXPECTED_CARD[expected] in edits[0] and not live.exists()
              and any(p.read_bytes() == legacy_bytes for p in superseded),
              f"table {label}: card shows {expected} once and the copy is retired: {edits}")
    else:
        held = list((directory / "conflicts").glob("wr_0000000000000000.*.json"))
        check(edits == [] and not live.exists() and superseded == []
              and any(p.read_bytes() == legacy_bytes for p in held),
              f"table {label}: unproven archive freezes the copy in conflicts/, no card edit: {edits}")

# 6d. The split, pinned: a proven pair is reconciled, while the same proven archive beside
# a copy of a different body is frozen in conflicts/ and never acts.
for label, live_body, proven in (("split-proven", "SECRET-BODY", True),
                                 ("split-other-body", "OTHER-BODY", False)):
    bridge._STATE = root / "table" / label
    directory = bridge._STATE / "withheld-team-results"
    (directory / "archive").mkdir(parents=True)
    archived = directory / "archive" / "wr_0000000000000000.json"
    archived.write_text(json.dumps({**card, "status": "kept_private", "decision": "sensitive"}))
    live = directory / "wr_0000000000000000.json"
    live.write_text(json.dumps({**card, **NO, "withheld_body": live_body}))
    legacy_bytes, mark = live.read_bytes(), len(calls)
    bridge._retry_withheld_reviews()
    edits = [p["body"] for _m, u, p in calls[mark:]
             if u == "/v1/room" and p.get("op") == "edit" and p.get("event_id") == "$table-card"]
    superseded = list((directory / "archive" / "superseded").glob("wr_0000000000000000.*.json"))
    held = list((directory / "conflicts").glob("wr_0000000000000000.*.json"))
    moved_to = superseded if proven else held
    check(not live.exists() and len(edits) == (1 if proven else 0)
          and (held == [] if proven else superseded == [])
          and any(p.read_bytes() == legacy_bytes for p in moved_to),
          f"{label}: {'reconciled and retired' if proven else 'frozen in conflicts/, never acts'}")
bridge._STATE = root / "state"

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
    # A decided record may be archived by the beat, and an unproven conflict is frozen in
    # conflicts/ for the owner: either way the same bytes, moved, never acted on.
    moved = directory / "archive" / case_path.name
    held = sorted((directory / "conflicts").glob(f"{case_path.stem}.*.json"))
    landed = (case_path if case_path.exists() else held[0] if held
              else moved if "archived" not in label else case_path)
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

# 10. Liveness: a retirement that raises (archive/superseded is a regular file) is logged
# for that record, and the same real poll beat still takes in a new task.
bridge._STATE = root / "states" / "retire-error"
directory = bridge._STATE / "withheld-team-results"
(directory / "archive").mkdir(parents=True)
(directory / "archive" / "superseded").write_text("not a directory")
archived = directory / "archive" / "wr_0000000000000000.json"
archived.write_text(json.dumps({**base_record, **YES, "dm_event_id": "$liveness-card"}))
live = directory / "wr_0000000000000000.json"
live.write_text(json.dumps({**base_record, **NO, "dm_event_id": "$liveness-card"}))
live_state, archive_state = frozen(live), frozen(archived)


class _Clock:
    now = 1_000_000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


written, alive, beat_logs = [], [True, False], []
patched = {name: getattr(bridge, name) for name in (
    "TOKEN", "URL", "time", "_acquire_singleton", "_load_inflight", "_recover_orphan_proactive",
    "_maybe_start_event_channel", "_heartbeat_singleton", "_post_heartbeat", "_req",
    "_write_task", "_post_task_ack", "_post_ready_results", "_post_proactive",
    "_reconcile_abandoned", "_emit_gateway_status", "_save_inflight", "_log",
    "_push_pool_advertisement", "refresh_routing")}


def beat_req(method, url, payload=None, timeout=35, **_kw):
    if url.startswith("/v1/tasks?wait="):
        return {"tasks": [{"id": "task-after-retire-error", "task": "hello",
                           "channel_id": SHARED, "user_id": OWNER}]}
    return fake_req(method, url, payload, timeout)


bridge.TOKEN, bridge.URL, bridge.time = "secret", "http://relay.invalid", _Clock()
bridge._acquire_singleton = lambda *a, **k: True
bridge._load_inflight = lambda *a, **k: set()
bridge._heartbeat_singleton = lambda *a, **k: alive.pop(0) if alive else False
for _noop in ("_recover_orphan_proactive", "_maybe_start_event_channel", "_post_heartbeat",
              "_post_task_ack", "_post_ready_results", "_post_proactive",
              "_emit_gateway_status", "_save_inflight", "_push_pool_advertisement",
              "refresh_routing"):
    setattr(bridge, _noop, lambda *a, **k: None)
bridge._reconcile_abandoned = lambda inflight, suspects, *a, **k: suspects
bridge._write_task = lambda task: written.append(task["id"]) or (task["id"], True)
bridge._req = beat_req
bridge._log = beat_logs.append
mark = len(calls)
try:
    bridge.main()
finally:
    for name, value in patched.items():
        setattr(bridge, name, value)
check(written == ["task-after-retire-error"],
      f"the beat whose retirement raised still took in the new task: {written}")
check(any("archive reconcile failed" in line for line in beat_logs)
      and not any(line.startswith("unexpected") for line in beat_logs),
      "the retirement fault is logged for its record and never aborts the beat")
check(unchanged(live, live_state) and unchanged(archived, archive_state),
      "a failed retirement leaves the copy and the archive untouched")
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

# 11. Stale router: while A's DM POST is paused, B routes and the owner's Yes commits;
# A must not write awaiting_owner back, so a later No can never publish.
bridge._STATE = root / "cas-router"
guard.materialize_withheld_verdict(
    leak, "SECRET-BODY", bridge._STATE, "task-cas",
    {"source": "ag2space", "channel_id": SHARED, "user_id": "@team:example"},
    "@agent:example", now=1000)
path = guard.withheld_review_path(bridge._STATE, "task-cas")
stage = {}


def router_race_req(method, url, payload=None, timeout=35, **_kw):
    if ("paused" not in stage and url == "/v1/room" and payload.get("op") == "message"
            and payload.get("room_id") == DM):
        stage["paused"] = True
        stage["b"] = bridge._route_withheld_review(path)
        stage["yes"] = guard.claim_withheld_decision(path, dict(YES))
    return fake_req(method, url, payload, timeout)


bridge._req = router_race_req
routed_a = bridge._route_withheld_review(path)
bridge._req = fake_req
record = json.loads(path.read_text())
check(stage.get("b") is True and stage.get("yes") is not None and routed_a is True
      and record["status"] == "kept_private" and record["decision"] == "sensitive",
      f"stale router: the owner's keep-private stands, got {record['status']}")
before = len(shared_posts())
no_claim = guard.claim_withheld_decision(path, dict(NO))
published = bridge._publish_review(path, {**record, **NO})
bridge._retry_withheld_reviews()
check(no_claim is None and published is False and len(shared_posts()) == before,
      f"stale router: a later No neither claims nor publishes ({len(shared_posts()) - before} posts)")

# 11b. Stale card resolver: its write after the record was published is refused, the newer
# state stands, and the live card retry then shows the published outcome.
bridge._STATE = root / "stale-resolver"
path, rid = new_review("task-stale-resolver")
snapshot = dict(guard.claim_withheld_decision(path, dict(NO)))
check(bridge._publish_review(path, json.loads(path.read_text())) is True, "stale resolver: published")
mark = len(calls)
resolved = bridge._resolve_review_card(path, snapshot)
record = json.loads(path.read_text())
check(resolved is False and record["status"] == "published" and record["card_resolution_pending"]
      and any(path.stem in line and "card resolution not recorded" in line for line in logs),
      f"stale resolver: refused, the published record stands ({record['status']})")
bridge._retry_withheld_reviews()
edits = card_edits_since(mark)
check(not path.exists() and "Published to the original room" in edits[-1],
      f"stale resolver: the live card retry shows Published and archives: {edits[-1:]}")

# 12. A proven archive beside a same-body copy whose id is reserved to another body is
# not proven: zero card edits, and the copy is frozen in conflicts/.
bridge._STATE = root / "reserved-other"
directory = bridge._STATE / "withheld-team-results"
(directory / "archive").mkdir(parents=True)
live = directory / "wr_0000000000000000.json"
(directory / "archive" / live.name).write_text(json.dumps({**card, "status": "kept_private",
                                                          "decision": "sensitive"}))
live.write_text(json.dumps({**card, **NO}))
guard._reserve(live, guard._body_digest("ANOTHER-BODY"))
legacy_bytes, mark = live.read_bytes(), len(calls)
bridge._retry_withheld_reviews()
held = list((directory / "conflicts").glob(f"{live.stem}.*.json"))
check(card_edits_since(mark) == [] and not live.exists()
      and not (directory / "archive" / "superseded").exists()
      and any(p.read_bytes() == legacy_bytes for p in held),
      "another body's reservation: no card edit, the copy is frozen in conflicts/")

# 13. One lock: every decision read and write runs under the ledger lock, and the one
# writer is only ever called while it is held.
bridge._STATE = root / "one-lock"
path, rid = new_review("task-one-lock")
real_lock, real_writer = guard._ledger_lock, guard._write_owned_record
held_depth, entered, unlocked_writes = [0], [], []


@contextlib.contextmanager
def recording_lock(directory):
    with real_lock(directory):
        held_depth[0] += 1
        entered.append(True)
        try:
            yield
        finally:
            held_depth[0] -= 1


def recording_writer(*args, **kwargs):
    if held_depth[0] == 0:
        unlocked_writes.append(args[0])
    return real_writer(*args, **kwargs)


guard._ledger_lock, guard._write_owned_record = recording_lock, recording_writer
try:
    for name, call in (
            ("claim", lambda: guard.claim_withheld_decision(path, dict(NO))),
            ("publishable", lambda: guard.withheld_claim_publishable(
                path, json.loads(path.read_text())["decision_claim_id"])),
            ("archived read", lambda: guard.archived_withheld_decision(path)),
            ("proven check", lambda: guard.proven_superseded_record(path)),
            ("retire", lambda: guard.retire_superseded_record(path)),
            ("update", lambda: guard.update_record(
                path, json.loads(path.read_text()),
                expect=guard.record_version(json.loads(path.read_text()))))):
        entered.clear()
        call()
        check(entered, f"one lock: {name} runs under the ledger lock")
finally:
    guard._ledger_lock, guard._write_owned_record = real_lock, real_writer
check(unlocked_writes == [], f"one lock: the writer ran without the lock: {unlocked_writes}")
bridge._STATE = root / "state"

# 11h. Two writers read the same version and change nothing but a same-status field: the
# second is refused, because every accepted write bumps the generation.
bridge._STATE = root / "same-status"
path, rid = new_review("task-same-status")
seen = json.loads(path.read_text())
first = guard.update_record(path, dict(seen, dm_sent_at=1.0), expect=guard.record_version(seen))
second = guard.update_record(path, dict(seen, dm_sent_at=2.0), expect=guard.record_version(seen))
check(first is True and second is False and json.loads(path.read_text())["dm_sent_at"] == 1.0,
      "same-status race: the second writer holding the same version is refused")

# 14. A publication POST lands while a card resolver commits: the refused write is
# committed onto the newer version, and no later beat posts the body again.
bridge._STATE = root / "post-landed"
path, rid = new_review("task-post-landed")
claimed = dict(guard.claim_withheld_decision(path, dict(NO)))


def post_race_req(method, url, payload=None, timeout=35, **_kw):
    if url == "/v1/room" and payload.get("room_id") == SHARED and "resolved" not in stage:
        stage["resolved"] = bridge._resolve_review_card(path, json.loads(path.read_text()))
    return fake_req(method, url, payload, timeout)


stage.pop("resolved", None)
before = len(shared_posts())
bridge._req = post_race_req
published = bridge._publish_review(path, claimed)
bridge._req = fake_req
record = json.loads(path.read_text())
check(stage.get("resolved") is True and published is True and record["status"] == "published"
      and record["card_resolution_pending"] is True,
      f"landed POST: committed onto the newer version, got {record['status']}")
bridge._retry_withheld_reviews()
bridge._retry_withheld_reviews()
check(len(shared_posts()) - before == 1,
      f"landed POST: exactly one shared-room post across later beats, got {len(shared_posts()) - before}")

# 14b. The POST lands while a concurrent beat archives the outcome and retires the copy:
# the write is reported stale, and no later beat posts again.
bridge._STATE = root / "post-landed-archived"
path, rid = new_review("task-post-archived")
claimed = dict(guard.claim_withheld_decision(path, dict(NO)))
archived = path.parent / "archive" / path.name


def post_archive_req(method, url, payload=None, timeout=35, **_kw):
    if url == "/v1/room" and payload.get("room_id") == SHARED and not archived.exists():
        archived.parent.mkdir(exist_ok=True)
        archived.write_text(json.dumps({**claimed, "status": "published",
                                        "decision": "false_positive"}))
        stage["retired"] = guard.retire_superseded_record(path)
    return fake_req(method, url, payload, timeout)


before = len(shared_posts())
bridge._req = post_archive_req
published = bridge._publish_review(path, claimed)
bridge._req = fake_req
check(published is False and any(path.stem in line and "publication not recorded" in line
                                 for line in logs), "archived during POST: reported stale")
bridge._retry_withheld_reviews()
bridge._retry_withheld_reviews()
check(stage.get("retired") is True and len(shared_posts()) - before == 1 and not path.exists(),
      f"archived during POST: no second post, copy retired ({len(shared_posts()) - before} posts)")

# 14b'. The POST lands while another process already committed it as published: the
# refused write rereads, finds published, and reports success; no later beat posts.
bridge._STATE = root / "post-landed-published"
path, rid = new_review("task-post-published")
claimed = dict(guard.claim_withheld_decision(path, dict(NO)))


def post_published_req(method, url, payload=None, timeout=35, **_kw):
    if url == "/v1/room" and payload.get("room_id") == SHARED and "other" not in stage:
        current = json.loads(path.read_text())
        stage["other"] = guard.update_record(
            path, dict(current, status="published", card_resolution_pending=True),
            expect=guard.record_version(current))
    return fake_req(method, url, payload, timeout)


stage.pop("other", None)
before = len(shared_posts())
bridge._req = post_published_req
published = bridge._publish_review(path, claimed)
bridge._req = fake_req
bridge._retry_withheld_reviews()
check(stage.get("other") is True and published is True and len(shared_posts()) - before == 1,
      f"already published during POST: success, one post ({len(shared_posts()) - before})")

# 14c. Invalid origin with a stale snapshot: the publish_failed write is refused, and the
# next beat records the failure from the current record without posting.
bridge._STATE = root / "invalid-origin"
guard.materialize_withheld_verdict(
    leak, "SECRET-BODY", bridge._STATE, "task-invalid",
    {"source": "ag2space", "channel_id": "not-a-room", "user_id": "@team:example"},
    "@agent:example", now=1000)
path = guard.withheld_review_path(bridge._STATE, "task-invalid")
bridge._route_withheld_review(path)
stale = dict(guard.claim_withheld_decision(path, dict(NO)))
fresh = json.loads(path.read_text())
guard.update_record(path, dict(fresh, card_resolution_pending=False),
                    expect=guard.record_version(fresh))
before = len(shared_posts())
check(bridge._publish_review(path, stale) is False
      and json.loads(path.read_text())["status"] == "publish_pending"
      and any(path.stem in line and "publication failure not recorded" in line for line in logs),
      "invalid origin, stale snapshot: the failure write is refused and reported")
bridge._retry_pending_publications()
live, record = final_record(path)
check(record["status"] == "publish_failed" and len(shared_posts()) == before,
      f"invalid origin: the next beat records publish_failed, no post ({record['status']})")

# 14f. A proven-copy check that faults is skipped for that record; the beat goes on.
bridge._STATE = root / "proven-faults"
path, rid = new_review("task-proven-faults")
real_proven = guard.proven_superseded_record


def faulting_proven(candidate, *args, **kwargs):
    raise OSError(5, "EIO")


guard.proven_superseded_record = faulting_proven
try:
    listed = [p for p, _r in bridge._pending_review_records()]
finally:
    guard.proven_superseded_record = real_proven
check(listed == [path], f"a faulting proven check skips only that check: {listed}")

# 14g. An archive written between the card retry's listing and its check: the retry
# defers to it instead of editing the card from the live copy.
bridge._STATE = root / "archived-mid-retry"
path, rid = new_review("task-archived-mid")
guard.claim_withheld_decision(path, dict(YES))
archived = path.parent / "archive" / path.name
real_listing = guard.actionable_records


def listing_then_archive(directory, *args, **kwargs):
    listed_now = real_listing(directory, *args, **kwargs)
    if not archived.exists():
        archived.parent.mkdir(exist_ok=True)
        archived.write_text(json.dumps({**json.loads(path.read_text()), "status": "kept_private",
                                        "decision": "sensitive"}))
    return listed_now


guard.actionable_records = listing_then_archive
mark = len(calls)
try:
    bridge._retry_review_card_resolutions()
finally:
    guard.actionable_records = real_listing
edits = card_edits_since(mark)
check(len(edits) == 1 and "Kept private" in edits[0] and not path.exists(),
      f"archived mid-retry: one card edit from the archive, copy retired: {edits}")

# 14h. Unreadable records: the writer refuses an unreadable live record it would own, and
# an unreadable archive reads as {}.
bridge._STATE = root / "unreadable"
path, rid = new_review("task-unreadable")
path.write_text("{not json")
(path.parent / "issued" / f"{path.stem}.issued").unlink()
check(guard.update_record(path, {"withheld_body": None}, expect=guard.record_version({}))
      is False and path.read_text() == "{not json",
      "an unreadable live record is never rewritten")
archived = path.parent / "archive" / path.name
archived.parent.mkdir(exist_ok=True)
archived.write_text("{not json")
check(guard.archived_withheld_decision(path) == {}, "an unreadable archive reads as {}")
bridge._STATE = root / "state"

# 17. Through the real poll loop: Yes is consumed and archived on one poll; a later No for the
# same review on the next poll is answered with the archived outcome, never handed to chat.
bridge._STATE = root / "states" / "later-reply"
bridge._WITHHELD_CONTROL_DIR = bridge._STATE / "withheld-review-control-results"
path, rid = new_review("task-later-reply")
polls = [[decision("Yes", rid, "later-yes")], [decision("No", rid, "later-no")]]
chat, alive = [], [True, True, False]


def poll_req(method, url, payload=None, timeout=35, **_kw):
    if url.startswith("/v1/tasks?wait="):
        return {"tasks": polls.pop(0) if polls else []}
    return fake_req(method, url, payload, timeout)


patched = {name: getattr(bridge, name) for name in (
    "TOKEN", "URL", "time", "_acquire_singleton", "_load_inflight", "_recover_orphan_proactive",
    "_maybe_start_event_channel", "_heartbeat_singleton", "_post_heartbeat", "_req",
    "_write_task", "_post_task_ack", "_post_ready_results", "_post_proactive",
    "_reconcile_abandoned", "_emit_gateway_status", "_save_inflight",
    "_push_pool_advertisement", "refresh_routing")}
bridge.TOKEN, bridge.URL, bridge.time = "secret", "http://relay.invalid", _Clock()
bridge._acquire_singleton = lambda *a, **k: True
bridge._load_inflight = lambda *a, **k: set()
bridge._heartbeat_singleton = lambda *a, **k: alive.pop(0) if alive else False
for _noop in ("_recover_orphan_proactive", "_maybe_start_event_channel", "_post_heartbeat",
              "_post_task_ack", "_post_ready_results", "_post_proactive",
              "_emit_gateway_status", "_save_inflight", "_push_pool_advertisement",
              "refresh_routing"):
    setattr(bridge, _noop, lambda *a, **k: None)
bridge._reconcile_abandoned = lambda inflight, suspects, *a, **k: suspects
bridge._write_task = lambda task: chat.append(task["id"]) or (task["id"], True)
bridge._req = poll_req
mark, before = len(calls), len(shared_posts())
try:
    bridge.main()
finally:
    for name, value in patched.items():
        setattr(bridge, name, value)
answers = {p["id"]: p["body"] for _m, u, p in calls[mark:] if u == "/v1/results"}
archived = path.parent / "archive" / path.name
check(chat == [] and polls == [], f"later reply: neither reply became chat ({chat}, {len(polls)} unpolled)")
check(json.loads(archived.read_text())["status"] == "kept_private" and not path.exists()
      and len(shared_posts()) == before,
      "later reply: the first decision stands archived and nothing is published")
check(answers.get("later-yes") == "[no-send]" and "Kept private" in answers.get("later-no", ""),
      f"later reply: the No is answered with the archived outcome: {answers}")
bridge._STATE = root / "state"
bridge._WITHHELD_CONTROL_DIR = bridge._STATE / "withheld-review-control-results"

# 19. A record that moved to another outcome while the POST was in flight is not
# overwritten, and a record gone mid-POST is reported stale; neither POSTs again.
for label in ("other-outcome", "gone"):
    bridge._STATE = root / f"post-{label}"
    path, rid = new_review(f"task-post-{label}")
    claimed = dict(guard.claim_withheld_decision(path, dict(NO)))

    def post_then_change(method, url, payload=None, timeout=35, _path=path, _label=label):
        answer = fake_req(method, url, payload, timeout)
        if url == "/v1/room" and payload.get("room_id") == SHARED and not stage.get(_label):
            current = json.loads(_path.read_text())
            if _label == "gone":
                stage[_label] = guard.archive_record(_path)
            else:
                stage[_label] = guard.update_record(
                    _path, dict(current, status="publish_failed", publish_error="concurrent"),
                    expect=guard.record_version(current))
        return answer

    before = len(shared_posts())
    bridge._req = post_then_change
    try:
        posted = bridge._publish_review(path, claimed)
    finally:
        bridge._req = fake_req
    bridge._retry_pending_publications()
    _live, record = final_record(path)
    check(stage.get(label) is True and posted is False and len(shared_posts()) - before == 1
          and record["status"] == ("publish_failed" if label == "other-outcome" else "publish_pending")
          and any(path.stem in line and "publication not recorded" in line for line in logs),
          f"{label} mid-POST: not overwritten, reported stale, one POST ({record['status']})")

# 19b. A later reply that names a decided review in another room is not that review's.
bridge._STATE = root / "archived-other-room"
path, rid = new_review("task-archived-other-room")
guard.claim_withheld_decision(path, dict(YES))
bridge._resolve_review_card(path, json.loads(path.read_text()))
elsewhere = {**decision("No", rid, "elsewhere-no"), "channel_id": "!another-room:example"}
check(bridge._match_review_decision(elsewhere) is None,
      "a reply from another room never matches a decided review")
bridge._STATE = root / "state"

# 21. A later reply to an archive that is not a recognised outcome is consumed, never sent
# to chat, and answered without claiming an outcome; nothing changes.
bridge._STATE = root / "unrecognised-archive"
path, rid = new_review("task-unrecognised")
record = json.loads(path.read_text())
archived = path.parent / "archive" / path.name
archived.parent.mkdir(exist_ok=True)
archived.write_text(json.dumps({**record, "status": "published", "decision": "sensitive"}))
path.unlink()
archive_state = frozen(archived)
reply = decision("No", rid, "later-unrecognised")
handled = bridge._handle_review_decision(reply)
answer = json.loads(bridge._control_result_path("later-unrecognised").read_text())["body"]
check(handled is True and "not a recognised outcome" in answer and "Kept private" not in answer
      and "Published to the original room" not in answer and unchanged(archived, archive_state),
      f"unrecognised archive: answered without an outcome, nothing changed: {answer[:80]}")

# 22. A move never deletes its only copy before the new one is durable: if syncing the
# target fails, the original stays (archive, freeze and retire alike).
real_sync, real_fd_sync = guard._fsync_directory, guard._fsync_fd


def failing_sync(_directory_or_fd):
    raise OSError(5, "EIO")


bridge._STATE = root / "durable-archive"
path, rid = new_review("task-durable-archive")
decided = dict(guard.claim_withheld_decision(path, dict(YES)))
guard._fsync_fd = failing_sync
try:
    moved = guard.archive_record(path)
finally:
    guard._fsync_fd = real_fd_sync
check(moved is False and path.exists(), "archive: the live record stays when the move cannot be made durable")
bridge._STATE = root / "durable-freeze"
directory = bridge._STATE / "withheld-team-results"
directory.mkdir(parents=True)
conflict = directory / "wr_conflict.json"
guard._reserve(conflict, guard._body_digest("first body"))
conflict.write_text(json.dumps({"withheld_body": "second body"}))
guard._fsync_fd = failing_sync
try:
    _acted, frozen_now = guard.actionable_records(directory)
finally:
    guard._fsync_fd = real_fd_sync
check(frozen_now == [] and conflict.exists(), "freeze: the record stays when the move cannot be made durable")
bridge._STATE = root / "durable-retire"
directory = bridge._STATE / "withheld-team-results"
(directory / "archive").mkdir(parents=True)
live = directory / "wr_0000000000000000.json"
(directory / "archive" / live.name).write_text(json.dumps({**card, "status": "kept_private",
                                                          "decision": "sensitive"}))
live.write_text(json.dumps({**card, **NO}))
guard._fsync_fd = failing_sync
try:
    retired = guard.retire_superseded_record(live)
    raised = None
except OSError as exc:
    retired, raised = None, exc
finally:
    guard._fsync_fd = real_fd_sync
check(raised is not None and live.exists(), "retire: the copy stays when the move cannot be made durable")

# 23. Every remaining record write runs under the ledger lock: no file is linked, unlinked,
# replaced or renamed in a review directory while the lock is not held.
bridge._STATE = root / "every-write-locked"
real_lock = guard._ledger_lock
depth, unlocked, locked_writes = [0], [], []


@contextlib.contextmanager
def counting_lock(directory):
    with real_lock(directory):
        depth[0] += 1
        try:
            yield
        finally:
            depth[0] -= 1


def watch(name):
    real = getattr(os, name)

    def watched(*args, **kwargs):
        if str(bridge._STATE) in str(args[0]):
            (locked_writes if depth[0] else unlocked).append((name, str(args[0]).rsplit("/", 2)[-2:]))
        return real(*args, **kwargs)
    return real, watched


originals_os = {}
guard._ledger_lock = counting_lock
for name in ("link", "unlink", "replace", "rename"):
    originals_os[name], patched_fn = watch(name)
    setattr(os, name, patched_fn)
try:
    path, rid = new_review("task-locked")
    claimed = dict(guard.claim_withheld_decision(path, dict(NO)))
    current = json.loads(path.read_text())
    guard.update_record(path, dict(current, dm_sent_at=1.0), expect=guard.record_version(current))
    current = json.loads(path.read_text())
    guard.update_record(path, dict(current, status="published", card_resolution_pending=False),
                        expect=guard.record_version(current))
    guard.archive_record(path)
    live = path.parent / path.name
    live.write_text((path.parent / "archive" / path.name).read_text())
    guard.retire_superseded_record(live)
    conflict = path.parent / "wr_conflict.json"
    guard._reserve(conflict, guard._body_digest("first body"))
    conflict.write_text(json.dumps({"withheld_body": "second body"}))
    guard.actionable_records(path.parent)
    guard.journal_suppressed_result(
        guard.TeamResultVerdict(guard.VERDICT_SUPPRESS, "[no-send]", "suppressed"), "[no-send]",
        bridge._STATE, "task-suppressed", {"source": "ag2space"}, "@agent:example", now=1000)
finally:
    guard._ledger_lock = real_lock
    for name, real in originals_os.items():
        setattr(os, name, real)
kinds = {name for name, _where in locked_writes}
check(unlocked == [] and {"link", "unlink", "replace"} <= kinds,
      f"every record write holds the ledger lock: {unlocked}; seen locked {sorted(kinds)}")
bridge._STATE = root / "state"

# 24. The release goes through the outbox. Only an event id confirms it: {"ok": true}
# without one may have landed, so it parks and is never sent again; the record stays pending.


outbox_mod = importlib.import_module("ag2_sparrow.outbox")
ProviderPermanentRefused = importlib.import_module("ag2_sparrow.delivery_core").ProviderPermanentRefused


def release(label, answer_for_room):
    bridge._STATE = root / f"outbox-{label}"
    path, rid = new_review(f"task-outbox-{label}")
    claimed = dict(guard.claim_withheld_decision(path, dict(NO)))

    def room_req(method, url, payload=None, timeout=35, **_kw):
        if url == "/v1/room" and (payload or {}).get("room_id") == SHARED:
            calls.append((method, url, payload))
            return answer_for_room()
        return fake_req(method, url, payload, timeout)
    return path, rid, claimed, room_req


def run_publish(path, claimed, room_req, beats=3):
    bridge._req = room_req
    try:
        first = bridge._publish_review(path, claimed)
        for _beat in range(beats):
            bridge._retry_pending_publications()
    finally:
        bridge._req = fake_req
    return first


def outbox_item(path, rid, claimed):
    return outbox_mod.read_item(bridge._withheld_outbox().backend.root,
                                f"{rid}.{claimed['decision_claim_id']}") or {}


path, rid, claimed, room_req = release("ok-no-id", lambda: {"ok": True})
before = len(shared_posts())
first = run_publish(path, claimed, room_req)
record = json.loads(path.read_text())
check(first is False and len(shared_posts()) - before == 1 and record["status"] == "publish_pending"
      and outbox_item(path, rid, claimed).get("reason") == "outcome-unknown",
      f"ok without an event id: not published, parked unknown, never re-sent ({record['status']})")

mark = len(calls)
bridge._retry_withheld_reviews()
parked_root = bridge._withheld_outbox().backend.root
check(json.loads(path.read_text()).get("publish_parked") == "outcome-unknown"
      and "could not be confirmed and requires attention" in card_edits_since(mark)[-1]
      and parked_root.parent == bridge.RESULTS_DIR and parked_root.name.startswith(".outbox"),
      "a parked release shows on the card and sits where the parked-outbox health check looks")
path, rid, claimed, room_req = release("event-only", lambda: {"event_id": "$only"})
before = len(shared_posts())
first = run_publish(path, claimed, room_req)
_live, record = final_record(path)
check(first is True and len(shared_posts()) - before == 1 and record["status"] == "published"
      and record["published_event_id"] == "$only",
      f"an event-id-only receipt is published once ({record['status']})")


def http_error(code):
    def raise_it():
        raise urllib.error.HTTPError("http://relay.invalid/v1/room", code, "refused", {}, None)
    return raise_it


path, rid, claimed, room_req = release("refused", http_error(400))
before = len(shared_posts())
run_publish(path, claimed, room_req)
_live, record = final_record(path)
check(len(shared_posts()) - before == 1 and record["status"] == "publish_failed"
      and record["publish_error"] == "permanent-refusal",
      f"a definite refusal is publish_failed after one POST ({record['status']})")

path, rid, claimed, room_req = release("server-error", http_error(503))
before = len(shared_posts())
run_publish(path, claimed, room_req)
check(len(shared_posts()) - before == 1 and json.loads(path.read_text())["status"] == "publish_pending"
      and outbox_item(path, rid, claimed).get("reason") == "outcome-unknown",
      "a 5xx may have applied: parked unknown, never re-sent, still pending")

path, rid, claimed, room_req = release("auth", http_error(401))
before = len(shared_posts())
run_publish(path, claimed, room_req, beats=0)
check(len(shared_posts()) - before == 1 and outbox_item(path, rid, claimed).get("status") == "READY"
      and json.loads(path.read_text())["status"] == "publish_pending",
      "an auth refusal did not apply: the outbox schedules a retry, the record stays pending")
try:
    bridge._withheld_outbox().provider.deliver("bad", b"not json", "bad#0")
    malformed = None
except ProviderPermanentRefused as exc:
    malformed = exc
check(malformed is not None, "a malformed outbox payload is a definite refusal, never sent")

def lost_in_transport():
    raise TimeoutError("no reply from the gateway")


path, rid, claimed, room_req = release("transport", lost_in_transport)
before = len(shared_posts())
run_publish(path, claimed, room_req)
check(len(shared_posts()) - before == 1 and json.loads(path.read_text())["status"] == "publish_pending"
      and outbox_item(path, rid, claimed).get("reason") == "outcome-unknown",
      "a lost reply may have landed: parked, never re-sent, still pending")

path, rid, claimed, room_req = release("declined", lambda: {"ok": False, "error": "no such room"})
before = len(shared_posts())
run_publish(path, claimed, room_req)
_live, record = final_record(path)
check(len(shared_posts()) - before == 1 and record["status"] == "publish_failed",
      f"an explicit decline is a definite refusal: publish_failed ({record['status']})")
check(bridge._withheld_outbox().provider.reconcile(None) is None,
      "the room provider has no read-back to reconcile from")

# 24a. A confirmed send whose record write is refused (a card resolver committed meanwhile)
# and whose first reread is unreadable reads again and commits; nothing is re-sent.
path, rid, claimed, room_req = release("reread", lambda: {"event_id": "$reread"})
real_read, reads_left = bridge._read_review_json, [1]


def unreadable_once(candidate, _path=path):
    if candidate == _path and reads_left[0] > 0:
        reads_left[0] -= 1
        return None
    return real_read(candidate)


def confirm_then_bump(method, url, payload=None, timeout=35, _path=path, _room_req=room_req):
    answer = _room_req(method, url, payload, timeout)
    if url == "/v1/room" and (payload or {}).get("room_id") == SHARED:
        bridge._resolve_review_card(_path, json.loads(_path.read_text()))
        bridge._read_review_json = unreadable_once
    return answer


before = len(shared_posts())
bridge._req = confirm_then_bump
try:
    committed = bridge._publish_review(path, claimed)
finally:
    bridge._req, bridge._read_review_json = fake_req, real_read
run_publish(path, claimed, room_req)
_live, record = final_record(path)
check(committed is True and reads_left == [0] and len(shared_posts()) - before == 1
      and record["status"] == "published",
      f"an unreadable reread is read again, never re-sent ({record['status']})")

# 24b. A crash at each outbox stage never posts beyond the outbox's guarantee. A restart is
# modelled by the claim's owner being a dead process past the reclaim window.
class Crash(BaseException):
    pass


dead = subprocess.Popen([sys.executable, "-c", "pass"])
dead.wait()


def owner_died(path, rid, claimed, age=3600):
    root_dir = bridge._withheld_outbox().backend.root
    claim_file = outbox_mod._claim_path(root_dir, f"{rid}.{claimed['decision_claim_id']}")
    data = json.loads(claim_file.read_text())
    claim_file.write_text(json.dumps({**data, "pid": dead.pid, "start_usec": None,
                                      "claimed_at": data["claimed_at"] - age}))


for stage, expected_posts in (("before-claim", 1), ("before-post", 1), ("after-post", 2),
                              ("after-delivered", 1)):
    path, rid, claimed, room_req = release(f"crash-{stage}", lambda: {"event_id": "$c"})
    crashed = []

    def crashing_req(method, url, payload=None, timeout=35, _stage=stage, _room_req=room_req):
        if url == "/v1/room" and (payload or {}).get("room_id") == SHARED and not crashed:
            if _stage == "before-post":
                crashed.append(True)
                raise Crash()
            if _stage == "after-post":
                _room_req(method, url, payload, timeout)
                crashed.append(True)
                raise Crash()
        return _room_req(method, url, payload, timeout)

    real_deliver, real_commit = bridge.DeliveryCore.deliver_one, bridge._commit_publication
    if stage == "before-claim":
        bridge.DeliveryCore.deliver_one = lambda self, *a, **k: (_ for _ in ()).throw(Crash())
    if stage == "after-delivered":
        def crash_commit(*a, **k):
            bridge._commit_publication = real_commit
            raise Crash()
        bridge._commit_publication = crash_commit
    before = len(shared_posts())
    bridge._req = crashing_req
    try:
        bridge._publish_review(path, claimed)
    except Crash:
        pass
    finally:
        bridge._req = fake_req
        bridge.DeliveryCore.deliver_one, bridge._commit_publication = real_deliver, real_commit
    if stage in ("before-post", "after-post"):
        owner_died(path, rid, claimed)
    bridge._WITHHELD_OUTBOX = None  # the restart builds a fresh outbox and provider
    run_publish(path, claimed, room_req)
    _live, record = final_record(path)
    check(len(shared_posts()) - before == expected_posts and record["status"] == "published"
          and record.get("published_event_id") == "$c",
          f"crash {stage}: {len(shared_posts()) - before} POSTs (outbox bound {expected_posts}), "
          f"{record['status']}, event id {record.get('published_event_id')!r}")

# 24c. A refusal wins over an event id in the same response, and only a Matrix event id
# confirms: an integer or another string is unconfirmed and parks.
for label, answer, expected in (
        ("refusal-with-id", {"ok": False, "error": "refused", "event_id": "$error-context"},
         "publish_failed"),
        ("integer-id", {"ok": True, "event_id": 17}, "publish_pending"),
        ("not-matrix-id", {"ok": True, "event_id": "msg-17"}, "publish_pending")):
    path, rid, claimed, room_req = release(label, lambda _a=answer: dict(_a))
    before = len(shared_posts())
    run_publish(path, claimed, room_req)
    _live, record = final_record(path)
    check(len(shared_posts()) - before == 1 and record["status"] == expected
          and "published_event_id" not in record,
          f"{label}: {record['status']} (expected {expected}), never published")

# 24d. DELIVERED without a recorded receipt is reported and left pending, never published;
# a receipt that cannot be recorded leaves the send unconfirmed.
path, rid, claimed, room_req = release("no-receipt", lambda: {"event_id": "$lost"})
real_commit = bridge._commit_publication


def crash_and_lose_receipt(*a, **k):
    bridge._commit_publication = real_commit
    current = json.loads(path.read_text())
    stripped = {k2: v for k2, v in current.items() if k2 != "release_event_id"}
    guard.update_record(path, stripped, expect=guard.record_version(current))
    raise Crash()


bridge._commit_publication = crash_and_lose_receipt
try:
    run_publish(path, claimed, room_req, beats=0)
except Crash:
    pass
finally:
    bridge._commit_publication = real_commit
logs_before = len(logs)
run_publish(path, claimed, room_req)
record = json.loads(path.read_text())
check(record["status"] == "publish_pending" and "published_event_id" not in record
      and any("no event id was recorded" in line for line in logs[logs_before:]),
      f"delivered without a receipt: reported, left pending ({record['status']})")
path, rid, claimed, room_req = release("receipt-fails", lambda: {"event_id": "$unrecorded"})
real_receipt = guard.record_release_receipt
guard.record_release_receipt = lambda *a, **k: False
try:
    run_publish(path, claimed, room_req)
finally:
    guard.record_release_receipt = real_receipt
check(json.loads(path.read_text())["status"] == "publish_pending"
      and outbox_item(path, rid, claimed).get("reason") == "outcome-unknown",
      "a receipt that cannot be recorded leaves the send unconfirmed and parked")

# 25. An owner decision is durable before it is reported: the record's directory is synced
# after the rename, a sync failure is not success, and an orphan claim blocks a second one.
bridge._STATE = root / "durable-decision"
path, rid = new_review("task-durable-decision")
order, real_replace, real_sync = [], os.replace, guard._fsync_directory


def spy_replace(source, target, *a, **k):
    real_replace(source, target, *a, **k)
    order.append(("replace", pathlib.Path(target)))


def spy_sync(directory):
    order.append(("sync", pathlib.Path(directory)))
    real_sync(directory)


os.replace, guard._fsync_directory = spy_replace, spy_sync
try:
    claimed = guard.claim_withheld_decision(path, dict(YES))
finally:
    os.replace, guard._fsync_directory = real_replace, real_sync
renamed = order.index(("replace", path))
check(claimed is not None and ("sync", path.parent) in order[renamed + 1:],
      f"the record's directory is synced after its rename: {order[-3:]}")
bridge._STATE = root / "durable-artifact"
order, real_link = [], os.link


def spy_link(source, target, *a, **k):
    real_link(source, target, *a, **k)
    order.append(("link", pathlib.Path(target)))


os.link, guard._fsync_directory = spy_link, spy_sync
try:
    guard.materialize_withheld_verdict(
        leak, "SECRET-BODY", bridge._STATE, "task-durable-artifact",
        {"source": "ag2space", "channel_id": SHARED, "user_id": "@team:example"},
        "@agent:example", now=1000)
finally:
    os.link, guard._fsync_directory = real_link, real_sync
artifact = guard.withheld_review_path(bridge._STATE, "task-durable-artifact")
linked = order.index(("link", artifact))
check(("sync", artifact.parent) in order[linked + 1:],
      f"a new review record's directory is synced after it is linked: {order[-3:]}")
bridge._STATE = root / "durable-artifact-fails"


def failing_artifact_sync(directory):
    if pathlib.Path(directory).name == guard.WITHHELD_RESULT_DIR:
        raise OSError(5, "EIO")
    real_sync(directory)


guard._fsync_directory = failing_artifact_sync
try:
    unsaved = guard.materialize_withheld_verdict(
        leak, "SECRET-BODY", bridge._STATE, "task-artifact-fails",
        {"source": "ag2space", "channel_id": SHARED}, "@agent:example", now=1000)
finally:
    guard._fsync_directory = real_sync
check(unsaved.body == guard.TEAM_LEAK_RESULT_UNSAVED,
      "a review record that cannot be made durable is reported unsaved, fail-closed")
bridge._STATE = root / "durable-decision-fails"
path, rid = new_review("task-durable-fails")


def failing_record_sync(directory):
    if pathlib.Path(directory) == path.parent:
        raise OSError(5, "EIO")
    real_sync(directory)


guard._fsync_directory = failing_record_sync
try:
    claimed = guard.claim_withheld_decision(path, dict(YES))
finally:
    guard._fsync_directory = real_sync
check(claimed is None and any((path.parent / "claims").glob(f"{path.stem}.*")),
      "a decision whose rename landed but cannot be made durable is not reported, and its claim stays")
bridge._STATE = root / "orphan-claim"
path, rid = new_review("task-orphan-claim")
awaiting = path.read_bytes()
check(guard.claim_withheld_decision(path, dict(YES)) is not None, "orphan claim: Yes claimed")
path.write_bytes(awaiting)  # power loss: the claim entry survived, the decided record did not
before = len(shared_posts())
check(guard.claim_withheld_decision(path, dict(NO)) is None
      and bridge._handle_review_decision(decision("No", rid, "orphan-no")) is True
      and len(shared_posts()) == before and json.loads(path.read_text())["status"] == "awaiting_owner",
      "orphan claim: a later opposite reply is refused and nothing is published")
check("was not saved" in json.loads(bridge._control_result_path("orphan-no").read_text())["body"],
      "orphan claim: the owner is told the earlier decision was lost")

# 25b. A decision whose record write fails before the rename leaves no claim behind: the
# owner is told it was not saved, and answering again works.
bridge._STATE = root / "unwritten-decision"
path, rid = new_review("task-unwritten")
real_replace, failed = os.replace, []


def replace_fails_once(source, target, *a, **k):
    if pathlib.Path(target) == path and not failed:
        failed.append(True)
        raise OSError(28, "No space left on device")
    return real_replace(source, target, *a, **k)


os.replace = replace_fails_once
try:
    first = bridge._handle_review_decision(decision("No", rid, "unwritten-1"))
finally:
    os.replace = real_replace
answer = json.loads(bridge._control_result_path("unwritten-1").read_text())["body"]
check(first is True and failed and not any((path.parent / "claims").glob(f"{path.stem}.*"))
      and json.loads(path.read_text())["status"] == "awaiting_owner" and "please reply again" in answer,
      f"unwritten decision: no claim left, the owner is told to answer again: {answer[:60]}")
before = len(shared_posts())
bridge._handle_review_decision(decision("No", rid, "unwritten-2"))
_live, record = final_record(path)
check(record["status"] == "published" and len(shared_posts()) - before == 1,
      f"unwritten decision: answering again publishes once ({record['status']})")

# 25c. The claim-ledger entry is synced before the record names it.
bridge._STATE = root / "ledger-synced"
path, rid = new_review("task-ledger-synced")
synced = []
guard._fsync_directory = lambda directory: (synced.append(pathlib.Path(directory)), real_sync(directory))
try:
    guard.claim_withheld_decision(path, dict(YES))
finally:
    guard._fsync_directory = real_sync
check(synced[:1] == [path.parent / "claims"], f"the claim ledger is synced first: {synced[:2]}")

# 25d. A decision whose rename landed but whose directory sync keeps failing is not durable:
# the real retry beat posts nothing and edits no card until a durability barrier succeeds.
bridge._STATE = root / "undurable-decision"
path, rid = new_review("task-undurable")
mark, before = len(calls), len(shared_posts())


def record_dir_sync_fails(directory):
    if pathlib.Path(directory) == path.parent:
        raise OSError(5, "EIO")
    real_sync(directory)


guard._fsync_directory = record_dir_sync_fails
try:
    bridge._handle_review_decision(decision("No", rid, "undurable-no"))
    for _beat in range(3):
        bridge._retry_withheld_reviews()
    _live, stuck = final_record(path)
finally:
    guard._fsync_directory = real_sync
check(stuck["status"] == "publish_pending" and stuck.get("decision_durable") is False
      and len(shared_posts()) == before and card_edits_since(mark) == [],
      f"undurable decision: no POST and no card edit while the sync fails ({stuck['status']})")
bridge._retry_withheld_reviews()
_live, record = final_record(path)
check(record["status"] == "published" and len(shared_posts()) - before == 1,
      f"undurable decision: exactly one publication after the barrier ({record['status']})")

# 25e. A claim survives a rollback of its record: the owner is told whether a publication
# may have been sent, which only the outbox can prove, and how to unlock the review.
for label, sent in (("rollback-unsent", False), ("rollback-sent", True)):
    bridge._STATE = root / label
    path, rid = new_review(f"task-{label}")
    awaiting = path.read_bytes()
    claimed = guard.claim_withheld_decision(path, dict(NO))
    if sent:
        check(bridge._publish_review(path, dict(claimed)) is True, f"{label}: one publication")
    path.write_bytes(awaiting)  # power loss: the claim survived, the decided record did not
    bridge._handle_review_decision(decision("Yes", rid, f"{label}-again"))
    notice = json.loads(bridge._control_result_path(f"{label}-again").read_text())["body"]
    claims = sorted((path.parent / "claims").glob(f"{path.stem}.*"))
    check(("may already have been sent" in notice) == sent and ("Nothing was sent" in notice) != sent
          and f"rm -- {claims[0]}" in notice,
          f"{label}: the notice says what the outbox proves and how to unlock: {notice[-90:]}")

# 25f. A crash between the receipt and DELIVERED ends published with one POST; the outbox
# item is driven DELIVERED once its claim can be taken (at once, or after the reclaim window).
for label, owner_state in (("receipt-crash-reclaimed", "dead"), ("receipt-crash-held", "alive"),
                           ("receipt-crash-fresh-death", "dead-before-ttl")):
    path, rid, claimed, room_req = release(label, lambda: {"event_id": "$landed"})
    provider = bridge._withheld_outbox().provider
    real_record = provider._record_receipt

    def record_then_crash(*a, _real=real_record):
        _real(*a)
        raise Crash()

    provider._record_receipt = record_then_crash
    before = len(shared_posts())
    bridge._req = room_req
    try:
        bridge._publish_review(path, claimed)
    except Crash:
        pass
    finally:
        bridge._req = fake_req
        provider._record_receipt = real_record
    if owner_state != "alive":
        owner_died(path, rid, claimed, age=3600 if owner_state == "dead" else 0)
    bridge._WITHHELD_OUTBOX = None
    run_publish(path, claimed, room_req)
    bridge._retry_withheld_reviews()  # the card resolves and the review is archived
    _live, record = final_record(path)
    first_status = outbox_item(path, rid, claimed).get("status")
    check(len(shared_posts()) - before == 1 and record["status"] == "published"
          and record["published_event_id"] == "$landed" and not path.exists()
          and (first_status == "DELIVERED") == (owner_state == "dead"),
          f"{label}: published from the receipt and archived, one POST, outbox {first_status}")
    if owner_state != "dead":
        owner_died(path, rid, claimed)
        bridge._retry_pending_publications()
    check(outbox_item(path, rid, claimed).get("status") == "DELIVERED"
          and len(shared_posts()) - before == 1,
          f"{label}: once the claim can be taken the outbox item ends DELIVERED, no second POST")

# 25g. A receipt whose sync failed after the POST landed still proves the landing: the
# release is published from it, never sent again.
path, rid, claimed, room_req = release("receipt-sync-fails", lambda: {"event_id": "$synced-late"})


def receipt_sync_fails(directory, _path=path):
    if pathlib.Path(directory) == _path.parent and "release_event_id" in _path.read_text():
        raise OSError(5, "EIO")
    real_sync(directory)


before = len(shared_posts())
guard._fsync_directory = receipt_sync_fails
try:
    run_publish(path, claimed, room_req, beats=0)
finally:
    guard._fsync_directory = real_sync
run_publish(path, claimed, room_req)
_live, record = final_record(path)
check(len(shared_posts()) - before == 1 and record["status"] == "published"
      and record["published_event_id"] == "$synced-late"
      and outbox_item(path, rid, claimed).get("status") == "DELIVERED",
      f"receipt sync failed after the POST: published once, the parked item settled DELIVERED "
      f"({record['status']}, {outbox_item(path, rid, claimed).get('status')})")

# 25h. Claim failures fail safe: a non-OSError write failure frees the review; a rollback
# that fails leaves a claim the owner is told how to remove; a raising claim is reported.
bridge._STATE = root / "claim-type-error"
path, rid = new_review("task-claim-type-error")
real_dump, dumped = guard.json.dump, []


def dump_fails_once(*a, **k):
    if not dumped:
        dumped.append(True)
        raise TypeError("not serialisable")
    return real_dump(*a, **k)


guard.json.dump = dump_fails_once
try:
    bridge._handle_review_decision(decision("Yes", rid, "type-error-1"))
finally:
    guard.json.dump = real_dump
answer = json.loads(bridge._control_result_path("type-error-1").read_text())["body"]
bridge._handle_review_decision(decision("Yes", rid, "type-error-2"))
_live, record = final_record(path)
check("please reply again" in answer and record["status"] == "kept_private",
      f"a non-OSError write failure frees the review ({record['status']})")
bridge._STATE = root / "rollback-fails"
path, rid = new_review("task-rollback-fails")
real_unlink, real_replace = pathlib.Path.unlink, os.replace


def unlink_claim_fails(self, *a, **k):
    if self.parent.name == "claims":
        raise OSError(13, "EACCES")
    return real_unlink(self, *a, **k)


def replace_record_fails(source, target, *a, **k):
    if pathlib.Path(target) == path:
        raise OSError(28, "ENOSPC")
    return real_replace(source, target, *a, **k)


pathlib.Path.unlink, os.replace = unlink_claim_fails, replace_record_fails
try:
    bridge._handle_review_decision(decision("No", rid, "rollback-fails-1"))
finally:
    pathlib.Path.unlink, os.replace = real_unlink, real_replace
bridge._handle_review_decision(decision("No", rid, "rollback-fails-2"))
notice = json.loads(bridge._control_result_path("rollback-fails-2").read_text())["body"]
check("To answer again, remove its claim: rm --" in notice and "Nothing was sent" in notice,
      f"a failed rollback is reported with its unlock step: {notice[-80:]}")
bridge._STATE = root / "claim-raises"
path, rid = new_review("task-claim-raises")
real_claim = guard.claim_withheld_decision
guard.claim_withheld_decision = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    raised = None
    handled = bridge._handle_review_decision(decision("No", rid, "claim-raises"))
except Exception as exc:  # noqa: BLE001
    raised = exc
finally:
    guard.claim_withheld_decision = real_claim
check(raised is None and handled is True and any("could not be processed (RuntimeError at " in line and "boom" not in line for line in logs)
      and "please reply again" in json.loads(bridge._control_result_path("claim-raises").read_text())["body"],
      f"a raising claim is answered, never escapes the beat: {raised!r}")

# 25j. A real shared-ledger-lock failure after the reply is matched is answered and contained;
# once the lock works again the owner's next reply is applied.
bridge._STATE = root / "lock-fails"
path, rid = new_review("task-lock-fails")
lock_file = path.parent / "issued" / ".lock"
real_match = bridge._match_review_decision


def match_then_lock_breaks(task):
    matched = real_match(task)
    os.chmod(lock_file, 0)
    return matched


bridge._match_review_decision = match_then_lock_breaks
try:
    raised = None
    handled = bridge._handle_review_decision(decision("No", rid, "lock-fails-1"))
except Exception as exc:  # noqa: BLE001
    raised = exc
finally:
    bridge._match_review_decision = real_match
    os.chmod(lock_file, 0o600)
answer = json.loads(bridge._control_result_path("lock-fails-1").read_text())["body"]
before = len(shared_posts())
bridge._handle_review_decision(decision("No", rid, "lock-fails-2"))
_live, record = final_record(path)
check(raised is None and handled is True and "could not be processed" in answer
      and record["status"] == "published" and len(shared_posts()) - before == 1,
      f"a real lock failure is answered, then the next reply applies ({record['status']})")

# 25k. A card edit refused with an event id is a refusal: the live copy is not retired.
bridge._STATE = root / "states" / "card-refused"
directory = bridge._STATE / "withheld-team-results"
(directory / "archive").mkdir(parents=True)
(directory / "archive" / "wr_0000000000000000.json").write_text(
    json.dumps({**base_record, **YES, "dm_event_id": "$card-refused"}))
live = directory / "wr_0000000000000000.json"
live.write_text(json.dumps({**base_record, **NO, "dm_event_id": "$card-refused"}))


def refused_edit(method, url, payload=None, timeout=35):
    if url == "/v1/room" and (payload or {}).get("op") == "edit":
        return {"ok": False, "error": "refused", "event_id": "$diagnostic"}
    return fake_req(method, url, payload, timeout)


bridge._req = refused_edit
try:
    bridge._retry_withheld_reviews()
finally:
    bridge._req = fake_req
check(live.exists() and not (directory / "archive" / "superseded").exists(),
      "a refused card edit carrying an event id does not retire the live copy")

# 25l. The unlock command in a lost-decision notice is shell-quoted.
bridge._STATE = root / "Application Support" / "unlock"
path, rid = new_review("task-unlock-quoted")
awaiting = path.read_bytes()
guard.claim_withheld_decision(path, dict(NO))
path.write_bytes(awaiting)
bridge._handle_review_decision(decision("Yes", rid, "unlock-quoted"))
notice = json.loads(bridge._control_result_path("unlock-quoted").read_text())["body"]
claim_file = next((path.parent / "claims").glob(f"{path.stem}.*"))
check(shlex.split(notice.split("remove its claim: ", 1)[1]) == ["rm", "--", str(claim_file)],
      f"the unlock command round-trips through the shell: {notice[-120:]}")

# 25i. The recovery command is shell-quoted, so a root under Application Support works.
command = bridge._requeue_command(pathlib.Path("/Users/x/Library/Application Support/ws/results/"
                                               ".outbox-withheld-review"), "wr_1.c")
check(shlex.split(command) == ["python3", "src/outbox_cli.py", "--root",
                               "/Users/x/Library/Application Support/ws/results/.outbox-withheld-review",
                               "requeue", "wr_1.c"], f"the recovery command is quoted: {command}")

# 25m. A stale live copy never hides the archived receipt: with the card edit refused (so the
# copy stays) and the claim owner dead past the window, the item still settles DELIVERED.
path, rid, claimed, room_req = release("archive-wins", lambda: {"event_id": "$archive-wins"})
stale = path.read_bytes()
provider = bridge._withheld_outbox().provider
real_record = provider._record_receipt


def receipt_then_crash(*a, _real=real_record):
    _real(*a)
    raise Crash()


provider._record_receipt = receipt_then_crash
before = len(shared_posts())
bridge._req = room_req
try:
    bridge._publish_review(path, claimed)
except Crash:
    pass
finally:
    bridge._req = fake_req
    provider._record_receipt = real_record
run_publish(path, claimed, room_req)
bridge._retry_withheld_reviews()
archived_first = outbox_item(path, rid, claimed).get("status")
path.write_bytes(stale)
owner_died(path, rid, claimed)
bridge._req = refused_edit
try:
    bridge._retry_withheld_reviews()
finally:
    bridge._req = fake_req
_archived = json.loads((path.parent / "archive" / path.name).read_text())
check(archived_first != "DELIVERED" and path.exists() and _archived["status"] == "published"
      and outbox_item(path, rid, claimed).get("status") == "DELIVERED"
      and len(shared_posts()) - before == 1,
      f"the archived receipt settles despite a live copy: "
      f"{archived_first} -> {outbox_item(path, rid, claimed).get('status')}, "
      f"{len(shared_posts()) - before} POST(s)")

# 25n. An unreadable archived record fails closed: the item is not settled from the live copy.
path, rid, claimed, room_req = release("archive-unreadable", lambda: {"event_id": "$unreadable"})
provider._record_receipt = receipt_then_crash
bridge._req = room_req
try:
    bridge._publish_review(path, claimed)
except Crash:
    pass
finally:
    bridge._req = fake_req
    provider._record_receipt = real_record
archived = path.parent / "archive" / path.name
archived.parent.mkdir(parents=True, exist_ok=True)
archived.write_text(path.read_text())
owner_died(path, rid, claimed)
os.chmod(archived, 0)
try:
    bridge._settle_landed_releases()
    unreadable_status = outbox_item(path, rid, claimed).get("status")
finally:
    os.chmod(archived, 0o600)
bridge._settle_landed_releases()
check(unreadable_status != "DELIVERED"
      and outbox_item(path, rid, claimed).get("status") == "DELIVERED",
      f"an unreadable archive is never outranked by its live copy ({unreadable_status})")

# 25n2. A dangling archive record is unknown, never absent: the live copy does not settle it.
path, rid, claimed, room_req = release("archive-dangling", lambda: {"event_id": "$dangling"})
provider._record_receipt = receipt_then_crash
bridge._req = room_req
try:
    bridge._publish_review(path, claimed)
except Crash:
    pass
finally:
    bridge._req = fake_req
    provider._record_receipt = real_record
archived = path.parent / "archive" / path.name
archived.parent.mkdir(parents=True, exist_ok=True)
archived.symlink_to("missing-target.json")
owner_died(path, rid, claimed)
bridge._settle_landed_releases()
check(outbox_item(path, rid, claimed).get("status") != "DELIVERED",
      "a dangling archive record is never outranked by its live copy at settlement")
archived.unlink()
bridge._settle_landed_releases()

# 25o. Settlement drives the outbox only through claim and complete: no POST, no other call.
path, rid, claimed, room_req = release("settle-only", lambda: {"event_id": "$settle-only"})
provider._record_receipt = receipt_then_crash
bridge._req = room_req
try:
    bridge._publish_review(path, claimed)
except Crash:
    pass
finally:
    bridge._req = fake_req
    provider._record_receipt = real_record
owner_died(path, rid, claimed)


class BackendSpy:
    def __init__(self, real):
        self._real, self.used = real, []

    def __getattr__(self, name):
        value = getattr(self._real, name)
        if callable(value):
            self.used.append(name)
        return value


def no_network(*args, **kwargs):
    raise AssertionError(("settlement reached the network", args))


core = bridge._withheld_outbox()
spy, real_backend, real_requeue = BackendSpy(core.backend), core.backend, bridge.requeue_item_with_epoch
requeued = []
core.backend, bridge._req = spy, no_network
bridge.requeue_item_with_epoch = lambda *a, **k: requeued.append(a)
try:
    bridge._settle_landed_releases()
finally:
    core.backend, bridge._req, bridge.requeue_item_with_epoch = real_backend, fake_req, real_requeue
check(set(spy.used) == {"claim", "complete"} and not requeued
      and outbox_item(path, rid, claimed).get("status") == "DELIVERED",
      f"settlement uses only claim and complete: {sorted(set(spy.used))}, requeue {len(requeued)}")

def control_body(task_id):
    answer_file = bridge._control_result_path(task_id)
    return json.loads(answer_file.read_text())["body"] if answer_file.is_file() else ""


# 25p. A reply the reviews cannot be inspected for (lock, issued/ or archive unreadable before
# the match) is consumed and answered, never written as ordinary work; then the next applies.
for label, target in (("first-lock", "issued/.lock"), ("issued-dir", "issued"),
                      ("archive-dir", "archive")):
    bridge._STATE = root / f"unreadable-{label}"
    path, rid = new_review(f"task-unreadable-{label}")
    broken = path.parent / target
    broken.mkdir(exist_ok=True) if target == "archive" else None
    mode = broken.stat().st_mode & 0o777
    before = len(shared_posts())
    os.chmod(broken, 0)
    try:
        raised, handled = None, None
        handled = bridge._handle_review_decision(decision("No", rid, f"unreadable-{label}-1"))
    except Exception as exc:  # noqa: BLE001
        raised = exc
    finally:
        os.chmod(broken, mode)
    answer = control_body(f"unreadable-{label}-1")
    unchanged = path.is_file() and json.loads(path.read_text())["status"] == "awaiting_owner"
    bridge._handle_review_decision(decision("No", rid, f"unreadable-{label}-2"))
    _live, record = final_record(path)
    check(raised is None and handled is True and answer and "please reply again" in answer
          and unchanged and record["status"] == "published" and len(shared_posts()) - before == 1,
          f"{label} unreadable before the match: consumed and answered, then the next reply "
          f"applies (raised={raised!r}, handled={handled}, {record['status']})")

# A later reply to an archived review, with the archive (or its record) unreadable, is
# consumed and answered too; once readable, the archived outcome answers it.
for label in ("archive-only-dir", "archive-only-record"):
    bridge._STATE = root / f"unreadable-{label}"
    path, rid = new_review(f"task-unreadable-{label}")
    bridge._handle_review_decision(decision("Yes", rid, f"{label}-decide"))
    bridge._retry_withheld_reviews()
    archived = path.parent / "archive" / path.name
    broken = archived.parent if label == "archive-only-dir" else archived
    os.chmod(broken, 0)
    try:
        raised, handled = None, None
        handled = bridge._handle_review_decision(decision("No", rid, f"{label}-1"))
    except Exception as exc:  # noqa: BLE001
        raised = exc
    finally:
        os.chmod(broken, 0o700 if broken.is_dir() else 0o600)
    answer = control_body(f"{label}-1")
    bridge._handle_review_decision(decision("No", rid, f"{label}-2"))
    later = control_body(f"{label}-2")
    check(not path.exists() and raised is None and handled is True
          and "please reply again" in answer and "please reply again" not in later
          and json.loads(archived.read_text())["status"] == "kept_private",
          f"{label} unreadable: the reply is answered, never ordinary work ({raised!r}, {handled})")

# 25q. An answer that cannot be saved is logged and the beat goes on; the reply stays unacked.
bridge._STATE = root / "unwritable-answer"
path, rid = new_review("task-unwritable-answer")
lock_file = path.parent / "issued" / ".lock"
real_control_dir = bridge._WITHHELD_CONTROL_DIR
bridge._WITHHELD_CONTROL_DIR = path.parent / "not-a-directory"
bridge._WITHHELD_CONTROL_DIR.write_text("")
logs_before = len(logs)
os.chmod(lock_file, 0)
try:
    raised, handled = None, None
    handled = bridge._handle_review_decision(decision("No", rid, "unwritable-answer"))
except Exception as exc:  # noqa: BLE001
    raised = exc
finally:
    os.chmod(lock_file, 0o600)
    bridge._WITHHELD_CONTROL_DIR = real_control_dir
check(raised is None and handled is True
      and any("unwritable-answer not saved" in line for line in logs[logs_before:]),
      f"an unsaved answer is logged, never raised (raised={raised!r}, handled={handled})")

# 25r. An unreadable archive is unknown, never absent: a publish_pending copy beside a kept_private
# decision never posts, under 3.14's reading (emulated on any Python) and a real mode 000.
@contextlib.contextmanager
def unreadable_like_314(target, stat=True, listing=False):
    target = pathlib.Path(target)
    real = {"stat": os.stat, "lstat": os.lstat, "listdir": os.listdir, "scandir": os.scandir}
    swallowed = {name: getattr(pathlib.Path, name) for name in ("exists", "is_file", "is_dir")}

    def refusing(name, inside):
        def call(p=".", *args, **kwargs):
            if isinstance(p, (str, bytes, os.PathLike)):
                candidate = pathlib.Path(os.fsdecode(p))
                if candidate == target or (inside and target in candidate.parents):
                    raise PermissionError(13, "Permission denied", os.fsdecode(p))
            return real[name](p, *args, **kwargs)
        return call

    def blocked(candidate):
        candidate = pathlib.Path(candidate)
        return candidate == target or target in candidate.parents

    def reads_absent(real):
        # Decided here, not via os.stat: Python 3.9's pathlib bound its stat before any patch.
        def check(self, *args, **kwargs):
            if stat and blocked(self):
                return False
            try:
                return real(self, *args, **kwargs)
            except OSError:
                return False
        return check

    if stat:
        os.stat, os.lstat = refusing("stat", inside=True), refusing("lstat", inside=True)
    if listing:
        os.listdir, os.scandir = refusing("listdir", False), refusing("scandir", False)
    for name, method in swallowed.items():
        setattr(pathlib.Path, name, reads_absent(method))
    try:
        yield
    finally:
        os.stat, os.lstat = real["stat"], real["lstat"]
        os.listdir, os.scandir = real["listdir"], real["scandir"]
        for name, method in swallowed.items():
            setattr(pathlib.Path, name, method)


@contextlib.contextmanager
def chmod_000(target):
    mode = target.stat().st_mode & 0o777
    os.chmod(target, 0)
    try:
        yield
    finally:
        os.chmod(target, mode)


variants = [("emulated", unreadable_like_314)]
if os.geteuid() != 0:
    variants.append(("chmod", chmod_000))
for label, make_unreadable in variants:
    path, rid, claimed, room_req = release(f"archive-unknown-{label}", lambda: {"event_id": "$leak"})
    archived = path.parent / "archive" / path.name
    archived.parent.mkdir(mode=0o700)
    archived.write_text(json.dumps({**json.loads(path.read_text()), **YES}))
    before, raised, mark = len(shared_posts()), [], len(calls)
    with make_unreadable(archived.parent):
        try:
            emulation_active = not archived.exists()
        except OSError:
            emulation_active = label == "chmod"  # this Python raises instead of reading absent
        for step in (bridge._retry_pending_publications,
                     lambda: bridge._handle_review_decision(decision("No", rid, f"unknown-{label}")),
                     bridge._retry_withheld_reviews):
            bridge._req = room_req
            try:
                step()
            except Exception as exc:  # noqa: BLE001
                raised.append(exc)
            finally:
                bridge._req = fake_req
    check(emulation_active and len(shared_posts()) == before
          and json.loads(archived.read_text())["status"] == "kept_private",
          f"{label}: an unreadable archive never lets a kept-private body post "
          f"({len(shared_posts()) - before} POST(s), raised {raised!r})")
    check(not raised, f"{label}: an unreadable archive is contained, never raised: {raised!r}")
    card = json.loads(path.read_text()).get("dm_event_id")
    edits = [p for _m, u, p in calls[mark:] if u == "/v1/room" and p.get("op") == "edit"
             and p.get("event_id") == card]
    check(not edits, f"{label}: the live copy never edits the owner's card while the archive is "
                     f"unknown ({len(edits)} edit(s))")
    with make_unreadable(archived.parent):
        try:
            bridge._publish_review(path, claimed)
            gate = "passed"
        except OSError:
            gate = "raised"
    check(gate == "raised" and len(shared_posts()) == before,
          f"{label}: the publication gate raises on an unknown archive, before any POST ({gate})")
    bridge._req = room_req
    try:
        published = bridge._publish_review(path, claimed)
    finally:
        bridge._req = fake_req
    check(published is False and len(shared_posts()) == before,
          f"{label}: once readable, the kept-private decision still wins: no POST")

# 25s. Each converted existence check reads unknown as unknown, never as absent: under the 3.14
# emulation (a refused stat or listing), or a symlink or wrong kind where a real entry belongs.
def raises_unknown(call):
    try:
        call()
    except OSError:
        return True
    return False


def refuses(call):
    try:
        return call() is False
    except Exception:  # noqa: BLE001
        return False


FIELD = "withheld_body"
bridge._STATE = root / "site-contracts"
path, rid = new_review("task-site-contracts")
archive_dir = path.parent / "archive"
archive_dir.mkdir(mode=0o700)
(archive_dir / path.name).write_text(json.dumps({**json.loads(path.read_text()), **YES}))
digest = guard._record_digest(path, FIELD)
reservation = path.parent / "issued" / f"{path.stem}.issued"
site = {}
with unreadable_like_314(archive_dir):
    site["live-undecided"] = raises_unknown(lambda: guard._live_undecided_record(path))
    site["owns-id"] = raises_unknown(lambda: guard._owns_its_id(path, FIELD))
    site["archived-decision"] = guard.archived_withheld_decision(path) == {}
    site["record-of"] = raises_unknown(lambda: guard._record_of(path, digest, FIELD))
    site["superseded"] = raises_unknown(lambda: guard._proven_superseded(path, FIELD))
    site["bridge-archived-reply"] = raises_unknown(
        lambda: bridge._archived_reply(path.with_name("wr_ffffffffffffffff.json")))
    site["archive-record-refuses"] = refuses(lambda: guard.archive_record(path)) and path.exists()
    saved_reservation = reservation.read_text()
    reservation.unlink()
    try:
        site["issued-seeding"] = raises_unknown(lambda: guard._issued_to(path, FIELD))
    finally:
        reservation.write_text(saved_reservation)
    real_match = bridge._match_review_decision
    decided = path.with_name("wr_ffffffffffffffff.json")  # no live copy: only its archive decides
    bridge._match_review_decision = lambda task: (decided, json.loads(path.read_text()), "no")
    try:
        before = len(shared_posts())
        site["handle-contains"] = (bridge._handle_review_decision(decision("No", rid, "site-handle"))
                                   is True and "please reply again" in control_body("site-handle")
                                   and len(shared_posts()) == before)
    except Exception:  # noqa: BLE001
        site["handle-contains"] = False
    finally:
        bridge._match_review_decision = real_match
real_read = bridge._read_review_json
bridge._read_review_json = lambda _path: None
with unreadable_like_314(path):
    site["reread"] = raises_unknown(lambda: bridge._reread_record(path))
bridge._read_review_json = real_read
with unreadable_like_314(path.parent, stat=False, listing=True):
    site["actionable-listing"] = raises_unknown(lambda: guard.actionable_records(path.parent))

# A symlink where a real record or directory belongs: present() refuses it on any Python.
bridge._STATE = root / "site-links"
linked, linked_rid = new_review("task-site-links")
real_copy = linked.with_name("real-copy.json")
real_copy.write_bytes(linked.read_bytes())
linked.unlink()
linked.symlink_to(real_copy.name)
version = guard.record_version(json.loads(real_copy.read_text()))
site["write-owned-refuses"] = (refuses(lambda: guard.update_record(
    linked, dict(json.loads(real_copy.read_text()), note="x"), expect=version)) and linked.is_symlink())
site["archive-record-symlink"] = refuses(lambda: guard.archive_record(linked)) and linked.is_symlink()
site["write-artifact"] = raises_unknown(lambda: guard._write_artifact(
    linked, json.loads(real_copy.read_text()), FIELD))
review_dir = linked.parent
moved = review_dir.with_name("withheld-team-results-real")
review_dir.rename(moved)
review_dir.symlink_to(moved.name)
try:
    site["actionable-dir"] = raises_unknown(lambda: guard.actionable_records(review_dir))
    site["find-record"] = raises_unknown(lambda: guard.withheld_review_artifact(
        bridge._STATE, "task-site-links", "SECRET-BODY"))
finally:
    review_dir.unlink()
    moved.rename(review_dir)

# A claims/ entry of the wrong kind: the ledger listings never read it as "no claim".
bridge._STATE = root / "site-claims"
fresh, _fresh_rid = new_review("task-site-claims")
claims_dir = fresh.parent / "claims"
shutil.rmtree(claims_dir, ignore_errors=True)
claims_dir.write_text("not a directory")
site["orphan-claim"] = (raises_unknown(lambda: guard.claim_withheld_decision(fresh, dict(NO)))
                        and json.loads(fresh.read_text())["status"] == "awaiting_owner")
site["issued-claims"] = raises_unknown(lambda: guard.issued_claims(fresh))
site["decision-lost"] = raises_unknown(lambda: guard.decision_lost(fresh))
claims_dir.unlink()
claims_dir.mkdir(mode=0o700)
with unreadable_like_314(claims_dir, stat=False, listing=True):
    site["orphan-claim-listing"] = raises_unknown(
        lambda: guard.claim_withheld_decision(fresh, dict(NO)))
site["orphan-claim-listing"] = (site["orphan-claim-listing"] and not any(claims_dir.iterdir())
                                and json.loads(fresh.read_text())["status"] == "awaiting_owner")
claimed_path, _claimed_rid = new_review("task-site-ledger")
ledger_claim = guard.claim_withheld_decision(claimed_path, dict(NO))["decision_claim_id"]
with unreadable_like_314(claimed_path.parent / "claims"):
    site["claim-ledger"] = raises_unknown(
        lambda: guard.withheld_claim_publishable(claimed_path, ledger_claim))
check(all(site.values()), f"every converted existence check reads unknown as unknown: "
                          f"{sorted(k for k, ok in site.items() if not ok)} did not")

# 25u. A malformed decision namespace (each kind below) is unknown: through the production paths
# nothing posts and the record is never marked published.
def break_namespace(kind, path):
    directory = path.parent
    if kind == "archive-is-file":
        (directory / "archive").write_text("not a directory")
    elif kind == "archive-record-dangling":
        (directory / "archive").mkdir(mode=0o700)
        (directory / "archive" / path.name).symlink_to("missing-target.json")
    elif kind == "archive-dir-dangling":
        (directory / "archive").symlink_to("missing-archive-dir")
    elif kind == "record-is-directory":
        path.unlink()
        path.mkdir()
    elif kind in ("claims-linked", "issued-linked"):
        folder = directory / kind.split("-")[0]
        folder.rename(directory / f"{folder.name}-real")
        folder.symlink_to(f"{folder.name}-real")
    elif kind == "record-linked":
        backing = path.with_name("backing.json")
        path.rename(backing)
        path.symlink_to(backing.name)
    elif kind == "issued-is-file":
        shutil.rmtree(directory / "issued")
        (directory / "issued").write_text("not a directory")


for kind in ("archive-is-file", "archive-record-dangling", "archive-dir-dangling",
             "record-is-directory", "issued-is-file", "claims-linked", "issued-linked",
             "record-linked"):
    path, rid, claimed, room_req = release(f"ns-{kind}", lambda: {"event_id": "$ns-leak"})
    break_namespace(kind, path)
    before, raised = len(shared_posts()), []
    for step in (lambda: bridge._publish_review(path, claimed),
                 bridge._retry_withheld_reviews,
                 lambda: bridge._handle_review_decision(decision("No", rid, f"ns-{kind}"))):
        bridge._req = room_req
        try:
            step()
        except OSError as exc:
            raised.append(type(exc).__name__)
        finally:
            bridge._req = fake_req
    status = json.loads(path.read_text()).get("status") if path.is_file() else None
    leaked_host_path = str(root) in control_body(f"ns-{kind}")
    archived_ok = not (path.parent / "archive").is_dir() or not any(
        json.loads(p.read_text()).get("status") == "published"
        for p in (path.parent / "archive").glob("*.json") if p.is_file())
    check(len(shared_posts()) == before and status != "published" and archived_ok
          and "reply again" in control_body(f"ns-{kind}") and not leaked_host_path,
          f"{kind}: nothing posts, nothing is marked published, the reply is answered "
          f"({len(shared_posts()) - before} POST(s), status {status}, gate raised {raised})")

# A conflicts/ of the wrong kind cannot be read at the match: the reply is answered, never applied.
bridge._STATE = root / "ns-conflicts-is-file"
path, rid = new_review("task-ns-conflicts-is-file")
(path.parent / "conflicts").write_text("not a directory")
before = len(shared_posts())
handled = bridge._handle_review_decision(decision("No", rid, "ns-conflicts-is-file"))
check(handled is True and "please reply again" in control_body("ns-conflicts-is-file")
      and json.loads(path.read_text())["status"] == "awaiting_owner" and len(shared_posts()) == before,
      f"conflicts/ as a file: the reply is answered and nothing changes ({handled})")

# The reply's readability scan reads each record under the ledger lock, so no move races it.
bridge._STATE = root / "scan-under-lock"
path, rid = new_review("task-scan-under-lock")
held, under_lock = [], []
real_lock, real_scan = guard.withheld_decision_lock, guard.namespace_problems


@contextlib.contextmanager
def recording_lock(directory):
    with real_lock(directory):
        held.append(True)
        try:
            yield
        finally:
            held.pop()


def recording_scan(directory):
    under_lock.append(bool(held))
    return real_scan(directory)


guard.withheld_decision_lock, guard.namespace_problems = recording_lock, recording_scan
try:
    bridge._handle_review_decision(decision("Yes", rid, "scan-under-lock"))
finally:
    guard.withheld_decision_lock, guard.namespace_problems = real_lock, real_scan
check(under_lock == [True], f"the reply's preflight runs once, under the ledger lock: {under_lock}")

# A linked state root with no review namespace at all: a plain owner Yes/No is ordinary chat.
# The namespace itself as a link stays unknown: the reply is answered, never ordinary work.
real_state = root / "real-state"
real_state.mkdir()
linked_state = root / "linked-state"
linked_state.symlink_to(real_state.name)
bridge._STATE = linked_state
plain = bridge._handle_review_decision(decision("Yes", "", "linked-root-plain") | {"task": "Yes"})
check(plain is False and not control_body("linked-root-plain"),
      f"a linked state root with no reviews leaves a plain Yes as ordinary chat ({plain})")
bridge._STATE = root / "linked-namespace"
path, rid = new_review("task-linked-namespace")
moved = path.parent.with_name("withheld-team-results-real")
path.parent.rename(moved)
moved.with_name(guard.WITHHELD_RESULT_DIR).symlink_to(moved.name)
answered = bridge._handle_review_decision(decision("Yes", "", "linked-namespace") | {"task": "Yes"})
check(answered is True and "please reply again" in control_body("linked-namespace"),
      f"a linked review namespace stays unknown: the plain reply is answered ({answered})")

# A linked archive/superseded/ or conflicts/ never takes a private record out of the namespace:
# the real retry beat leaves it in place and writes nothing outside; the reply is answered.
def outside_files(outside):
    return sorted(p.name for p in outside.rglob("*") if p.is_file())


for label in ("superseded-linked", "conflicts-linked"):
    bridge._STATE = root / f"write-{label}"
    directory = bridge._STATE / "withheld-team-results"
    outside = root / f"outside-{label}"
    outside.mkdir()
    if label == "superseded-linked":
        (directory / "archive").mkdir(parents=True)
        (directory / "archive" / "wr_0000000000000000.json").write_text(
            json.dumps({**base_record, **YES, "dm_event_id": f"${label}"}))
        live = directory / "wr_0000000000000000.json"
        live.write_text(json.dumps({**base_record, **NO, "dm_event_id": f"${label}"}))
        (directory / "archive" / "superseded").symlink_to(outside)
    else:
        live, _rid = new_review(f"task-{label}")
        record = json.loads(live.read_text())
        live.write_text(json.dumps({**record, "withheld_body": "ANOTHER-BODY"}))
        (directory / "conflicts").symlink_to(outside)
    before_bytes, before = live.read_bytes(), len(shared_posts())
    for _beat in range(2):
        bridge._retry_withheld_reviews()
    rid = json.loads(before_bytes)["review_id"]
    handled = bridge._handle_review_decision(decision("No", rid, label))
    check(live.is_file() and live.read_bytes() == before_bytes and not outside_files(outside)
          and len(shared_posts()) == before and handled is True
          and "please reply again" in control_body(label),
          f"{label}: the record stays, nothing is written outside ({outside_files(outside)}), "
          f"the reply is answered ({handled})")

# archive/superseded/ swapped for a link right after it is verified: the move goes through the
# verified directory fds, so the record stays and nothing lands outside.
bridge._STATE = root / "write-superseded-swapped"
directory = bridge._STATE / "withheld-team-results"
outside = root / "outside-superseded-swapped"
outside.mkdir()
(directory / "archive" / "superseded").mkdir(parents=True)
(directory / "archive" / "wr_0000000000000000.json").write_text(
    json.dumps({**base_record, **YES, "dm_event_id": "$superseded-swapped"}))
live = directory / "wr_0000000000000000.json"
live.write_text(json.dumps({**base_record, **NO, "dm_event_id": "$superseded-swapped"}))
before_bytes, real_ensure = live.read_bytes(), guard._ensure_dir


def ensure_then_swap(folder):
    verified = real_ensure(folder)
    if pathlib.Path(folder).name == "superseded" and not pathlib.Path(folder).is_symlink():
        pathlib.Path(folder).rename(pathlib.Path(folder).with_name("superseded-moved"))
        pathlib.Path(folder).symlink_to(outside)
    return verified


guard._ensure_dir = ensure_then_swap
try:
    bridge._retry_withheld_reviews()
finally:
    guard._ensure_dir = real_ensure
check(live.is_file() and live.read_bytes() == before_bytes and not outside_files(outside),
      f"superseded/ swapped after its check: the record stays, nothing outside ({outside_files(outside)})")

# The suppression and quarantine journals refuse a linked suppressed-team-results/: nothing is
# written or changed outside, and each reports it could not save.
for label in ("suppressed", "quarantined"):
    state, outside = root / f"journal-{label}", root / f"journal-{label}-outside"
    state.mkdir()
    outside.mkdir(mode=0o755)
    (state / guard.SUPPRESSED_RESULT_DIR).symlink_to(outside)
    if label == "suppressed":
        deliver = guard.TeamResultVerdict(guard.VERDICT_DELIVER, "[no-send]", None)
        saved = guard.journal_suppressed_result(deliver, "[no-send]", state, "task-j", now=1000)
        refused = saved.kind == guard.VERDICT_SUPPRESS and saved.body == guard.TEAM_SUPPRESS_RESULT
    else:
        refused = guard.journal_quarantined_attachment("[file: /tmp/x]\nB", state, "task-j", now=1000) is False
    check(refused and not any(outside.iterdir()) and outside.stat().st_mode & 0o777 == 0o755,
          f"journal-{label}: a linked suppression namespace is refused, nothing written outside")

# The move syncs the folder it linked into by fd; with that folder swapped right after the link, the
# rebind is caught: the copy is removed, the record stays, and the replacement is never synced.
bridge._STATE = root / "move-sync-by-fd"
path, rid = new_review("task-move-sync-by-fd")
guard.claim_withheld_decision(path, dict(YES))
archive_dir = path.parent / "archive"
archive_dir.mkdir(mode=0o700)
original_inode, events = os.stat(archive_dir).st_ino, []
real_link, real_unlink, real_fd_sync = os.link, os.unlink, guard._fsync_fd


def link_then_swap(*args, **kwargs):
    real_link(*args, **kwargs)
    if archive_dir.exists() and os.stat(archive_dir).st_ino == original_inode:
        archive_dir.rename(archive_dir.with_name("archive-linked-into"))
        archive_dir.mkdir(mode=0o700)


def spy_unlink(*args, **kwargs):
    events.append(("unlink", None))
    return real_unlink(*args, **kwargs)


def spy_fd_sync(fd):
    events.append(("sync", os.fstat(fd).st_ino))
    return real_fd_sync(fd)


os.link, os.unlink, guard._fsync_fd = link_then_swap, spy_unlink, spy_fd_sync
try:
    moved = guard.archive_record(path)
finally:
    os.link, os.unlink, guard._fsync_fd = real_link, real_unlink, real_fd_sync
replacement_inode = os.stat(archive_dir).st_ino
linked_into = archive_dir.with_name("archive-linked-into")
check(moved is False and path.is_file() and not any(linked_into.iterdir())
      and ("sync", original_inode) in events and ("sync", replacement_inode) not in events,
      f"a folder rebound after the link: the record stays, nothing left behind: {events}")

# The move links the verified entry itself, never what a link swapped in for it points to.
bridge._STATE = root / "move-no-follow"
namespace = bridge._STATE / guard.WITHHELD_RESULT_DIR
namespace.mkdir(parents=True)
(bridge._STATE / "outside-record.json").write_text('{"withheld_body": "OUTSIDE"}')
source = namespace / "wr_3.json"
source.write_text("{}")
real_present = guard.present


def checked_then_linked(candidate, kind=None):
    answer = real_present(candidate, kind)
    if pathlib.Path(candidate) == source and not source.is_symlink():
        source.unlink()
        source.symlink_to(bridge._STATE / "outside-record.json")
    return answer


guard.present = checked_then_linked
try:
    guard._durable_move(source, namespace / "archive" / "wr_3.json")
except OSError:
    pass
finally:
    guard.present = real_present
moved_copy = namespace / "archive" / "wr_3.json"
check(not (moved_copy.exists() and not moved_copy.is_symlink()
           and os.stat(moved_copy).st_ino == os.stat(bridge._STATE / "outside-record.json").st_ino),
      "a source swapped for a link is never followed into the namespace")

# A FIFO where a record belongs (archived or live) is unknown and never blocks the read, even with
# the walk's own regular-file check out of the way: the open itself refuses it.
class Hung(BaseException):
    pass


def bounded(call, seconds=5):
    def on_alarm(_signum, _frame):
        raise Hung()
    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(seconds)
    try:
        call()
        return "read"
    except OSError:
        return "unknown"
    except Hung:
        return "hung"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


bridge._STATE = root / "fifo-records"
path, rid = new_review("task-fifo-records")
(path.parent / "archive").mkdir(mode=0o700)
fifos = {"archived": path.parent / "archive" / "wr_ffff000000000000.json",
         "live": path.parent / "wr_ffff000000000001.json"}
for fifo in fifos.values():
    os.mkfifo(fifo)
outcomes, real_present = {}, guard.present
for label, fifo in fifos.items():
    outcomes[label] = bounded(lambda: guard.read_json_strict(fifo))
    guard.present = lambda _p, _k=None: True
    try:
        outcomes[f"{label}-without-the-walk-check"] = bounded(lambda: guard.read_json_strict(fifo))
    finally:
        guard.present = real_present
reported = set()
outcomes["preflight"] = bounded(lambda: reported.update(p for p, _r in guard.namespace_problems(path.parent)))
outcomes["preflight"] = "unknown" if outcomes["preflight"] == "read" else outcomes["preflight"]
check(set(outcomes.values()) == {"unknown"} and set(fifos.values()) <= reported,
      f"a FIFO record is unknown and never hangs a read: {outcomes}")
for fifo in fifos.values():
    fifo.unlink()

# Kewei's schedule through the real retry beat: the already-open archive/superseded/ is renamed
# outside and its name replaced by a link just before the fd-relative link. Nothing escapes.
bridge._STATE = root / "write-superseded-rebound"
directory = bridge._STATE / "withheld-team-results"
outside = root / "outside-superseded-rebound"
outside.mkdir()
(directory / "archive" / "superseded").mkdir(parents=True)
(directory / "archive" / "wr_0000000000000000.json").write_text(
    json.dumps({**base_record, **YES, "dm_event_id": "$superseded-rebound"}))
live = directory / "wr_0000000000000000.json"
live.write_text(json.dumps({**base_record, **NO, "dm_event_id": "$superseded-rebound"}))
before_bytes, before, real_link, real_unlink = live.read_bytes(), len(shared_posts()), os.link, os.unlink
source_unlinks = []


def spy_source_unlink(name, *args, **kwargs):
    if str(name).endswith(live.name):
        source_unlinks.append(name)
    return real_unlink(name, *args, **kwargs)


def rebind_then_link(*args, **kwargs):
    superseded = directory / "archive" / "superseded"
    if kwargs.get("dst_dir_fd") is not None and not superseded.is_symlink():
        superseded.rename(outside / "superseded-moved")
        superseded.symlink_to(outside / "elsewhere")
    return real_link(*args, **kwargs)


os.link, os.unlink = rebind_then_link, spy_source_unlink
try:
    for _beat in range(2):
        bridge._retry_withheld_reviews()
finally:
    os.link, os.unlink = real_link, real_unlink
check(live.is_file() and live.read_bytes() == before_bytes and not outside_files(outside)
      and len(shared_posts()) == before and not source_unlinks,
      f"superseded/ rebound after it was opened: the record stays, nothing outside "
      f"({outside_files(outside)})")

# The target folder rebound after the check but before the source is unlinked: the record is put
# back through the held fds and nothing is left outside.
bridge._STATE = root / "write-superseded-late-rebind"
directory = bridge._STATE / "withheld-team-results"
outside = root / "outside-superseded-late-rebind"
outside.mkdir()
(directory / "archive" / "superseded").mkdir(parents=True)
(directory / "archive" / "wr_0000000000000000.json").write_text(
    json.dumps({**base_record, **YES, "dm_event_id": "$late-rebind"}))
live = directory / "wr_0000000000000000.json"
live.write_text(json.dumps({**base_record, **NO, "dm_event_id": "$late-rebind"}))
before_bytes, real_unlink = live.read_bytes(), os.unlink


def rebind_then_unlink(name, *args, **kwargs):
    superseded = directory / "archive" / "superseded"
    if kwargs.get("dir_fd") is not None and str(name) == live.name and not superseded.is_symlink():
        superseded.rename(outside / "superseded-moved")
        superseded.symlink_to(outside / "elsewhere")
    return real_unlink(name, *args, **kwargs)


os.unlink = rebind_then_unlink
try:
    bridge._retry_withheld_reviews()
finally:
    os.unlink = real_unlink
check(live.is_file() and live.read_bytes() == before_bytes and not outside_files(outside),
      f"superseded/ rebound just before the unlink: the record is put back, nothing outside "
      f"({outside_files(outside)})")

# Each rollback is durable, in order: through the production move and the _fsync_fd seam, a rebind
# before the unlink undoes the copy and syncs it away; one after it puts the record back first.
def move_ops(swap_on):
    namespace = root / f"rollback-{swap_on}" / guard.WITHHELD_RESULT_DIR
    (namespace / "archive" / "superseded").mkdir(parents=True)
    source, target = namespace / "wr_5.json", namespace / "archive" / "superseded" / "wr_5.json"
    source.write_text('{"withheld_body": "SECRET-BODY"}')
    outside = namespace.parent / "outside"
    outside.mkdir()
    held = {"T": os.stat(target.parent).st_ino, "S": os.stat(namespace).st_ino}
    names = {ino: key for key, ino in held.items()}
    ops, real_link, real_unlink, real_fd_sync = [], os.link, os.unlink, guard._fsync_fd

    def rebind():
        if not target.parent.is_symlink():
            target.parent.rename(outside / "superseded-moved")
            target.parent.symlink_to(outside / "elsewhere")

    def link(*args, **kwargs):
        if swap_on == "link":
            rebind()
        ops.append(("link", pathlib.Path(args[1]).name))
        return real_link(*args, **kwargs)

    def unlink(name, *args, **kwargs):
        if swap_on == "unlink" and str(name) == source.name and kwargs.get("dir_fd") is not None:
            rebind()
        ops.append(("unlink", pathlib.Path(name).name))
        return real_unlink(name, *args, **kwargs)

    def fd_sync(fd):
        ops.append(("sync", names.get(os.fstat(fd).st_ino, "?")))
        return real_fd_sync(fd)

    os.link, os.unlink, guard._fsync_fd = link, unlink, fd_sync
    try:
        raises_unknown(lambda: guard._durable_move(source, target))
    finally:
        os.link, os.unlink, guard._fsync_fd = real_link, real_unlink, real_fd_sync
    left_outside = sorted(p.name for p in outside.rglob("*") if p.is_file())
    return ops, source.is_file() and "SECRET-BODY" in source.read_text(), left_outside


first, kept_first, outside_first = move_ops("link")
second, kept_second, outside_second = move_ops("unlink")
check(first == [("link", "wr_5.json"), ("sync", "T"), ("unlink", "wr_5.json"), ("sync", "T")]
      and kept_first and not outside_first,
      f"a rebind before the unlink: undo the copy, then sync it away, record kept: {first}")
check(second == [("link", "wr_5.json"), ("sync", "T"), ("unlink", "wr_5.json"), ("sync", "S"),
                 ("link", "wr_5.json"), ("sync", "S"), ("unlink", "wr_5.json"), ("sync", "T")]
      and kept_second and not outside_second,
      f"a rebind after the unlink: the record is linked back and synced before the copy goes: {second}")

# An ancestor of the target (archive/) renamed out and its name replaced by a link back to it: the
# path still reaches the same inode, so only the walk's link check catches it. Nothing escapes.
bridge._STATE = root / "write-ancestor-rebound"
directory = bridge._STATE / "withheld-team-results"
outside = root / "outside-ancestor-rebound"
outside.mkdir()
(directory / "archive" / "superseded").mkdir(parents=True)
(directory / "archive" / "wr_0000000000000000.json").write_text(
    json.dumps({**base_record, **YES, "dm_event_id": "$ancestor-rebound"}))
live = directory / "wr_0000000000000000.json"
live.write_text(json.dumps({**base_record, **NO, "dm_event_id": "$ancestor-rebound"}))
before_bytes, real_link = live.read_bytes(), os.link


def rebind_ancestor_then_link(*args, **kwargs):
    archive = directory / "archive"
    if kwargs.get("dst_dir_fd") is not None and not archive.is_symlink():
        archive.rename(outside / "archive-moved")
        archive.symlink_to(outside / "archive-moved")
    return real_link(*args, **kwargs)


os.link = rebind_ancestor_then_link
try:
    bridge._retry_withheld_reviews()
finally:
    os.link = real_link
check(live.is_file() and live.read_bytes() == before_bytes and not outside_files(outside / "archive-moved" / "superseded"),
      f"archive/ rebound to a link back to itself: the record stays, nothing moved out "
      f"({outside_files(outside / 'archive-moved' / 'superseded')})")

# Without directory-fd confinement a move is refused, never made by path: the record stays, nothing
# is linked anywhere, and the owner's reply is still answered.
bridge._STATE = root / "no-dir-fds"
directory = bridge._STATE / "withheld-team-results"
outside = root / "outside-no-dir-fds"
outside.mkdir()
(directory / "archive" / "superseded").mkdir(parents=True)
(directory / "archive" / "wr_0000000000000000.json").write_text(
    json.dumps({**base_record, **YES, "dm_event_id": "$no-dir-fds"}))
live = directory / "wr_0000000000000000.json"
live.write_text(json.dumps({**base_record, **NO, "dm_event_id": "$no-dir-fds"}))
before_bytes, real_link, links, saved_flag = live.read_bytes(), os.link, [], os.O_DIRECTORY


def recording_link(*args, **kwargs):
    links.append(args)
    return real_link(*args, **kwargs)


del os.O_DIRECTORY
os.link = recording_link
try:
    for _beat in range(2):
        bridge._retry_withheld_reviews()
    handled = bridge._handle_review_decision(decision("No", "wr_0000000000000000", "no-dir-fds"))
finally:
    os.O_DIRECTORY, os.link = saved_flag, real_link
check(live.is_file() and live.read_bytes() == before_bytes and not links
      and not outside_files(outside) and handled is True and control_body("no-dir-fds"),
      f"no directory fds: the move is refused, the record stays, nothing linked ({len(links)}), "
      f"the reply is answered ({handled})")

# A Windows junction is not S_ISLNK; the walk refuses its reparse-point attribute at every step.
junction_dir = root / "junction" / guard.WITHHELD_RESULT_DIR / "conflicts"
junction_dir.mkdir(parents=True)
real_lstat = os.lstat


class JunctionStatus:
    def __init__(self, status):
        self.st_mode, self.st_dev, self.st_ino = status.st_mode, status.st_dev, status.st_ino
        self.st_file_attributes = 0x400


def lstat_marking_junction(candidate, *args, **kwargs):
    status = real_lstat(candidate, *args, **kwargs)
    return JunctionStatus(status) if pathlib.Path(candidate) == junction_dir else status


os.lstat = lstat_marking_junction
try:
    junction_refused = raises_unknown(lambda: guard.present(junction_dir / "wr_1.json"))
    junction_listing = raises_unknown(lambda: guard.entries(junction_dir, "*"))
finally:
    os.lstat = real_lstat
check(junction_refused and junction_listing, "a junction on the way is unknown, like a link")

# A final .issued reservation that is a link makes the reply unknown, never "no matching review".
bridge._STATE = root / "reservation-leaf-linked"
path, rid = new_review("task-reservation-leaf-linked")
reservation = path.parent / "issued" / f"{path.stem}.issued"
copy = path.parent / "reservation-copy"
copy.write_text(reservation.read_text())
reservation.unlink()
reservation.symlink_to(copy.name)
before = len(shared_posts())
handled = bridge._handle_review_decision(decision("No", rid, "reservation-leaf-linked"))
check(handled is True and "please reply again" in control_body("reservation-leaf-linked")
      and json.loads(path.read_text())["status"] == "awaiting_owner" and len(shared_posts()) == before,
      f"a linked .issued reservation: the reply is consumed and answered ({handled})")

# A claims/ or issued/ entry that is a directory or a FIFO is unknown too: the reply is answered,
# the guard's preflight reports it, and nothing posts.
for folder in ("claims", "issued"):
    for kind in ("directory", "fifo"):
        label = f"odd-{folder}-{kind}"
        bridge._STATE = root / label
        path, rid = new_review(f"task-{label}")
        entry = path.parent / folder / f"{path.stem}.odd"
        entry.parent.mkdir(mode=0o700, exist_ok=True)
        entry.mkdir() if kind == "directory" else os.mkfifo(entry)
        before = len(shared_posts())
        handled = bridge._handle_review_decision(decision("No", rid, label))
        reported = [p for p, _reason in guard.namespace_problems(path.parent)]
        check(handled is True and "please reply again" in control_body(label) and entry in reported
              and json.loads(path.read_text())["status"] == "awaiting_owner"
              and len(shared_posts()) == before,
              f"{label}: the reply is answered and the preflight reports it ({handled}, {reported})")

# 25v. The absence rule itself: only a genuinely missing entry below a real, readable directory
# is absent; a symlink, a wrong kind, a non-directory parent or a dangling link raises.
scratch = root / "absence"
scratch.mkdir()
(scratch / "file").write_text("x")
(scratch / "dir").mkdir()
(scratch / "link-to-file").symlink_to("file")
(scratch / "dangling").symlink_to("missing")
(scratch / "dangling-dir").symlink_to("missing-dir")
(scratch / "link-dir").symlink_to("dir")
(scratch / "dir" / "inner").write_text("{}")
rule = {
    "missing": guard.present(scratch / "missing") is False,
    "missing-chain": guard.present(scratch / "a" / "b" / "c.json") is False,
    "file": guard.present(scratch / "file", stat.S_IFREG) is True,
    "dir": guard.present(scratch / "dir", stat.S_IFDIR) is True,
    "file-as-dir": raises_unknown(lambda: guard.present(scratch / "file", stat.S_IFDIR)),
    "dir-as-file": raises_unknown(lambda: guard.present(scratch / "dir", stat.S_IFREG)),
    "symlink": raises_unknown(lambda: guard.present(scratch / "link-to-file")),
    "dangling": raises_unknown(lambda: guard.present(scratch / "dangling")),
    "below-a-file": raises_unknown(lambda: guard.present(scratch / "file" / "x.json")),
    "below-dangling-dir": raises_unknown(lambda: guard.present(scratch / "dangling-dir" / "x.json")),
    "below-linked-dir": raises_unknown(lambda: guard.present(scratch / "link-dir" / "x.json")),
    "namespace-absent-below-linked-root": guard.present(
        scratch / "link-dir" / guard.WITHHELD_RESULT_DIR / "x.json") is False,
    "read-below-linked-dir": raises_unknown(lambda: guard.read_strict(scratch / "link-dir" / "inner")),
    "entries-missing": guard.entries(scratch / "missing", "*") == [],
    "entries-file": raises_unknown(lambda: guard.entries(scratch / "file", "*")),
    "entries-dangling-dir": raises_unknown(lambda: guard.entries(scratch / "dangling-dir", "*")),
    "entries-listed": guard.entries(scratch, "f*") == [scratch / "file"],
}
if os.geteuid() != 0:
    (scratch / "closed").mkdir()
    os.chmod(scratch / "closed", 0o300)
    try:
        rule["below-unreadable"] = raises_unknown(lambda: guard.present(scratch / "closed" / "x"))
    finally:
        os.chmod(scratch / "closed", 0o700)


def swapped_after_check(target):
    """The leaf becomes a link between the walk and the open: the open itself refuses it."""
    real_present = guard.present

    def checked_then_swapped(path, kind=None):
        answer = real_present(path, kind)
        if pathlib.Path(path) == target:
            target.unlink()
            target.symlink_to("file")
        return answer
    guard.present = checked_then_swapped
    try:
        return raises_unknown(lambda: guard.read_strict(target))
    finally:
        guard.present = real_present


(scratch / "swap").write_text("{}")
rule["swap-before-open"] = swapped_after_check(scratch / "swap")


def directory_swapped_after_check(which):
    """A directory on the way (or the namespace root) becomes a link between the walk and the open."""
    base = scratch / f"swap-{which}"
    namespace = base / guard.WITHHELD_RESULT_DIR
    (namespace / "archive").mkdir(parents=True)
    (namespace / "archive" / "wr_1.json").write_text("{}")
    (base / "decoy" / "archive").mkdir(parents=True)
    (base / "decoy" / "archive" / "wr_1.json").write_text("{}")
    target, real_present = namespace / "archive" / "wr_1.json", guard.present
    swapped = namespace / "archive" if which == "step" else namespace
    decoy = base / "decoy" / "archive" if which == "step" else base / "decoy"

    def checked_then_swapped(path, kind=None):
        answer = real_present(path, kind)
        if pathlib.Path(path) == target:
            swapped.rename(swapped.with_name(swapped.name + "-moved"))
            swapped.symlink_to(decoy)
        return answer
    guard.present = checked_then_swapped
    try:
        return raises_unknown(lambda: guard.read_strict(target))
    finally:
        guard.present = real_present


rule["directory-swap-before-open"] = directory_swapped_after_check("step")
rule["root-swap-before-open"] = directory_swapped_after_check("root")

def move_refuses_linked_source():
    namespace = scratch / "moves" / guard.WITHHELD_RESULT_DIR
    namespace.mkdir(parents=True)
    (scratch / "moves" / "outside.json").write_text("{}")
    (namespace / "wr_2.json").symlink_to(scratch / "moves" / "outside.json")
    refused = raises_unknown(lambda: guard._durable_move(namespace / "wr_2.json",
                                                         namespace / "archive" / "wr_2.json"))
    return refused and (namespace / "wr_2.json").is_symlink() and not (namespace / "archive").exists()


def first_review_under_linked_namespace():
    state, outside = scratch / "first-review", scratch / "first-review-outside"
    state.mkdir()
    outside.mkdir(mode=0o755)
    (state / guard.WITHHELD_RESULT_DIR).symlink_to(outside)
    verdict = guard.materialize_withheld_verdict(
        leak, "SECRET-BODY", state, "task-first-review", {"source": "ag2space",
        "channel_id": SHARED, "user_id": "@team:example"}, "@agent:example", now=1000)
    return (verdict.body == guard.TEAM_LEAK_RESULT_UNSAVED and not any(outside.iterdir())
            and outside.stat().st_mode & 0o777 == 0o755)


rule["move-refuses-linked-source"] = move_refuses_linked_source()
rule["first-review-under-linked-namespace"] = first_review_under_linked_namespace()
bridge._STATE = scratch
review_link = scratch / "linked-review.json"
review_link.symlink_to("dir/inner")
rule["bridge-review-reader"] = bridge._read_review_json(review_link) is None
bridge._STATE = root / "state"
check(all(rule.values()), f"the absence rule: {sorted(k for k, ok in rule.items() if not ok)} broke")

# 25t. A review record that reads but is not a record (archived, live, or frozen into
# conflicts/) is unknown: the owner's reply is answered "please reply again", never ordinary work.
MALFORMED = ("{not json", "[]", '"str"', "")
for kind, content in enumerate(MALFORMED):
    for where in ("archive", "live", "conflicts"):
        label = f"malformed-{where}-{kind}"
        bridge._STATE = root / label
        path, rid = new_review(f"task-{label}")
        if where == "archive":
            bridge._handle_review_decision(decision("Yes", rid, f"{label}-decide"))
            bridge._retry_withheld_reviews()
            target = path.parent / "archive" / path.name
        else:
            target = path
        target.write_text(content)
        if where == "conflicts":
            bridge._retry_withheld_reviews()  # the beat freezes the live file into conflicts/
        frozen = sorted((path.parent / "conflicts").glob("*.json")) if where == "conflicts" else []
        before = len(shared_posts())
        try:
            raised, handled = None, bridge._handle_review_decision(decision("No", rid, label))
        except Exception as exc:  # noqa: BLE001
            raised, handled = exc, None
        named = path.stem in control_body(label)
        check(raised is None and handled is True and "please reply again" in control_body(label)
              and named
              and len(shared_posts()) == before and (where != "conflicts" or len(frozen) == 1),
              f"{label}: a record that is not a record makes the reply unknown, answered "
              f"(handled={handled}, raised={raised!r}, frozen={len(frozen)})")

# 26. A reconcile that keeps failing stops re-editing the card after a bounded number of
# tries, and says so once.
bridge._STATE = root / "states" / "reconcile-gives-up"
directory = bridge._STATE / "withheld-team-results"
(directory / "archive").mkdir(parents=True)
(directory / "archive" / "superseded").write_text("not a directory")
(directory / "archive" / "wr_0000000000000000.json").write_text(
    json.dumps({**base_record, **YES, "dm_event_id": "$gives-up"}))
(directory / "wr_0000000000000000.json").write_text(
    json.dumps({**base_record, **NO, "dm_event_id": "$gives-up"}))
mark, logs_before = len(calls), len(logs)
for _beat in range(5):
    bridge._retry_withheld_reviews()
edits = [p for _m, u, p in calls[mark:] if u == "/v1/room" and p.get("event_id") == "$gives-up"]
gave_up = [line for line in logs[logs_before:] if "no further card edits" in line]
check(len(edits) == bridge._RECONCILE_GIVE_UP_AFTER and len(gave_up) == 1,
      f"a failing reconcile is bounded: {len(edits)} card edits, {len(gave_up)} give-up line")
bridge._STATE = root / "state"

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
