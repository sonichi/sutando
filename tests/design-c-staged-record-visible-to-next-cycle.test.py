#!/usr/bin/env python3
"""A staged terminal record must be visible to the scan that reads it.

`_write_terminal` stages as `terminal~<incarnation>~<ns>.json` and `_next_cycle`
globs that by KEY, so only a key-leading incarnation is ever seen. A real claim
incarnation starts with the key; the importer's stand-in did not, so its staged
record was invisible in the window before the rename into archive/.
"""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow.delivery_core import (  # noqa: E402
    migration as mig)
from ag2_sparrow.delivery_core.backend_c import (  # noqa: E402
    SEP, DesignCClaimBackend, _safe_key)

failures = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


def _staged_is_seen(root, key, incarnation) -> bool:
    """Stage a record under `incarnation` and ask _next_cycle whether it counts."""
    c = DesignCClaimBackend(root, activate=True)
    tmp = c._d("tmp") / f"terminal{SEP}{incarnation}{SEP}999.json"
    tmp.write_text('{"cycle": 7}')
    return c._next_cycle(key) == 8      # 7 seen -> next is 8; unseen -> 1


with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    key = _safe_key("item-42")

    # A REAL claim incarnation — the positive control. If this fails the scan is
    # broken for everyone and the importer case below proves nothing.
    c = DesignCClaimBackend(root, activate=True)
    c.publish("item-42", b"x")
    tok = c.claim("item-42", "worker-7")
    check(tok.incarnation.startswith(key + SEP),
          "a real claim incarnation starts with the key")
    check(_staged_is_seen(root, key, tok.incarnation),
          "a record staged under a real claim incarnation IS seen by _next_cycle")

with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    key = _safe_key("item-42")
    check(_staged_is_seen(root, key, mig._pseudo_incarnation(key)),
          "the importer's pseudo-incarnation is seen too")

with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    key = _safe_key("item-42")
    # The shape that was shipped. Kept as a NEGATIVE control: without it, an
    # implementation that saw everything would pass every assertion above.
    check(not _staged_is_seen(root, key, f"a-import{SEP}{key}"),
          "a non-key-leading incarnation is NOT seen — the defect, pinned")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
