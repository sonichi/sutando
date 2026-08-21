#!/usr/bin/env python3
"""Direct contract tests for durable outbox terminal receipts."""
from __future__ import annotations

import errno
import json
import multiprocessing
import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import outbox  # noqa: E402


def _race_record(root, disposition, start, results):
    start.wait()
    receipt = outbox.record_terminal_receipt(root, "race/item", disposition, now=100.0)
    results.put((receipt.state.value, receipt.disposition.value, receipt.recorded_at))


def _read_receipt_state(root, item_id, results):
    receipt = outbox.read_terminal_receipt(root, item_id, now=100.0)
    results.put(receipt.state.value)


def _item_id_for_record_size(size):
    sample = "x"
    overhead = len(outbox._terminal_bytes(outbox._terminal_payload(
        sample, 0, outbox.TerminalDisposition.DELIVERED, 100.0))) - len(sample)
    item_id = "x" * (size - overhead)
    encoded = outbox._terminal_bytes(outbox._terminal_payload(
        item_id, 0, outbox.TerminalDisposition.DELIVERED, 100.0))
    assert len(encoded) == size
    return item_id


def test_states_dispositions_and_first_terminal_wins():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        absent = outbox.read_terminal_receipt(root, "missing", now=10.0)
        assert absent == outbox.TerminalReceipt(
            outbox.TerminalReceiptState.ABSENT, "missing", 0)
        for index, disposition in enumerate(outbox.TerminalDisposition):
            item_id = f"outcome-{index}"
            receipt = outbox.record_terminal_receipt(
                root, item_id, disposition, now=10.0 + index)
            assert receipt.state is outbox.TerminalReceiptState.TERMINAL
            assert receipt.disposition is disposition
            assert outbox.read_terminal_receipt(
                root, item_id, now=20.0) == receipt

        first = outbox.record_terminal_receipt(
            root, "one-winner", outbox.TerminalDisposition.DELIVERED, now=30.0)
        second = outbox.record_terminal_receipt(
            root, "one-winner", outbox.TerminalDisposition.NO_SEND, now=31.0)
        assert second == first

        invalid = [
            (outbox.TerminalReceiptState.ABSENT, None, 1.0),
            (outbox.TerminalReceiptState.ABSENT,
             outbox.TerminalDisposition.DELIVERED, None),
            (outbox.TerminalReceiptState.UNKNOWN, None, 1.0),
            (outbox.TerminalReceiptState.UNKNOWN,
             outbox.TerminalDisposition.DELIVERED, None),
        ]
        for state, disposition, recorded_at in invalid:
            try:
                outbox.TerminalReceipt(
                    state, "invalid", 0, disposition, recorded_at)
            except ValueError:
                pass
            else:
                raise AssertionError("partial nonterminal receipt was accepted")


def test_concurrent_writers_observe_one_terminal_winner():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = multiprocessing.get_context("fork")
        start = ctx.Event()
        results = ctx.Queue()
        dispositions = list(outbox.TerminalDisposition) * 2
        workers = [ctx.Process(target=_race_record, args=(tmp, value, start, results))
                   for value in dispositions]
        for worker in workers:
            worker.start()
        start.set()
        observed = [results.get(timeout=10) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
        assert len(set(observed)) == 1, observed
        files = list((Path(tmp) / outbox.TERMINAL_RECEIPTS_DIR).glob("*/*.json"))
        assert len(files) == 1, files


def test_generation_and_path_identity_are_exact():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ids = ["a/b", "a_b", "../escape", "snowman-☃"]
        for item_id in ids:
            outbox.record_terminal_receipt(
                root, item_id, outbox.TerminalDisposition.DELIVERED, now=100.0)
        paths = [outbox._terminal_receipt_path(root, item_id, 0) for item_id in ids]
        assert len(set(paths)) == len(ids)
        assert all(path.parent.parent == root / outbox.TERMINAL_RECEIPTS_DIR
                   for path in paths)
        assert all(len(path.stem) == 64 and path.parent.name == path.stem[:2]
                   for path in paths)

        zero = outbox.record_terminal_receipt(
            root, "generation", outbox.TerminalDisposition.DELIVERED,
            generation=0, now=101.0)
        one = outbox.record_terminal_receipt(
            root, "generation", outbox.TerminalDisposition.REDIRECTED,
            generation=1, now=102.0)
        assert zero.disposition is outbox.TerminalDisposition.DELIVERED
        assert one.disposition is outbox.TerminalDisposition.REDIRECTED
        assert outbox._terminal_receipt_path(root, "generation", 0) != \
            outbox._terminal_receipt_path(root, "generation", 1)


def test_corruption_is_unknown_and_record_never_overwrites_it():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        original = outbox.record_terminal_receipt(
            root, "corrupt", outbox.TerminalDisposition.DELIVERED, now=100.0)
        assert original.state is outbox.TerminalReceiptState.TERMINAL
        path = outbox._terminal_receipt_path(root, "corrupt", 0)
        path.write_bytes(b'{"schema":1')
        before = path.read_bytes()
        unknown = outbox.read_terminal_receipt(root, "corrupt", now=101.0)
        assert unknown.state is outbox.TerminalReceiptState.UNKNOWN
        refused = outbox.record_terminal_receipt(
            root, "corrupt", outbox.TerminalDisposition.NO_SEND, now=102.0)
        assert refused.state is outbox.TerminalReceiptState.UNKNOWN
        assert path.read_bytes() == before

        other = outbox.record_terminal_receipt(
            root, "other", outbox.TerminalDisposition.DEDUPED, now=103.0)
        assert other.state is outbox.TerminalReceiptState.TERMINAL
        path.write_bytes(outbox._terminal_receipt_path(root, "other", 0).read_bytes())
        assert outbox.read_terminal_receipt(
            root, "corrupt", now=104.0).state is outbox.TerminalReceiptState.UNKNOWN


def test_reader_rejects_every_semantically_invalid_record_shape():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cases = []

        item_id = "missing-fields"
        cases.append((item_id, 0, {"schema": outbox.TERMINAL_RECEIPT_SCHEMA}))

        item_id = "wrong-schema"
        data = outbox._terminal_payload(
            item_id, 0, outbox.TerminalDisposition.DELIVERED, 100.0)
        data["schema"] += 1
        cases.append((item_id, 0, data))

        item_id = "empty-stored-item"
        data = outbox._terminal_payload(
            item_id, 0, outbox.TerminalDisposition.DELIVERED, 100.0)
        data["item_id"] = ""
        cases.append((item_id, 0, data))

        item_id = "invalid-stored-generation"
        data = outbox._terminal_payload(
            item_id, 0, outbox.TerminalDisposition.DELIVERED, 100.0)
        data["generation"] = True
        cases.append((item_id, 0, data))

        item_id = "invalid-recorded-at"
        data = outbox._terminal_payload(
            item_id, 0, outbox.TerminalDisposition.DELIVERED, 100.0)
        data["recorded_at"] = "yesterday"
        cases.append((item_id, 0, data))

        item_id = "mismatched-generation"
        data = outbox._terminal_payload(
            item_id, 1, outbox.TerminalDisposition.DELIVERED, 100.0)
        cases.append((item_id, 0, data))

        item_id = "invalid-disposition"
        data = outbox._terminal_payload(
            item_id, 0, outbox.TerminalDisposition.DELIVERED, 100.0)
        data["disposition"] = "maybe"
        cases.append((item_id, 0, data))

        item_id = "invalid-checksum"
        data = outbox._terminal_payload(
            item_id, 0, outbox.TerminalDisposition.DELIVERED, 100.0)
        data["checksum"] = "0" * 64
        cases.append((item_id, 0, data))

        for item_id, generation, data in cases:
            path = outbox._terminal_receipt_path(root, item_id, generation)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(outbox._terminal_bytes(data))
            receipt = outbox.read_terminal_receipt(
                root, item_id, generation=generation, now=101.0)
            assert receipt.state is outbox.TerminalReceiptState.UNKNOWN, item_id

        wrong_path_item = "wrong-path"
        wrong_path = root / outbox.TERMINAL_RECEIPTS_DIR / "ff" / "wrong.json"
        wrong_path.parent.mkdir(parents=True, exist_ok=True)
        wrong_path.write_bytes(outbox._terminal_bytes(outbox._terminal_payload(
            wrong_path_item, 0, outbox.TerminalDisposition.DELIVERED, 100.0)))
        assert outbox._read_terminal_path(
            wrong_path, wrong_path_item, 0).state is outbox.TerminalReceiptState.UNKNOWN


def test_reader_open_failures_and_oversized_disk_record_are_unknown():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        item_id = "lstat-denied"
        with mock.patch.object(outbox.os, "open", side_effect=FileNotFoundError()), \
                mock.patch.object(Path, "lstat", side_effect=PermissionError()):
            receipt = outbox.read_terminal_receipt(root, item_id, now=100.0)
        assert receipt.state is outbox.TerminalReceiptState.UNKNOWN

        item_id = "vanished-open-existing-name"
        path = outbox._terminal_receipt_path(root, item_id, 0)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"present")
        with mock.patch.object(outbox.os, "open", side_effect=FileNotFoundError()):
            receipt = outbox.read_terminal_receipt(root, item_id, now=100.0)
        assert receipt.state is outbox.TerminalReceiptState.UNKNOWN

        with mock.patch.object(outbox.os, "open", side_effect=PermissionError()):
            receipt = outbox.read_terminal_receipt(root, "open-denied", now=100.0)
        assert receipt.state is outbox.TerminalReceiptState.UNKNOWN

        item_id = "oversized-on-disk"
        path = outbox._terminal_receipt_path(root, item_id, 0)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * (outbox.TERMINAL_RECEIPT_MAX_BYTES + 1))
        receipt = outbox.read_terminal_receipt(root, item_id, now=100.0)
        assert receipt.state is outbox.TerminalReceiptState.UNKNOWN


def test_fifo_receipt_returns_unknown_without_blocking():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        item_id = "fifo-receipt"
        path = outbox._terminal_receipt_path(root, item_id, 0)
        path.parent.mkdir(parents=True)
        os.mkfifo(path)

        ctx = multiprocessing.get_context("fork")
        results = ctx.Queue()
        worker = ctx.Process(target=_read_receipt_state, args=(root, item_id, results))
        worker.start()
        worker.join(timeout=2)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=2)
            raise AssertionError("terminal receipt reader blocked on a FIFO")
        assert worker.exitcode == 0
        assert results.get(timeout=1) == outbox.TerminalReceiptState.UNKNOWN.value
        direct = outbox.read_terminal_receipt(root, item_id, now=100.0)
        assert direct.state is outbox.TerminalReceiptState.UNKNOWN


def test_ttl_expiry_and_cleanup_are_conservative():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        outbox.record_terminal_receipt(
            root, "expired", outbox.TerminalDisposition.DELIVERED,
            now=10.0, ttl_seconds=5.0)
        assert outbox.read_terminal_receipt(
            root, "expired", now=15.0,
            ttl_seconds=5.0).state is outbox.TerminalReceiptState.ABSENT
        replacement = outbox.record_terminal_receipt(
            root, "expired", outbox.TerminalDisposition.DEDUPED,
            now=15.0, ttl_seconds=5.0)
        assert replacement.disposition is outbox.TerminalDisposition.DEDUPED

        path = outbox._terminal_receipt_path(root, "bad-old", 0)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"torn")
        os.utime(path, (1.0, 1.0))
        assert outbox.read_terminal_receipt(
            root, "bad-old", now=20.0,
            ttl_seconds=5.0).state is outbox.TerminalReceiptState.UNKNOWN
        cleanup = outbox.cleanup_terminal_receipts(root, ttl_seconds=5.0, now=20.0)
        assert cleanup.expired >= 1
        assert cleanup.unknown >= 1
        assert outbox.read_terminal_receipt(
            root, "bad-old", now=20.0,
            ttl_seconds=5.0).state is outbox.TerminalReceiptState.UNKNOWN


def test_unknown_receipt_survives_ttl_and_overflow_cleanup():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = outbox._terminal_receipt_path(root, "protected-unknown", 0)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"corrupt")
        os.utime(path, (1.0, 1.0))

        report = outbox.cleanup_terminal_receipts(
            root, ttl_seconds=1.0, max_records=0, now=100.0)

        assert path.read_bytes() == b"corrupt"
        assert outbox.read_terminal_receipt(
            root, "protected-unknown", now=100.0,
            ttl_seconds=1.0).state is outbox.TerminalReceiptState.UNKNOWN
        assert report.unknown == 1
        assert report.kept == 1
        assert report.incomplete is True


def test_explicit_cleanup_finishes_preexisting_overflow():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shard = root / outbox.TERMINAL_RECEIPTS_DIR / "00"
        shard.mkdir(parents=True)
        count = outbox.TERMINAL_RECEIPT_SWEEP_BATCH + 8
        written = 0
        candidate = 0
        while written < count:
            item_id = f"preexisting-{candidate}"
            candidate += 1
            digest = outbox._terminal_digest(item_id, 0)
            if not digest.startswith("00"):
                continue
            payload = outbox._terminal_payload(
                item_id, 0, outbox.TerminalDisposition.DELIVERED, 100.0)
            (shard / f"{digest}.json").write_bytes(outbox._terminal_bytes(payload))
            written += 1
        report = outbox.cleanup_terminal_receipts(
            root, max_records=0, now=100.0)
        assert not list(shard.glob("*.json"))
        assert report.overflow == count
        assert report.kept == 0
        assert report.incomplete is False


def test_cleanup_removes_recognized_temps_and_ignores_foreign_names():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shard_name = "aa"
        shard = root / outbox.TERMINAL_RECEIPTS_DIR / shard_name
        shard.mkdir(parents=True)
        unrelated = shard / "short.json"
        malformed_temp = shard / ".not-a-digest.token.tmp"
        digest = shard_name + "0" * 62
        recognized_temp = shard / f".{digest}.token.tmp"
        unrelated.write_bytes(b"leave me")
        malformed_temp.write_bytes(b"leave me too")
        recognized_temp.write_bytes(b"orphaned publication")

        report = outbox.cleanup_terminal_receipts(root, now=100.0)
        assert report.stale_temps == 1
        assert not recognized_temp.exists()
        assert unrelated.read_bytes() == b"leave me"
        assert malformed_temp.read_bytes() == b"leave me too"


def test_cleanup_keeps_entry_when_identity_recheck_cannot_stat_it():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        item_id = "same-stat-denied"
        outbox.record_terminal_receipt(
            root, item_id, outbox.TerminalDisposition.DELIVERED, now=100.0)
        path = outbox._terminal_receipt_path(root, item_id, 0)
        real_lstat = Path.lstat
        target_calls = 0

        def deny_recheck(candidate):
            nonlocal target_calls
            if candidate == path:
                target_calls += 1
                if target_calls > 1:
                    raise PermissionError("injected identity recheck denial")
            return real_lstat(candidate)

        with mock.patch.object(Path, "lstat", deny_recheck):
            report = outbox.cleanup_terminal_receipts(
                root, ttl_seconds=0.0, max_records=0, now=101.0)
        assert target_calls >= 2
        assert path.exists()
        assert report.kept == 1
        assert report.incomplete is True


def test_indeterminate_receipt_survives_capacity_pressure_and_blocks_publication():
    item_a = "indeterminate-a"
    shard_name = outbox._terminal_digest(item_a, 0)[:2]
    for candidate in range(10_000):
        item_b = f"capacity-b-{candidate}"
        if outbox._terminal_digest(item_b, 0).startswith(shard_name):
            break
    else:
        raise AssertionError("could not find a colliding receipt shard")

    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(outbox, "TERMINAL_RECEIPT_MAX_RECORDS", 256):
        root = Path(tmp)
        path_a = outbox._terminal_receipt_path(root, item_a, 0)
        path_a.parent.mkdir(parents=True)
        path_a.write_bytes(b"torn")
        os.utime(path_a, (1.0, 1.0))

        refused = outbox.record_terminal_receipt(
            root, item_b, outbox.TerminalDisposition.DELIVERED,
            now=100.0, ttl_seconds=0.0)
        assert refused.state is outbox.TerminalReceiptState.UNKNOWN
        assert outbox.read_terminal_receipt(
            root, item_a, now=100.0,
            ttl_seconds=0.0).state is outbox.TerminalReceiptState.UNKNOWN
        assert not outbox._terminal_receipt_path(root, item_b, 0).exists()

        report = outbox.cleanup_terminal_receipts(
            root, ttl_seconds=0.0, max_records=0, now=100.0)
        assert report.incomplete is True
        assert report.unknown >= 1
        assert report.kept == 1
        assert path_a.read_bytes() == b"torn"


def test_unreadable_shard_blocks_publication_and_marks_cleanup_incomplete():
    item_a = "unreadable-shard-a"
    shard_name = outbox._terminal_digest(item_a, 0)[:2]
    for candidate in range(10_000):
        item_b = f"unreadable-shard-b-{candidate}"
        if outbox._terminal_digest(item_b, 0).startswith(shard_name):
            break
    else:
        raise AssertionError("could not find a colliding receipt shard")

    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(outbox, "TERMINAL_RECEIPT_MAX_RECORDS", 256):
        root = Path(tmp)
        outbox.record_terminal_receipt(
            root, item_a, outbox.TerminalDisposition.DELIVERED, now=100.0)
        path_b = outbox._terminal_receipt_path(root, item_b, 0)
        with mock.patch.object(
                outbox.os, "scandir", side_effect=PermissionError("injected")):
            refused = outbox.record_terminal_receipt(
                root, item_b, outbox.TerminalDisposition.DELIVERED, now=101.0)
            report = outbox.cleanup_terminal_receipts(
                root, max_records=256, now=101.0)
        assert refused.state is outbox.TerminalReceiptState.UNKNOWN
        assert not path_b.exists()
        assert report.incomplete is True
        assert report.unknown >= 1


def test_unstatable_receipt_entry_blocks_same_shard_publication():
    item_a = "unstatable-entry-a"
    shard_name = outbox._terminal_digest(item_a, 0)[:2]
    for candidate in range(10_000):
        item_b = f"unstatable-entry-b-{candidate}"
        if outbox._terminal_digest(item_b, 0).startswith(shard_name):
            break
    else:
        raise AssertionError("could not find a colliding receipt shard")

    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(outbox, "TERMINAL_RECEIPT_MAX_RECORDS", 256):
        root = Path(tmp)
        outbox.record_terminal_receipt(
            root, item_a, outbox.TerminalDisposition.DELIVERED, now=100.0)
        path_a = outbox._terminal_receipt_path(root, item_a, 0)
        path_b = outbox._terminal_receipt_path(root, item_b, 0)
        real_lstat = Path.lstat

        def selective_lstat(path):
            if path == path_a:
                raise PermissionError("injected")
            return real_lstat(path)

        with mock.patch.object(Path, "lstat", selective_lstat):
            refused = outbox.record_terminal_receipt(
                root, item_b, outbox.TerminalDisposition.DELIVERED, now=101.0)
        assert refused.state is outbox.TerminalReceiptState.UNKNOWN
        assert path_a.exists()
        assert not path_b.exists()


def test_production_writer_enforces_shard_bound():
    buckets = {}
    for number in range(10_000):
        item_id = f"bounded-{number}"
        shard = outbox._terminal_digest(item_id, 0)[:2]
        buckets.setdefault(shard, []).append(item_id)
        if len(buckets[shard]) == 3:
            same_shard = buckets[shard]
            break
    else:
        raise AssertionError("could not find three ids in one receipt shard")

    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(outbox, "TERMINAL_RECEIPT_MAX_RECORDS", 512):
        root = Path(tmp)
        for index, item_id in enumerate(same_shard):
            outbox.record_terminal_receipt(
                root, item_id, outbox.TerminalDisposition.DELIVERED,
                now=100.0 + index)
        shard_dir = outbox._terminal_receipt_path(root, same_shard[0], 0).parent
        assert len(list(shard_dir.glob("*.json"))) == 2
        assert outbox.read_terminal_receipt(
            root, same_shard[0], now=103.0).state is outbox.TerminalReceiptState.ABSENT
        for item_id in same_shard[1:]:
            assert outbox.read_terminal_receipt(
                root, item_id, now=103.0).state is outbox.TerminalReceiptState.TERMINAL

    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(outbox, "TERMINAL_RECEIPT_MAX_RECORDS", 0):
        refused = outbox.record_terminal_receipt(
            tmp, "zero-capacity", outbox.TerminalDisposition.DELIVERED, now=100.0)
        assert refused.state is outbox.TerminalReceiptState.UNKNOWN
        assert not list(Path(tmp).rglob("*.json"))


def test_receipt_directory_type_collisions_fail_without_replacement():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        receipt_root = root / outbox.TERMINAL_RECEIPTS_DIR
        receipt_root.write_bytes(b"not a directory")
        digest = outbox._terminal_digest("root-collision", 0)
        try:
            outbox._ensure_terminal_receipts_dir(root, digest)
        except FileExistsError:
            pass
        else:
            raise AssertionError("receipt-root file was treated as a directory")
        assert receipt_root.read_bytes() == b"not a directory"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        digest = outbox._terminal_digest("shard-collision", 0)
        receipt_root = root / outbox.TERMINAL_RECEIPTS_DIR
        receipt_root.mkdir()
        shard = outbox._terminal_receipt_shard(root, digest)
        shard.write_bytes(b"not a directory")
        try:
            outbox._ensure_terminal_receipts_dir(root, digest)
        except FileExistsError:
            pass
        else:
            raise AssertionError("receipt-shard file was treated as a directory")
        assert shard.read_bytes() == b"not a directory"


def test_expiry_unlink_races_fail_closed_or_publish_a_replacement():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        item_id = "expiry-vanished"
        outbox.record_terminal_receipt(
            root, item_id, outbox.TerminalDisposition.DELIVERED, now=100.0)
        path = outbox._terminal_receipt_path(root, item_id, 0)
        real_unlink = Path.unlink
        injected = False

        def vanish_then_report_missing(candidate, *args, **kwargs):
            nonlocal injected
            if candidate == path and not injected:
                injected = True
                real_unlink(candidate)
                raise FileNotFoundError("injected expiry race")
            return real_unlink(candidate, *args, **kwargs)

        with mock.patch.object(Path, "unlink", vanish_then_report_missing):
            replacement = outbox.record_terminal_receipt(
                root, item_id, outbox.TerminalDisposition.DEDUPED,
                now=101.0, ttl_seconds=0.0)
        assert injected is True
        assert replacement.disposition is outbox.TerminalDisposition.DEDUPED

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        item_id = "expiry-unlink-denied"
        original = outbox.record_terminal_receipt(
            root, item_id, outbox.TerminalDisposition.DELIVERED, now=100.0)
        path = outbox._terminal_receipt_path(root, item_id, 0)
        before = path.read_bytes()
        real_unlink = Path.unlink

        def deny_target_unlink(candidate, *args, **kwargs):
            if candidate == path:
                raise PermissionError("injected expiry denial")
            return real_unlink(candidate, *args, **kwargs)

        with mock.patch.object(Path, "unlink", deny_target_unlink):
            refused = outbox.record_terminal_receipt(
                root, item_id, outbox.TerminalDisposition.DEDUPED,
                now=101.0, ttl_seconds=0.0)
        assert original.state is outbox.TerminalReceiptState.TERMINAL
        assert refused.state is outbox.TerminalReceiptState.UNKNOWN
        assert path.read_bytes() == before


def test_atomic_link_collision_returns_the_published_winner():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        real_link = outbox.os.link

        def publish_then_report_collision(source, destination):
            real_link(source, destination)
            raise FileExistsError("injected publication collision")

        with mock.patch.object(outbox.os, "link", publish_then_report_collision):
            receipt = outbox.record_terminal_receipt(
                root, "link-collision", outbox.TerminalDisposition.DELIVERED,
                now=100.0)
        assert receipt.state is outbox.TerminalReceiptState.TERMINAL
        assert outbox.read_terminal_receipt(
            root, "link-collision", now=101.0) == receipt


def test_publication_fsyncs_content_before_atomic_link_and_directory_after():
    with tempfile.TemporaryDirectory() as tmp:
        events = []
        real_fsync = outbox.os.fsync
        real_link = outbox.os.link

        def traced_fsync(fd):
            events.append(("fsync", stat.S_ISDIR(os.fstat(fd).st_mode)))
            return real_fsync(fd)

        def traced_link(source, destination):
            payload = json.loads(Path(source).read_text())
            assert payload["item_id"] == "durable"
            assert not Path(destination).exists()
            assert ("fsync", False) in events
            events.append(("link", False))
            return real_link(source, destination)

        with mock.patch.object(outbox.os, "fsync", traced_fsync), \
                mock.patch.object(outbox.os, "link", traced_link):
            receipt = outbox.record_terminal_receipt(
                tmp, "durable", outbox.TerminalDisposition.DELIVERED, now=100.0)
        assert receipt.state is outbox.TerminalReceiptState.TERMINAL
        link_index = events.index(("link", False))
        assert any(event == ("fsync", True) for event in events[link_index + 1:])

        destination = outbox._terminal_receipt_path(Path(tmp), "failed-link", 0)
        with mock.patch.object(outbox.os, "link",
                               side_effect=OSError(errno.EIO, "injected")):
            try:
                outbox.record_terminal_receipt(
                    tmp, "failed-link", outbox.TerminalDisposition.DELIVERED,
                    now=101.0)
            except OSError as exc:
                assert exc.errno == errno.EIO
            else:
                raise AssertionError("link failure was swallowed")
        assert not destination.exists()
        assert not list(destination.parent.glob("*.tmp"))


def test_first_root_creation_fsyncs_parent_before_publication():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "new-receipt-root"
        assert not root.exists()
        events = []
        real_fsync_directory = outbox._fsync_directory
        real_link = outbox.os.link

        def traced_fsync_directory(path):
            events.append(("fsync-directory", Path(path)))
            return real_fsync_directory(path)

        def traced_link(source, destination):
            events.append(("link", Path(destination)))
            return real_link(source, destination)

        with mock.patch.object(outbox, "_fsync_directory", traced_fsync_directory), \
                mock.patch.object(outbox.os, "link", traced_link):
            receipt = outbox.record_terminal_receipt(
                root, "new-root", outbox.TerminalDisposition.DELIVERED, now=100.0)

        assert receipt.state is outbox.TerminalReceiptState.TERMINAL
        parent_fsync = events.index(("fsync-directory", root.parent))
        publication = next(index for index, event in enumerate(events)
                           if event[0] == "link")
        assert parent_fsync < publication, events


def test_record_at_reader_size_limit_round_trips_as_terminal():
    item_id = _item_id_for_record_size(outbox.TERMINAL_RECEIPT_MAX_BYTES)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        receipt = outbox.record_terminal_receipt(
            root, item_id, outbox.TerminalDisposition.DELIVERED, now=100.0)
        path = outbox._terminal_receipt_path(root, item_id, 0)
        assert receipt.state is outbox.TerminalReceiptState.TERMINAL
        assert path.stat().st_size == outbox.TERMINAL_RECEIPT_MAX_BYTES
        assert outbox.read_terminal_receipt(
            root, item_id, now=101.0) == receipt


def test_record_over_reader_size_limit_is_rejected_before_publication():
    item_id = _item_id_for_record_size(outbox.TERMINAL_RECEIPT_MAX_BYTES + 1)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        try:
            outbox.record_terminal_receipt(
                root, item_id, outbox.TerminalDisposition.DELIVERED, now=100.0)
        except ValueError as exc:
            assert "size limit" in str(exc)
        else:
            raise AssertionError("oversized terminal receipt was published")
        assert not outbox._terminal_receipt_path(root, item_id, 0).exists()
        assert outbox.read_terminal_receipt(
            root, item_id, now=101.0).state is outbox.TerminalReceiptState.ABSENT


def test_invalid_identity_and_clock_inputs_are_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        invalid_calls = [
            lambda: outbox.read_terminal_receipt(tmp, "", now=1.0),
            lambda: outbox.read_terminal_receipt(tmp, "x", generation=-1, now=1.0),
            lambda: outbox.read_terminal_receipt(tmp, "x", generation=True, now=1.0),
            lambda: outbox.read_terminal_receipt(tmp, "x", now=float("nan")),
            lambda: outbox.read_terminal_receipt(tmp, "x", now=1.0, ttl_seconds=-1.0),
            lambda: outbox.record_terminal_receipt(tmp, "x", "bogus", now=1.0),
        ]
        for call in invalid_calls:
            try:
                call()
            except ValueError:
                pass
            else:
                raise AssertionError("invalid terminal receipt input was accepted")


def test_content_digest_roundtrips_and_gates_replacement():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        item = "task-content-digest"
        dig_a = "a" * 64
        dig_b = "b" * 64

        # First record persists the digest and read returns it.
        r1 = outbox.record_terminal_receipt(
            root, item, outbox.TerminalDisposition.DELIVERED,
            now=100.0, content_digest=dig_a)
        assert r1.state is outbox.TerminalReceiptState.TERMINAL
        assert r1.content_digest == dig_a
        assert outbox.read_terminal_receipt(
            root, item, now=101.0).content_digest == dig_a

        # Same digest within TTL is idempotent: the standing record wins, its
        # timestamp does not move.
        r2 = outbox.record_terminal_receipt(
            root, item, outbox.TerminalDisposition.DELIVERED,
            now=200.0, content_digest=dig_a)
        assert r2.recorded_at == 100.0
        assert r2.content_digest == dig_a

        # A different digest is a follow-up/revision: it replaces the record.
        r3 = outbox.record_terminal_receipt(
            root, item, outbox.TerminalDisposition.DELIVERED,
            now=300.0, content_digest=dig_b)
        assert r3.recorded_at == 300.0
        assert r3.content_digest == dig_b
        assert outbox.read_terminal_receipt(
            root, item, now=301.0).content_digest == dig_b


def test_content_digest_must_be_nonempty_string_or_none():
    with tempfile.TemporaryDirectory() as tmp:
        for bad in ("", 123, b"x"):
            try:
                outbox.record_terminal_receipt(
                    tmp, "x", outbox.TerminalDisposition.DELIVERED,
                    now=1.0, content_digest=bad)
            except ValueError:
                pass
            else:
                raise AssertionError("invalid content_digest was accepted")


def test_legacy_schema_receipt_reads_as_unknown():
    # A v1 record (no content_digest field) predates this format; it must not
    # be trusted as TERMINAL — corrupt/foreign state is UNKNOWN, never ABSENT.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        item = "task-legacy"
        path = outbox._terminal_receipt_path(root, item, 0)
        path.parent.mkdir(parents=True, exist_ok=True)
        base = {
            "schema": 1,
            "item_id": item,
            "generation": 0,
            "disposition": outbox.TerminalDisposition.DELIVERED.value,
            "recorded_at": 100.0,
        }
        import hashlib as _h
        import json as _j
        canonical = _j.dumps(base, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        legacy = {**base, "checksum": _h.sha256(canonical).hexdigest()}
        path.write_bytes(outbox._terminal_bytes(legacy))
        assert outbox.read_terminal_receipt(
            root, item, now=101.0).state is outbox.TerminalReceiptState.UNKNOWN


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("PASS - durable terminal receipt contract")
