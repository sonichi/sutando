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

import fcntl
import json
import os
import sys
import tempfile
import threading
import time
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


def concurrently(fn):
    """Run fn in another thread and wait for it to finish contending.

    The claim operations serialize on a per-item lock, and that lock is held per
    open file description: a nested call in the SAME thread is a re-entry (which
    now raises), while a real peer is a separate thread or process. Modelling a
    competitor as a nested call tests an interleaving that cannot occur.
    """
    th = threading.Thread(target=fn, daemon=True)
    th.start()
    time.sleep(0.05)          # let it reach the lock and block there
    return th


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


# 12 ---------------------------------------------------------------------------
@contract("a concurrent peer never loses a claim it was told it holds")
def _c12():
    """The single-owner invariant, stated as a property rather than a mechanism.

    A releases while a peer force-releases and re-acquires. Whatever the
    interleaving, exactly one drainer may end up holding the item, and a drainer
    that was told `acquire` succeeded must not have that claim removed underneath
    it by an operation belonging to an earlier incarnation.
    """
    m = outbox()
    acquire = need(m, "acquire_delivery_claim")
    release = need(m, "release_delivery_claim")
    read = need(m, "read_delivery_claim")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert acquire(root, "item-aba", "A") is True
        granted, original, tripped = [], m._read_claim_at, {"done": False}

        def peer():
            release(root, "item-aba", force=True)
            if acquire(root, "item-aba", "B"):
                granted.append("B")

        def turnover(path, item_id):
            rec = original(path, item_id)
            if not tripped["done"] and rec is not None and rec.drainer_id == "A":
                tripped["done"] = True
                tripped["th"] = concurrently(peer)
            return rec

        m._read_claim_at = turnover
        try:
            release(root, "item-aba", "A")
        finally:
            m._read_claim_at = original
        th = tripped.get("th")
        if th:
            th.join(timeout=5)

        holder = read(root, "item-aba")
        hid = holder.drainer_id if holder else None
        for w in granted:
            assert hid == w, (
                f"{w} was told it holds delivery, but the claim now reads {hid!r}; "
                "an earlier incarnation's operation removed a live holder's claim")


# 14 ---------------------------------------------------------------------------
@contract("a release and a concurrent reclaim cannot both leave the item owned")
def _c14():
    """Two drainers act on the same item at once; at most one may own it after."""
    m = outbox()
    acquire = need(m, "acquire_delivery_claim")
    release = need(m, "release_delivery_claim")
    reclaim = need(m, "reclaim_delivery_claim")
    read = need(m, "read_delivery_claim")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert acquire(root, "item-aba2", "A") is True
        won = []

        def peer():
            if reclaim(root, "item-aba2", 0.0, "B"):
                won.append("B")

        th = concurrently(peer)
        if release(root, "item-aba2", "A"):
            won.append("A-released")
        th.join(timeout=5)
        holder = read(root, "item-aba2")
        hid = holder.drainer_id if holder else None
        assert hid in (None, "B"), (
            f"claim reads {hid!r} after A released and B reclaimed concurrently")
        if "B" in won:
            assert hid == "B", (
                "B was told it reclaimed the item, but the claim no longer names it")


# 15 ---------------------------------------------------------------------------
@contract("a turnover between the ownership decision and the unlink loses nothing")
def _c15():
    """The window a name-based conditional unlink cannot close by narrowing.

    A proves the slot is its own, and the slot is rebound before A's unlink runs.
    `requeue_item` reaches this with a live releaser via force-release, so it is
    not a crash-only path. Hooked at the stat that ends the decision, which is
    the last instant before the act — thread timing alone does not land here.
    """
    m = outbox()
    acquire = need(m, "acquire_delivery_claim")
    release = need(m, "release_delivery_claim")
    read = need(m, "read_delivery_claim")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert acquire(root, "item-aba3", "A") is True
        real_stat, fired, threads = os.stat, {"n": False}, []

        def peer():
            release(root, "item-aba3", force=True)      # requeue_item's path
            acquire(root, "item-aba3", "B")             # successor takes the slot

        def hook(path, *a, **k):
            res = real_stat(path, *a, **k)
            if not fired["n"] and ".release-" in str(path):
                fired["n"] = True
                threads.append(concurrently(peer))
            return res

        os.stat = hook
        try:
            release(root, "item-aba3", "A")
        finally:
            os.stat = real_stat
        for th in threads:
            th.join(timeout=5)

        assert fired["n"], "the decision point was never reached; the hook is inert"
        holder = read(root, "item-aba3")
        assert holder is not None and holder.drainer_id == "B", (
            f"claim reads {holder.drainer_id if holder else None!r}; A unlinked a "
            "claim it had not judged, so B believes it owns delivery of an item "
            "no longer claimed and a third drainer can acquire and send it too")


# 16 --------------------------------------------------------------------------
@contract("a reclaim that dies mid-swap must not wedge the item forever")
def _c16():
    """The swap token is created by one syscall and consumed by a later one.

    A crash between them leaves the token behind, and every future reclaim then
    fails its link — so a DEAD owner's claim can never be taken over and the item
    stalls permanently. An abandoned token must age out; a live one must not.
    """
    m = outbox()
    acquire = need(m, "acquire_delivery_claim")
    reclaim = need(m, "reclaim_delivery_claim")
    read = need(m, "read_delivery_claim")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert acquire(root, "item-wedge", "dead-owner") is True
        claim = m._claim_path(root, "item-wedge")
        rec = json.loads(claim.read_text(encoding="utf-8"))
        rec.update(pid=999999, claimed_at=0.0)
        claim.write_text(json.dumps(rec, sort_keys=True), encoding="utf-8")

        real_link = os.link

        def link_then_crash(src, dst):
            real_link(src, dst)
            raise KeyboardInterrupt("died after the swap token was created")

        os.link = link_then_crash
        try:
            reclaim(root, "item-wedge", 1.0, "X")
        except KeyboardInterrupt:
            pass
        finally:
            os.link = real_link

        tokens = list(m._claims_dir(root).glob(f"{claim.name}.reclaim-*"))
        assert len(tokens) == 1, f"expected the crashed swap token, got {tokens}"

        # A FRESH token means a peer may be mid-swap right now: do not steal.
        assert reclaim(root, "item-wedge", 1.0, "Y") is False, (
            "a token seconds old belongs to a live peer; sweeping it re-opens the "
            "double-reclaim this swap exists to prevent")

        old = time.time() - 120
        os.utime(str(tokens[0]), (old, old))
        assert reclaim(root, "item-wedge", 1.0, "Z") is True, (
            "an abandoned token must age out; otherwise one crash wedges this item "
            "forever and nothing ever delivers it")
        holder = read(root, "item-wedge")
        assert holder is not None and holder.drainer_id == "Z"


# 17 --------------------------------------------------------------------------
@contract("a torn claim must age out, or a crash mid-write wedges the item forever")
def _c17():
    """`acquire` creates the file and then writes it: a crash between the two
    leaves a claim naming no owner. Refusing it forever is safe for exclusion and
    fatal for liveness — nothing can ever deliver that item again.
    """
    m = outbox()
    acquire = need(m, "acquire_delivery_claim")
    reclaim = need(m, "reclaim_delivery_claim")
    read = need(m, "read_delivery_claim")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert acquire(root, "item-torn", "writer") is True
        claim = m._claim_path(root, "item-torn")
        claim.write_text("", encoding="utf-8")     # created, never written

        assert read(root, "item-torn").state == "UNKNOWN", (
            "a torn claim must never read as free")
        # Fresh: a writer may be between its open() and its write() right now.
        assert reclaim(root, "item-torn", 0.001, "R") is False, (
            "stealing a claim that is being written duplicates the delivery")

        old = time.time() - 3600
        os.utime(str(claim), (old, old))
        assert reclaim(root, "item-torn", 0.001, "R") is True, (
            "a torn claim nobody will ever finish writing must become reclaimable; "
            "otherwise one crash mid-acquire strands this item permanently")
        holder = read(root, "item-torn")
        assert holder is not None and holder.drainer_id == "R"


# 18 --------------------------------------------------------------------------
@contract("the lock namespace is bounded: N items never leave more than LOCK_STRIPES files")
def _c18():
    m = outbox()
    acquire = need(m, "acquire_delivery_claim")
    release = need(m, "release_delivery_claim")
    stripes = need(m, "LOCK_STRIPES")
    locks_dir = need(m, "LOCKS_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        need(m, "activate_lock_striping")(root)
        for i in range(256):
            item = f"task-bound-{i:04d}"
            assert acquire(root, item, "D1") is True
            assert release(root, item, "D1") is True
        locks = list((root / locks_dir).glob("*.lock"))
        assert len(locks) <= stripes, (
            f"{len(locks)} lock files after 256 items — ids are perpetually unique, "
            "so an unbounded lock namespace grows one inode per item forever")
        assert all(f.name.startswith("stripe-") for f in locks)


# 19 --------------------------------------------------------------------------
@contract("upgrading sweeps the legacy per-item lock files a pre-striping build left")
def _c19():
    m = outbox()
    acquire = need(m, "acquire_delivery_claim")
    release = need(m, "release_delivery_claim")
    locks_dir = need(m, "LOCKS_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        d = root / locks_dir
        d.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            (d / f"old-item-{i}.deadbeefdeadbeef.lock").touch()
        m.activate_lock_striping(root)          # deploy step after old files exist
        swept = getattr(m._HELD, "swept_roots", None)
        if swept is not None:
            swept.discard(str(root))
        assert acquire(root, "task-x", "D1") is True
        leftovers = [f for f in d.glob("*.lock") if not f.name.startswith("stripe-")]
        assert leftovers == [], f"legacy lock files survived the sweep: {leftovers}"
        assert release(root, "task-x", "D1") is True


# 20 --------------------------------------------------------------------------
@contract("nesting two stripe-mates on one thread raises instead of self-deadlocking")
def _c20():
    m = outbox()
    acquire = need(m, "acquire_delivery_claim")
    release = need(m, "release_delivery_claim")
    item_lock = need(m, "_item_lock")
    stripe_of = need(m, "_lock_stripe")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        need(m, "activate_lock_striping")(root)
        base = "task-mate-0"
        mate = next(f"task-mate-{i}" for i in range(1, 100000)
                    if stripe_of(f"task-mate-{i}") == stripe_of(base))
        raised = False
        with item_lock(root, base):
            try:
                with item_lock(root, mate):
                    pass
            except RuntimeError:
                raised = True
        assert raised, ("two items on one stripe nested in one thread must raise "
                        "loudly — blocking silently here is a self-deadlock")
        assert acquire(root, mate, "D1") is True, "stripe usable after unwinding"
        assert release(root, mate, "D1") is True


# 21 --------------------------------------------------------------------------
@contract("a failed legacy-lock sweep is swallowed and never takes down the consumer")
def _c21():
    m = outbox()
    sweep = need(m, "_sweep_legacy_locks")
    acquire = need(m, "acquire_delivery_claim")
    release = need(m, "release_delivery_claim")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sweep(root / "does-not-exist")          # iterdir -> FileNotFoundError
        ro = root / "unreadable"
        ro.mkdir()
        os.chmod(ro, 0o000)                     # iterdir -> PermissionError
        try:
            sweep(ro)
        finally:
            os.chmod(ro, 0o755)
        # the whole point of the swallow: claiming still works after a bad sweep
        assert acquire(root, "task-after-bad-sweep", "D1") is True
        assert release(root, "task-after-bad-sweep", "D1") is True


# 22 --------------------------------------------------------------------------
@contract("without the fence, locking is byte-compatible with pre-striping builds")
def _c22():
    m = outbox()
    acquire = need(m, "acquire_delivery_claim")
    release = need(m, "release_delivery_claim")
    item_lock = need(m, "_item_lock")
    locks_dir = need(m, "LOCKS_DIR")
    safe = need(m, "_safe_key")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # An origin/main process flocks the per-item file directly; without
        # the fence the new build must contend on the SAME inode.
        with item_lock(root, "task-mixed"):
            legacy = root / locks_dir / f"{safe('task-mixed')}.lock"
            assert legacy.exists(), (
                "fence absent yet no per-item lock file — the new build moved "
                "namespaces without a migration, old writers cannot see it")
            fd = os.open(str(legacy), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                blocked = False
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    blocked = True
                assert blocked, ("an old-version flock on the per-item file "
                                 "succeeded while the new build held the item "
                                 "lock — no cross-version mutual exclusion")
            finally:
                os.close(fd)
        # and no fence means NO sweep: a legacy file must survive an acquire
        (root / locks_dir / "survivor-item.aaaabbbbccccdddd.lock").touch()
        assert acquire(root, "task-other", "D1") is True
        assert release(root, "task-other", "D1") is True
        assert (root / locks_dir / "survivor-item.aaaabbbbccccdddd.lock").exists(), (
            "legacy lock file swept without the fence — the sweep ran while "
            "old writers could still hold these files")
        # One namespace per process lifetime: a fence appearing under a live
        # root must NOT flip it mid-flight (the memoized read is the guard)
        fence = root / locks_dir / need(m, "STRIPES_FENCE")
        fence.write_text('{"stripes": %d}' % need(m, "LOCK_STRIPES"))
        assert acquire(root, "task-late-fence", "D1") is True
        assert (root / locks_dir / f"{safe('task-late-fence')}.lock").exists(), (
            "a fence written under a live process flipped its namespace "
            "mid-flight — the memoized mode read must hold for process life")
        assert release(root, "task-late-fence", "D1") is True
    # fence error arms are loud on FRESH roots (first read, nothing cached)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        d = root / locks_dir
        d.mkdir(parents=True, exist_ok=True)
        fence = d / need(m, "STRIPES_FENCE")
        fence.write_text('{"stripes": 128}')
        for fn, args in ((acquire, (root, "task-m", "D1")),
                         (need(m, "activate_lock_striping"), (root,))):
            try:
                fn(*args); ok = False
            except RuntimeError as e:
                ok = "migration required" in str(e)
            assert ok, f"{fn.__name__} guessed a mode on a mismatched fence"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        d = root / locks_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / need(m, "STRIPES_FENCE")).write_text("not json")
        try:
            acquire(root, "task-m", "D1"); ok = False
        except RuntimeError as e:
            ok = "unreadable" in str(e)
        assert ok, "unreadable fence must raise, not fall back silently"


# 23 --------------------------------------------------------------------------
@contract("path aliases of one root share one lock namespace across activation")
def _c23():
    m = outbox()
    acquire = need(m, "acquire_delivery_claim")
    release = need(m, "release_delivery_claim")
    activate = need(m, "activate_lock_striping")
    locks_dir = need(m, "LOCKS_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        canonical = Path(tmp) / "root"
        (canonical / "sub").mkdir(parents=True)
        alias = canonical / "sub" / ".."          # same directory, second spelling
        # prime the alias spelling BEFORE activation (caches legacy mode)
        assert acquire(alias, "task-alias", "D1") is True
        assert release(alias, "task-alias", "D1") is True
        activate(canonical)
        # the alias must see stripe mode too — a raw-string cache leaves it
        # on legacy locks, straddling namespaces within one process
        assert acquire(alias, "task-alias-2", "D1") is True
        assert release(alias, "task-alias-2", "D1") is True
        legacy_after = [f.name for f in (canonical / locks_dir).glob("*.lock")
                        if not f.name.startswith("stripe-")
                        and "task-alias-2" in f.name]
        assert legacy_after == [], (
            f"alias spelling still uses legacy locks after activation "
            f"({legacy_after}) — root cache keyed on raw path, namespaces straddled")
        assert activate(canonical) is False, "second activation must be a no-op"
        # a FRESH process reads the fence from disk, not from this one's cache
        need(m, "_STRIPE_MODE").clear()
        assert acquire(canonical, "task-fresh-proc", "D1") is True
        assert release(canonical, "task-fresh-proc", "D1") is True
        stripes = [f.name for f in (canonical / locks_dir).glob("stripe-*.lock")]
        assert stripes, "fresh-process disk read of a valid fence must stripe"


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
