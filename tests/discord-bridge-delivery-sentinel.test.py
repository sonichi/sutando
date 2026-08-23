#!/usr/bin/env python3
"""Discord task-result receipts survive archive and fence legacy sentinels."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
WORKSPACE = Path(tempfile.mkdtemp(prefix="discord-receipt-test-"))
CONFIG = WORKSPACE / "config"
os.environ["SUTANDO_WORKSPACE"] = str(WORKSPACE)
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["CLAUDE_CONFIG_DIR"] = str(CONFIG)
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
(CONFIG / "channels" / "discord").mkdir(parents=True)
(CONFIG / "channels" / "discord" / "access.json").write_text(
    '{"allowFrom": []}', encoding="utf-8")


def _load_bridge():
    stub = types.ModuleType("discord")
    stub.Intents = type("Intents", (), {
        "default": staticmethod(
            lambda: type("I", (), {"message_content": False, "members": False})())})
    stub.Client = type("Client", (), {
        "__init__": lambda self, **_kw: None,
        "event": staticmethod(lambda fn: fn),
    })
    stub.File = type("File", (), {})
    stub.DMChannel = type("DMChannel", (), {})
    stub.Object = lambda id: type("Object", (), {"id": id})()
    sys.modules["discord"] = stub
    spec = importlib.util.spec_from_file_location(
        "discord_bridge_receipt_test", REPO / "src" / "discord-bridge.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()


def _reset():
    shutil.rmtree(WORKSPACE / "results", ignore_errors=True)
    shutil.rmtree(WORKSPACE / "state" / "discord-delivered", ignore_errors=True)
    (WORKSPACE / "results").mkdir(parents=True)
    bridge._ambiguous_receipt_notices.clear()
    bridge._ambiguous_receipt_notice_overflow = False


def test_default_is_not_terminal():
    _reset()
    assert bridge._is_delivered("task-100") is False


def test_receipt_read_error_does_not_claim_a_durable_terminal_state():
    _reset()
    with mock.patch.object(
            bridge.outbox, "read_terminal_receipt",
            side_effect=PermissionError("receipt unreadable")):
        assert bridge._has_durable_terminal_receipt("task-100b") is False


def test_success_writes_a_shared_terminal_receipt():
    _reset()
    bridge._mark_delivered("task-101")
    assert bridge._is_delivered("task-101") is True
    assert bridge._delivered_sentinel_path("task-101").exists()
    receipt = bridge.outbox.read_terminal_receipt(
        bridge._result_receipt_root(), "task-101")
    assert receipt.state is bridge.outbox.TerminalReceiptState.TERMINAL
    assert receipt.disposition is bridge.outbox.TerminalDisposition.DELIVERED


def test_clearing_legacy_state_does_not_erase_the_shared_receipt():
    _reset()
    bridge._mark_delivered("task-102")
    assert bridge._delivered_sentinel_path("task-102").exists()
    bridge._clear_delivered("task-102")
    assert not bridge._delivered_sentinel_path("task-102").exists()
    assert bridge._is_delivered("task-102") is True


def test_clearing_legacy_state_is_idempotent():
    _reset()
    bridge._mark_delivered("task-102b")
    bridge._clear_delivered("task-102b")
    bridge._clear_delivered("task-102b")
    assert not bridge._delivered_sentinel_path("task-102b").exists()
    assert bridge._is_delivered("task-102b") is True


def test_legacy_only_sentinel_is_ambiguous_and_not_promoted():
    _reset()
    bridge.DELIVERED_DIR.mkdir(parents=True, exist_ok=True)
    bridge._delivered_sentinel_path("task-103").touch()
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        states = [bridge._terminal_receipt_state("task-103") for _ in range(3)]
    assert states == [bridge.outbox.TerminalReceiptState.UNKNOWN] * 3
    assert bridge._delivered_sentinel_path("task-103").exists()
    assert bridge._has_durable_terminal_receipt("task-103") is False
    receipt = bridge.outbox.read_terminal_receipt(
        bridge._result_receipt_root(), "task-103")
    assert receipt.state is bridge.outbox.TerminalReceiptState.ABSENT
    assert output.getvalue().count(
        "holding task-103: terminal outcome needs reconciliation") == 1


def test_ambiguous_notice_capacity_warns_once_then_stays_bounded():
    _reset()
    output = io.StringIO()
    with mock.patch.object(bridge, "_AMBIGUOUS_RECEIPT_NOTICE_LIMIT", 1), \
            contextlib.redirect_stdout(output):
        bridge._note_ambiguous_receipt("task-capacity-first")
        bridge._note_ambiguous_receipt("task-capacity-overflow")
        bridge._note_ambiguous_receipt("task-capacity-overflow-again")

    assert bridge._ambiguous_receipt_notices == {"task-capacity-first"}
    assert bridge._ambiguous_receipt_notice_overflow is True
    text = output.getvalue()
    assert text.count("holding task-capacity-first") == 1
    assert text.count("additional ambiguous receipt notices suppressed") == 1
    assert "holding task-capacity-overflow" not in text


def test_receipt_read_error_returns_unknown_and_bounds_reconciliation_notice():
    _reset()
    output = io.StringIO()
    with mock.patch.object(
            bridge.outbox, "read_terminal_receipt",
            side_effect=PermissionError("receipt unreadable")), \
            contextlib.redirect_stdout(output):
        states = [bridge._terminal_receipt_state("task-103a") for _ in range(3)]

    assert states == [bridge.outbox.TerminalReceiptState.UNKNOWN] * 3
    assert bridge._ambiguous_receipt_notices == {"task-103a"}
    text = output.getvalue()
    assert text.count("receipt read failed for task-103a: receipt unreadable") == 3
    assert text.count(
        "holding task-103a: terminal outcome needs reconciliation") == 1


def test_unreadable_legacy_sentinel_is_ambiguous_not_absent():
    _reset()

    class UnreadableSentinel:
        @staticmethod
        def lstat():
            raise PermissionError("injected")

    output = io.StringIO()
    with mock.patch.object(
            bridge, "_delivered_sentinel_path", return_value=UnreadableSentinel()), \
            contextlib.redirect_stdout(output):
        states = [bridge._terminal_receipt_state("task-103b") for _ in range(3)]
    assert states == [bridge.outbox.TerminalReceiptState.UNKNOWN] * 3
    assert bridge._has_durable_terminal_receipt("task-103b") is False
    assert output.getvalue().count(
        "holding task-103b: terminal outcome needs reconciliation") == 1


def test_receipt_write_failure_keeps_the_legacy_fallback():
    _reset()
    with mock.patch.object(
            bridge.outbox, "record_terminal_receipt", side_effect=OSError("disk full")):
        bridge._mark_delivered("task-104")
    assert bridge._delivered_sentinel_path("task-104").exists()
    assert bridge._is_delivered("task-104") is True


def test_legacy_sentinel_write_error_preserves_shared_receipt_and_audit():
    _reset()

    class UnwritableSentinel:
        @staticmethod
        def touch():
            raise PermissionError("sentinel unwritable")

    output = io.StringIO()
    with mock.patch.object(
            bridge, "_delivered_sentinel_path",
            return_value=UnwritableSentinel()), \
            mock.patch.object(bridge.result_audit, "record") as audit, \
            contextlib.redirect_stdout(output):
        bridge._mark_delivered("task-104b")

    receipt = bridge.outbox.read_terminal_receipt(
        bridge._result_receipt_root(), "task-104b")
    assert receipt.state is bridge.outbox.TerminalReceiptState.TERMINAL
    assert receipt.disposition is bridge.outbox.TerminalDisposition.DELIVERED
    assert not (bridge.DELIVERED_DIR / "task-104b.sentinel").exists()
    audit.assert_called_once_with("task-104b", "delivered", "discord")
    assert output.getvalue().count(
        "sentinel write failed for task-104b: sentinel unwritable") == 1


def test_skip_receipt_write_failure_leaves_legacy_fallback_and_audits():
    _reset()
    output = io.StringIO()
    with mock.patch.object(
            bridge.outbox, "record_terminal_receipt",
            side_effect=OSError("receipt disk full")), \
            mock.patch.object(bridge.result_audit, "record") as audit, \
            contextlib.redirect_stdout(output):
        bridge._record_skip_audit("task-104c", "deduped")

    assert bridge._delivered_sentinel_path("task-104c").exists()
    receipt = bridge.outbox.read_terminal_receipt(
        bridge._result_receipt_root(), "task-104c")
    assert receipt.state is bridge.outbox.TerminalReceiptState.ABSENT
    audit.assert_called_once_with("task-104c", "deduped", "discord")
    assert output.getvalue().count(
        "receipt write failed for task-104c: receipt disk full") == 1


def test_terminal_receipts_are_per_task():
    _reset()
    bridge._mark_delivered("task-105")
    assert bridge._is_delivered("task-105") is True
    assert bridge._is_delivered("task-106") is False


def test_bridge_delegates_terminal_decisions_to_outbox_before_send():
    src = (REPO / "src" / "discord-bridge.py").read_text(encoding="utf-8")
    start = src.index("async def poll_results():")
    end = src.index("\nasync def ", start + 1)
    body = src[start:end]
    receipt_pos = body.index("_terminal_receipt_state(task_id,")
    send_pos = body.index("await channel.send(")
    mark_pos = body.index("_mark_delivered(task_id,")
    assert receipt_pos < send_pos < mark_pos


def main():
    tests = [fn for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failures = []
    for fn in tests:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except Exception as exc:
            failures.append((fn.__name__, exc))
            print(f"  FAIL  {fn.__name__}: {exc}")
    if failures:
        raise SystemExit(1)
    print("PASS - Discord terminal receipt compatibility")


if __name__ == "__main__":
    main()
