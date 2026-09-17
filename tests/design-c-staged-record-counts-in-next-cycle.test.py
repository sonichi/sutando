#!/usr/bin/env python3
"""A staged terminal record must count toward the next cycle number.

`_next_cycle`'s docstring promises "the archive PLUS any staged record not yet
finalized IS the ledger". The staged half is reachable only because a claim
incarnation begins with the key: `_write_terminal` stages as
`terminal~<incarnation>~<ns>.json` and the scan globs that by key. Nothing here
pinned that, so a mutation making the staged scan inert survived.

Written against ordinary claims only — the importer's stand-in incarnation is a
later layer's concern and its pin lives with it.
"""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow.delivery_core.backend_c import (  # noqa: E402
    SEP, DesignCClaimBackend, _safe_key)

failures = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


def _cycle_after_staging(root, key, incarnation) -> int:
    """Stage a record holding cycle=7 and ask what _next_cycle returns."""
    c = DesignCClaimBackend(root, activate=True)
    (c._d("tmp") / f"terminal{SEP}{incarnation}{SEP}999.json").write_text('{"cycle": 7}')
    return c._next_cycle(key)


with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    key = _safe_key("item-42")
    c = DesignCClaimBackend(root, activate=True)
    c.publish("item-42", b"x")
    tok = c.claim("item-42", "w0")

    check(tok.incarnation.startswith(key + SEP),
          "a claim incarnation begins with the key — what makes the scan reachable")
    got = _cycle_after_staging(root, key, tok.incarnation)
    check(got == 8,
          f"a staged record COUNTS: cycle 7 staged -> next is 8 (got {got})")

with tempfile.TemporaryDirectory() as td:
    # NEGATIVE CONTROL. Without it, a _next_cycle that returned 8 for anything
    # would satisfy the assertion above and the staged scan could still be inert.
    root = Path(td) / "root"
    key = _safe_key("item-42")
    got = _cycle_after_staging(root, key, f"not-the-key{SEP}{key}")
    check(got == 1,
          f"a record the scan cannot match does NOT count (got {got})")

with tempfile.TemporaryDirectory() as td:
    # And an empty store must not already answer 8 by accident.
    root = Path(td) / "root"
    c = DesignCClaimBackend(root, activate=True)
    check(c._next_cycle(_safe_key("item-42")) == 1,
          "an empty store starts at cycle 1")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
