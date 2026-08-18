#!/usr/bin/env python3
"""Contracts for the Outbox's DeliveryAdapter seam. Step 0 — these MUST fail today.

The adapter is the ONLY place the Outbox touches a transport. Everything above it
stays transport-agnostic: the core decides claim / retry / park from a
DeliveryReceipt, and never from an HTTP status, a JSON body, or a provider quirk.

    outbox core   claim -> deliver -> archive | retry | park
          |
          v  DeliveryAdapter.send(item) -> DeliveryReceipt
    transport     AG2 Space gateway, Discord, Slack, ... (provider I/O only)

The receipt is the whole contract, and its job is to preserve NOT-KNOWING:

    CONFIRMED       the provider gave positive proof (an id)
    NOT_DELIVERED   the provider said no
    OUTCOME_UNKNOWN it neither confirmed nor denied

That third state is why this seam exists. The live AG2 Space gateway answers
`{"ok": true}` with no event_id on a successful post, which is not proof of
delivery — and an adapter that maps a 200 to CONFIRMED would launder a guess into
a fact, then let the core archive an item that may never have arrived. The same
shape in reverse — mapping it to NOT_DELIVERED — is what makes a sender re-send
an accepted message forever.

Four-state reporting:

    NOT_IMPLEMENTED  the symbol is absent
    FAIL             implemented, contract broken
    ERROR            implemented, it RAISED  -> not a contract verdict
    PASS             implemented, contract held

Run: python3 tests/outbox-adapter-contract.test.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

NOT_IMPL: list[str] = []
FAILED: list[str] = []
PASSED: list[str] = []
ERRORED: list[str] = []


class NotImplementedYet(Exception):
    """The contract's subject does not exist. Distinct from a contract breach."""


def adapter_mod():
    try:
        import outbox_adapter as m  # noqa: PLC0415
    except ImportError as exc:
        raise NotImplementedYet(f"src/outbox_adapter.py: {exc}") from exc
    return m


def need(mod, name: str):
    if not hasattr(mod, name):
        raise NotImplementedYet(f"{mod.__name__}.{name}")
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
            ERRORED.append(f"{title}: {type(exc).__name__}: {exc}")
            print(f"  ERR  {title}\n         {type(exc).__name__}: {exc}")
        else:
            PASSED.append(title)
            print(f"  ok   {title}")
        return fn
    return run


# 1 ---------------------------------------------------------------------------
@contract("ok-without-an-id is OUTCOME_UNKNOWN, never CONFIRMED")
def _ok_no_id_is_unknown():
    m = adapter_mod()
    classify = need(m, "classify_response")
    from outbox import DeliveryOutcome as O  # noqa: PLC0415
    r = classify(status=200, body={"ok": True})
    assert r.outcome == O.OUTCOME_UNKNOWN, (
        f"got {r.outcome}; a 200 with no id is the live AG2 Space reply and is NOT proof of "
        "delivery. Calling it CONFIRMED archives an item that may never have arrived; calling "
        "it NOT_DELIVERED re-sends one that did")


# 2 ---------------------------------------------------------------------------
@contract("an id in the body is CONFIRMED")
def _id_is_confirmed():
    m = adapter_mod()
    classify = need(m, "classify_response")
    from outbox import DeliveryOutcome as O  # noqa: PLC0415
    r = classify(status=200, body={"ok": True, "event_id": "$abc"})
    assert r.outcome == O.CONFIRMED, f"got {r.outcome}; an explicit id is the positive proof"
    assert r.receipt_id == "$abc", f"the id must be carried on the receipt; got {r.receipt_id!r}"


# 3 ---------------------------------------------------------------------------
@contract("4xx is NOT_DELIVERED; 5xx and a timeout are UNKNOWN")
def _status_mapping():
    m = adapter_mod()
    classify = need(m, "classify_response")
    from outbox import DeliveryOutcome as O  # noqa: PLC0415
    assert classify(status=403, body={}).outcome == O.NOT_DELIVERED, (
        "a refusal is a definite no — the request was understood and rejected")
    assert classify(status=500, body={}).outcome == O.OUTCOME_UNKNOWN, (
        "a 5xx may have applied the write before failing; it is not a denial")
    assert classify(status=None, body=None).outcome == O.OUTCOME_UNKNOWN, (
        "a timeout is the canonical unknown — the request may be in flight")


# 4 ---------------------------------------------------------------------------
@contract("the core decides from the RECEIPT, never from transport details")
def _core_is_transport_agnostic():
    m = adapter_mod()
    import outbox as core  # noqa: PLC0415
    src = Path(core.__file__).read_text(encoding="utf-8")
    for leak in ("status_code", "http", "urllib", "requests", "event_id", "200", "403"):
        assert leak not in src.lower().replace("https://", ""), (
            f"outbox.py mentions {leak!r}: transport detail has leaked into the core. The core "
            "must reach its verdict from DeliveryOutcome alone, or every new provider quirk "
            "becomes a change to the retry policy")


# 5 ---------------------------------------------------------------------------
@contract("a send that raises becomes UNKNOWN, never a silent failure")
def _raise_is_unknown():
    m = adapter_mod()
    Adapter = need(m, "DeliveryAdapter")
    from outbox import DeliveryOutcome as O  # noqa: PLC0415

    class Exploding(Adapter):
        def _transmit(self, item):
            raise ConnectionResetError("peer went away mid-write")

    with tempfile.TemporaryDirectory() as tmp:
        r = Exploding().send({"item_id": "i1", "body": "x", "root": tmp})
    assert r.outcome == O.OUTCOME_UNKNOWN, (
        f"got {r.outcome}; a connection reset mid-write is the case where the peer may have "
        "processed the request. Mapping an exception to NOT_DELIVERED re-sends it")


# 6 ---------------------------------------------------------------------------
@contract("the adapter never decides retry; it only reports")
def _adapter_has_no_policy():
    m = adapter_mod()
    Adapter = need(m, "DeliveryAdapter")
    for banned in ("retry", "park", "attempts", "backoff", "sleep"):
        assert not any(banned in n.lower() for n in dir(Adapter)), (
            f"DeliveryAdapter exposes {banned!r}: policy belongs to the core. An adapter that "
            "retries on its own is invisible to the attempt budget and un-bounded by it — which "
            "is exactly the loop this whole design exists to remove")


# 7 ---------------------------------------------------------------------------
@contract("an error status is never overridden by an id in its body")
def _status_beats_id():
    m = adapter_mod()
    classify = need(m, "classify_response")
    from outbox import DeliveryOutcome as O  # noqa: PLC0415

    # A trace id in an error envelope is not a delivery receipt; reading it as
    # one archives an item the provider explicitly refused.
    for status, want in ((500, O.OUTCOME_UNKNOWN), (503, O.OUTCOME_UNKNOWN),
                         (400, O.NOT_DELIVERED), (404, O.NOT_DELIVERED)):
        got = classify(status=status, body={"id": "trace-123", "error": "failed"})
        assert got.outcome == want, (
            f"status {status} with an id in the body -> {got.outcome.name}, expected "
            f"{want.name}; the id scan must not run before status semantics")
        assert got.receipt_id is None, (
            f"status {status} produced receipt_id={got.receipt_id!r}; an error "
            "envelope's id is not a delivery receipt")


# 8 ---------------------------------------------------------------------------
@contract("a caller may pin which keys count as proof")
def _id_keys_are_pinnable():
    m = adapter_mod()
    classify = need(m, "classify_response")
    from outbox import DeliveryOutcome as O  # noqa: PLC0415

    # Status-first does not help a caller whose transport reports failure INSIDE
    # a 200 body; only narrowing the key set can, so the seam must allow it.
    body = {"ok": True, "id": "170099"}
    assert classify(200, body).outcome == O.CONFIRMED, (
        "the default key set is deliberately broad, and that default is what "
        "makes pinning necessary rather than optional")
    r = classify(200, body, id_keys=("event_id",))
    assert r.outcome == O.OUTCOME_UNKNOWN and r.receipt_id is None, (
        f"got {r.outcome} / {r.receipt_id!r}; a caller that knows its provider "
        "proves delivery only with event_id must be able to say so")


# 9 ---------------------------------------------------------------------------
@contract("an integer identifier is proof, exactly as a string one is")
def _integer_ids_are_proof():
    m = adapter_mod()
    classify = need(m, "classify_response")
    from outbox import DeliveryOutcome as O  # noqa: PLC0415

    # receipt.py answers this for the same envelope and accepts (str, int); a
    # str-only rule here means one reader archives what the other re-sends.
    r = classify(200, {"event_id": 12345})
    assert r.outcome == O.CONFIRMED and r.receipt_id == "12345", (
        f"got {r.outcome} / {r.receipt_id!r}; an integer event_id is the same "
        "proof as a string one, and must be normalised to str")

    # bool is an int subclass. `True` is a flag, never an identifier — this is a
    # deliberate divergence from receipt.py, which confirms on {"event_id": True}.
    assert classify(200, {"event_id": True}).outcome == O.OUTCOME_UNKNOWN, (
        "a boolean must never be read as an identifier")
    assert classify(200, {"event_id": "   "}).outcome == O.OUTCOME_UNKNOWN, (
        "a whitespace-only id proves nothing")


# 10 --------------------------------------------------------------------------
@contract("`ts` is a timestamp, not a receipt, and is not proof by default")
def _ts_is_not_a_receipt():
    m = adapter_mod()
    classify = need(m, "classify_response")
    from outbox import DeliveryOutcome as O  # noqa: PLC0415

    # `ts` is a Slack idiom; here it is a send time, so confirming on it would
    # archive an item nothing proved was delivered.
    r = classify(200, {"ok": True, "ts": "1699.000"})
    assert r.outcome == O.OUTCOME_UNKNOWN and r.receipt_id is None, (
        f"got {r.outcome} / {r.receipt_id!r}; `ts` must not be in the default "
        "id keys")
    # A caller whose provider really does name its receipt `ts` can still say so.
    assert classify(200, {"ts": "1699.000"}, id_keys=("ts",)).outcome == O.CONFIRMED, (
        "pinning must still be able to opt IN to a key the default drops")


def main() -> int:
    print(f"  target: src/outbox_adapter.py  (repo {REPO.name})\n")
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
    print("\nPASS — the adapter seam holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
