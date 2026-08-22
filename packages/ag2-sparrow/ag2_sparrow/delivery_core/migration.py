"""Migration fencing: single-protocol-per-epoch (seam doc §4).

One logical item is interpreted by exactly ONE claim protocol within an
epoch. Migration = lock out drainers -> one-shot convert (each item
individually atomic) -> write the version fence -> start only the new
drainer. The fence is written LAST: a crash anywhere mid-conversion leaves
the fence at the old epoch, so the old protocol stays authoritative and no
item is ever interpreted by both protocols.

Legacy delivered-sentinels map conservatively: a sentinel written after the
provider call returned is evidence, not proof (the crash window between
API-return and sentinel-write means its absence proves nothing and its
presence only witnesses the call returned). Only a durable provider receipt
reference upgrades to CONFIRMED; anything else converts to OUTCOME_UNKNOWN
for park/reconcile — never CONFIRMED (seam doc §4, Discord's sentinel).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterable, Optional

from .contract import DeliveryOutcome

EPOCH_FILE = "protocol-epoch"
DEFAULT_EPOCH = "A"


def read_epoch(root: Path) -> str:
    try:
        return (Path(root) / EPOCH_FILE).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return DEFAULT_EPOCH


def write_fence(root: Path, epoch: str) -> None:
    p = Path(root) / EPOCH_FILE
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(epoch, encoding="utf-8")
    os.replace(tmp, p)


def classify_legacy_sentinel(
        receipt_ref: Optional[str]) -> DeliveryOutcome:
    """Sentinel-mapping table (normative). receipt_ref is the provider's
    durable receipt reference when the legacy record carries one; a bare
    sentinel (written after the call, no receipt) is OUTCOME_UNKNOWN."""
    if receipt_ref:
        return DeliveryOutcome.CONFIRMED
    return DeliveryOutcome.OUTCOME_UNKNOWN


def convert_item_atomic(path: Path, render: Callable[[bytes], bytes],
                        is_converted: Callable[[bytes], bool],
                        fault: Optional[Callable[[str], None]] = None) -> bool:
    """The per-item conversion primitive: read -> write temp -> ONE
    os.replace over the SAME path. There is no unlink step and no second
    visible name, so no crash point can leave old and new both visible.
    Idempotent: an already-converted item is left untouched (False), which
    is what lets a restarted migrator resume mid-pass. `fault` is the
    fault-injection hook — called before each internal mutation with a
    step label; tests raise from it to crash INSIDE the item."""
    data = path.read_bytes()
    if is_converted(data):
        return False
    tmp = path.with_name(path.name + ".tmp")
    if fault:
        fault("pre-write-tmp")
    tmp.write_bytes(render(data))
    if fault:
        fault("pre-replace")
    os.replace(tmp, path)
    if fault:
        fault("post-replace")
    return True


def convert_epoch(root: Path, items: Iterable[str],
                  convert_one: Callable[[str], None], target_epoch: str,
                  crash_after: Optional[int] = None) -> int:
    """One-shot conversion pass. convert_one(item) must be per-item atomic
    over a single path (use convert_item_atomic — one os.replace, no
    unlink, no second visible name) and idempotent, so a restarted pass
    resumes over the same item list and completes BEFORE any drainer
    starts: the fence is written only after ALL items converted, and until
    then read_epoch still names the old protocol. crash_after=N simulates
    a crash before the (N+1)th item; intra-item crash points are the
    convert_item_atomic fault hook."""
    done = 0
    for item in items:
        if crash_after is not None and done >= crash_after:
            raise RuntimeError("simulated crash mid-conversion")
        convert_one(item)
        done += 1
    write_fence(root, target_epoch)
    return done


def import_a_state(root: Path) -> dict:
    """Idempotent A->C import of one outbox root, run in a QUIESCE window
    (no drainer of either protocol may be running).

    Mapping (ruling's table): READY/QUEUED unclaimed -> C ready/; PARKED ->
    C undelivered/ with A's reason; DELIVERED -> a C terminal record carrying
    A's persisted receipt (imported=True, no incarnation — there is no live
    claim to bind); any CLAIMED non-terminal item -> C undelivered/ as
    import-outcome-unknown (reconcile territory: the claim's fate is exactly
    as unknowable as a mid-delivery crash). Attempt counts carry over.

    Originals are PRESERVED: after every item converts and the per-state
    counts verify, .items is renamed to .items-migrated (rollback = rename
    back) and the epoch fence is written LAST — a crash anywhere earlier
    leaves .items and the A fence intact, and a re-run resumes.
    Returns per-state counts plus verified/fence flags.
    """
    import json as _json
    import time as _time

    from .backend_c import SEP, DesignCClaimBackend, _safe_key

    root = Path(root)
    items_dir = root / ".items"
    report = {"ready": 0, "parked": 0, "delivered": 0, "unknown": 0,
              "skipped": 0, "verified": False, "fenced": False}
    if not items_dir.is_dir():
        report["verified"] = True            # nothing to import is a clean state
        return report
    c = DesignCClaimBackend(root, activate=True)
    for f in sorted(items_dir.glob("*.json")):
        try:
            rec = _json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rec = None
        if not isinstance(rec, dict) or not rec.get("item_id"):
            report["unknown"] += 1           # unreadable A record: park by name
            key = _safe_key(f.stem)
            marker = c._d("undelivered") / f"{key}{SEP}import-unreadable{SEP}{_time.time_ns()}"
            if not marker.exists():
                marker.write_bytes(f.read_bytes() if f.exists() else b"")
            continue
        item_id = rec["item_id"]
        key = _safe_key(item_id)
        status = rec.get("status", "QUEUED")
        payload = rec.get("payload", "").encode("utf-8")
        n = int(rec.get("attempts", 0) or 0)
        if n:
            ap = c._attempts_path(key)
            if not ap.exists():
                ap.write_text(str(n))
        from . import contract as _contract  # noqa: F401 — outcome names below
        import ag2_sparrow.outbox as _outbox
        claim = _outbox.read_delivery_claim(root, item_id)
        if status == "DELIVERED":
            dst = c._terminal_path(key)
            if dst.exists():
                report["skipped"] += 1
                continue
            c._write_terminal(key, {
                "schema": 1, "item_id": item_id, "outcome": "confirmed",
                "receipt": {"provider": rec.get("provider"),
                             "destination": rec.get("destination")},
                "completed_ns": _time.time_ns(), "worker": "a-import",
                "attempts": n, "incarnation": None, "imported": True,
            }, f"a-import{SEP}{key}")
            report["delivered"] += 1
        elif status == "PARKED":
            reason = str(rec.get("reason") or "parked")[:40].replace(SEP, "-")
            marker = c._d("undelivered") / f"{key}{SEP}{reason}{SEP}import"
            if marker.exists():
                report["skipped"] += 1
            else:
                marker.write_bytes(payload)
                report["parked"] += 1
        elif claim is not None:
            marker = c._d("undelivered") / f"{key}{SEP}import-outcome-unknown{SEP}import"
            if marker.exists():
                report["skipped"] += 1
            else:
                marker.write_bytes(payload)
                report["unknown"] += 1
        else:
            if (c._d("ready") / key).exists():
                report["skipped"] += 1
            elif c.publish(item_id, payload):
                report["ready"] += 1
            else:
                report["skipped"] += 1       # tokens present: C already owns it
    # Verify by MEMBERSHIP: every A item is represented somewhere in C.
    missing = []
    for f in sorted(items_dir.glob("*.json")):
        try:
            rec = _json.loads(f.read_text(encoding="utf-8"))
            k = _safe_key(rec.get("item_id", f.stem))
        except (OSError, ValueError):
            k = _safe_key(f.stem)
        present = ((c._d("ready") / k).exists()
                   or c._terminal_path(k).exists()
                   or any(e.name.startswith(f"{k}{SEP}")
                          for e in c._d("undelivered").iterdir())
                   or any(e.name.startswith(f"{k}{SEP}")
                          for e in c._d("archive").iterdir())
                   or c._tokens(k))
        if not present:
            missing.append(k)
    if missing:
        report["missing"] = missing[:5]
        return report                        # fence NOT written; re-run resumes
    report["verified"] = True
    items_dir.rename(root / ".items-migrated")
    write_fence(root, "C")
    report["fenced"] = True
    return report
