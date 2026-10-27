#!/usr/bin/env python3
"""An outbox item record that exists but cannot be parsed is never read as a fresh item:
it reads parked with a send in doubt, so a cached payload is not sent again. Only an
absent record is new.

Run: python3 tests/outbox-unreadable-item-fail-closed.test.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
sys.path.insert(1, str(REPO / "src"))

from ag2_sparrow import outbox  # noqa: E402
from ag2_sparrow.delivery_core import (DeliveryCore, DeliveryOutcome, DeliveryReceipt,  # noqa: E402
                                       DesignAClaimBackend, DrainStatus, ProviderCapabilities,
                                       RetryPolicy)

_SPEC = importlib.util.spec_from_file_location("src_outbox_under_test", REPO / "src" / "outbox.py")
src_outbox = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = src_outbox
_SPEC.loader.exec_module(src_outbox)

ITEM = "proactive-x~0123456789abcdef"
PAYLOAD = json.dumps({"room_id": "!r:s", "body": "hi"}).encode("utf-8")
GARBAGE = ('{"status": "READY", "payl', b"\xff\xfe\x00", "[]", "null")
fails: list = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


class Provider:
    capabilities = ProviderCapabilities()

    def __init__(self):
        self.posts = 0

    def deliver(self, item_id, payload, key):
        self.posts += 1
        return DeliveryReceipt(outcome=DeliveryOutcome.CONFIRMED, provider_ref="$e")

    def reconcile(self, attempt):
        return None


def corrupt(root: Path, item_id: str = ITEM, garbage="{\"status\": \"READY\", \"payl") -> None:
    p = outbox._item_path(root, item_id)
    if isinstance(garbage, bytes):
        p.write_bytes(garbage)
    else:
        p.write_text(garbage, encoding="utf-8")


def accepted_then_interrupted(receipt: bool) -> Path:
    """An earlier incarnation began a send (and, with `receipt`, recorded the provider's
    acceptance) and died before completing it; its claim is gone."""
    root = Path(tempfile.mkdtemp(prefix="outbox-unreadable-"))
    backend = DesignAClaimBackend(root)
    backend.publish(ITEM, PAYLOAD)
    token = backend.claim(ITEM, "dead-worker")
    check(backend.mark_dispatching(token), "the earlier incarnation marked its send")
    if receipt:
        check(outbox.record_provider_ref(root, ITEM, "$accepted"), "its receipt was recorded")
    outbox.release_delivery_claim(root, ITEM, force=True)
    return root


def drain(root: Path, corrupt_after: str) -> "tuple[object, Provider]":
    """A restarted drain whose item record is corrupted right after `corrupt_after` runs."""
    class Corrupting(DesignAClaimBackend):
        def payload_for_claim(self, token):
            out = super().payload_for_claim(token)
            if corrupt_after == "payload":
                corrupt(self.root)
            return out

        def prior_dispatch(self, token):
            out = super().prior_dispatch(token)
            if corrupt_after == "prior_dispatch":
                corrupt(self.root)
            return out

    provider = Provider()
    core = DeliveryCore(Corrupting(root), provider, worker="restarted",
                        policy=RetryPolicy(park_interrupted_send=True))
    return core.deliver_one(ITEM, PAYLOAD), provider


def corrupted_mid_drain() -> None:
    for receipt in (False, True):
        root = accepted_then_interrupted(receipt)
        result, provider = drain(root, "payload")
        what = "a recorded receipt" if receipt else "a dispatch mark"
        check(provider.posts == 0,
              f"after {what}, corruption between the payload read and prior_dispatch sends nothing "
              f"({provider.posts} POSTs)")
        check(result.outcome is DeliveryOutcome.OUTCOME_UNKNOWN,
              f"after {what}, it settles as unknown ({result.outcome})")
        check(outbox.item_status(root, ITEM) == "PARKED", f"after {what}, the item is parked")

    root = Path(tempfile.mkdtemp(prefix="outbox-unreadable-"))
    DesignAClaimBackend(root).publish(ITEM, PAYLOAD)
    result, provider = drain(root, "prior_dispatch")
    check(provider.posts == 0 and result.status is DrainStatus.NOT_CLAIMED,
          f"corruption between prior_dispatch and the dispatch mark sends nothing "
          f"({provider.posts} POSTs, {result.status})")
    check(outbox.item_status(root, ITEM) == "PARKED", "and the item reads parked")


def corrupted_before_drain() -> None:
    for garbage in GARBAGE:
        root = Path(tempfile.mkdtemp(prefix="outbox-unreadable-"))
        backend = DesignAClaimBackend(root)
        backend.publish(ITEM, PAYLOAD)
        corrupt(root, garbage=garbage)
        provider = Provider()
        result = DeliveryCore(backend, provider, worker="w",
                              policy=RetryPolicy(park_interrupted_send=True)).deliver_one(ITEM, PAYLOAD)
        check(provider.posts == 0 and result.status is DrainStatus.TERMINAL,
              f"a record corrupt before the drain ({garbage!r}) is terminal and sends nothing "
              f"({provider.posts} POSTs, {result.status})")
        check(backend.publish(ITEM, PAYLOAD) is False, f"and a republish cannot replace it ({garbage!r})")


def the_read_contract(mod, name: str) -> None:
    root = Path(tempfile.mkdtemp(prefix="outbox-unreadable-"))
    fresh = mod._read_item(root, ITEM)
    check(fresh["status"] == "QUEUED" and not mod.dispatch_unsettled(fresh),
          f"{name}: an absent record is new")
    check(mod.read_item(root, ITEM) is None and mod.item_status(root, ITEM) is None,
          f"{name}: an absent record has no public read")
    mod._write_item(root, ITEM, {"item_id": ITEM, "status": "READY", "payload": "{}"})
    for garbage in GARBAGE:
        corrupt(root, garbage=garbage)
        d = mod._read_item(root, ITEM)
        check(d["status"] == "PARKED" and d["reason"] == mod.UNREADABLE_ITEM and mod.dispatch_unsettled(d),
              f"{name}: a record that cannot be parsed ({garbage!r}) reads parked, a send in doubt")
        check(mod.item_status(root, ITEM) == "PARKED", f"{name}: its public status is PARKED ({garbage!r})")
        check(mod.record_dispatching(root, ITEM, "inc") is False,
              f"{name}: no dispatch mark is written over it ({garbage!r})")
        check(mod.requeue_item_with_epoch(root, ITEM)[0] is mod.RequeueOutcome.UNREADABLE,
              f"{name}: an operator requeue refuses it as unreadable ({garbage!r})")
        check(mod.park_unclaimed(root, ITEM, "destination-changed") is False,
              f"{name}: it is not re-parked as a live item ({garbage!r})")
    mod.park_item(root, ITEM, "outcome-unknown")
    check(mod.requeue_item_with_epoch(root, ITEM)[0] is mod.RequeueOutcome.UNREADABLE,
          f"{name}: once parked over, it still cannot be requeued without a payload")


def the_operator_surface(mod, cli, name: str) -> None:
    """requeue refuses an unreadable record with its own exit code and leaves the body
    quarantined; list shows it by file name instead of skipping it or raising."""
    import undelivered_quarantine as uq
    for garbage in GARBAGE:
        results = Path(tempfile.mkdtemp(prefix="outbox-unreadable-")) / "results"
        results.mkdir()
        root = results / ".outbox-test"
        mod._write_item(root, ITEM, {"item_id": ITEM, "status": "READY", "payload": "{}"})
        mod.park_item(root, ITEM, "outcome-unknown")
        live = results / "proactive-x.txt"
        live.write_text("the reply", encoding="utf-8")
        uq.place(live, results, "proactive-x", when=1700000000)
        corrupt(root, garbage=garbage)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--root", str(root), "--json", "requeue", ITEM, "--body-id", "proactive-x"])
        check(code == 6 and json.loads(out.getvalue()).get("result") == "unreadable",
              f"{name}: requeue of an unreadable record exits 6, 'unreadable' ({garbage!r}: {code})")
        check(not live.exists() and len(uq.find_quarantined(results, "proactive-x")) == 1,
              f"{name}: and its quarantined body is not restored ({garbage!r})")
        try:
            listed = mod.list_items(root)
            parked = mod.list_items(root, "PARKED")
        except Exception as exc:  # noqa: BLE001
            check(False, f"{name}: list does not raise on {garbage!r} ({type(exc).__name__})")
            continue
        file_name = mod._item_path(root, ITEM).name
        check(len(listed) == 1 and listed[0].get("reason") == mod.UNREADABLE_ITEM
              and listed[0].get("file") == file_name and "item_id" not in listed[0] and parked == listed,
              f"{name}: list surfaces it as PARKED unreadable-item by file name ({garbage!r}: {listed})")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--root", str(root), "list"])
        check("reason=unreadable-item" in out.getvalue() and f"file={file_name}" in out.getvalue(),
              f"{name}: the plain listing names it and its file ({garbage!r})")


def main() -> int:
    import outbox_cli as src_cli
    from ag2_sparrow import outbox_cli as pkg_cli
    the_operator_surface(src_cli.outbox, src_cli, "src cli")
    the_operator_surface(outbox, pkg_cli, "vendored cli")
    the_read_contract(src_outbox, "src")
    the_read_contract(outbox, "vendored")
    corrupted_mid_drain()
    corrupted_before_drain()
    print(f"\n{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
