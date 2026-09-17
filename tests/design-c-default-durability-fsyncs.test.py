#!/usr/bin/env python3
"""The DEFAULT-constructed backend fsyncs the terminal record before its rename.

Every existing durability assertion passes the mode explicitly, so mutating the
constructor default from "default" to "lax" survived: correct behaviour with
nothing holding it in place. These drive a default construction and assert the
ORDER, which is the property that makes the fsync worth anything.
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow.delivery_core import (  # noqa: E402
    backend_c as bc)
from ag2_sparrow.delivery_core.backend_c import (  # noqa: E402
    DesignCClaimBackend)
from ag2_sparrow.delivery_core.contract import (  # noqa: E402
    DeliveryOutcome)

failures = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


def _trace(durability):
    """Complete one item, returning the ordered fsync/rename call log."""
    calls = []
    real_fsync, real_rename = os.fsync, os.rename

    def fsync(fd):
        calls.append("fsync")
        return real_fsync(fd)

    def rename(a, b):
        calls.append("rename" if "archive" in str(b) else "rename-other")
        return real_rename(a, b)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "root"
        kw = {} if durability is None else {"durability": durability}
        c = DesignCClaimBackend(root, activate=True, **kw)
        c.publish("item-1", b"payload")
        tok = c.claim("item-1", "w0")
        bc.os.fsync, bc.os.rename = fsync, rename
        try:
            c.complete(tok, DeliveryOutcome.CONFIRMED, provider="p", destination="d")
        finally:
            bc.os.fsync, bc.os.rename = real_fsync, real_rename
    return calls


default_calls = _trace(None)          # DEFAULT construction — no durability kwarg
check("fsync" in default_calls,
      f"a default-constructed backend fsyncs the record ({default_calls})")
check("rename" in default_calls,
      f"the record is renamed into archive/ ({default_calls})")
check(default_calls.index("fsync") < default_calls.index("rename"),
      "the fsync happens BEFORE the archive rename — order, not just presence")

# NEGATIVE CONTROL. Without it, an implementation that fsynced unconditionally
# would satisfy every assertion above and the default would still be untested.
lax_calls = _trace("lax")
check("fsync" not in lax_calls,
      f"lax does NOT fsync — so the assertions above are about the DEFAULT ({lax_calls})")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
