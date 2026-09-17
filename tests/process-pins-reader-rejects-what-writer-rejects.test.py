#!/usr/bin/env python3
"""What the production writer refuses to store must not arm a veto when read.

load_pins() fails OPEN because a pin only ever SUPPRESSES a restart prescription
— so a broken record must suppress nothing. It checked only the outer JSON shape
and `isinstance(entry, dict)`, so a schema-invalid entry (no `reason`, no
`expires_at`, a non-numeric pid) reached evaluate() and became ARMED, vetoing
recovery on a record the writer would have rejected. The documented bound
MAX_PINS was likewise never applied on the read side.

Every negative here is paired with the writer's own verdict on the same entry,
so the two sides are asserted to agree rather than asserted separately, and with
a positive control proving a VALID pin still arms through the same call.

Run: python3 tests/process-pins-reader-rejects-what-writer-rejects.test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import process_pins  # noqa: E402

LSTART = "Mon Aug 25 00:00:00 2026"
LIVE = {"7001": LSTART}
NOW = 0.0
VALID = {"service": "discord-bridge", "pid": "7001", "lstart": LSTART,
         "reason": "branch-only witness in flight",
         "expires_at": "2099-01-01T00:00:00Z"}


def _write(pins: list) -> Path:
    d = Path(tempfile.mkdtemp(prefix="pins-reader-"))
    p = d / "process-pins.json"
    p.write_text(json.dumps({"pins": pins}))
    return p


def _armed(path) -> list:
    return [v for v, _pin, _d in process_pins.evaluate(
        process_pins.load_pins(path), "discord-bridge", LIVE, NOW)
        if v == process_pins.ARMED]


def _writer_rejects(pin) -> str:
    try:
        process_pins._validated(pin)
    except (ValueError, TypeError) as exc:
        return str(exc)
    return ""


# POSITIVE CONTROL FIRST: a valid pin still arms through load_pins().
assert not _writer_rejects(VALID), "fixture is not writer-valid; the negatives below prove nothing"
assert _armed(_write([VALID])) == [process_pins.ARMED], (
    "a valid pin no longer arms — the reader now over-rejects")

# NEGATIVES: each is a record the writer refuses. The reader must agree.
BAD = {
    "missing reason": {k: v for k, v in VALID.items() if k != "reason"},
    "missing expires_at": {k: v for k, v in VALID.items() if k != "expires_at"},
    "blank lstart": {**VALID, "lstart": "   "},
    "non-numeric pid": {**VALID, "pid": "not-a-pid"},
    "naive expires_at": {**VALID, "expires_at": "2099-01-01T00:00:00"},
    "oversized reason": {**VALID, "reason": "x" * 501},
}
for label, pin in BAD.items():
    why = _writer_rejects(pin)
    assert why, f"{label}: the writer accepts this, so it is not a malformed-state case"
    assert _armed(_write([pin])) == [], (
        f"{label}: reader armed a veto the writer rejects ({why})")

# A malformed entry must not take a VALID sibling down with it.
mixed = _write([{k: v for k, v in VALID.items() if k != "reason"}, VALID])
assert _armed(mixed) == [process_pins.ARMED], (
    "one malformed entry suppressed a well-formed sibling pin")

# MAX_PINS is a documented bound; past it the record is out of contract.
over = _write([{**VALID, "pid": str(7001 + i)} for i in range(process_pins.MAX_PINS + 1)])
assert _armed(over) == [], "a record past MAX_PINS still armed"
at_bound = _write([VALID] + [{**VALID, "pid": str(8000 + i)}
                             for i in range(process_pins.MAX_PINS - 1)])
assert process_pins.ARMED in _armed(at_bound), (
    "a record AT the bound was rejected — the bound is off by one")

print(f"PASS — the reader rejects all {len(BAD)} entry shapes its writer rejects "
      f"and honours MAX_PINS={process_pins.MAX_PINS}; a valid pin still arms")
