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
import stat
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "skills" / "worker-pool" / "scripts"))
sys.path.insert(0, str(_REPO / "tests" / "_helpers"))

import pool_record  # noqa: E402
import pool_delivery  # noqa: E402

from deadline import deadline  # noqa: E402

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

    def test_an_unknown_stage_is_refused(self):
        with self.assertRaises(ValueError):
            pool_record.record_path("/ws", "worker-a", "task-1", "done")
        for stage in pool_record.STAGES:
            self.assertTrue(str(pool_record.record_path(
                "/ws", "worker-a", "task-1", stage)).endswith("." + stage))

    def test_an_absent_record_reads_absent_and_an_unreadable_one_raises(self):
        root = Path(tempfile.mkdtemp())
        self.assertIs(pool_record.read_record_state(root / "nope"),
                      pool_record.RecordState.ABSENT)
        d = root / "sub"
        d.mkdir()
        self.assertIs(pool_record.read_record_state(d),
                      pool_record.RecordState.MALFORMED)

    def test_a_special_file_classifies_without_waiting_on_the_open(self):
        """The probe opens before it can classify, so the open must not be able
        to block: a FIFO has no writer and a blocking one would wait forever."""
        root = Path(tempfile.mkdtemp())
        regular = root / "regular"
        regular.write_text("")
        with deadline(5.0, "read_record_state over a regular file"):
            self.assertIs(pool_record.read_record_state(regular),
                          pool_record.RecordState.PRESENT)   # control
        fifo = root / "fifo"
        os.mkfifo(str(fifo))
        self.assertTrue(stat.S_ISFIFO(os.lstat(fifo).st_mode))
        with deadline(5.0, "read_record_state over a FIFO"):
            self.assertIs(pool_record.read_record_state(fifo),
                          pool_record.RecordState.MALFORMED)

    def test_an_entry_that_cannot_be_typed_is_left_for_the_record_probe(self):
        """is_dir() failing does not PROVE the entry is not a recipient, so it
        stays in the list and the record probe decides (and fails closed)."""
        class _Entry:
            name = "worker-a"

            def is_dir(self, follow_symlinks=True):
                raise PermissionError("denied")

            def is_symlink(self):
                raise PermissionError("denied")

        class _Scan:
            def __enter__(self):
                return iter([_Entry()])

            def __exit__(self, *a):
                return False

        with unittest.mock.patch.object(pool_record.os, "scandir",
                                        return_value=_Scan()):
            self.assertEqual(pool_record.iter_recipients("/ws"), ["worker-a"])

    def test_a_non_directory_entry_is_not_a_recipient(self):
        root = Path(tempfile.mkdtemp()) / "workers"
        (root / "worker-a").mkdir(parents=True)
        (root / "worker-b").write_text("")
        (root / ".DS_Store").write_text("")
        self.assertEqual(pool_record.iter_recipients(root), ["worker-a"])

    def test_a_recipient_named_symlink_is_malformed_state_not_a_stray(self):
        # The writer follows an alias, so a record under the target could wear
        # this name; skipping it hands the target a unique claim — abstain instead.
        root = Path(tempfile.mkdtemp()) / "workers"
        (root / "worker-b").mkdir(parents=True)
        (root / "worker-a").symlink_to(root / "worker-b")
        with self.assertRaises(pool_record.RecipientAliasError) as cm:
            pool_record.iter_recipients(root)
        self.assertIsInstance(cm.exception, OSError, "an alias must abstain like any unreadable state")
        # A symlink with a name the writer would refuse is still merely not a recipient.
        (root / "worker-a").unlink()
        (root / "Not-A-Recipient").symlink_to(root / "worker-b")
        self.assertEqual(pool_record.iter_recipients(root), ["worker-b"])

    def test_require_own_dir_accepts_absent_and_real_and_refuses_an_alias(self):
        root = Path(tempfile.mkdtemp())
        self.assertEqual(pool_record.require_own_dir(root / "absent"), root / "absent")
        (root / "real").mkdir()
        self.assertEqual(pool_record.require_own_dir(root / "real"), root / "real")
        (root / "alias").symlink_to(root / "real")
        with self.assertRaises(pool_record.RecipientAliasError):
            pool_record.require_own_dir(root / "alias")


if __name__ == "__main__":
    unittest.main(verbosity=2)
