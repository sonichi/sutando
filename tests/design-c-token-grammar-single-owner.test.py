#!/usr/bin/env python3
"""A five-part token with a non-numeric pid is NOT C ownership.

`recover()` and `_live_and_dead()` both skip a token whose pid component is not
a digit — permanently, so such a name can never be recovered or retired. The
A->C importer checked ARITY only, so it counted that same name as proof C owns
the item, fenced the root, and left the A payload with no claimable or
recoverable representation.

One predicate now, `is_producer_token`, used by all three. This pins that the
three agree, and that arity alone is not enough.

Run: python3 tests/design-c-token-grammar-single-owner.test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow.delivery_core import backend_c as bc          # noqa: E402
from ag2_sparrow.delivery_core import migration as mig         # noqa: E402

SEP = bc.SEP
failures = []


def check(cond, label):
    print(f"{'ok' if cond else 'FAIL'}: {label}")
    if not cond:
        failures.append(label)


def tok(*parts):
    return SEP.join(parts)

KEY = "k=0123456789abcdef"
GOOD = tok(KEY, "worker", "4321", "birth", "nanos")
# kewei's exact case: five parts, pid is not a number.
BAD_PID = tok(KEY, "worker", "notapid", "birth", "nanos")
SHORT = tok(KEY, "worker", "4321")

check(len(GOOD.split(SEP)) == bc.TOKEN_PARTS,
      "fixture GOOD has producer arity (else the test proves nothing)")
check(len(BAD_PID.split(SEP)) == bc.TOKEN_PARTS,
      "fixture BAD_PID has producer arity — it fails on the PID, not the shape")

check(bc.is_producer_token(GOOD), "GOOD is producer-valid")
check(not bc.is_producer_token(BAD_PID), "five parts + non-numeric pid is NOT producer-valid")
check(not bc.is_producer_token(SHORT), "wrong arity is not producer-valid")

# The importer must reach the SAME verdict as C's recovery — that agreement is
# the property, not any particular spelling of it.
src_mig = (REPO / "packages/ag2-sparrow/ag2_sparrow/delivery_core/migration.py").read_text()
check("is_producer_token" in src_mig,
      "migration consults the shared predicate")
check("TOKEN_PARTS" not in src_mig,
      "migration no longer carries its own arity-only rule")

src_bc = (REPO / "packages/ag2-sparrow/ag2_sparrow/delivery_core/backend_c.py").read_text()
check(src_bc.count("parts[2].isdigit()") == 1,
      "the pid rule is stated ONCE (inside the predicate), not copied per reader")

# Control: the predicate must be able to REJECT. A rule that says yes to
# everything would pass every assertion above except this one.
check(not bc.is_producer_token(tok(KEY, "w", "", "b", "n")),
      "control: an EMPTY pid is rejected — the predicate can say no")
check(bc.is_producer_token(tok(KEY, "w", "0", "b", "n")),
      "control: pid '0' is accepted — rejection is not blanket")

# Behavioural: the importer's VERDICT must change, not just its source. An A
# item whose only C evidence is a five-part token with a non-numeric pid.

import tempfile  # noqa: E402
from ag2_sparrow.delivery_core.backend_a import DesignAClaimBackend  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "root"
    DesignAClaimBackend(root).publish("stranded-1", b"payload")
    key = bc._safe_key("stranded-1")
    inflight = root / "inflight"
    inflight.mkdir(parents=True, exist_ok=True)
    (inflight / tok(key, "worker", "notapid", "birth", "nanos")).write_text("")
    report = mig.import_a_state(root)
    check(not report.get("fenced"),
          "a bad-pid token does NOT fence the root (kewei: was verified/fenced)")
    check("missing" in report and any(key in m for m in report["missing"]),
          "the item is reported MISSING rather than represented in C")
    check(report.get("malformed_tokens"),
          "the bogus token is named in the report, not silently dropped")

# kewei r? P3: str.isdigit() is a wider set than what the writer emits AND a
# wider set than what int() accepts, and the two disagreements differ.
_SUP2, _ARABIC3 = "\u00b2", "\u0663"
check(not bc.is_producer_token(tok("k", "w", _SUP2, "1", "x")),
      "a superscript-digit pid is rejected (isdigit true, int() raises)")
check(not bc.is_producer_token(tok("k", "w", _ARABIC3, "1", "x")),
      "a non-ASCII decimal pid is rejected (int() accepts it, the writer "
      "cannot emit it)")
check(bc.is_producer_token(tok("k", "w", "4242", "1", "x")),
      "positive control: an ordinary pid is still a producer token")

print(f"\n{'OK' if not failures else 'FAILED'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
