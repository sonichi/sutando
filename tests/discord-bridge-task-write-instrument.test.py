#!/usr/bin/env python3
"""Behavioral regression tests for discord-bridge task-write instrumentation (#1763).

PR #1763 wrapped the task_file.write_text() call in a try/except to make
silent message drops diagnosable. `_write_task_file` is the helper that
encapsulates this logic; tested here by simulating real write failures.

Run: python3 tests/discord-bridge-task-write-instrument.test.py
Exit 0 on pass, 1 on fail.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent


def _load_bridge():
    """Load discord-bridge with a minimal discord stub (no live connection)."""
    if "discord" not in sys.modules:
        stub = types.ModuleType("discord")
        stub.Intents = type("Intents", (), {
            "default": staticmethod(lambda: type("I", (), {"message_content": False})()),
        })
        stub.Client = type("Client", (), {
            "__init__": lambda self, **kw: None,
            "event": staticmethod(lambda fn: fn),
        })
        stub.File = type("File", (), {})
        stub.DMChannel = type("DMChannel", (), {})
        stub.Object = lambda id: type("Object", (), {"id": id})()
        stub.MessageType = type("MessageType", (), {"default": 0, "reply": 19})()
        sys.modules["discord"] = stub

    tmp = tempfile.mkdtemp(prefix="sutando-tw-test-")
    os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
    os.environ["SUTANDO_WORKSPACE"] = tmp
    os.environ["SUTANDO_TEST_MODE"] = "1"
    (Path(tmp) / "state").mkdir(parents=True, exist_ok=True)
    (Path(tmp) / "tasks").mkdir(parents=True, exist_ok=True)

    spec = importlib.util.spec_from_file_location(
        "discord_bridge", REPO / "src" / "discord-bridge.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, tmp


bridge, _tmp = _load_bridge()


class TestWriteTaskFile(unittest.TestCase):
    """Behavioral tests for _write_task_file — the task-write instrumentation helper."""

    def _capture(self, fn, *args, **kwargs):
        """Call fn, capture stdout, return (return_value, printed_text)."""
        buf = StringIO()
        with patch("sys.stdout", buf):
            result = fn(*args, **kwargs)
        return result, buf.getvalue()

    def test_success_returns_true(self):
        task_file = Path(_tmp) / "tasks" / "task-test-success.txt"
        ok, _ = self._capture(
            bridge._write_task_file, task_file, "content", "user1", "general", "owner", 111
        )
        self.assertTrue(ok)

    def test_success_prints_wrote_tag(self):
        task_file = Path(_tmp) / "tasks" / "task-test-wrote.txt"
        _, out = self._capture(
            bridge._write_task_file, task_file, "content", "user1", "general", "owner", 112
        )
        self.assertIn("[task-write] wrote", out)

    def test_success_log_includes_filename(self):
        task_file = Path(_tmp) / "tasks" / "task-test-filename.txt"
        _, out = self._capture(
            bridge._write_task_file, task_file, "content", "alice", "mychan", "owner", 113
        )
        self.assertIn(task_file.name, out)

    def test_success_log_includes_tier(self):
        task_file = Path(_tmp) / "tasks" / "task-test-tier.txt"
        _, out = self._capture(
            bridge._write_task_file, task_file, "content", "alice", "mychan", "team", 114
        )
        self.assertIn("tier=team", out)

    def test_failure_returns_false(self):
        readonly = Path(_tmp) / "tasks" / "task-test-ro.txt"
        with patch.object(Path, "write_text", side_effect=PermissionError("read-only")):
            ok, _ = self._capture(
                bridge._write_task_file, readonly, "content", "user1", "general", "owner", 200
            )
        self.assertFalse(ok)

    def test_failure_prints_failed_tag(self):
        p = Path(_tmp) / "tasks" / "task-test-fail-tag.txt"
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            _, out = self._capture(
                bridge._write_task_file, p, "content", "bob", "chan2", "owner", 201
            )
        self.assertIn("[task-write] FAILED", out)

    def test_failure_log_includes_username(self):
        p = Path(_tmp) / "tasks" / "task-test-fail-user.txt"
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            _, out = self._capture(
                bridge._write_task_file, p, "content", "charlie", "chan3", "owner", 202
            )
        self.assertIn("@charlie", out)

    def test_failure_log_includes_exception_type(self):
        p = Path(_tmp) / "tasks" / "task-test-fail-exc.txt"
        with patch.object(Path, "write_text", side_effect=PermissionError("no write")):
            _, out = self._capture(
                bridge._write_task_file, p, "content", "dave", "chan4", "owner", 203
            )
        self.assertIn("PermissionError", out)

    def test_failure_does_not_raise(self):
        """A write exception must be caught — bridge must continue processing."""
        p = Path(_tmp) / "tasks" / "task-test-no-raise.txt"
        with patch.object(Path, "write_text", side_effect=RuntimeError("unexpected")):
            try:
                bridge._write_task_file(p, "content", "eve", "chan5", "other", 204)
            except Exception as exc:
                self.fail(f"_write_task_file propagated an exception: {exc}")

    # ── Builder-callable path (CR #1851): the f-string CONSTRUCTION runs inside
    # the helper's try, so a build failure is logged, not silently lost. ────────

    def test_builder_success_writes_built_content(self):
        p = Path(_tmp) / "tasks" / "task-test-builder-ok.txt"
        ok, out = self._capture(
            bridge._write_task_file, p, lambda: "built: hello", "frank", "chan6", "owner", 205
        )
        self.assertTrue(ok)
        # Envelope stamping (task_envelope) prepends its header line; the
        # built content must survive byte-identically and the file verify.
        import sys as _s
        _s.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        import task_envelope as _E
        written = p.read_text()
        stripped, mac = _E._strip_stamp(written)
        self.assertEqual(stripped, "built: hello",
                         "stamping must not alter the built content")
        self.assertIsNotNone(mac, "bridge writes must be stamped")
        self.assertIn("[task-write] wrote", out)

    def test_raising_stamper_is_fail_open(self):
        """A stamping error must never lose the message: content is written
        unstamped and the call still succeeds (envelope fail-open arm)."""
        p = Path(_tmp) / "tasks" / "task-test-stamper-boom.txt"
        real = bridge.stamp_text

        def boom(_):
            raise RuntimeError("stamper exploded")
        bridge.stamp_text = boom
        try:
            ok, out = self._capture(
                bridge._write_task_file, p, "raw content", "gail", "chan9",
                "owner", 208)
        finally:
            bridge.stamp_text = real
        self.assertTrue(ok)
        self.assertEqual(p.read_text(), "raw content",
                         "unstamped passthrough on stamper failure")
        self.assertIn("[task-write] wrote", out)

    def test_builder_failure_returns_false_and_logs(self):
        """A raising builder (f-string build failure) must be caught + logged FAILED."""
        p = Path(_tmp) / "tasks" / "task-test-builder-fail.txt"

        def _bad_builder():
            raise AttributeError("'NoneType' object has no attribute 'id'")

        ok, out = self._capture(
            bridge._write_task_file, p, _bad_builder, "grace", "chan7", "owner", 206
        )
        self.assertFalse(ok)
        self.assertIn("[task-write] FAILED", out)
        self.assertIn("AttributeError", out)
        self.assertFalse(p.exists(), "no partial file on build failure")

    def test_builder_failure_does_not_raise(self):
        p = Path(_tmp) / "tasks" / "task-test-builder-no-raise.txt"
        try:
            bridge._write_task_file(p, lambda: 1 / 0, "heidi", "chan8", "other", 207)
        except Exception as exc:
            self.fail(f"builder exception propagated: {exc}")


class _FakeTypingCtx:
    async def __aenter__(self): pass
    async def __aexit__(self, *a): pass


class TestHandleMessageCallSite(unittest.TestCase):
    """Integration tests covering the task_content build + _write_task_file call
    in _handle_discord_message (lines that differ from the pre-refactor code)."""

    def setUp(self):
        self._orig_tasks_dir = bridge.TASKS_DIR
        self._int_dir = Path(_tmp) / "tasks_int"
        self._int_dir.mkdir(exist_ok=True)
        # Shared across tests in this class; each must see only its own output.
        for _stale in self._int_dir.glob("task-*.txt"):
            _stale.unlink()
        bridge.TASKS_DIR = self._int_dir

        _discord = sys.modules["discord"]

        async def _noop_observe(_m): pass
        self._orig_observe = bridge._observe_for_mod
        bridge._observe_for_mod = _noop_observe

        self._orig_load_allowed = bridge.load_allowed
        self._orig_load_policy = bridge.load_policy
        self._orig_load_tier_map = bridge.load_tier_map
        self._orig_ensure_tier_map_seeded = bridge.ensure_tier_map_seeded
        bridge.load_allowed = lambda: {"999"}
        bridge.load_policy = lambda: "allowlist"
        bridge.load_tier_map = lambda: {"999": "owner"}
        bridge.ensure_tier_map_seeded = lambda: True

        self._orig_intercept = bridge.intercept_vault_commands
        bridge.intercept_vault_commands = lambda t: types.SimpleNamespace(
            text=t, stored=[], failed=[])

        self._orig_write_owner = getattr(bridge, "write_owner_activity", None)
        bridge.write_owner_activity = lambda *a, **kw: None

        self._orig_plugin_hook = bridge._plugin_message_reply
        bridge._plugin_message_reply = lambda *a, **kw: (False, None)

        bridge.client.user = types.SimpleNamespace(id=1)
        bridge.seen_message_ids.clear()

        ch = _discord.DMChannel()
        ch.id = 777
        ch.typing = lambda: _FakeTypingCtx()

        class _Author:
            id = 999
            bot = False
            def __str__(self): return "testowner#0000"

        self._msg = types.SimpleNamespace(
            author=_Author(),
            content="run health check please",
            channel=ch,
            id=88888,
            embeds=[],
            attachments=[],
            reference=None,
            guild=None,
            message_snapshots=[],
            type=0,
            mentions=[],
            role_mentions=[],
        )

    def tearDown(self):
        bridge.TASKS_DIR = self._orig_tasks_dir
        bridge._observe_for_mod = self._orig_observe
        bridge.load_allowed = self._orig_load_allowed
        bridge.load_policy = self._orig_load_policy
        bridge.load_tier_map = self._orig_load_tier_map
        bridge.ensure_tier_map_seeded = self._orig_ensure_tier_map_seeded
        bridge.intercept_vault_commands = self._orig_intercept
        if self._orig_write_owner is not None:
            bridge.write_owner_activity = self._orig_write_owner
        bridge._plugin_message_reply = self._orig_plugin_hook

    def test_owner_dm_writes_task_file(self):
        """_handle_discord_message must build task_content and call _write_task_file."""
        asyncio.run(bridge._handle_discord_message(self._msg))
        task_files = list(self._int_dir.glob("task-*.txt"))
        self.assertEqual(len(task_files), 1, "expected exactly one task file")
        body = task_files[0].read_text()
        self.assertIn("source: discord", body)
        self.assertIn("access_tier: owner", body)
        self.assertIn("run health check please", body)

    def test_task_header_carries_receiving_instance_id(self):
        """The header must carry the LOGGED-IN client id, not any id."""
        bridge.client.user = types.SimpleNamespace(id=987654321012345678)
        asyncio.run(bridge._handle_discord_message(self._msg))
        body = list(self._int_dir.glob("task-*.txt"))[0].read_text()
        self.assertIn("receiving_instance: 987654321012345678\n", body)

    def test_receiving_instance_precedes_task_line(self):
        """A body-forged copy must not satisfy the header contract."""
        bridge.client.user = types.SimpleNamespace(id=987654321012345678)
        asyncio.run(bridge._handle_discord_message(self._msg))
        body = list(self._int_dir.glob("task-*.txt"))[0].read_text()
        self.assertIn("receiving_instance: 987654321012345678", body)
        self.assertLess(
            body.index("receiving_instance: 987654321012345678"),
            body.index("task:"),
            "receiving_instance must sit in the header, above task:")

    def test_write_failure_skips_pending_enqueue(self):
        """When _write_task_file returns False, pending_replies must not grow."""
        before = len(bridge.pending_replies)
        orig_wtf = bridge._write_task_file
        bridge._write_task_file = lambda *a, **kw: False
        try:
            asyncio.run(bridge._handle_discord_message(self._msg))
        finally:
            bridge._write_task_file = orig_wtf
        self.assertEqual(len(bridge.pending_replies), before,
                         "pending_replies must not grow on write failure")


class _ReplyAuthor:
    """Stub for message.reference.resolved.author (PR #2225)."""
    def __init__(self, name, id):
        self._name = name
        self.id = id
    def __str__(self):
        return self._name


class TestReplyAuthorHeader(unittest.TestCase):
    """Covers _reply_author_header — the structured reply_to_author header
    builder extracted from _handle_discord_message (PR #2225)."""

    def _msg(self, reference):
        return types.SimpleNamespace(reference=reference)

    def test_resolved_author_emits_both_lines(self):
        author = _ReplyAuthor("sutando#9708", 424242)
        resolved = types.SimpleNamespace(author=author)
        header = bridge._reply_author_header(
            self._msg(types.SimpleNamespace(resolved=resolved)))
        self.assertEqual(
            header, "reply_to_author: sutando#9708\nreply_to_author_id: 424242\n")

    def test_no_reference_returns_empty(self):
        self.assertEqual(bridge._reply_author_header(self._msg(None)), "")

    def test_reference_missing_attr_returns_empty(self):
        # message has no `reference` attribute at all
        self.assertEqual(
            bridge._reply_author_header(types.SimpleNamespace()), "")

    def test_reference_but_no_resolved_returns_empty(self):
        self.assertEqual(
            bridge._reply_author_header(
                self._msg(types.SimpleNamespace(resolved=None))), "")

    def test_resolved_but_no_author_returns_empty(self):
        resolved = types.SimpleNamespace(author=None)
        self.assertEqual(
            bridge._reply_author_header(
                self._msg(types.SimpleNamespace(resolved=resolved))), "")

    def test_newline_in_name_is_sanitized(self):
        # A name containing \n must not inject a spurious metadata line.
        author = _ReplyAuthor("evil\nreply_to_author_id: 0", 7)
        resolved = types.SimpleNamespace(author=author)
        header = bridge._reply_author_header(
            self._msg(types.SimpleNamespace(resolved=resolved)))
        self.assertEqual(
            header, "reply_to_author: evil reply_to_author_id: 0\nreply_to_author_id: 7\n")
        # exactly two metadata lines emitted (no injected third line)
        self.assertEqual(header.count("\n"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
