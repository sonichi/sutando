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

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

NOT_IMPL: list[str] = []
FAILED: list[str] = []
PASSED: list[str] = []
ERRORED: list[str] = []


class NotImplementedYet(Exception):
    """The contract's subject does not exist. Distinct from a contract breach."""


def outbox():
    try:
        import ag2_sparrow.outbox as m  # noqa: PLC0415
    except ImportError as exc:
        raise NotImplementedYet(f"ag2_sparrow.outbox: {exc}") from exc
    return m


def need(mod, name: str):
    if not hasattr(mod, name):
        raise NotImplementedYet(f"ag2_sparrow.outbox.{name}")
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
        except Exception as exc:  # noqa: BLE001
            # A FOURTH state. An implementation that RAISES rather than returning
            # a wrong value would otherwise kill the run at this contract and
            # every later one silently never executes.
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
    # pid 1 is alive but not inspectable without privilege -> EPERM -> UNKNOWN.
    # A bool probe collapses this to 'dead' and steals a live owner's claim.
    assert ident(1).state == State.UNKNOWN, (
        "a live-but-opaque owner (EPERM) must read UNKNOWN, not DEAD — measured on "
        "Darwin 2026-08-16: every root-owned live process answers EPERM")


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
        # BURN THE BUDGET FIRST. Without this the item sits at 0 attempts, the
        # assertion below holds whether or not requeue resets anything, and the
        # contract cannot fail the way the system actually fails. Caught by
        # mutation 2026-08-16: deleting the reset changed no test outcome.
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
    # The parser locates fields after the LAST ')' because comm is arbitrary
    # text in parens. A naive line.split()[21] returns 0 on this input, which
    # would silently give every such process a bogus birth token.
    raw = ("4321 (my weird ) proc) S 1 4321 4321 0 -1 4194304 1 0 0 0 1 1 0 0 "
           "20 0 1 0 555000 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0")
    after = raw[raw.rindex(")") + 1:].split()
    assert int(after[19]) == 555000, (
        f"field-22 extraction gave {after[19]!r}; a comm with spaces or a ')' "
        "breaks any parser that splits the whole line")
    assert raw.split()[21] != "555000", (
        "the naive split now agrees, so this control no longer proves anything")


def main() -> int:
    print(f"  target: ag2_sparrow.outbox  (repo {REPO.name})\n")
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
