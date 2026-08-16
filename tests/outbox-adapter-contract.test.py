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
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

NOT_IMPL: list[str] = []
FAILED: list[str] = []
PASSED: list[str] = []
ERRORED: list[str] = []


class NotImplementedYet(Exception):
    """The contract's subject does not exist. Distinct from a contract breach."""


def adapter_mod():
    try:
        import ag2_sparrow.outbox_adapter as m  # noqa: PLC0415
    except ImportError as exc:
        raise NotImplementedYet(f"ag2_sparrow.outbox_adapter: {exc}") from exc
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
    from ag2_sparrow.outbox import DeliveryOutcome as O  # noqa: PLC0415
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
    from ag2_sparrow.outbox import DeliveryOutcome as O  # noqa: PLC0415
    r = classify(status=200, body={"ok": True, "event_id": "$abc"})
    assert r.outcome == O.CONFIRMED, f"got {r.outcome}; an explicit id is the positive proof"
    assert r.receipt_id == "$abc", f"the id must be carried on the receipt; got {r.receipt_id!r}"


# 3 ---------------------------------------------------------------------------
@contract("4xx is NOT_DELIVERED; 5xx and a timeout are UNKNOWN")
def _status_mapping():
    m = adapter_mod()
    classify = need(m, "classify_response")
    from ag2_sparrow.outbox import DeliveryOutcome as O  # noqa: PLC0415
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
    import ag2_sparrow.outbox as core  # noqa: PLC0415
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
    from ag2_sparrow.outbox import DeliveryOutcome as O  # noqa: PLC0415

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


def main() -> int:
    print(f"  target: ag2_sparrow.outbox_adapter  (repo {REPO.name})\n")
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
