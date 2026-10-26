#!/usr/bin/env python3
"""outbox.receipt_state: a status only from a record that reads cleanly.

A caller deciding whether a claimed item may be sent again needs "absent" to mean
no record exists, never "the record could not be read". Pinned on the src copy and
the vendored copy the gateway imports.

Run: python3 tests/outbox-receipt-state.test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

import outbox as src_outbox  # noqa: E402
from ag2_sparrow import outbox as vendored_outbox  # noqa: E402

MODULES = (src_outbox, vendored_outbox)


class ReceiptState(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def record(self, mod) -> Path:
        mod.record_delivered(self.root, "proactive-1", provider="p")
        (path,) = (self.root / mod.ITEMS_DIR).iterdir()
        return path

    def test_absent_only_when_no_record_exists(self):
        for mod in MODULES:
            with self.subTest(mod=mod.__name__):
                self.assertEqual(mod.receipt_state(self.root / mod.__name__, "proactive-1"), "absent")

    def test_a_clean_record_reads_as_its_status(self):
        for mod in MODULES:
            with self.subTest(mod=mod.__name__):
                self.record(mod)
                self.assertEqual(mod.receipt_state(self.root, "proactive-1"), "DELIVERED")

    def test_torn_malformed_or_statusless_records_are_unknown(self):
        for mod in MODULES:
            root = self.root / f"torn-{mod.__name__}"
            mod.record_delivered(root, "proactive-1", provider="p")
            (path,) = (root / mod.ITEMS_DIR).iterdir()
            for body in ('{"status": "DELIV', "[1, 2]", '{"status": ""}', "{}", '{"status": "absent"}',
                         '{"status": "delivered"}', '{"status": 1}'):
                with self.subTest(mod=mod.__name__, body=body):
                    path.write_text(body)
                    self.assertEqual(mod.receipt_state(root, "proactive-1"), "unknown")

    def test_a_dangling_or_non_regular_entry_is_unknown_not_absent(self):
        for mod in MODULES:
            with self.subTest(mod=mod.__name__):
                path = self.record(mod)
                path.unlink()
                path.symlink_to(self.root / "nowhere")
                self.assertEqual(mod.receipt_state(self.root, "proactive-1"), "unknown")
                path.unlink()
                path.mkdir()
                self.assertEqual(mod.receipt_state(self.root, "proactive-1"), "unknown")
                path.rmdir()

    def test_a_root_that_is_not_a_directory_is_unknown_not_absent(self):
        for mod in MODULES:
            with self.subTest(mod=mod.__name__):
                root = self.root / f"file-{mod.__name__}"
                root.write_text("not a directory")
                self.assertEqual(mod.receipt_state(root, "proactive-1"), "unknown")

    def test_a_non_string_status_is_unknown_and_never_raises(self):
        for mod in MODULES:
            root = self.root / f"types-{mod.__name__}"
            mod.record_delivered(root, "proactive-1", provider="p")
            (path,) = (root / mod.ITEMS_DIR).iterdir()
            for body in ('{"status": []}', '{"status": {}}', '{"status": null}'):
                with self.subTest(mod=mod.__name__, body=body):
                    path.write_text(body)
                    self.assertEqual(mod.receipt_state(root, "proactive-1"), "unknown")

    def test_a_record_swapped_for_a_symlink_while_it_is_read_is_unknown(self):
        from unittest import mock
        for mod in MODULES:
            with self.subTest(mod=mod.__name__):
                root = self.root / f"swap-{mod.__name__}"
                mod.record_delivered(root, "proactive-1", provider="p")
                (path,) = (root / mod.ITEMS_DIR).iterdir()
                path.write_text('{"status": "QUEUED"}')
                elsewhere = self.root / f"elsewhere-{mod.__name__}.json"
                elsewhere.write_text('{"status": "DELIVERED"}')
                real_lstat, real_open, done = os.lstat, os.open, []

                def swap():
                    if not done:
                        done.append(1)
                        path.unlink()
                        path.symlink_to(elsewhere)

                def lstat(p, *a, **kw):
                    st = real_lstat(p, *a, **kw)
                    if str(p) == str(path):
                        swap()  # after the check
                    return st

                def open_(p, *a, **kw):
                    if str(p) == str(path):
                        swap()  # before the read
                    return real_open(p, *a, **kw)

                with mock.patch.object(os, "lstat", side_effect=lstat), \
                        mock.patch.object(os, "open", side_effect=open_):
                    got = mod.receipt_state(root, "proactive-1")
                self.assertTrue(done)
                self.assertEqual(got, "unknown")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs mkfifo")
    def test_a_fifo_record_is_unknown_and_never_blocks_the_reader(self):
        import subprocess
        for mod in MODULES:
            with self.subTest(mod=mod.__name__):
                path = self.record(mod)
                path.unlink()
                os.mkfifo(path)
                code = ("import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2]); "
                        f"from {mod.__name__} import receipt_state; from pathlib import Path; "
                        "print(receipt_state(Path(sys.argv[3]), 'proactive-1'))")
                try:
                    done = subprocess.run([sys.executable, "-c", code, str(REPO / "src"),
                                           str(REPO / "packages" / "ag2-sparrow"), str(self.root)],
                                          capture_output=True, text=True, timeout=5)
                except subprocess.TimeoutExpired:
                    path.unlink()
                    self.fail("receipt_state blocked opening a FIFO")
                path.unlink()
                self.assertEqual(done.stdout.strip(), "unknown", done.stderr)

    def test_without_a_no_follow_primitive_a_linked_record_is_unknown(self):
        for mod in MODULES:
            with self.subTest(mod=mod.__name__):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                self.root = Path(tmp.name)
                path = self.record(mod)
                real = path.with_name("real.json")
                path.rename(real)
                path.symlink_to(real)
                with mock.patch.object(os, "O_NOFOLLOW", 0, create=True):
                    self.assertEqual(mod.receipt_state(self.root, "proactive-1"), "unknown")

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-000 file")
    def test_an_unreadable_record_is_unknown_not_absent(self):
        for mod in MODULES:
            with self.subTest(mod=mod.__name__):
                path = self.record(mod)
                path.chmod(0)
                try:
                    self.assertEqual(mod.receipt_state(self.root, "proactive-1"), "unknown")
                finally:
                    path.chmod(0o644)


    def test_receipt_carries_the_resend_epoch_only_a_requeue_advances(self):
        for mod in MODULES:
            with self.subTest(mod=mod.__name__):
                root = self.root / mod.__name__
                self.assertEqual(mod.receipt(root, "proactive-1"), ("absent", None))
                mod._write_item(root, "proactive-1", {"item_id": "proactive-1", "status": "PARKED", "payload": "x"})
                self.assertEqual(mod.receipt(root, "proactive-1"), ("PARKED", 0))
                self.assertEqual(mod.requeue_item_with_epoch(root, "proactive-1")[0], mod.RequeueOutcome.REQUEUED)
                self.assertEqual(mod.receipt(root, "proactive-1"), ("QUEUED", 1))
                for bad in ("1", -1, True, None):
                    mod._write_item(root, "proactive-1", {"item_id": "proactive-1", "status": "QUEUED",
                                                          "resend_epoch": bad})
                    self.assertEqual(mod.receipt(root, "proactive-1"), ("QUEUED", None), f"epoch {bad!r}")
                mod._item_path(root, "proactive-1").write_text('{"status": "QUEU')
                self.assertEqual(mod.receipt(root, "proactive-1"), ("unknown", None))
                path = mod._item_path(root, "proactive-1")
                path.unlink()
                os.mkfifo(path)  # O_NONBLOCK opens it at once, so no deadline is needed in-process
                self.assertEqual(mod.receipt(root, "proactive-1"), ("unknown", None))

if __name__ == "__main__":
    unittest.main(verbosity=1)
