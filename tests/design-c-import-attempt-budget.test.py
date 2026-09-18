#!/usr/bin/env python3
"""The imported attempt budget must equal A's current count, and an imported
terminal must not leave one live. C retires a budget with its cycle, so a
republished item inheriting a spent count parks on its first transient
failure."""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow.delivery_core import (  # noqa: E402
    migration as mig)
from ag2_sparrow.delivery_core.backend_a import (  # noqa: E402
    DesignAClaimBackend)
from ag2_sparrow.delivery_core.backend_c import (  # noqa: E402
    DesignCClaimBackend, _safe_key)
from ag2_sparrow.delivery_core.contract import (  # noqa: E402
    DeliveryOutcome)

failures = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


def _rollback(root):
    (root / ".items-migrated").rename(root / ".items")
    mig.write_fence(root, "A")


def _fail_once(a, item_id):
    tok = a.claim(item_id, "w0")
    a.complete(tok, DeliveryOutcome.NOT_DELIVERED)


# --- VALID-BUT-WRONG: the file parses, and holds another cycle's number ----
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("budget-1", b"body")
    _fail_once(a, "budget-1")
    _fail_once(a, "budget-1")
    check(a.attempts("budget-1") == 2, "A holds 2 attempts before the import")
    mig.import_a_state(root)
    check(DesignCClaimBackend(root).attempts("budget-1") == 2,
          "the first import carries A's count across")

    _rollback(root)
    for f in (root / ".items").glob("budget-1.*.json"):
        f.unlink()
    a2 = DesignAClaimBackend(root)
    a2.publish("budget-1", b"body")          # fresh cycle, one failure
    _fail_once(a2, "budget-1")
    check(a2.attempts("budget-1") == 1, "A's fresh cycle holds 1 attempt")

    mig.import_a_state(root)
    got = DesignCClaimBackend(root).attempts("budget-1")
    check(got == 1,
          f"C is re-bound to A's current count, not the stale file (got {got})")

# --- FRESH CYCLE: an imported terminal must not leave a live budget -------
def _first_failure_after(root, item_id):
    """Republish in C and take one transient failure; report what C decided."""
    c = DesignCClaimBackend(root, activate=True)
    c.publish(item_id, b"redelivery")
    tok = c.claim(item_id, "w0")
    c.complete(tok, DeliveryOutcome.NOT_DELIVERED, park_at_attempts=3)
    key = _safe_key(item_id)
    parked = [f.name for f in c._d("undelivered").iterdir()
              if f.name.startswith(key) and "max-attempts" in f.name]
    return c.attempts(item_id), (c._d("ready") / key).exists(), parked


with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    a = DesignAClaimBackend(root)
    a.publish("done-b", b"body")
    _fail_once(a, "done-b")
    _fail_once(a, "done-b")
    tok = a.claim("done-b", "w0")
    a.complete(tok, DeliveryOutcome.CONFIRMED, provider="p1", destination="d1")
    r = mig.import_a_state(root)
    check(r.get("delivered") == 1, f"the terminal is imported ({r})")
    check(DesignCClaimBackend(root).attempts("done-b") == 0,
          "the imported terminal leaves no live attempt budget")
    rec = DesignCClaimBackend(root).terminal_record("done-b")
    check(rec.get("attempts") == 2,
          "the count is still RECORDED in the terminal, only the budget dies")

    n, ready, parked = _first_failure_after(root, "done-b")
    check((n, ready, parked) == (1, True, []),
          f"a fresh cycle's first failure retries (attempts={n} "
          f"ready={ready} parked={parked})")

# --- CLEAN-C CONTROL: same sequence with no import at all ----------------
# Without this, the assertion above passes for a C that never parks anything.
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    n, ready, parked = _first_failure_after(root, "done-b")
    check((n, ready, parked) == (1, True, []),
          f"clean C behaves identically (attempts={n} ready={ready})")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
