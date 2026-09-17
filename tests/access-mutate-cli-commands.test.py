#!/usr/bin/env python3
"""Branch coverage for the access-mutate.py commands the bridge newly routes
through it, plus the transition-window `pair` path regression (#3318).

tests/access-mutate-cli.test.py covers only `group-append` / `group-rm-allow`
and arg parsing. Everything the `/discord:access` skill now delegates —
`pair`, `deny`, `allow`, `remove`, `policy`, `group-add`, `group-rm`, `set` —
was unexercised.

The `pair` case is not just coverage. `_backup()` writes the durable backup
during a successful mutation, and `resolve_discord_access_file()` returns the
canonical path once that backup exists. `_pair()` used to resolve twice: once
for the transaction and once for the approved-marker directory. In the
transition window (canonical absent, legacy populated) those two calls return
DIFFERENT parents, so the grant committed to legacy while the "you're in"
marker was written under canonical — which `_approved_dirs()` does not poll.
The sender is authorized and never told. TestPairPathOwnership pins that the
marker lands beside the file the mutation actually committed to.

Isolation: same two-axis fixture as tests/access-mutate-cli.test.py —
$CLAUDE_CONFIG_DIR for the live access.json and a direct monkeypatch of
access_store.resolve_workspace for the durable backup, which resolves through
resolve_workspace() and honors neither env var.

Run: python3 tests/access-mutate-cli-commands.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import access_store  # noqa: E402

# discord-bridge.py resolves host config at import time, so isolating
# CLAUDE_CONFIG_DIR inside setUp() would already be too late.
_BRIDGE_CCD = tempfile.mkdtemp(prefix="access-mutate-cmds-bridge-ccd-")
_BRIDGE_SRC = tempfile.mkdtemp(prefix="access-mutate-cmds-bridge-vanilla-")
os.environ["CLAUDE_CONFIG_DIR"] = _BRIDGE_CCD
os.environ["SOURCE_CLAUDE_CONFIG_DIR"] = _BRIDGE_SRC
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
_bridge_ch = Path(_BRIDGE_CCD) / "channels" / "discord"
_bridge_ch.mkdir(parents=True, exist_ok=True)
(_bridge_ch / "access.json").write_text(json.dumps({"allowFrom": ["4242"]}))

try:  # pragma: no cover - present in dev, absent in clean CI
    import discord  # noqa: F401
except Exception:
    _stub = types.ModuleType("discord")
    _stub.Intents = type("Intents", (), {"default": staticmethod(
        lambda: type("I", (), {"message_content": False})())})
    _stub.Client = type("Client", (), {"__init__": lambda self, **kw: None,
                                       "event": staticmethod(lambda fn: fn)})
    _stub.File = type("File", (), {"__init__": lambda self, *a, **kw: None})
    _stub.Message = type("Message", (), {})
    _stub.DMChannel = type("DMChannel", (), {})
    sys.modules["discord"] = _stub

_bspec = importlib.util.spec_from_file_location(
    "dbridge_access_mutate_cmds", REPO / "src" / "discord-bridge.py")
db_bridge = importlib.util.module_from_spec(_bspec)
sys.modules["dbridge_access_mutate_cmds"] = db_bridge
_bspec.loader.exec_module(db_bridge)

_spec = importlib.util.spec_from_file_location(
    "access_mutate_cmds", REPO / "scripts" / "access-mutate.py"
)
access_mutate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(access_mutate)


class _Isolated(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="access-mutate-cmds-"))
        self._old_ccd = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.d / "ccd")
        self._old_rw = access_store.resolve_workspace
        access_store.resolve_workspace = lambda *a, **kw: self.d / "workspace"
        self.access_file = self.d / "ccd" / "channels" / "discord" / "access.json"
        self.backup_file = self.d / "workspace" / "state" / "auth" / "discord-access-backup.json"

    def tearDown(self):
        if self._old_ccd is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._old_ccd
        access_store.resolve_workspace = self._old_rw

    def _write(self, doc):
        self.access_file.parent.mkdir(parents=True, exist_ok=True)
        self.access_file.write_text(json.dumps(doc))

    def _read(self):
        return json.loads(self.access_file.read_text())

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = access_mutate.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _ok(self, argv):
        rc, out, _ = self._run(["access-mutate.py"] + argv)
        self.assertEqual(rc, 0, out)
        return json.loads(out)

    def _fail(self, argv):
        rc, out, _ = self._run(["access-mutate.py"] + argv)
        self.assertEqual(rc, 1, out)
        return json.loads(out)


class TestPair(_Isolated):
    def _pending(self, code, sender="s-1", chat="c-1", ttl_ms=60_000):
        return {"dmPolicy": "pairing", "allowFrom": [], "pending": {
            code: {"senderId": sender, "chatId": chat,
                   "expiresAt": int(time.time() * 1000) + ttl_ms}}}

    def test_pair_grants_clears_pending_and_writes_marker(self):
        self._write(self._pending("ABC123"))
        r = self._ok(["pair", "ABC123"])
        self.assertEqual((r["senderId"], r["chatId"]), ("s-1", "c-1"))
        doc = self._read()
        self.assertEqual(doc["allowFrom"], ["s-1"])
        self.assertEqual(doc["pending"], {})
        marker = self.access_file.parent / "approved" / "s-1"
        self.assertTrue(marker.exists(), "approved marker not written")
        self.assertEqual(marker.read_text(), "c-1")

    def test_pair_expired_code_is_refused_and_file_untouched(self):
        self._write(self._pending("OLD999", ttl_ms=-1))
        before = self.access_file.read_text()
        r = self._fail(["pair", "OLD999"])
        self.assertIn("not found or expired", r["error"])
        self.assertEqual(self.access_file.read_text(), before)
        self.assertFalse((self.access_file.parent / "approved").exists())

    def test_pair_unknown_code_is_refused(self):
        self._write({"dmPolicy": "pairing", "allowFrom": [], "pending": {}})
        r = self._fail(["pair", "NOPE"])
        self.assertIn("not found or expired", r["error"])

    def test_pair_already_allowed_sender_is_not_duplicated(self):
        doc = self._pending("DUP1", sender="s-9")
        doc["allowFrom"] = ["s-9"]
        self._write(doc)
        self._ok(["pair", "DUP1"])
        self.assertEqual(self._read()["allowFrom"], ["s-9"])

    def test_pair_marker_write_failure_surfaces_as_warning_not_failure(self):
        self._write(self._pending("WARN1"))
        # approved/ occupied by a FILE — mkdir raises, the grant still stands.
        (self.access_file.parent / "approved").write_text("not a directory")
        r = self._ok(["pair", "WARN1"])
        self.assertIn("warning", r)
        self.assertIn("approved-marker write failed", r["warning"])
        self.assertEqual(self._read()["allowFrom"], ["s-1"])


class TestPairPendingNotifyAtomicity(_Isolated):
    """#3318 blocker 1: the grant and the 'approval owed' record must land in
    ONE transaction. These simulate a crash between _pair()'s locked mutator
    returning and the external marker file ever being written — pendingNotify
    must already be durable at that point, with no dependency on the marker."""

    def _pending(self, code, sender="s-1", chat="c-1", ttl_ms=60_000):
        return {"dmPolicy": "pairing", "allowFrom": [], "pending": {
            code: {"senderId": sender, "chatId": chat,
                   "expiresAt": int(time.time() * 1000) + ttl_ms}}}

    def test_pending_notify_committed_in_the_same_write_as_the_grant(self):
        self._write(self._pending("PN01"))
        r = self._ok(["pair", "PN01"])
        self.assertEqual(r["senderId"], "s-1")
        # Read the file back directly — no marker check anywhere in this
        # path — proving pendingNotify is durable on its own.
        doc = self._read()
        self.assertEqual(doc["allowFrom"], ["s-1"])
        self.assertEqual(doc["pendingNotify"], {"s-1": "c-1"})

    def test_pending_notify_survives_even_when_the_marker_write_fails(self):
        self._write(self._pending("PN02"))
        (self.access_file.parent / "approved").write_text("not a directory")
        r = self._ok(["pair", "PN02"])
        self.assertIn("warning", r)
        # The obligation is recorded regardless of the marker failure.
        self.assertEqual(self._read()["pendingNotify"], {"s-1": "c-1"})

    def test_ack_notify_clears_the_obligation(self):
        self._write(self._pending("PN03"))
        self._ok(["pair", "PN03"])
        r = self._ok(["ack-notify", "s-1"])
        self.assertTrue(r["removed"])
        self.assertEqual(self._read()["pendingNotify"], {})

    def test_ack_notify_on_an_absent_sender_is_a_noop_success_not_an_error(self):
        self._write({"dmPolicy": "pairing", "allowFrom": [], "pending": {}, "pendingNotify": {}})
        r = self._ok(["ack-notify", "never-paired"])
        self.assertFalse(r["removed"])

    def test_ack_notify_is_idempotent_on_retry(self):
        self._write(self._pending("PN04"))
        self._ok(["pair", "PN04"])
        first = self._ok(["ack-notify", "s-1"])
        second = self._ok(["ack-notify", "s-1"])
        self.assertTrue(first["removed"])
        self.assertFalse(second["removed"])

    def test_pending_notify_keys_multiple_senders_independently(self):
        self._write(self._pending("PN05", sender="s-a", chat="c-a"))
        self._ok(["pair", "PN05"])
        doc = self._read()
        doc["pending"] = {"PN06": {"senderId": "s-b", "chatId": "c-b",
                                    "expiresAt": int(time.time() * 1000) + 60_000}}
        self.access_file.write_text(json.dumps(doc))
        self._ok(["pair", "PN06"])
        self.assertEqual(self._read()["pendingNotify"], {"s-a": "c-a", "s-b": "c-b"})
        self._ok(["ack-notify", "s-a"])
        self.assertEqual(self._read()["pendingNotify"], {"s-b": "c-b"})


class TestBridgePendingNotifyAck(_Isolated):
    """Consumer-side ack/park primitives, exercised directly against the
    imported bridge module — mirrors the CLI-side assertions above so both
    halves of the same locked-mutator contract are pinned."""

    def setUp(self):
        super().setUp()
        self._write({"dmPolicy": "pairing", "allowFrom": ["s-1"], "pending": {},
                     "pendingNotify": {"s-1": "c-1", "s-2": "c-2"}})
        self._old_access_file = db_bridge.ACCESS_FILE
        db_bridge.ACCESS_FILE = self.access_file

    def tearDown(self):
        db_bridge.ACCESS_FILE = self._old_access_file
        super().tearDown()

    def test_ack_pending_notify_removes_only_the_named_sender(self):
        db_bridge._ack_pending_notify("s-1")
        self.assertEqual(self._read()["pendingNotify"], {"s-2": "c-2"})

    def test_ack_pending_notify_on_absent_sender_does_not_raise_or_touch_file(self):
        before = self.access_file.read_text()
        db_bridge._ack_pending_notify("never-there")  # must not raise
        self.assertEqual(self.access_file.read_text(), before)

    def test_park_pending_notify_moves_entry_to_notify_failed(self):
        db_bridge._park_pending_notify("s-1", "c-1")
        doc = self._read()
        self.assertNotIn("s-1", doc["pendingNotify"])
        self.assertEqual(doc["notifyFailed"], {"s-1": "c-1"})
        # The other sender's obligation is untouched.
        self.assertEqual(doc["pendingNotify"], {"s-2": "c-2"})

    def test_park_pending_notify_on_absent_sender_is_a_noop(self):
        before = self.access_file.read_text()
        db_bridge._park_pending_notify("never-there", "c-x")
        self.assertEqual(self.access_file.read_text(), before)


class TestPairPathOwnership(_Isolated):
    """The marker must land beside the access.json the mutation committed to,
    even when the resolver's answer changes mid-transaction."""

    def _flipping_resolver(self, first: Path, later: Path):
        calls = []

        def _resolve():
            calls.append(1)
            return first if len(calls) == 1 else later
        return _resolve, calls

    def test_marker_follows_the_committed_path_not_a_second_resolve(self):
        legacy = self.d / "legacy" / "channels" / "discord" / "access.json"
        canonical = self.d / "canonical" / "channels" / "discord" / "access.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"dmPolicy": "pairing", "allowFrom": [], "pending": {
            "TW01": {"senderId": "s-tw", "chatId": "c-tw",
                     "expiresAt": int(time.time() * 1000) + 60_000}}}))

        resolver, calls = self._flipping_resolver(legacy, canonical)
        old = access_mutate.resolve_discord_access_file
        access_mutate.resolve_discord_access_file = resolver
        try:
            r = self._ok(["pair", "TW01"])
        finally:
            access_mutate.resolve_discord_access_file = old

        # Behaviour first: these are what the sender actually experiences, and
        # they must fail on their own if the marker is ever stranded again.
        self.assertEqual(r["senderId"], "s-tw")
        self.assertEqual(json.loads(legacy.read_text())["allowFrom"], ["s-tw"])
        self.assertTrue((legacy.parent / "approved" / "s-tw").exists(),
                        "marker not written beside the committed access.json")
        self.assertFalse((canonical.parent / "approved" / "s-tw").exists(),
                         "marker stranded under the path the grant did NOT go to")
        self.assertEqual(len(calls), 1,
                         "pair resolved the access path more than once; the second "
                         "answer can name a different file than the one committed")

    def test_committed_marker_dir_is_one_the_bridge_polls(self):
        """Consumer half: _approved_dirs() is derived from the bridge's own
        ACCESS_FILE, so a marker beside a DIFFERENT access.json is never seen."""
        db = db_bridge
        legacy = self.d / "legacy" / "channels" / "discord" / "access.json"
        canonical = self.d / "canonical" / "channels" / "discord" / "access.json"
        old_af = db.ACCESS_FILE
        db.ACCESS_FILE = legacy
        try:
            polled = db._approved_dirs()
        finally:
            db.ACCESS_FILE = old_af
        self.assertIn(legacy.parent / "approved", polled)
        self.assertNotIn(canonical.parent / "approved", polled)


class TestDenyAllowRemovePolicy(_Isolated):
    def setUp(self):
        super().setUp()
        self._write({"dmPolicy": "pairing", "allowFrom": ["keep"], "pending": {
            "CODE1": {"senderId": "s-2", "chatId": "c-2",
                      "expiresAt": int(time.time() * 1000) + 60_000}}})

    def test_deny_removes_pending_code(self):
        self.assertTrue(self._ok(["deny", "CODE1"])["removed"])
        self.assertEqual(self._read()["pending"], {})

    def test_deny_unknown_code_is_a_noop(self):
        self.assertFalse(self._ok(["deny", "GONE"])["removed"])

    def test_allow_appends(self):
        self.assertTrue(self._ok(["allow", "s-new"])["added"])
        self.assertEqual(self._read()["allowFrom"], ["keep", "s-new"])

    def test_allow_existing_is_a_noop(self):
        self.assertFalse(self._ok(["allow", "keep"])["added"])

    def test_remove_drops_the_sender(self):
        self.assertTrue(self._ok(["remove", "keep"])["removed"])
        self.assertEqual(self._read()["allowFrom"], [])

    def test_remove_absent_is_a_noop(self):
        self.assertFalse(self._ok(["remove", "never"])["removed"])

    def test_policy_accepts_each_valid_mode(self):
        for mode in ("pairing", "allowlist", "disabled"):
            self.assertEqual(self._ok(["policy", mode])["dmPolicy"], mode)
            self.assertEqual(self._read()["dmPolicy"], mode)

    def test_policy_rejects_an_invalid_mode_without_touching_the_file(self):
        before = self.access_file.read_text()
        self.assertIn("invalid policy", self._fail(["policy", "open"])["error"])
        self.assertEqual(self.access_file.read_text(), before)


class TestGroupAddRemove(_Isolated):
    def setUp(self):
        super().setUp()
        self._write({"dmPolicy": "pairing", "allowFrom": [], "pending": {}})

    def test_group_add_defaults_to_require_mention(self):
        r = self._ok(["group-add", "ch-1"])
        self.assertTrue(r["requireMention"])
        self.assertEqual(r["allowFrom"], [])
        self.assertEqual(self._read()["groups"]["ch-1"],
                         {"requireMention": True, "allowFrom": []})

    def test_group_add_no_mention_flag(self):
        self.assertFalse(self._ok(["group-add", "ch-2", "--no-mention"])["requireMention"])

    def test_group_add_allow_space_form(self):
        self.assertEqual(self._ok(["group-add", "ch-3", "--allow", "a,b"])["allowFrom"], ["a", "b"])

    def test_group_add_allow_equals_form(self):
        self.assertEqual(self._ok(["group-add", "ch-4", "--allow=a,,c"])["allowFrom"], ["a", "c"])

    def test_group_add_allow_without_a_value_is_rejected(self):
        self.assertIn("bad group-add flags", self._fail(["group-add", "ch-5", "--allow"])["error"])

    def test_group_add_unknown_flag_is_rejected(self):
        self.assertIn("bad group-add flags", self._fail(["group-add", "ch-6", "--nope"])["error"])

    def test_group_add_overwrites_an_existing_entry(self):
        self._ok(["group-add", "ch-7", "--allow", "a"])
        self._ok(["group-add", "ch-7", "--no-mention"])
        self.assertEqual(self._read()["groups"]["ch-7"],
                         {"requireMention": False, "allowFrom": []})

    def test_group_rm_removes_and_is_idempotent(self):
        self._ok(["group-add", "ch-8"])
        self.assertTrue(self._ok(["group-rm", "ch-8"])["removed"])
        self.assertNotIn("ch-8", self._read()["groups"])
        self.assertFalse(self._ok(["group-rm", "ch-8"])["removed"])


class TestSet(_Isolated):
    def setUp(self):
        super().setUp()
        self._write({"dmPolicy": "pairing", "allowFrom": [], "pending": {}})

    def test_set_ack_reaction_passes_through_as_a_string(self):
        self.assertEqual(self._ok(["set", "ackReaction", "eyes"])["ackReaction"], "eyes")
        self.assertEqual(self._read()["ackReaction"], "eyes")

    def test_set_reply_to_mode_accepts_each_valid_mode(self):
        for mode in ("off", "first", "all"):
            self.assertEqual(self._ok(["set", "replyToMode", mode])["replyToMode"], mode)

    def test_set_reply_to_mode_rejects_an_invalid_mode(self):
        self.assertIn("invalid replyToMode", self._fail(["set", "replyToMode", "some"])["error"])

    def test_set_text_chunk_limit_is_coerced_to_int(self):
        self.assertEqual(self._ok(["set", "textChunkLimit", "1900"])["textChunkLimit"], 1900)
        self.assertIsInstance(self._read()["textChunkLimit"], int)

    def test_set_text_chunk_limit_rejects_a_non_number(self):
        self.assertIn("must be a number", self._fail(["set", "textChunkLimit", "wide"])["error"])

    def test_set_chunk_mode_accepts_both_and_rejects_others(self):
        for mode in ("length", "newline"):
            self.assertEqual(self._ok(["set", "chunkMode", mode])["chunkMode"], mode)
        self.assertIn("invalid chunkMode", self._fail(["set", "chunkMode", "word"])["error"])

    def test_set_mention_patterns_parses_a_json_array(self):
        self.assertEqual(self._ok(["set", "mentionPatterns", '["a","b"]'])["mentionPatterns"],
                         ["a", "b"])

    def test_set_mention_patterns_rejects_malformed_json(self):
        self.assertIn("must be a JSON array of strings",
                      self._fail(["set", "mentionPatterns", "[a,"])["error"])

    def test_set_mention_patterns_rejects_a_non_list_and_non_string_members(self):
        self.assertIn("must be a JSON array of strings",
                      self._fail(["set", "mentionPatterns", '"a"'])["error"])
        self.assertIn("must be a JSON array of strings",
                      self._fail(["set", "mentionPatterns", "[1,2]"])["error"])

    def test_set_unknown_key_is_rejected_without_touching_the_file(self):
        before = self.access_file.read_text()
        self.assertIn("unknown key", self._fail(["set", "nope", "x"])["error"])
        self.assertEqual(self.access_file.read_text(), before)

    def test_set_wrong_arity_prints_usage(self):
        rc, _, err = self._run(["access-mutate.py", "set", "ackReaction"])
        self.assertEqual(rc, 1)
        self.assertIn("usage:", err)


class TestArityForNewlyRoutedCommands(_Isolated):
    def test_each_one_arg_command_rejects_the_wrong_arity(self):
        for cmd in ("pair", "deny", "allow", "remove", "ack-notify", "policy", "group-rm"):
            for rest in ([], ["a", "b"]):
                rc, _, err = self._run(["access-mutate.py", cmd] + rest)
                self.assertEqual(rc, 1, cmd)
                self.assertIn("usage:", err)

    def test_group_add_requires_a_channel_id(self):
        rc, _, err = self._run(["access-mutate.py", "group-add"])
        self.assertEqual(rc, 1)
        self.assertIn("usage:", err)




class _Sentinel(Exception):
    """Breaks a poller loop after exactly one iteration."""


class TestSingleSendOwner(_Isolated):
    """Two pollers, one grant, one confirmation.

    `_pair()` leaves BOTH durable records — the legacy `approved/<sender>`
    marker and `pendingNotify[sender]`. `poll_approved()` must adopt the
    marker rather than deliver it, so only `poll_pending_notify()` sends.
    """

    def _drive(self, coro_fn, sends):
        class _Chan:
            def __init__(self, cid): self.cid = cid

            async def send(self, text):
                sends.append((self.cid, text))

        class _Client:
            async def fetch_channel(self, cid):
                return _Chan(cid)

        db_bridge.ACCESS_FILE = self.access_file
        db_bridge.client = _Client()

        async def _sleep(_secs):
            raise _Sentinel()

        orig = db_bridge.asyncio.sleep
        db_bridge.asyncio.sleep = _sleep
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                asyncio.run(coro_fn())
        except _Sentinel:
            pass
        finally:
            db_bridge.asyncio.sleep = orig

    def test_only_one_confirmation_from_the_post_pair_state(self):
        self._write({"dmPolicy": "pairing", "allowFrom": [], "pending": {
            "ONE1": {"senderId": "s-one", "chatId": "424242",
                     "expiresAt": int(time.time() * 1000) + 60_000}}})
        self._ok(["pair", "ONE1"])

        marker = self.access_file.parent / "approved" / "s-one"
        doc = self._read()
        self.assertTrue(marker.exists(), "premise: pair must leave the legacy marker")
        self.assertEqual(doc.get("pendingNotify", {}).get("s-one"), "424242",
                         "premise: pair must leave the pendingNotify obligation")

        sends = []
        self._drive(db_bridge.poll_approved, sends)
        self.assertEqual(sends, [], "poll_approved must adopt, never deliver")
        self.assertFalse(marker.exists(), "adopted marker should be consumed")
        self.assertEqual(self._read().get("pendingNotify", {}).get("s-one"), "424242",
                         "the obligation must survive adoption")

        self._drive(db_bridge.poll_pending_notify, sends)
        self.assertEqual(len(sends), 1, f"exactly one confirmation, got {sends}")
        self.assertEqual(sends[0][1], "You're in! Access approved.")
        self.assertNotIn("s-one", self._read().get("pendingNotify", {}),
                         "a delivered obligation must be acked")

    def test_a_marker_with_no_pending_entry_is_still_delivered_once(self):
        """Legacy ingress: the upstream plugin writes a marker and no
        pendingNotify, so adoption is what keeps that path working."""
        self._write({"dmPolicy": "pairing", "allowFrom": ["s-legacy"], "pending": {}})
        d = self.access_file.parent / "approved"
        d.mkdir(parents=True, exist_ok=True)
        (d / "s-legacy").write_text("515151")

        sends = []
        self._drive(db_bridge.poll_approved, sends)
        self.assertEqual(sends, [])
        self.assertEqual(self._read().get("pendingNotify", {}).get("s-legacy"), "515151")

        self._drive(db_bridge.poll_pending_notify, sends)
        self.assertEqual(len(sends), 1, f"exactly one confirmation, got {sends}")
        self.assertEqual(sends[0][0], 515151)

    def test_pending_first_order_is_not_duplicated_by_a_stale_marker(self):
        """Reviewer repro (#3318, review 5004371632): the two pollers are
        independent async loops and can interleave pendingNotify-first
        instead of marker-first. `poll_pending_notify()` sends and acks
        before `poll_approved()` ever looks at the still-on-disk legacy
        marker; adopting that stale marker afterward must not re-arm the
        already-fulfilled obligation and trigger a second send."""
        self._write({"dmPolicy": "pairing", "allowFrom": [], "pending": {
            "ONE2": {"senderId": "s-one", "chatId": "424242",
                     "expiresAt": int(time.time() * 1000) + 60_000}}})
        self._ok(["pair", "ONE2"])

        marker = self.access_file.parent / "approved" / "s-one"
        self.assertTrue(marker.exists(), "premise: pair must leave the legacy marker")

        sends = []
        self._drive(db_bridge.poll_pending_notify, sends)
        self.assertEqual(len(sends), 1, f"after_pending_first: sends={sends}")
        self.assertTrue(marker.exists(), "after_pending_first: marker=False (not yet adopted)")
        self.assertEqual(self._read().get("pendingNotify", {}), {},
                         "after_pending_first: pendingNotify must be acked")

        self._drive(db_bridge.poll_approved, sends)
        self.assertEqual(len(sends), 1,
                         f"after_marker_adopt: a stale marker must not re-arm an "
                         f"already-acked obligation, sends={sends}")
        self.assertFalse(marker.exists(), "after_marker_adopt: marker still consumed")
        self.assertEqual(self._read().get("pendingNotify", {}), {},
                         "after_marker_adopt: pendingNotify must stay empty")

        self._drive(db_bridge.poll_pending_notify, sends)
        self.assertEqual(len(sends), 1,
                         f"after_pending_second: exactly one confirmation total, sends={sends}")

if __name__ == "__main__":
    _r = unittest.main(exit=False)
    try:
        import coverage

        _cov = coverage.Coverage.current()
        if _cov is not None:
            _cov.save()
    except Exception:
        pass
    sys.exit(0 if _r.result.wasSuccessful() else 1)
