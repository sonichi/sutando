#!/usr/bin/env python3
"""Characterization: pin the outbox ON-DISK FORMAT, not just its API behavior.

The Delivery Core refactor must leave the on-disk layout compatible, but a suite
that drives publish/claim/complete through the API passes just as happily against
a renamed directory. Measured by mutating one name at a time and running the six
existing outbox suites: only CLAIMS_DIR was caught, and only incidentally (one
test pre-creates `.claims` in setup, another globs it to locate a file).
LOCKS_DIR, ITEMS_DIR and STRIPES_FENCE were caught by nothing.

A first version of this file pinned only those directory names, and review
showed that was still too narrow: the stripe COUNT, the fence payload, the
stripe lock filename and the lifecycle item path could all move while every
assertion here stayed green. Those are pinned below too - the concurrency
protocol and the lifecycle layout are as much on-disk format as the paths.

Expected values are spelled out as literals. Comparing a constant to itself, or
rebuilding a name by calling the same helper that produces it, is a tautology
that survives every rename - the golden values below are what make a rename fail.

Run: python3 tests/outbox-disk-format.test.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# CI runs this with no arguments, so the repo root must be derived, not passed.
REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import outbox  # noqa: E402

# sha256(id)[:16], computed independently of _safe_key.
GOLDEN = {"item-1": "59908df50572502c", "a/b": "c14cddc033f64b9d", "a_b": "648fa9b31bc7ff7e"}


def test_on_disk_names_are_literal():
    """The four names that define the layout."""
    assert outbox.CLAIMS_DIR == ".claims", outbox.CLAIMS_DIR
    assert outbox.LOCKS_DIR == ".claim-locks", outbox.LOCKS_DIR
    assert outbox.ITEMS_DIR == ".items", outbox.ITEMS_DIR
    assert outbox.STRIPES_FENCE == "stripes-active.json", outbox.STRIPES_FENCE


def test_claim_path_shape():
    """<root>/.claims/<readable>.<sha256[:16]>.claim"""
    with tempfile.TemporaryDirectory() as tmp:
        p = outbox._claim_path(tmp, "item-1")
        assert p == Path(tmp) / ".claims" / "item-1.59908df50572502c.claim", p
        assert p.parent.name == ".claims", p.parent.name
        assert p.suffix == ".claim", p.suffix


def test_safe_key_stays_injective_across_the_lossy_sanitizer():
    """'a/b' and 'a_b' sanitize alike, so the digest is what keeps them apart.

    If this ever collides, two unrelated items share one claim file and one of
    them is silently denied delivery.
    """
    assert outbox._safe_key("a/b") == f"a_b.{GOLDEN['a/b']}", outbox._safe_key("a/b")
    assert outbox._safe_key("a_b") == f"a_b.{GOLDEN['a_b']}", outbox._safe_key("a_b")
    assert outbox._safe_key("a/b") != outbox._safe_key("a_b")


def test_stripe_fence_path_shape():
    """<root>/.claim-locks/stripes-active.json"""
    with tempfile.TemporaryDirectory() as tmp:
        p = outbox._fence_path(tmp)
        assert p == Path(tmp) / ".claim-locks" / "stripes-active.json", p


def test_claim_dir_is_created_where_the_constant_says():
    """Drive the production writer and assert the layout it actually made."""
    with tempfile.TemporaryDirectory() as tmp:
        assert outbox.acquire_delivery_claim(tmp, "item-1", drainer_id="d0")
        on_disk = {e.name for e in os.scandir(tmp) if e.is_dir()}
        assert ".claims" in on_disk, on_disk
        claims = {e.name for e in os.scandir(Path(tmp) / ".claims")}
        assert claims == {"item-1.59908df50572502c.claim"}, claims


# -- concurrency protocol: the stripe count is a migration, not a tuning knob ---

def test_lock_stripe_count_and_mapping():
    """LOCK_STRIPES is part of the on-disk contract: it decides which lock file
    an item takes, so two builds with different values stop excluding each other.
    """
    assert outbox.LOCK_STRIPES == 64, outbox.LOCK_STRIPES
    # sha256(id) % 64, computed independently of _lock_stripe.
    for item_id, expected in (("item-1", 51), ("a/b", 17), ("a_b", 34)):
        assert outbox._lock_stripe(item_id) == expected, (item_id, outbox._lock_stripe(item_id))


def test_fence_payload_as_written_by_the_production_writer():
    """<root>/.claim-locks/stripes-active.json holds {"stripes": 64} exactly.

    _stripe_mode refuses a fence declaring a different count, so the key name and
    the value are both load-bearing for a rolling upgrade.
    """
    with tempfile.TemporaryDirectory() as tmp:
        assert outbox.activate_lock_striping(tmp) is True
        fence = Path(tmp) / ".claim-locks" / "stripes-active.json"
        assert fence.is_file(), sorted(p.name for p in (Path(tmp) / ".claim-locks").iterdir())
        assert json.loads(fence.read_text()) == {"stripes": 64}, fence.read_text()


def test_stripe_lock_filename_is_zero_padded_two_digits():
    """stripe-NN.lock — the width is what makes two builds agree on one inode."""
    with tempfile.TemporaryDirectory() as tmp:
        outbox.activate_lock_striping(tmp)
        assert outbox.acquire_delivery_claim(tmp, "item-1", drainer_id="d0")
        locks = {p.name for p in (Path(tmp) / ".claim-locks").iterdir() if p.suffix == ".lock"}
        assert locks == {"stripe-51.lock"}, locks


def test_pre_migration_lock_filename_is_per_item():
    """Without the fence, the lock is <safe-key>.lock — the same inode a
    pre-striping build takes, which is what lets a rolling upgrade mix safely.
    """
    with tempfile.TemporaryDirectory() as tmp:
        assert outbox.acquire_delivery_claim(tmp, "item-1", drainer_id="d0")
        locks = {p.name for p in (Path(tmp) / ".claim-locks").iterdir() if p.suffix == ".lock"}
        assert locks == {"item-1.59908df50572502c.lock"}, locks


# -- item lifecycle records ----------------------------------------------------

def test_item_path_shape():
    """<root>/.items/<readable>.<sha256[:16]>.json"""
    with tempfile.TemporaryDirectory() as tmp:
        p = outbox._item_path(Path(tmp), "item-1")
        assert p == Path(tmp) / ".items" / "item-1.59908df50572502c.json", p


def test_lifecycle_file_written_by_the_production_writer():
    """Drive note_attempt/park_item and inspect what actually lands on disk."""
    with tempfile.TemporaryDirectory() as tmp:
        assert outbox.note_attempt(tmp, "item-1") == 1
        outbox.park_item(tmp, "item-1", reason="destination refused")
        items = {p.name for p in (Path(tmp) / ".items").iterdir()}
        assert items == {"item-1.59908df50572502c.json"}, items
        rec = json.loads((Path(tmp) / ".items" / "item-1.59908df50572502c.json").read_text())
        assert rec == {"item_id": "item-1", "attempts": 1,
                       "status": "PARKED", "reason": "destination refused"}, rec


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("PASS - outbox on-disk format pinned")
