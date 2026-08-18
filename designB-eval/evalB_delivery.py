#!/usr/bin/env python3
"""Delivery fault harness — the layer Design B does NOT solve, isolated per the
owner's 2026-08-17 split: local ownership (designB.py, ClaimMachine's subject)
vs external delivery, where the local rename and the remote API call are never
in one transaction.

Crash windows around the remote call, each injected explicitly:

    w0  before the attempt marker      (nothing sent, no local trace)
    w1  after attempt marker, before the remote call   (marked, NOT sent)
    w2  after the remote call, before delivered evidence (sent, looks like w1!)
    w3  after delivered evidence, before terminal rename

Ground truth is the provider's receive log; the local view after recovery is
compared against it. Three recovery policies × two provider contracts:

    naive      any dead-owner item returns to ready/ and is re-sent
    park       items with an attempt marker but no delivered evidence are
               OUTCOME_UNKNOWN -> parked to undelivered/, never auto-resent
    idem       like naive, but every send carries a stable idempotency key
               and the provider dedupes on it

Assertions (the owner's acceptance criteria):
    A1  the naive policy DOES duplicate at w2 — demonstrating the window is
        real, not hypothetical (a harness where naive also passes proves
        nothing about the park policy).
    A2  the park policy never duplicates, at any window, and never
        misclassifies: every parked item is genuinely ambiguous (its ground
        truth differs between w1 and w2 while its local state is identical).
    A3  with an idempotency key, every window ends exactly-once — duplicates
        zero AND losses zero — because ambiguity becomes safely retryable.
    A4  evidence-ordering: writing the delivered evidence BEFORE the send
        misclassifies a w1-style crash as delivered (silent loss). The
        sentinel narrows the window only in the send-then-evidence order,
        and even then w2 remains — evidence is risk control, not proof.
"""
from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import designB as B

EVIDENCE = "evidence"
fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


class Provider:
    """Fake remote endpoint. `received` is the ground truth the local side
    can never read directly. With a contract key, duplicate keys dedupe."""

    def __init__(self, idempotent: bool):
        self.idempotent = idempotent
        self.received = []          # every accepted delivery, in order
        self.keys = set()

    def send(self, body: str, key: str | None = None):
        if self.idempotent:
            assert key is not None, "idem contract requires a key"
            if key in self.keys:
                return              # deduped — second accept is a no-op
            self.keys.add(key)
        self.received.append(body)


class Crash(Exception):
    pass


def _ev(root, key, kind):
    d = pathlib.Path(root) / EVIDENCE
    d.mkdir(exist_ok=True)
    return d / f"{key}{B.SEP}{kind}"


def deliver(root, item_id, provider, window=None, evidence_first=False):
    """One delivery attempt by worker w1, crashing at `window` if set.
    Marker order is send-then-evidence unless evidence_first (for A4)."""
    tok = B.claim(root, item_id, "w1")
    if tok is None:
        return
    key = tok.split(B.SEP, 1)[0]
    body = (pathlib.Path(root) / B.INFLIGHT / tok).read_text()
    if window == "w0":
        raise Crash(window)
    _ev(root, key, "attempt").write_text(tok)
    if window == "w1":
        raise Crash(window)
    if evidence_first:
        _ev(root, key, "delivered").write_text(tok)
        if window == "w1e":                    # after early evidence, before send
            raise Crash(window)
    provider.send(body, key=key if provider.idempotent else None)
    if window == "w2":
        raise Crash(window)
    if not evidence_first:
        _ev(root, key, "delivered").write_text(tok)
    if window == "w3":
        raise Crash(window)
    B.complete(root, tok)
    _ev(root, key, "attempt").unlink(missing_ok=True)
    _ev(root, key, "delivered").unlink(missing_ok=True)


def recover_with_policy(root, policy):
    """Dead-owner recovery consulting local evidence. Returns keys parked."""
    parked = []
    inflight = pathlib.Path(root) / B.INFLIGHT
    if not inflight.exists():
        return parked
    for f in list(inflight.iterdir()):
        key = f.name.split(B.SEP)[0]
        if _ev(root, key, "delivered").exists():
            # evidence says the send landed: finish without resending
            B.complete(root, f.name)
            continue
        ambiguous = _ev(root, key, "attempt").exists()
        if policy == "park" and ambiguous:
            B._quarantine(f, root, key, "outcome_unknown", "w1")
            parked.append(key)
            continue
        # naive/idem (or unambiguous): return to ready for resend.
        # The dead token was renamed by our own claim; hand it back via the
        # same guarded transfer recover() uses.
        B._move(f, (pathlib.Path(root) / B.READY / key))
    return parked


def run_window(policy, window, evidence_first=False):
    """One item through claim->send with a crash at `window`, then recovery
    and (except for parked items) one re-delivery. Returns
    (ground_truth_receives, parked?, terminal?)"""
    root = tempfile.mkdtemp()
    provider = Provider(idempotent=(policy == "idem"))
    B.publish(root, "msg", "hello")
    try:
        deliver(root, "msg", provider, window=window,
                evidence_first=evidence_first)
    except Crash:
        pass
    parked = recover_with_policy(root, policy)
    if not parked:
        # the retry incarnation delivers whatever recovery made ready again
        deliver(root, "msg", provider)
    key = B.safe_key("msg")
    arch = pathlib.Path(root) / B.ARCHIVE
    terminal = arch.exists() and any(f.name.split(B.SEP)[0] == key
                                     for f in arch.iterdir())
    n = len(provider.received)
    shutil.rmtree(root, ignore_errors=True)
    return n, bool(parked), terminal


WINDOWS = ("w0", "w1", "w2", "w3", None)      # None = no crash (control)

print("== A1: the naive policy really duplicates (window is real) ==")
naive = {w: run_window("naive", w) for w in WINDOWS}
check(naive["w2"][0] == 2, f"naive at w2 double-sends (got {naive['w2'][0]})")
check(naive[None][0] == 1 and naive["w0"][0] == 1 and naive["w1"][0] == 1,
      "naive is fine when the crash precedes the send (w0/w1/control)")
check(naive["w3"][0] == 1, "naive at w3 honors delivered evidence, no resend")

print("== A2: park never duplicates and parks exactly the ambiguous set ==")
park = {w: run_window("park", w) for w in WINDOWS}
check(all(n <= 1 for n, _, _ in park.values()),
      f"park never double-sends (counts {[n for n, _, _ in park.values()]})")
check(park["w1"][1] and park["w2"][1],
      "w1 and w2 both park — locally indistinguishable, so BOTH must")
check(park["w1"][0] == 0 and park["w2"][0] == 1,
      "parked pair straddles the truth (w1 unsent, w2 sent): ambiguity is real")
check(not park["w0"][1] and park["w0"][0] == 1,
      "w0 (no attempt marker) is unambiguous: safe resend, not parked")
check(not park["w3"][1] and park["w3"][0] == 1 and park["w3"][2],
      "w3 completes from evidence: delivered once, archived, not parked")

print("== A3: idempotency key gives exactly-once at every window ==")
idem = {w: run_window("idem", w) for w in WINDOWS}
check(all(n == 1 for n, _, _ in idem.values()),
      f"idem: every window delivers exactly once (counts {[n for n, _, _ in idem.values()]})")
check(all(t for _, _, t in idem.values()),
      "idem: every window reaches the terminal state")

print("== A4: evidence order matters; the sentinel is risk control, not proof ==")
# evidence-first + crash between evidence and send = never sent, looks delivered
n, parked_, terminal = run_window("park", "w1e", evidence_first=True)
check(n == 0 and terminal,
      f"evidence-BEFORE-send + crash pre-send: item archived yet never sent "
      f"(received={n}) — silent loss, the wrong order")
# send-first (the correct order) still leaves w2: sent, evidence missing
check(park["w2"][1], "send-then-evidence still leaves w2 ambiguous — "
                     "no ordering removes OUTCOME_UNKNOWN without provider support")

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
