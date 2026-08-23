#!/usr/bin/env python3
"""Failure-path contracts for bounded terminal-receipt cleanup."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import outbox  # noqa: E402


def _same_shard_ids(prefix: str) -> tuple[str, str]:
    first = f"{prefix}-a"
    shard = outbox._terminal_digest(first, 0)[:2]
    for index in range(10_000):
        second = f"{prefix}-b-{index}"
        if outbox._terminal_digest(second, 0).startswith(shard):
            return first, second
    raise AssertionError("could not find a same-shard receipt id")


def _write_receipt(root: Path, item_id: str, recorded_at: float = 10.0) -> Path:
    path = outbox._terminal_receipt_path(root, item_id, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = outbox._terminal_payload(
        item_id, 0, outbox.TerminalDisposition.DELIVERED, recorded_at)
    path.write_bytes(outbox._terminal_bytes(payload))
    return path


def _cleanup(root: Path, path: Path, *, clock: float = 20.0,
             ttl: float = 100.0, max_records: int = 10):
    shard_name = path.parent.name
    with outbox._item_lock(root, outbox._terminal_shard_lock_id(shard_name)):
        return outbox._cleanup_terminal_receipt_shard_locked(
            root, shard_name, clock, ttl, max_records, scan_all=True)


class TestBoundedScan(unittest.TestCase):
    def test_truncated_scan_refuses_same_shard_publication(self):
        first, target = _same_shard_ids("scan-bound")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(outbox, "TERMINAL_RECEIPT_MAX_RECORDS", 256), \
                mock.patch.object(outbox, "TERMINAL_RECEIPT_SWEEP_BATCH", 1):
            root = Path(tmp)
            shard = outbox._terminal_receipt_path(root, first, 0).parent
            shard.mkdir(parents=True)
            (shard / "junk-a").write_text("a")
            (shard / "junk-b").write_text("b")

            refused = outbox.record_terminal_receipt(
                root, target, outbox.TerminalDisposition.DELIVERED, now=20.0)

            self.assertIs(refused.state, outbox.TerminalReceiptState.UNKNOWN)
            self.assertFalse(outbox._terminal_receipt_path(root, target, 0).exists())
            self.assertEqual(sorted(path.name for path in shard.iterdir()),
                             ["junk-a", "junk-b"])


class TestTemporaryEntries(unittest.TestCase):
    def _temp(self, root: Path, item_id: str) -> Path:
        digest = outbox._terminal_digest(item_id, 0)
        shard = outbox._terminal_receipt_shard(root, digest)
        shard.mkdir(parents=True, exist_ok=True)
        path = shard / f".{digest}.orphan.tmp"
        path.write_text("partial")
        return path

    def test_stale_temp_is_removed_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._temp(root, "temp-removed")

            report = _cleanup(root, path)

            self.assertFalse(path.exists())
            self.assertEqual(report.stale_temps, 1)
            self.assertEqual(report.unknown, 0)
            self.assertFalse(report.incomplete)

    def test_temp_disappearing_before_stat_is_a_clean_race(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._temp(root, "temp-vanished")
            real_lstat = Path.lstat

            def vanished(candidate):
                if candidate == path:
                    path.unlink()
                    raise FileNotFoundError(path)
                return real_lstat(candidate)

            with mock.patch.object(Path, "lstat", vanished):
                report = _cleanup(root, path)

            self.assertFalse(path.exists())
            self.assertEqual(report.stale_temps, 0)
            self.assertEqual(report.unknown, 0)
            self.assertFalse(report.incomplete)

    def test_unstatable_temp_is_preserved_and_marks_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._temp(root, "temp-unreadable")
            real_lstat = Path.lstat

            def unreadable(candidate):
                if candidate == path:
                    raise PermissionError("injected")
                return real_lstat(candidate)

            with mock.patch.object(Path, "lstat", unreadable):
                report = _cleanup(root, path)

            self.assertTrue(path.exists())
            self.assertEqual(report.unknown, 1)
            self.assertTrue(report.incomplete)


class TestEntryRaces(unittest.TestCase):
    def test_unrelated_entries_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shard = outbox._terminal_receipts_dir(root) / "00"
            shard.mkdir(parents=True)
            junk = shard / "not-a-receipt"
            wrong = shard / f"{'f' * 64}.json"
            junk.write_text("junk")
            wrong.write_text("wrong shard")

            report = _cleanup(root, junk)

            self.assertEqual(report, outbox.TerminalReceiptCleanup())
            self.assertTrue(junk.exists())
            self.assertTrue(wrong.exists())

    def test_receipt_disappearing_before_stat_is_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = _write_receipt(root, "receipt-before-stat")
            real_lstat = Path.lstat

            def vanished(candidate):
                if candidate == path:
                    path.unlink()
                    raise FileNotFoundError(path)
                return real_lstat(candidate)

            with mock.patch.object(Path, "lstat", vanished):
                report = _cleanup(root, path)

            self.assertFalse(path.exists())
            self.assertEqual(report, outbox.TerminalReceiptCleanup())

    def test_receipt_disappearing_during_read_is_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = _write_receipt(root, "receipt-during-read")

            def vanished(*_args, **_kwargs):
                path.unlink()
                return outbox.TerminalReceipt(
                    outbox.TerminalReceiptState.ABSENT, "", 0)

            with mock.patch.object(outbox, "_read_terminal_path", vanished):
                report = _cleanup(root, path)

            self.assertFalse(path.exists())
            self.assertEqual(report, outbox.TerminalReceiptCleanup())


class TestExpiryRaces(unittest.TestCase):
    def _run(self, error_type):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = _write_receipt(root, f"expired-{error_type.__name__}", 1.0)
            real_unlink = Path.unlink

            def fail_unlink(candidate, *args, **kwargs):
                if candidate != path:
                    return real_unlink(candidate, *args, **kwargs)
                if error_type is FileNotFoundError:
                    real_unlink(candidate)
                raise error_type("injected")

            with mock.patch.object(Path, "unlink", fail_unlink):
                report = _cleanup(root, path, clock=20.0, ttl=5.0)
            return path.exists(), report

    def test_expired_receipt_removed_by_peer_is_not_counted(self):
        exists, report = self._run(FileNotFoundError)
        self.assertFalse(exists)
        self.assertEqual(report.expired, 0)
        self.assertEqual(report.kept, 0)
        self.assertEqual(report.unknown, 0)

    def test_unremovable_expired_receipt_is_preserved_and_accounted(self):
        exists, report = self._run(PermissionError)
        self.assertTrue(exists)
        self.assertEqual(report.expired, 0)
        self.assertEqual(report.kept, 1)
        self.assertEqual(report.unknown, 1)


class TestOverflowRaces(unittest.TestCase):
    def test_rebound_overflow_entry_is_kept_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = _write_receipt(root, "overflow-rebound")

            with mock.patch.object(outbox, "_same_stat", return_value=False):
                report = _cleanup(root, path, max_records=0)

            self.assertTrue(path.exists())
            self.assertEqual(report.overflow, 0)
            self.assertEqual(report.kept, 1)
            self.assertTrue(report.incomplete)

    def _run_unlink_error(self, error_type):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = _write_receipt(root, f"overflow-{error_type.__name__}")
            real_unlink = Path.unlink

            def fail_unlink(candidate, *args, **kwargs):
                if candidate != path:
                    return real_unlink(candidate, *args, **kwargs)
                if error_type is FileNotFoundError:
                    real_unlink(candidate)
                raise error_type("injected")

            with mock.patch.object(Path, "unlink", fail_unlink):
                report = _cleanup(root, path, max_records=0)
            return path.exists(), report

    def test_overflow_removed_by_peer_is_conservatively_accounted(self):
        exists, report = self._run_unlink_error(FileNotFoundError)
        self.assertFalse(exists)
        self.assertEqual(report.overflow, 0)
        self.assertEqual(report.kept, 1)
        self.assertTrue(report.incomplete)

    def test_unremovable_overflow_is_preserved_and_accounted(self):
        exists, report = self._run_unlink_error(PermissionError)
        self.assertTrue(exists)
        self.assertEqual(report.overflow, 0)
        self.assertEqual(report.kept, 1)
        self.assertEqual(report.unknown, 1)
        self.assertTrue(report.incomplete)


class TestPublicCleanupErrors(unittest.TestCase):
    def test_invalid_capacity_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for invalid in (True, -1):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    outbox.cleanup_terminal_receipts(tmp, max_records=invalid)

    def test_missing_receipt_root_is_a_clean_empty_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = outbox.cleanup_terminal_receipts(tmp)
        self.assertEqual(report, outbox.TerminalReceiptCleanup())

    def test_unstatable_receipt_root_is_unknown_and_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = outbox._terminal_receipts_dir(root)
            directory.mkdir()
            real_lstat = Path.lstat

            def unreadable(candidate):
                if candidate == directory:
                    raise PermissionError("injected")
                return real_lstat(candidate)

            with mock.patch.object(Path, "lstat", unreadable):
                report = outbox.cleanup_terminal_receipts(root)

            self.assertEqual(report.unknown, 1)
            self.assertTrue(report.incomplete)

    def test_non_directory_receipt_root_is_unknown_and_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = outbox._terminal_receipts_dir(root)
            directory.write_text("not a directory")

            report = outbox.cleanup_terminal_receipts(root)

            self.assertEqual(report.unknown, 1)
            self.assertTrue(report.incomplete)

    def test_unstatable_shard_is_unknown_and_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = outbox._terminal_receipts_dir(root)
            directory.mkdir()
            shard = directory / "7f"
            real_lstat = Path.lstat

            def unreadable(candidate):
                if candidate == shard:
                    raise PermissionError("injected")
                return real_lstat(candidate)

            with mock.patch.object(Path, "lstat", unreadable):
                report = outbox.cleanup_terminal_receipts(root)

            self.assertEqual(report.unknown, 1)
            self.assertTrue(report.incomplete)


if __name__ == "__main__":
    unittest.main(verbosity=2)
