#!/usr/bin/env python3
"""The pin record's production writer: validated, bounded, atomic.

load_pins fails OPEN by design, so any torn intermediate on disk silently
becomes "no pins" — and a health tick in that window loses the restart veto
the pin exists to carry. The writer therefore must never let a reader observe
a partial snapshot: save_pins goes through a same-directory temp + os.replace.

The detector control proves a torn file IS detectable by the probe used here
(a truncated snapshot fails json.loads while load_pins returns []). The
concurrency drills then call the PRODUCTION writer from forked OS processes
and assert BOTH halves of the transaction: no reader ever observes a torn
snapshot, and every successful unique arm PERSISTS — os.replace alone keeps
snapshots whole while silently dropping concurrent updates, so snapshot
integrity without persistence is half a writer.

Run: python3 tests/process-pins-writer-atomicity.test.py
"""
from __future__ import annotations

import json
import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import process_pins  # noqa: E402

GOOD = {"service": "discord-bridge", "pid": "123",
        "lstart": "Sat Aug 23 12:24:57 2026",
        "reason": "witness armed", "expires_at": "2026-12-31T00:00:00Z"}


def _arm_one(path: str, worker: int) -> None:
    process_pins.arm_pin(path, f"svc-{worker}", str(1000 + worker),
                         f"lstart {worker}", f"worker {worker}",
                         "2026-12-31T00:00:00Z")


def _arm_release_cycles(path: str, worker: int, cycles: int) -> None:
    for i in range(cycles):
        process_pins.arm_pin(path, f"svc-{worker}", str(1000 + worker),
                             f"lstart {worker}", f"cycle {i}",
                             "2026-12-31T00:00:00Z")
        process_pins.release_pin(path, f"svc-{worker}", str(1000 + worker),
                                 lstart=f"lstart {worker}")


class WriterValidatesAndBounds(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pins-writer-"))
        self.path = self.tmp / "process-pins.json"

    def test_roundtrip_through_production_reader(self) -> None:
        process_pins.save_pins(self.path, [GOOD])
        self.assertEqual(process_pins.load_pins(self.path)[0]["service"],
                         "discord-bridge")

    def test_non_dict_pin_raises(self) -> None:
        with self.assertRaises(ValueError):
            process_pins.save_pins(self.path, ["not-a-dict"])

    def test_oversize_field_raises(self) -> None:
        with self.assertRaises(ValueError):
            process_pins.save_pins(self.path, [dict(GOOD, reason="x" * 501)])

    def test_arm_on_wrong_shape_valid_json_RAISES(self) -> None:
        # Valid JSON, wrong shape: json.loads succeeds, the shape check must raise.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('{"pins": "nope"}')
        with self.assertRaises(ValueError):
            process_pins.arm_pin(self.path, "svc", "9", "l", "r",
                                 "2026-12-31T00:00:00Z")
        self.assertEqual(self.path.read_text(), '{"pins": "nope"}')

    def test_missing_field_raises(self) -> None:
        bad = dict(GOOD); del bad["lstart"]
        with self.assertRaises(ValueError):
            process_pins.save_pins(self.path, [bad])

    def test_naive_or_garbage_expiry_raises(self) -> None:
        for exp in ("2026-12-31T00:00:00", "soon", ""):
            with self.assertRaises(ValueError):
                process_pins.save_pins(self.path, [dict(GOOD, expires_at=exp)])

    def test_bound_enforced(self) -> None:
        many = [dict(GOOD, pid=str(i)) for i in range(process_pins.MAX_PINS + 1)]
        with self.assertRaises(ValueError):
            process_pins.save_pins(self.path, many)

    def test_arm_replaces_same_identity_and_release_removes(self) -> None:
        process_pins.arm_pin(self.path, "svc", "9", "l1", "first",
                             "2026-12-31T00:00:00Z")
        process_pins.arm_pin(self.path, "svc", "9", "l2", "second",
                             "2026-12-31T00:00:00Z")
        pins = process_pins.load_pins(self.path)
        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["reason"], "second")
        self.assertEqual(
            process_pins.release_pin(self.path, "svc", "9", lstart="l2"), 1)
        self.assertEqual(process_pins.load_pins(self.path), [])

    def test_release_requires_lstart_and_survives_PID_REUSE(self) -> None:
        """A bare pid cannot tell a reused pid's NEW process from the pinned one."""
        process_pins.arm_pin(self.path, "svc", "77", "OLD lstart", "old witness",
                             "2099-01-01T00:00:00Z")
        process_pins.release_pin(self.path, "svc", "77", lstart="OLD lstart")
        process_pins.arm_pin(self.path, "svc", "77", "NEW lstart", "new witness",
                             "2099-01-01T00:00:00Z")
        # A stale cleanup carrying the OLD identity must remove NOTHING.
        self.assertEqual(
            process_pins.release_pin(self.path, "svc", "77", lstart="OLD lstart"), 0)
        pins = process_pins.load_pins(self.path)
        self.assertEqual([p["reason"] for p in pins], ["new witness"], pins)
        # A bare pid is refused outright rather than guessing.
        with self.assertRaises(ValueError):
            process_pins.release_pin(self.path, "svc", "77")
        # CONTROL: the CURRENT identity does remove it.
        self.assertEqual(
            process_pins.release_pin(self.path, "svc", "77", lstart="NEW lstart"), 1)
        self.assertEqual(process_pins.load_pins(self.path), [])
        # Service-wide release stays available as an EXPLICIT choice.
        process_pins.arm_pin(self.path, "svc", "78", "l", "r", "2099-01-01T00:00:00Z")
        self.assertEqual(process_pins.release_pin(self.path, "svc"), 1)

    def test_writers_refuse_a_malformed_ENTRY_leaving_bytes_untouched(self) -> None:
        """A wrong-shaped entry inside a well-formed list is still malformed."""
        for bad in ('{"pins": ["not-a-dict"]}',
                    '{"pins": [{"service": "svc", "pid": "1"}]}'):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(bad)
            with self.assertRaises(ValueError, msg=bad):
                process_pins.arm_pin(self.path, "svc", "9", "l", "r",
                                     "2099-01-01T00:00:00Z")
            self.assertEqual(self.path.read_text(), bad, "arm rewrote bad state")
            with self.assertRaises(ValueError, msg=bad):
                process_pins.release_pin(self.path, "svc", "9", lstart="l")
            self.assertEqual(self.path.read_text(), bad, "release rewrote bad state")

    def test_CONTROL_torn_snapshot_is_detectable_and_fails_open(self) -> None:
        process_pins.save_pins(self.path, [GOOD])
        whole = self.path.read_text()
        self.path.write_text(whole[: len(whole) // 2])   # emulate a torn write
        with self.assertRaises(json.JSONDecodeError):
            json.loads(self.path.read_text())            # probe CAN detect it
        self.assertEqual(process_pins.load_pins(self.path), [])  # reader fails open

    def test_16_simultaneous_unique_arms_ALL_persist(self) -> None:
        procs = [multiprocessing.Process(target=_arm_one,
                                         args=(str(self.path), w))
                 for w in range(16)]
        for pr in procs:
            pr.start()
        for pr in procs:
            pr.join()
            self.assertEqual(pr.exitcode, 0, "a writer process failed")
        pins = process_pins.load_pins(self.path)
        got = sorted(p["service"] for p in pins)
        self.assertEqual(got, sorted(f"svc-{w}" for w in range(16)),
                         "a successful arm was silently dropped by a racer")

    def test_concurrent_arm_release_cycles_stay_whole_and_serialized(self) -> None:
        procs = [multiprocessing.Process(target=_arm_release_cycles,
                                         args=(str(self.path), w, 25))
                 for w in range(4)]
        for pr in procs:
            pr.start()
        observations = 0
        while any(pr.is_alive() for pr in procs):
            if self.path.exists():
                raw = self.path.read_text()
                if raw == "":
                    self.fail("reader observed an EMPTY snapshot")
                data = json.loads(raw)   # torn write -> JSONDecodeError -> fail
                self.assertIsInstance(data.get("pins"), list)
                observations += 1
        for pr in procs:
            pr.join()
            self.assertEqual(pr.exitcode, 0, "a writer process failed")
        self.assertGreater(observations, 10,
                           "reader loop never actually raced the writers")
        # Every worker's last act was release: a serialized ledger ends empty.
        self.assertEqual(process_pins.load_pins(self.path), [])

    def test_arm_on_malformed_existing_state_RAISES_not_replaces(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json")
        with self.assertRaises(ValueError):
            process_pins.arm_pin(self.path, "svc", "9", "l", "r",
                                 "2026-12-31T00:00:00Z")
        self.assertEqual(self.path.read_text(), "{not json",
                         "a writer must not destroy state it cannot parse")


if __name__ == "__main__":
    unittest.main(verbosity=2)
