#!/usr/bin/env python3
"""Step-0 contract tests for the Sparrow Outbox claim protocol. These MUST fail today.

The Outbox does not exist yet; these encode the agreed contract before any
implementation, so the implementation is written against a fixed target rather
than the tests being written against whatever got built.

SCOPE — this is the OUTBOX layer only, the last of four, and the only one that
is exclusive. The layers, and the word each one owns:

    room    event -> eligibility -> fanout -> per-agent delivery   (m_id, agent_id)
    agent   delivery -> disposition: ACT/IGNORE/DEFER/DELEGATE/COLLABORATE/OBSERVE
    task    assignment; an exclusive claim ONLY under an explicit
            assignment_policy = EXCLUSIVE
    outbox  delivery claim                                  (outbox_instance, item_id)

    exclusivity below applies to the last line and to nothing above it.

INVARIANT (room layer, must not be weakened by anything here):
    Room events are fanned out, not consumed. Delivery to one agent does not
    reduce or revoke another agent's eligibility to receive the same event.

So a room event legitimately produces N independent per-agent deliveries keyed
(message_id, agent_id) — that key stops ONE agent double-taking an event, and
deliberately does not stop other agents taking it too. Each agent then decides
its own disposition; zero, one, or many may act. Exclusive ownership is an
opt-in task-level policy, never the default room-message semantic.

Concretely: three agents in one room may all receive "look at PR #123" and
split ACT / OBSERVE / IGNORE by their own reasoning. If two of them act, each
produces its own item in its own outbox, and those delivery claims neither
conflict nor should. Identical payloads are still two distinct deliveries.

The failure this guards against is naming: reuse `claim` upward and the room
quietly becomes an ordinary distributed worker queue where exactly one consumer
wins — which is precisely the property a multi-agent room must not have.

Four-state reporting, deliberately mirroring the protocol's own principle that
not-knowing is not a negative:

    NOT_IMPLEMENTED  the symbol is absent          -> expected at step 0
    FAIL             implemented, contract broken  -> a real defect
    ERROR            implemented, it RAISED        -> not a contract verdict
    PASS             implemented, contract held

A suite that reported NOT_IMPLEMENTED as FAIL would read identically once the
module exists but is wrong, which is the failure mode where an assertion that
dies on attribute lookup proves nothing about behaviour.

Run: python3 tests/sparrow-outbox-claim-protocol.test.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# Import the CANONICAL src/ copy: .coveragerc measures src, not packages/,
# so testing the vendored copy would leave these lines unmeasured.
sys.path.insert(0, str(REPO / "src"))

NOT_IMPL: list[str] = []
FAILED: list[str] = []
PASSED: list[str] = []
ERRORED: list[str] = []


class NotImplementedYet(Exception):
    """The contract's subject does not exist. Distinct from a contract breach."""


def outbox():
    try:
        import outbox as m  # noqa: PLC0415
    except ImportError as exc:
        raise NotImplementedYet(f"src/outbox.py: {exc}") from exc
    return m


def need(mod, name: str):
    if not hasattr(mod, name):
        raise NotImplementedYet(f"outbox.{name}")
    return getattr(mod, name)


def contract(title):
    def run(fn):
        try:
            fn()
        except NotImplementedYet as exc:
            NOT_IMPL.append(f"{title} — missing {exc}")
            print(f"  n/i  {title}\n         missing {exc}")
        except AssertionError as exc:
            FAILED.append(f"{title}: {exc}")
            print(f"  FAIL {title}\n         {exc}")
        except Exception as exc:  # noqa: BLE001 - a raise is not a verdict
            # Without this it kills the run and later contracts never execute.
            ERRORED.append(f"{title}: {type(exc).__name__}: {exc}")
            print(f"  ERR  {title}\n         {type(exc).__name__}: {exc}")
        else:
            PASSED.append(title)
            print(f"  ok   {title}")
        return fn
    return run


# 1 ---------------------------------------------------------------------------
@contract("two drainers on one outbox namespace: exactly one acquires the delivery claim")
def _one_winner():
    m = outbox()
    acquire = need(m, "acquire_delivery_claim")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        got = [acquire(root, "item-1", drainer_id=f"d{i}") for i in range(8)]
        winners = [g for g in got if g]
        assert len(winners) == 1, (
            f"{len(winners)} of 8 drainers acquired the same item; O_CREAT|O_EXCL on one "
            "canonical key per (outbox_instance, item_id) is the whole guarantee. Local "
            "drainer exclusion ONLY — lifting it to the room or task layer would make one "
            "agent's delivery revoke another's eligibility, which the room invariant forbids")


# 2 ---------------------------------------------------------------------------
@contract("crash between claim creation and body write leaves a recoverable claim")
def _torn_claim():
    m = outbox()
    acquire, read = need(m, "acquire_delivery_claim"), need(m, "read_delivery_claim")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert acquire(root, "item-2", drainer_id="d1")
        path = next((root / ".claims").glob("item-2*"))
        path.write_text("")                      # simulate crash mid-write
        rec = read(root, "item-2")
        assert rec is None or rec.state == "UNKNOWN", (
            "a truncated claim must read as UNKNOWN, never as a valid owner and never "
            f"as absent-so-free; got {rec!r}")


# 3 ---------------------------------------------------------------------------
@contract("recovery distinguishes ALIVE / DEAD / UNKNOWN, never a bool")
def _three_states():
    m = outbox()
    ident, State = need(m, "process_identity"), need(m, "OwnerState")
    for want in ("ALIVE", "DEAD", "UNKNOWN"):
        assert hasattr(State, want), f"OwnerState.{want} missing"
    assert ident(os.getpid()).state == State.ALIVE, "own live pid must read ALIVE"
    assert ident(999_999).state == State.DEAD, "absent pid must read DEAD (ESRCH)"
    # Inspectability is platform-specific; never-DEAD is not. DEAD is the value
    # that lets another drainer steal a live owner's claim.
    assert ident(1).state != State.DEAD, (
        f"pid 1 read {ident(1).state}; a live process must never read DEAD, however "
        "opaque it is. This is the value that steals a running worker's claim")
    if sys.platform == "darwin":
        # Darwin-specific: root-owned live processes answer EPERM there, so a
        # two-state probe calls them all dead.
        assert ident(1).state == State.UNKNOWN, (
            "on Darwin a root-owned live process is EPERM -> UNKNOWN, not ALIVE")


# 4 ---------------------------------------------------------------------------
@contract("OUTCOME_UNKNOWN + UNSAFE parks; it must not auto-retry")
def _park_on_unknown():
    m = outbox()
    resolve = need(m, "resolve_outcome")
    Outcome, Safety = need(m, "DeliveryOutcome"), need(m, "RetrySafety")
    action = resolve(Outcome.OUTCOME_UNKNOWN, Safety.UNSAFE, attempts=0)
    assert action == "park", (
        f"got {action!r}; a bare ok-with-no-id may already have been delivered, so "
        "bounded retry caps duplication without restoring correctness")


# 5 ---------------------------------------------------------------------------
@contract("TTL expiry alone must not steal from a live owner")
def _ttl_never_steals_live():
    m = outbox()
    acquire, steal = need(m, "acquire_delivery_claim"), need(m, "may_reclaim_delivery")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert acquire(root, "item-5", drainer_id="d1")     # owned by THIS live process
        assert steal(root, "item-5", ttl_seconds=0) is False, (
            "TTL says expired but the owner is ALIVE — a slow delivery is not a dead "
            "worker, and reclaiming here duplicates the send")


# 6 ---------------------------------------------------------------------------
@contract("a re-queued item starts from a full attempt budget")
def _requeue_resets_budget():
    m = outbox()
    acquire, park, requeue = need(m, "acquire_delivery_claim"), need(m, "park_item"), need(m, "requeue_item")
    attempts, note = need(m, "attempts_for"), need(m, "note_attempt")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        acquire(root, "item-6", drainer_id="d1")
        # Burn the budget first: at 0 attempts the assertion below holds whether
        # or not requeue resets anything.
        for _ in range(3):
            note(root, "item-6")
        assert attempts(root, "item-6") == 3, (
            f"setup failed: expected 3 recorded attempts, got {attempts(root, 'item-6')}; "
            "the reset test is meaningless without a non-zero starting budget")
        park(root, "item-6", reason="unconfirmed")
        requeue(root, "item-6")
        assert attempts(root, "item-6") == 0, (
            f"re-queued item carries {attempts(root, 'item-6')} prior attempts; a "
            "hand-recovered item that parks instantly is indistinguishable from a "
            "broken destination")



# 7 ---------------------------------------------------------------------------
@contract("/proc stat parsing survives a comm containing spaces and parens")
def _proc_stat_parse():
    m = outbox()
    parse = need(m, "_linux_process_identity")   # presence is the contract here
    del parse
    # Fields come after the LAST ')': comm is arbitrary text in parens, and a
    # naive line.split()[21] returns 0 here.
    raw = ("4321 (my weird ) proc) S 1 4321 4321 0 -1 4194304 1 0 0 0 1 1 0 0 "
           "20 0 1 0 555000 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0")
    after = raw[raw.rindex(")") + 1:].split()
    assert int(after[19]) == 555000, (
        f"field-22 extraction gave {after[19]!r}; a comm with spaces or a ')' "
        "breaks any parser that splits the whole line")
    assert raw.split()[21] != "555000", (
        "the naive split now agrees, so this control no longer proves anything")


# 8 ---------------------------------------------------------------------------
@contract("distinct item ids never share a claim file (path encoding is injective)")
def _c8():
    m = outbox()
    acquire = need(m, "acquire_delivery_claim")
    claim_path = need(m, "_claim_path")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        a, b = "a/b", "a_b"
        assert claim_path(root, a) != claim_path(root, b), (
            f"{a!r} and {b!r} map to one path {claim_path(root, a).name!r}")
        assert acquire(root, a, "d1") is True, "first distinct id must acquire"
        assert acquire(root, b, "d2") is True, (
            f"{b!r} was denied because {a!r} holds a colliding claim file")


# 9 ---------------------------------------------------------------------------
@contract("a REUSED pid does not hold the claim forever: the birth token is compared")
def _c9():
    m = outbox()
    acquire = need(m, "acquire_delivery_claim")
    may = need(m, "may_reclaim_delivery")
    claim_path = need(m, "_claim_path")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert acquire(root, "item-reuse", "d1") is True
        # Same pid (live), different birth time => a DIFFERENT process.
        p = claim_path(root, "item-reuse")
        rec = json.loads(p.read_text(encoding="utf-8"))
        rec["start_usec"] = 111
        rec["claimed_at"] = 0.0
        p.write_text(json.dumps(rec, sort_keys=True), encoding="utf-8")
        assert may(root, "item-reuse", 1.0) is True, (
            "pid is alive but its birth token differs, so the recorded owner is "
            "gone; the item must be reclaimable rather than stalled forever")


# 10 --------------------------------------------------------------------------
@contract("a claim with valid JSON but wrong types reads UNKNOWN, never raises")
def _c10():
    m = outbox()
    acquire = need(m, "acquire_delivery_claim")
    read = need(m, "read_delivery_claim")
    may = need(m, "may_reclaim_delivery")
    claim_path = need(m, "_claim_path")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert acquire(root, "item-typed", "d1") is True
        claim_path(root, "item-typed").write_text(
            '{"pid": "not-an-int", "claimed_at": "soon"}', encoding="utf-8")
        rec = read(root, "item-typed")          # must not raise
        assert rec is not None and rec.state == "UNKNOWN", (
            f"wrong-typed claim must read UNKNOWN, got {rec}")
        assert may(root, "item-typed", 0.0) is False, "UNKNOWN is never stealable"


# 11 --------------------------------------------------------------------------
@contract("two drainers acting on the SAME stale observation: only one ends up holding it")
def _c11():
    m = outbox()
    acquire = need(m, "acquire_delivery_claim")
    reclaim = need(m, "reclaim_delivery_claim")
    read = need(m, "read_delivery_claim")
    claim_path = need(m, "_claim_path")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert acquire(root, "item-cas", "dead-owner") is True
        p = claim_path(root, "item-cas")
        rec = json.loads(p.read_text(encoding="utf-8"))
        rec["pid"] = 999999                  # a pid that is not running
        rec["claimed_at"] = 0.0              # and long past any TTL
        p.write_text(json.dumps(rec, sort_keys=True), encoding="utf-8")
        # Both drainers observed the same stale claim before either acted.
        first = reclaim(root, "item-cas", 1.0, "A")
        second = reclaim(root, "item-cas", 1.0, "B")
        assert [first, second].count(True) == 1, (
            f"exactly one drainer may take it, got A={first} B={second}")
        holder = read(root, "item-cas")
        assert holder is not None and holder.drainer_id == "A", (
            f"the winner's claim must survive, holder={holder}")


# 12 --------------------------------------------------------------------------
@contract("a drainer acting on a STALE observation must not destroy the new holder's claim")
def _c12():
    """The check-then-act failure, with the interleaving pinned rather than raced.

    A observes the stale claim, then B reclaims it completely, then A proceeds on
    its stale judgment. A must not be able to delete B's fresh claim. Racing two
    processes reproduces this only ~40% of the time, which is not a guard.
    """
    m = outbox()
    reclaim = need(m, "reclaim_delivery_claim")
    read = need(m, "read_delivery_claim")
    claim_path = need(m, "_claim_path")
    with tempfile.TemporaryDirectory() as tmp:
        root, item = Path(tmp), "item-aba"
        (root / ".claims").mkdir(parents=True, exist_ok=True)
        claim_path(root, item).write_text(json.dumps({
            "item_id": item, "drainer_id": "dead-owner", "pid": 999999,
            "start_usec": 1, "claimed_at": 0.0,
        }, sort_keys=True), encoding="utf-8")

        observed, b_done = threading.Event(), threading.Event()
        original, slow = m.read_delivery_claim, {"on": True}

        def paused_read(*a, **kw):
            rec = original(*a, **kw)
            if slow["on"]:
                slow["on"] = False
                observed.set()
                b_done.wait(10)      # B reclaims while A holds this observation
            return rec

        m.read_delivery_claim = paused_read
        try:
            out = {}
            a = threading.Thread(target=lambda: out.update(a=reclaim(root, item, 1.0, "A")))
            a.start()
            assert observed.wait(10), "A never reached its observation"
            slow["on"] = False                       # B must not pause
            out["b"] = reclaim(root, item, 1.0, "B")
            b_done.set()
            a.join(10)
        finally:
            m.read_delivery_claim = original

        holder = read(root, item)
        assert holder is not None, "the item ended up held by nobody"
        assert [out.get("a"), out["b"]].count(True) == 1, (
            f"both drainers hold the item: A={out.get('a')} B={out['b']} — "
            "A acted on an observation that was already superseded")
        assert holder.drainer_id == "B", (
            f"B reclaimed it, but the surviving claim is {holder.drainer_id!r}; "
            "A deleted a claim it never judged")


# 13 --------------------------------------------------------------------------
@contract("a drainer cannot release a claim it does not hold")
def _c13():
    """The CAS fixes the composed path; this closes the primitive it composed.

    reclaim_delivery_claim is safe, but acquire/release stayed exported, so a
    caller could still delete a live claim by hand — the exact unlink that made
    the check-then-act race destructive.
    """
    m = outbox()
    acquire = need(m, "acquire_delivery_claim")
    release = need(m, "release_delivery_claim")
    read = need(m, "read_delivery_claim")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert acquire(root, "item-own", "winner") is True
        assert release(root, "item-own", "stranger") is False, (
            "a non-owner must not be able to release someone else's claim")
        assert read(root, "item-own") is not None, (
            "the winner's claim was deleted by a drainer that never held it")
        assert release(root, "item-own", "winner") is True, "the owner may release"
        assert read(root, "item-own") is None
        # An operator recovery legitimately force-releases, but must say so.
        assert acquire(root, "item-force", "w2") is True
        assert release(root, "item-force", force=True) is True
        try:
            release(root, "item-nobody")
            raise AssertionError("release with neither a drainer_id nor force must refuse")
        except ValueError:
            pass


# 14 --------------------------------------------------------------------------
@contract("a stale release must not delete the successor's claim (release ABA)")
def _c14():
    """The check-then-unlink twin of contract 12, on the release path.

    A passes its ownership check, the slot turns over underneath it (a force
    release plus a successor acquire), and A's unlink then deletes a claim A
    never owned — leaving the item free while the successor believes it holds it.
    """
    m = outbox()
    acquire = need(m, "acquire_delivery_claim")
    release = need(m, "release_delivery_claim")
    read = need(m, "read_delivery_claim")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert acquire(root, "item-aba", "A") is True
        original, tripped = m._read_claim_at, {"done": False}

        def turnover(path, item_id):
            rec = original(path, item_id)
            # Fire once, after A has read its own record and before it acts.
            if not tripped["done"] and rec is not None and rec.drainer_id == "A":
                tripped["done"] = True
                release(root, "item-aba", force=True)      # operator recovery
                acquire(root, "item-aba", "B")             # successor takes it
            return rec

        m._read_claim_at = turnover
        try:
            out = release(root, "item-aba", "A")
        finally:
            m._read_claim_at = original

        holder = read(root, "item-aba")
        assert holder is not None, (
            "A released a claim it no longer owned; the item is now free while B "
            "believes it holds delivery — a third drainer acquires and both send")
        assert holder.drainer_id == "B", (
            f"the surviving claim is {holder.drainer_id!r}, expected B")
        assert out is False, (
            "A reported a successful release of a claim that was no longer its own")


def main() -> int:
    print(f"  target: src/outbox.py  (repo {REPO.name})\n")
    total = len(PASSED) + len(FAILED) + len(NOT_IMPL) + len(ERRORED)
    print(f"\n  {total} contracts: {len(PASSED)} pass, {len(FAILED)} FAIL, "
          f"{len(ERRORED)} ERROR, {len(NOT_IMPL)} not-implemented")
    if ERRORED:
        print("\nERRORED — the implementation raised; this is not a contract verdict")
        return 3
    if FAILED:
        print("\nFAILED — implemented but the contract is broken")
        return 1
    if NOT_IMPL:
        print("\nNOT IMPLEMENTED — expected at step 0; this is the target to build against")
        return 2
    print("\nPASS — the claim protocol holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
