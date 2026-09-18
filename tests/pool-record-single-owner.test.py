#!/usr/bin/env python3
"""The worker-pool completion record has ONE contract, and both sides bind it.

src/pool_record.py owns the recipient grammar, the workers-root layout, the
stage names and the record predicate. The pool's writer and the gateway reader
delegate to it; a second private regex or open/lstat predicate in either is the
drift this guard exists to catch (docs/architecture-boundaries.md, "Shared
adapter policy").

Run: python3 tests/pool-record-single-owner.test.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "skills" / "worker-pool" / "scripts"))

import pool_record  # noqa: E402
import pool_delivery  # noqa: E402

_BRIDGE = _REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "remote_gateway_bridge.py"
_VENDORED = _REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "pool_record.py"


def _worker_of_source() -> str:
    text = _BRIDGE.read_text(encoding="utf-8")
    start = text.index("def _worker_of(")
    return text[start:text.index("\ndef ", start + 1)]


class WriterBindsTheContract(unittest.TestCase):
    def test_the_writer_uses_the_shared_recipient_grammar(self):
        self.assertIs(pool_delivery.RECIPIENT, pool_record.RECIPIENT)

    def test_the_writer_predicate_is_the_shared_one(self):
        root = Path(tempfile.mkdtemp())
        flag = pool_delivery.mark_done(root, "worker-a", "task-1", published=True)
        self.assertTrue(pool_delivery.is_done_flag(flag))
        self.assertIs(pool_record.read_record_state(flag),
                      pool_record.RecordState.PRESENT)
        # Unreadable is NOT absent: both sides must raise, not answer False.
        if os.geteuid() != 0:
            os.chmod(flag, 0o000)
            try:
                with self.assertRaises(PermissionError):
                    pool_delivery.is_done_flag(flag)
                with self.assertRaises(PermissionError):
                    pool_record.read_record_state(flag)
            finally:
                os.chmod(flag, 0o600)

    def test_the_writer_path_is_the_shared_layout(self):
        root = Path("/ws")
        self.assertEqual(
            pool_delivery.done_flag(root, "worker-a", "task-1"),
            pool_record.record_path(pool_record.workers_root(root / "state"),
                                    "worker-a", "task-1", pool_record.DONE_STAGE))
        self.assertEqual(
            pool_delivery.pending_flag(root, "worker-a", "task-1"),
            pool_record.record_path(pool_record.workers_root(root / "state"),
                                    "worker-a", "task-1", pool_record.PENDING_STAGE))


class ReaderKeepsNoPrivateCopy(unittest.TestCase):
    def test_the_resolver_declares_no_predicate_of_its_own(self):
        src = _worker_of_source()
        # Positive control: the probe sees the literals it guards against.
        banned = ("lstat", "S_ISREG", "[a-z0-9]", '"workers"', "'workers'")
        control = ('os.lstat(root / n / "done")  # [a-z0-9] S_ISREG '
                   '"workers" + \'workers\'')
        for token in banned:
            self.assertIn(token, control, token)
            self.assertNotIn(token, src, f"_worker_of re-declares {token!r}")

    def test_the_resolver_reaches_the_contract_through_the_package_copy(self):
        self.assertIn("from . import pool_record",
                      _BRIDGE.read_text(encoding="utf-8"))
        self.assertIn("pool_record.", _worker_of_source())

    def test_the_package_never_names_the_skill(self):
        # The bundled copy is how the standalone package gets the contract
        # without importing an optional local skill.
        self.assertTrue(_VENDORED.exists())
        for p in (_VENDORED, _BRIDGE):
            self.assertNotIn("skills/worker-pool", p.read_text(encoding="utf-8"))


class TheGrammarIsBounded(unittest.TestCase):
    def test_only_writer_acceptable_names_are_recipients(self):
        for good in ("worker-a", "core-2", "w", "a" * 32):
            self.assertTrue(pool_record.is_recipient(good), good)
        for bad in ("Worker-NOT-WRITABLE", ".DS_Store", "README.txt", "-x",
                    "a/b", "", "a" * 33, None):
            self.assertFalse(pool_record.is_recipient(bad), bad)
            if isinstance(bad, str):
                with self.assertRaises(ValueError):
                    pool_record.require_recipient(bad)

    def test_a_non_directory_entry_is_not_a_recipient(self):
        root = Path(tempfile.mkdtemp()) / "workers"
        (root / "worker-a").mkdir(parents=True)
        (root / "worker-b").write_text("")
        (root / ".DS_Store").write_text("")
        self.assertEqual(pool_record.iter_recipients(root), ["worker-a"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
