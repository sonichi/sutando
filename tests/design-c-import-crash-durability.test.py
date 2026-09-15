#!/usr/bin/env python3
"""The A->C importer must publish by rename. Writing a final name in place
leaves a truncated file that every `if not path.exists()` idempotence check
reads as already-written, so the crashed item is never repaired."""
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow.delivery_core import migration as mig  # noqa: E402
from ag2_sparrow.delivery_core.backend_c import (  # noqa: E402
    DesignCClaimBackend, _safe_key)
from ag2_sparrow.delivery_core.backend_a import DesignAClaimBackend  # noqa: E402
from ag2_sparrow.delivery_core.contract import DeliveryOutcome  # noqa: E402

failures = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


def _a_with_attempts(root):
    a = DesignAClaimBackend(root)
    a.publish("tried-1", b"try")
    tok = a.claim("tried-1", "w0")
    a.complete(tok, DeliveryOutcome.NOT_DELIVERED)   # attempts=1, back to ready
    return a


# --- 1. a crash-truncated attempts file is repaired, not accepted ------------
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    _a_with_attempts(root)
    c = DesignCClaimBackend(root, activate=True)
    key = _safe_key("tried-1")
    ap = c._attempts_path(key)
    ap.parent.mkdir(parents=True, exist_ok=True)
    ap.write_text("")                    # the crashed write sonichi reproduced

    rep = mig.import_a_state(root)
    check(rep.get("verified") is True, f"import still verifies ({rep})")
    check(ap.read_text().strip() == "1",
          f"truncated attempts file repaired (holds {ap.read_text()!r}, want '1')")

# --- 2. a crash inside the write publishes nothing at the final name ---------
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("parked-1", b"held")
    a.park("parked-1", "operator-hold")

    real_replace = mig.os.replace
    crashed = {}

    def crash_once(src, dst):
        # Crash where a direct write would already have published a partial
        # file. Scoped to the marker: activation also replaces.
        if not crashed and "undelivered" in Path(dst).parts:
            crashed["dst"] = Path(dst)
            raise RuntimeError("simulated crash before publish")
        return real_replace(src, dst)

    mig.os.replace = crash_once
    try:
        mig.import_a_state(root)
    except RuntimeError:
        pass
    finally:
        mig.os.replace = real_replace

    dst = crashed.get("dst")
    check(dst is not None, "the import staged a write we could interrupt")
    if dst is not None:
        check(not dst.exists(),
              f"nothing published at the final name {dst.name} after the crash")

    # The retry must complete the item rather than read the crash as done.
    rep2 = mig.import_a_state(root)
    check(rep2.get("verified") is True and rep2.get("fenced") is True,
          f"re-run after the crash completes the migration ({rep2})")
    if dst is not None:
        check(dst.exists() and dst.read_bytes() == b"held",
              "the retry published the full payload")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
