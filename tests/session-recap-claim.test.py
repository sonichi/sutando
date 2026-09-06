#!/usr/bin/env python3
"""Tests for session-recap recap_claim.py (single-flight boot recap) — hermetic.

The boot recap worker runs in the background for ~1-2 min and only stamps
state/last-recap-session.txt at the END, so the stamp alone cannot stop a
mid-session /schedule-crons re-run from double-launching a second worker
inside that window (#2454 review, P1). recap_claim.py closes the window with
an O_EXCL-atomic claim file. Pinned behaviors:
  1. concurrency: N simultaneous claims for the same session -> exactly ONE
     exit-0 winner (the double-launch proof)
  2. a fresh claim blocks later claimers (in-flight skip)
  3. release --stamp writes the stamp atomically and drops the claim;
     a later claim for the same session skips as already-recapped
  4. release without --stamp (failure path) drops the claim only -> a later
     claim for the session wins again (retry works)
  5. a stale claim (dead worker) is reclaimed by the next claimer
  6. a corrupt claim file counts as stale (age falls back to epoch 0)
  7. CLI surface: main() wires claim/release + --state-dir + exit codes

All state goes through --state-dir tempdirs (the supported test hook — no
env vars); calls are in-process (importlib + argv patch, the repo's
coverage-visible pattern).

Run: python3 tests/session-recap-claim.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "session-recap" / "scripts" / "recap_claim.py"

spec = importlib.util.spec_from_file_location("recap_claim", SCRIPT)
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f" — {detail}"))
    if not cond:
        failures.append(name)


def cli(*argv: str) -> tuple[int, str]:
    out, old = io.StringIO(), sys.argv
    sys.argv = ["recap_claim.py", *argv]
    try:
        with redirect_stdout(out):
            code = rc.main()
    finally:
        sys.argv = old
    return code, out.getvalue()


SESSION = "aaaa-bbbb-cccc"
STALE_S = 15 * 60

# 1. concurrency: 8 simultaneous claimers, exactly one winner — repeated 20
# rounds. One round passes on timing luck (the original create-then-write
# implementation went green locally, then produced 5-of-8 winners on the
# 2-core CI runner when a claimer read a nascent claim as corrupt→stale and
# unlinked it); the repeat makes the test actually sensitive to the race.
bad_rounds = []
for rnd in range(20):
    dr = Path(tempfile.mkdtemp(prefix=f"claim-race-{rnd}-"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        with redirect_stdout(io.StringIO()):
            codes = list(pool.map(
                lambda _: rc.claim(dr, SESSION, STALE_S), range(8)))
    claim_ok = ((dr / rc.CLAIM_NAME).exists() and
                json.loads((dr / rc.CLAIM_NAME).read_text())["session"]
                == SESSION)
    leftover_tmp = list(dr.glob(".recap-claim-*"))
    if codes.count(0) != 1 or codes.count(1) != 7 or not claim_ok \
            or leftover_tmp:
        bad_rounds.append((rnd, codes, claim_ok, leftover_tmp))
    d1 = dr  # last round's dir feeds the lifecycle checks below
check("race: exactly one of 8 concurrent claims wins, every round (x20), "
      "claim intact, no temp litter",
      not bad_rounds, str(bad_rounds[:3]))

def live_token(d: Path) -> str:
    return json.loads((d / rc.CLAIM_NAME).read_text())["token"]


def token_of(msg: str) -> str:
    return msg.split("token=")[1].strip()


# 2. a fresh claim blocks a later claimer
code, msg = cli("claim", SESSION, "--state-dir", str(d1))
check("fresh claim blocks re-claim", code == 1 and "in-flight" in msg, msg)

# 3. release --stamp with the live token: stamp written, claim dropped,
# session now skipped
tok = live_token(d1)
code, msg = cli("release", SESSION, "--stamp", "--token", tok,
                "--state-dir", str(d1))
check("release --stamp exits 0", code == 0 and "stamped" in msg, msg)
check("release --stamp writes the stamp",
      (d1 / rc.STAMP_NAME).read_text().strip() == SESSION)
check("release --stamp drops the claim", not (d1 / rc.CLAIM_NAME).exists())
code, msg = cli("claim", SESSION, "--state-dir", str(d1))
check("stamped session skips as already-recapped",
      code == 1 and "already-recapped" in msg, msg)

# ...but a NEW session claims fine over an old stamp
code, msg = cli("claim", "dddd-next", "--state-dir", str(d1))
check("new session claims over an old stamp",
      code == 0 and "token=" in msg, msg)
tok_next = token_of(msg)

# 4. failure-path release (no --stamp): claim dropped, no stamp, retry wins
code, msg = cli("release", "dddd-next", "--token", tok_next,
                "--state-dir", str(d1))
check("bare release exits 0 without stamping",
      code == 0 and "stamped" not in msg
      and (d1 / rc.STAMP_NAME).read_text().strip() == SESSION, msg)
code, msg = cli("claim", "dddd-next", "--state-dir", str(d1))
check("retry after failure-path release wins", code == 0)
cli("release", "dddd-next", "--token", token_of(msg), "--state-dir", str(d1))

# 4b. ownership gate — the stale-A / reclaimed-B / late-A-release race
# (#2454 review on 567e8014): a slow worker whose claim was legitimately
# reclaimed must not release the reclaimer's live reservation, stamp over
# its run, or open the door for a third worker.
d5 = Path(tempfile.mkdtemp(prefix="claim-ownership-"))
_, msg = cli("claim", "session-a", "--state-dir", str(d5))
tok_a = token_of(msg)
code, msg = cli("claim", "session-b", "--stale-minutes", "0",
                "--state-dir", str(d5))
check("B reclaims A's (stale) claim", code == 0, msg)
tok_b = token_of(msg)
code, msg = cli("release", "session-a", "--stamp", "--token", tok_a,
                "--state-dir", str(d5))
check("late A release refused (claim is B's now)",
      code == 1 and "not ours" in msg, msg)
check("B's live claim survived A's late release",
      live_token(d5) == tok_b)
check("A's late --stamp did not land",
      not (d5 / rc.STAMP_NAME).exists())
code, msg = cli("claim", "session-c", "--state-dir", str(d5))
check("C cannot claim while B's reservation is live",
      code == 1 and "in-flight" in msg, msg)
code, msg = cli("release", "session-b", "--stamp", "--token", tok_b,
                "--state-dir", str(d5))
check("B's own release --stamp still works", code == 0
      and (d5 / rc.STAMP_NAME).read_text().strip() == "session-b", msg)

# 4a. lease renewal: a live worker refreshes its age at bounded milestones;
# a worker that wakes after stale reclaim loses the guard and must perform
# neither durable recap write nor private-room post.
d12 = Path(tempfile.mkdtemp(prefix="claim-renew-"))
_, msg = cli("claim", "lease-a", "--state-dir", str(d12))
tok_lease_a = token_of(msg)
lease_payload = json.loads((d12 / rc.CLAIM_NAME).read_text())
lease_payload["ts"] = time.time() - STALE_S - 60
(d12 / rc.CLAIM_NAME).write_text(json.dumps(lease_payload))
code, msg = cli("renew", "lease-a", "--token", tok_lease_a,
                "--state-dir", str(d12))
check("live worker renews an aged lease it still owns",
      code == 0 and "renewed" in msg, msg)
check("renew atomically refreshes the claim timestamp",
      time.time() - json.loads(
          (d12 / rc.CLAIM_NAME).read_text())["ts"] < 5)
code, msg = cli("claim", "lease-b", "--state-dir", str(d12))
check("refreshed lease blocks a second worker",
      code == 1 and "in-flight" in msg, msg)

# Age A again, let B legitimately reclaim it, then model the worker contract:
# BOTH side effects are conditional on a successful immediate renew.
lease_payload = json.loads((d12 / rc.CLAIM_NAME).read_text())
lease_payload["ts"] = time.time() - STALE_S - 60
(d12 / rc.CLAIM_NAME).write_text(json.dumps(lease_payload))
code, msg = cli("claim", "lease-b", "--state-dir", str(d12))
check("B reclaims A after the refreshed lease later expires", code == 0, msg)
side_effects = {"file_write": 0, "room_post": 0}
for effect in side_effects:
    guard, guard_msg = cli("renew", "lease-a", "--token", tok_lease_a,
                           "--state-dir", str(d12))
    if guard == 0:
        side_effects[effect] += 1
    check(f"reclaimed A cannot pass the {effect} ownership guard",
          guard == 1 and "not ours" in guard_msg, guard_msg)
check("reclaimed worker performs no recap-write or room-post side effect",
      side_effects == {"file_write": 0, "room_post": 0},
      str(side_effects))

# Renew fails closed when the claim disappeared or the mutation lock cannot
# be acquired. These are not success-path curiosities: either state is
# ambiguous ownership, so the worker must not proceed to an output.
d13 = Path(tempfile.mkdtemp(prefix="claim-renew-missing-"))
code, msg = cli("renew", "lease-a", "--token", tok_lease_a,
                "--state-dir", str(d13))
check("renew with no live claim fails closed",
      code == 1 and "no live claim" in msg, msg)
real_acquire = rc._acquire_ownership_lock
rc._acquire_ownership_lock = lambda *args, **kwargs: None
try:
    code = rc.renew(d12, "lease-b", "irrelevant")
finally:
    rc._acquire_ownership_lock = real_acquire
check("renew with a busy ownership lock fails closed", code == 1)

# The CLI must not admit a token-less renew by accidentally treating it like
# a claim. argparse owns this error path and exits 2.
old_argv = sys.argv
sys.argv = ["recap_claim.py", "renew", "lease-a", "--state-dir", str(d13)]
try:
    with redirect_stdout(io.StringIO()):
        try:
            rc.main()
            missing_token_exit = None
        except SystemExit as exc:
            missing_token_exit = exc.code
finally:
    sys.argv = old_argv
check("renew CLI requires the winning token", missing_token_exit == 2)

# The helper is load-bearing only if the boot worker contract actually invokes
# it around both externally visible side effects. Pin those prompt obligations
# so a later prose rewrite cannot silently leave renew() unused.
schedule_contract = (REPO / "skills" / "schedule-crons" / "SKILL.md").read_text()
recap_contract = (REPO / "skills" / "session-recap" / "SKILL.md").read_text()
for contract_name, contract in (("schedule", schedule_contract),
                                ("recap", recap_contract)):
    check(f"{contract_name} contract renews before recap file write",
          "immediately before writing" in contract)
    check(f"{contract_name} contract renews before private-room post",
          "immediately before" in contract and "private-room post" in contract)
    check(f"{contract_name} contract aborts after ownership loss",
          "stop without writing" in contract)

# 4c. release with no live claim at all: refuse, never stamp
code, msg = cli("release", "session-b", "--stamp", "--token", tok_b,
                "--state-dir", str(d5))
check("release after claim gone refuses without stamping",
      code == 1 and "no live claim" in msg, msg)

# 4b2. release-vs-reap barrier regression (#2454 round 5): A's release is
# PAUSED between its ownership validation and its unlink (the pause point
# is os.replace — the stamp write that sits exactly between them). While
# paused, B attempts a full stale reclaim; on the pre-fix head B reaped
# A's claim + linked fresh, and A's resume then stamped A and deleted B's
# reservation. With release under the ownership lock, B must LOSE while A
# is inside the section, and A must complete untouched.
import threading

d8 = Path(tempfile.mkdtemp(prefix="claim-relrace-"))
_, msg = cli("claim", "session-a", "--state-dir", str(d8))
tok_a8 = token_of(msg)
paused, resume = threading.Event(), threading.Event()
real_replace = rc.os.replace


def pausing_replace(src, dst):
    paused.set()
    assert resume.wait(5), "release pause never resumed"
    return real_replace(src, dst)


rc.os.replace = pausing_replace
rel_out: dict = {}


def run_release():
    # No redirect_stdout here: it swaps sys.stdout PROCESS-wide, so the
    # main thread's concurrent check() lines would be swallowed into the
    # buffer while release is paused. release's own one-line print is
    # harmless on the console.
    rel_out["code"] = rc.release(d8, "session-a", True, tok_a8)


t = threading.Thread(target=run_release)
t.start()
try:
    check("release reached its critical section", paused.wait(5))
    # B races a stale reclaim mid-release (stale-minutes=0 makes A's fresh
    # claim look reclaimable). It must not win while A holds the lock.
    with redirect_stdout(io.StringIO()):
        b_code = rc.claim(d8, "session-b", 0.0)
    check("B cannot reclaim while A's release holds the lock", b_code == 1)
    # Aged-LIVE-lock regression (#2454 round 6): age the lock FILE far past
    # any wall-clock threshold while A still holds the flock. With a TTL'd
    # lock file this let a reclaimer steal the lock from a live-but-slow
    # holder, admit session-c, and A's resume then stamped A and deleted
    # C's reservation. flock has no expiry — a live holder cannot be
    # dispossessed — so both attempts must still lose.
    ancient = time.time() - 3600
    os.utime(d8 / rc.OWNERSHIP_LOCK_NAME, (ancient, ancient))
    with redirect_stdout(io.StringIO()):
        aged_b = rc.claim(d8, "session-b", 0.0)
        aged_c = rc.claim(d8, "session-c", 0.0)
    check("aged lock file cannot be stolen from a live holder",
          aged_b == 1 and aged_c == 1, f"b={aged_b} c={aged_c}")
finally:
    resume.set()
    t.join(5)
    rc.os.replace = real_replace
check("A's paused release completed normally",
      rel_out.get("code") == 0, str(rel_out))
check("stamp is A's and A's claim is gone",
      (d8 / rc.STAMP_NAME).read_text().strip() == "session-a"
      and not (d8 / rc.CLAIM_NAME).exists())
code, _ = cli("claim", "session-b", "--state-dir", str(d8))
check("B claims cleanly after A's release finishes", code == 0)

# 4b3. stamp-read / release / late-link barrier regression (#2454 round
# 7): B reads the missing stamp, PAUSES just before os.link; A completes
# release --stamp (stamp precedes unlink, freeing the path); B's link then
# succeeds — pre-fix, B returned exit 0 and a duplicate recap started for
# a session whose stamp already existed. The post-link stamp recheck must
# instead retract B's claim and report already-recapped.
d9 = Path(tempfile.mkdtemp(prefix="claim-latelink-"))
_, msg = cli("claim", "session-a", "--state-dir", str(d9))
tok_a9 = token_of(msg)
b_paused, b_resume = threading.Event(), threading.Event()
real_link = rc.os.link
pause_once = {"armed": True}


def pausing_link(src, dst):
    if pause_once["armed"]:
        pause_once["armed"] = False
        b_paused.set()
        assert b_resume.wait(5), "late-link pause never resumed"
    return real_link(src, dst)


b9_out: dict = {}


def run_b_claim():
    buf = io.StringIO()
    with redirect_stdout(buf):
        b9_out["code"] = rc.claim(d9, "session-a", 0.0)
    b9_out["msg"] = buf.getvalue()


rc.os.link = pausing_link
tb = threading.Thread(target=run_b_claim)
tb.start()
try:
    check("B paused before publishing its claim", b_paused.wait(5))
    code, _ = cli("release", "session-a", "--stamp", "--token", tok_a9,
                  "--state-dir", str(d9))
    check("A's stamped release completed while B was paused", code == 0)
finally:
    b_resume.set()
    tb.join(5)
    rc.os.link = real_link
check("B's late link self-retracts as already-recapped",
      b9_out.get("code") == 1 and "already-recapped" in b9_out.get("msg", ""),
      str(b9_out))
check("no live claim survives B's retraction",
      not (d9 / rc.CLAIM_NAME).exists())
check("stamp remains session-a",
      (d9 / rc.STAMP_NAME).read_text().strip() == "session-a")
code, msg = cli("claim", "session-next", "--state-dir", str(d9))
check("a different session claims cleanly afterwards", code == 0)
# retract-edge branches: a foreign token never removes the live claim, and
# a busy lock leaves it for stale reclaim (both are safe no-ops)
rc._retract_own_claim(d9, "not-the-live-token")
check("retract with a foreign token leaves the live claim",
      (d9 / rc.CLAIM_NAME).exists())
busy_fd = rc._acquire_ownership_lock(d9)
rc._retract_own_claim(d9, token_of(msg))
check("retract under a busy lock leaves the claim (stale reclaim later)",
      (d9 / rc.CLAIM_NAME).exists())
rc._release_ownership_lock(busy_fd)
cli("release", "session-next", "--token", token_of(msg),
    "--state-dir", str(d9))

# 4b4. ambiguous stamp reads fail CLOSED (#2454 round 8): a stamp that
# exists but cannot be read (permissions, transient I/O — simulated
# deterministically by a DIRECTORY at the stamp path, which raises
# IsADirectoryError, an OSError that is not FileNotFoundError) must never
# admit work. Pre-fix, `except OSError: pass` treated it as "no stamp".
d10 = Path(tempfile.mkdtemp(prefix="claim-ambig-"))
(d10 / rc.STAMP_NAME).mkdir()
code, msg = cli("claim", "session-x", "--state-dir", str(d10))
check("pre-link ambiguous stamp refuses to claim",
      code == 1 and "state unknown" in msg, msg)
check("no claim was published on pre-link ambiguity",
      not (d10 / rc.CLAIM_NAME).exists())

# ...and after publication: B pauses before os.link with NO stamp, the
# stamp path turns unreadable mid-pause, B resumes — the post-link recheck
# must retract and fail closed (A may have stamped this very session).
d11 = Path(tempfile.mkdtemp(prefix="claim-ambig2-"))
b11_paused, b11_resume = threading.Event(), threading.Event()
pause11 = {"armed": True}


def pausing_link11(src, dst):
    if pause11["armed"]:
        pause11["armed"] = False
        b11_paused.set()
        assert b11_resume.wait(5), "ambig-link pause never resumed"
    return real_link(src, dst)


b11_out: dict = {}


def run_b11():
    buf = io.StringIO()
    with redirect_stdout(buf):
        b11_out["code"] = rc.claim(d11, "session-y", 0.0)
    b11_out["msg"] = buf.getvalue()


rc.os.link = pausing_link11
t11 = threading.Thread(target=run_b11)
t11.start()
try:
    check("ambig-B paused before publishing", b11_paused.wait(5))
    (d11 / rc.STAMP_NAME).mkdir()  # stamp becomes unreadable mid-pause
finally:
    b11_resume.set()
    t11.join(5)
    rc.os.link = real_link
check("post-link ambiguous stamp retracts and fails closed",
      b11_out.get("code") == 1
      and "unreadable after publication" in b11_out.get("msg", ""),
      str(b11_out))
check("no live claim survives the ambiguity retraction",
      not (d11 / rc.CLAIM_NAME).exists())

# 4d. compare-and-delete reap (#2454 round 4): the deterministic hazard —
# after a claimer reads a STALE claim, the claim is reaped-and-replaced by
# a FRESH one; the late deleter must NOT remove the fresh claim.
d6 = Path(tempfile.mkdtemp(prefix="claim-reap-"))
(d6 / rc.CLAIM_NAME).write_text(json.dumps(
    {"session": "old", "ts": time.time() - STALE_S - 60, "token": "tok-old"}))
stale_tok, stale_age = rc._read_claim(d6 / rc.CLAIM_NAME)
check("stale read sees the old token", stale_tok == "tok-old"
      and stale_age >= STALE_S)
# ...meanwhile another reclaimer wins: stale reaped, FRESH claim linked
(d6 / rc.CLAIM_NAME).write_text(json.dumps(
    {"session": "new", "ts": time.time(), "token": "tok-fresh"}))
rc._try_reap(d6, stale_tok, STALE_S)   # the late deleter fires
check("late reap refuses to delete the fresh claim",
      (d6 / rc.CLAIM_NAME).exists()
      and json.loads((d6 / rc.CLAIM_NAME).read_text())["token"] == "tok-fresh")
# ...but a genuine stale claim IS reaped under the lock
(d6 / rc.CLAIM_NAME).write_text(json.dumps(
    {"session": "old2", "ts": time.time() - STALE_S - 60, "token": "tok-o2"}))
rc._try_reap(d6, "tok-o2", STALE_S)
check("matching stale claim is reaped", not (d6 / rc.CLAIM_NAME).exists())
post_fd = rc._acquire_ownership_lock(d6)
check("ownership lock is free again after the reap", post_fd is not None)
if post_fd is not None:
    rc._release_ownership_lock(post_fd)
# ...a HELD ownership lock blocks deletion entirely (flock, not file
# presence: the lock file always exists; only a held flock excludes)
(d6 / rc.CLAIM_NAME).write_text(json.dumps(
    {"session": "old3", "ts": time.time() - STALE_S - 60, "token": "tok-o3"}))
holder_fd = rc._acquire_ownership_lock(d6)
rc._try_reap(d6, "tok-o3", STALE_S)
check("held ownership lock blocks the reap",
      (d6 / rc.CLAIM_NAME).exists())
rc._release_ownership_lock(holder_fd)
rc._try_reap(d6, "tok-o3", STALE_S)
check("reap proceeds once the holder releases",
      not (d6 / rc.CLAIM_NAME).exists())

# 4e. concurrent stale-reclaim stress (reviewer hit 2 winners by round 219
# of 24 claimers on the pre-fix head): every round starts from one STALE
# claim; the safety property is never >1 winner, and the surviving claim
# must be a winner's. Liveness: nearly every round should produce a winner.
rounds, claimers, winner_counts = 300, 12, []
d7 = Path(tempfile.mkdtemp(prefix="claim-stress-"))
with ThreadPoolExecutor(max_workers=claimers) as pool:
    for rnd in range(rounds):
        for f in d7.iterdir():
            f.unlink()
        (d7 / rc.CLAIM_NAME).write_text(json.dumps(
            {"session": "dead", "ts": time.time() - STALE_S - 60,
             "token": f"dead-{rnd}"}))
        with redirect_stdout(io.StringIO()):
            codes = list(pool.map(
                lambda _: rc.claim(d7, SESSION, STALE_S), range(claimers)))
        wins = codes.count(0)
        winner_counts.append(wins)
        if wins > 1:
            break
check("stale-reclaim stress: never more than one winner "
      f"({rounds} rounds x {claimers} claimers)",
      max(winner_counts) <= 1, f"round {len(winner_counts)-1}: "
      f"{winner_counts[-1]} winners")
check("stale-reclaim stress: liveness (>=90% rounds produce a winner)",
      sum(winner_counts) >= 0.9 * len(winner_counts),
      f"{sum(winner_counts)}/{len(winner_counts)}")

# 5. stale claim (dead worker) is reclaimed
d2 = Path(tempfile.mkdtemp(prefix="claim-stale-"))
(d2 / rc.CLAIM_NAME).write_text(json.dumps(
    {"session": SESSION, "ts": time.time() - STALE_S - 60, "pid": 1}))
code, msg = cli("claim", SESSION, "--state-dir", str(d2))
check("stale claim reclaimed", code == 0 and "claimed" in msg, msg)
check("reclaim refreshed the ts",
      time.time() - json.loads((d2 / rc.CLAIM_NAME).read_text())["ts"] < 60)

# 6. corrupt claims: non-reclaimable while their mtime is FRESH (round-2
# review: an unreadable-but-young claim must not be treated as stale), and
# reclaimable once the file mtime itself is stale.
d3 = Path(tempfile.mkdtemp(prefix="claim-corrupt-"))
(d3 / rc.CLAIM_NAME).write_text("not json{")
code, msg = cli("claim", SESSION, "--state-dir", str(d3))
check("fresh corrupt claim is NOT reclaimable (in-flight by mtime)",
      code == 1 and "in-flight" in msg, msg)
old_m = time.time() - STALE_S - 60
os.utime(d3 / rc.CLAIM_NAME, (old_m, old_m))
code, msg = cli("claim", SESSION, "--state-dir", str(d3))
check("mtime-stale corrupt claim is reclaimed", code == 0, msg)

# 6b. defensive loop exit: if every link round collides (claim keeps
# reading stale), the claimer gives up with exit 1 instead of spinning
d4 = Path(tempfile.mkdtemp(prefix="claim-lostrace-"))
real_link = rc.os.link


def always_busy(*a, **k):
    raise FileExistsError


rc.os.link = always_busy
try:
    with redirect_stdout(io.StringIO()):
        code = rc.claim(d4, SESSION, STALE_S)
finally:
    rc.os.link = real_link
check("exhausted re-race gives up with exit 1", code == 1)
check("temp file cleaned up even on give-up",
      not list(d4.glob(".recap-claim-*")))

# 7. state_dir default resolution reaches <workspace>/state (no override)
resolved = rc.state_dir(None)
check("state_dir default resolves under the workspace",
      resolved.name == "state", str(resolved))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All recap_claim checks passed.")
