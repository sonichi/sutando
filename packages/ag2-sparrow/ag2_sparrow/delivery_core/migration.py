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
