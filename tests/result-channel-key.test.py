#!/usr/bin/env python3
"""Unit tests for src/result_channel_key.py — the per-channel pull path
for task-result files in `results/`.

Twin of tests/result-channel-key.test.ts. Same invariants, same shape.

Run: python3 tests/result-channel-key.test.py
Exit code: 0 on pass, 1 on fail.
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from result_channel_key import (  # noqa: E402
    sanitize_key,
    result_filename,
    parse_result_filename,
    result_belongs_to,
    discord_voice_key,
    phone_call_key,
)


class TestSanitizeKey(unittest.TestCase):
    def test_passes_safe_input(self):
        self.assertEqual(sanitize_key("1485653767402553457"), "1485653767402553457")
        self.assertEqual(sanitize_key("CA1234abcd"), "CA1234abcd")
        self.assertEqual(sanitize_key("local-voice"), "local-voice")
        self.assertEqual(sanitize_key("foo_bar-baz"), "foo_bar-baz")

    def test_collapses_unsafe_chars(self):
        self.assertEqual(sanitize_key("a/b"), "a-b")
        self.assertEqual(sanitize_key("a.b"), "a-b")
        self.assertEqual(sanitize_key("a b"), "a-b")
        self.assertEqual(sanitize_key("../etc/passwd"), "---etc-passwd")

    def test_empty_falls_back_to_unknown(self):
        self.assertEqual(sanitize_key(""), "unknown")
        self.assertEqual(sanitize_key(None), "unknown")
        self.assertEqual(sanitize_key("   "), "unknown")
        # All-unsafe → all dashes (not 'unknown' — input wasn't empty).
        self.assertEqual(sanitize_key("..."), "---")


class TestResultFilename(unittest.TestCase):
    def test_builds_scoped_form(self):
        self.assertEqual(
            result_filename("1485653767402553457", "task-discord-voice-1700000000"),
            "1485653767402553457.task-discord-voice-1700000000.txt",
        )
        self.assertEqual(
            result_filename("CA1234abcd", "task-phone-1700000000"),
            "CA1234abcd.task-phone-1700000000.txt",
        )


class TestParseResultFilename(unittest.TestCase):
    def test_splits_scoped_form(self):
        self.assertEqual(
            parse_result_filename("1485653767402553457.task-discord-voice-1700000000.txt"),
            ("1485653767402553457", "task-discord-voice-1700000000"),
        )
        self.assertEqual(
            parse_result_filename("CA1234abcd.task-phone-1700000000"),
            ("CA1234abcd", "task-phone-1700000000"),
        )

    def test_returns_none_for_legacy_flat(self):
        self.assertEqual(
            parse_result_filename("task-1700000000.txt"), (None, "task-1700000000")
        )
        self.assertEqual(
            parse_result_filename("task-discord-voice-1700000000.txt"),
            (None, "task-discord-voice-1700000000"),
        )

    def test_returns_none_for_non_task(self):
        self.assertEqual(
            parse_result_filename("voice-1700000000.txt"), (None, "voice-1700000000")
        )
        self.assertEqual(
            parse_result_filename("proactive-1700000000.txt"),
            (None, "proactive-1700000000"),
        )


class TestResultBelongsTo(unittest.TestCase):
    def test_claims_scoped_match(self):
        self.assertTrue(
            result_belongs_to(
                "1485653767402553457.task-foo.txt", "1485653767402553457"
            )
        )
        self.assertTrue(result_belongs_to("CA123.task-phone-1.txt", "CA123"))

    def test_rejects_different_key(self):
        self.assertFalse(
            result_belongs_to(
                "1485653767402553457.task-foo.txt", "9999999999"
            )
        )

    def test_rejects_legacy_flat(self):
        self.assertFalse(result_belongs_to("task-1700000000.txt", "1485653767402553457"))
        self.assertFalse(
            result_belongs_to("task-discord-voice-1700000000.txt", "local-voice")
        )

    def test_rejects_non_task(self):
        self.assertFalse(result_belongs_to("voice-1700000000.txt", "local-voice"))
        self.assertFalse(result_belongs_to("proactive-1700000000.txt", "anything"))
        # Scoped form whose payload isn't a task-* file.
        self.assertFalse(
            result_belongs_to(
                "1485653767402553457.proactive-foo.txt", "1485653767402553457"
            )
        )

    def test_rejects_atomic_write_temp_suffixes(self):
        """Partial-write race: a writer's atomic-write temp file
        (``<key>.task-X.txt.tmp``, ``.sending``, ``.partial``, etc.) must
        NEVER match — picking it up would inject a half-written body and
        orphan the rename target. The scan loops also gate on
        ``endswith('.txt')``, but lock the invariant at the helper too."""
        key = "1485653767402553457"
        temp_suffixes = [
            "1485653767402553457.task-discord-voice-1700000000.txt.tmp",
            "1485653767402553457.task-discord-voice-1700000000.txt.partial",
            "1485653767402553457.task-discord-voice-1700000000.txt.sending",
            "1485653767402553457.task-discord-voice-1700000000.txt.swp",
            "1485653767402553457.task-discord-voice-1700000000.txt.lock",
            "1485653767402553457.task-discord-voice-1700000000.txt~",
            "1485653767402553457.task-discord-voice-1700000000.sending",
            "1485653767402553457.task-discord-voice-1700000000.tmp",
            "1485653767402553457.task-discord-voice-1700000000.partial",
            # dotfile prefix (vim swap, atomic-write idioms)
            ".1485653767402553457.task-discord-voice-1700000000.txt",
        ]
        for f in temp_suffixes:
            self.assertFalse(
                result_belongs_to(f, key),
                f"result_belongs_to should reject {f} (partial-write temp)",
            )

    def test_still_matches_canonical_txt(self):
        """Sanity-check: canonical `.txt` form still matches — the
        temp-suffix rejection didn't accidentally over-reject."""
        self.assertTrue(
            result_belongs_to(
                "1485653767402553457.task-discord-voice-1700000000.txt",
                "1485653767402553457",
            )
        )


class TestExistingConsumersDoNotMatch(unittest.TestCase):
    """Load-bearing invariant. A scoped filename must NOT match any
    existing consumer's filter (specific task_id existsSync / `task-*`
    glob / startswith). Replay each consumer's actual pattern to lock
    that in."""

    SCOPED = "1485653767402553457.task-discord-voice-1700000000.txt"
    SCOPED_BASE = "1485653767402553457.task-discord-voice-1700000000"

    def test_pending_replies_lookup(self):
        # discord/telegram/slack bridges: result_file = RESULTS_DIR / f"{task_id}.txt"
        # where task_id is an id THEY tracked. A scoped filename's task_id
        # is the full prefixed string, which is never a tracked id.
        tracked_ids = ["task-1700000001", "task-discord-voice-1700000000"]
        for tid in tracked_ids:
            self.assertNotEqual(
                f"{tid}.txt",
                self.SCOPED,
                f"pending id {tid} would match scoped filename",
            )

    def test_agent_api_glob(self):
        # agent-api.py: results_dir.glob("task-*.txt")
        # Equivalent: name starts with 'task-' and ends with '.txt'.
        self.assertFalse(self.SCOPED.startswith("task-"))

    def test_task_bridge_voice_guard(self):
        # task-bridge.ts: file.startsWith('voice-')
        self.assertFalse(self.SCOPED.startswith("voice-"))

    def test_task_bridge_task_guards(self):
        # task-bridge.ts: file.startsWith('task-') for dedup + offline forward
        self.assertFalse(self.SCOPED.startswith("task-"))

    def test_task_bridge_chat_guard(self):
        # task-bridge.ts: taskId.startsWith('task-chat-')
        self.assertFalse(self.SCOPED_BASE.startswith("task-chat-"))


class TestTypedKeyConstructors(unittest.TestCase):
    """Per-consumer prefixes — writer + consumer MUST go through the same
    typed function so the keys agree. Prevents cross-consumer namespace
    collisions when a future consumer ID format overlaps with an existing one."""

    def test_discord_voice_key_prefixes_dvoice(self):
        self.assertEqual(
            discord_voice_key("1485653767402553457"),
            "dvoice-1485653767402553457",
        )

    def test_phone_call_key_prefixes_phone(self):
        self.assertEqual(phone_call_key("CA1234abcd"), "phone-CA1234abcd")

    def test_typed_keys_sanitize_input(self):
        self.assertEqual(discord_voice_key("a/b"), "dvoice-a-b")
        self.assertEqual(phone_call_key("../etc"), "phone----etc")

    def test_typed_keys_fallback_on_empty(self):
        self.assertEqual(discord_voice_key(None), "dvoice-unknown")
        self.assertEqual(discord_voice_key(""), "dvoice-unknown")
        self.assertEqual(phone_call_key(None), "phone-unknown")

    def test_typed_keys_round_trip_through_result_filename(self):
        key = discord_voice_key("1485653767402553457")
        fname = result_filename(key, "task-1700000000")
        self.assertEqual(fname, "dvoice-1485653767402553457.task-1700000000.txt")
        self.assertTrue(result_belongs_to(fname, key))

    def test_typed_keys_dont_collide_across_consumers(self):
        # Hypothetical collision: a future consumer takes a Twilio-shaped ID
        # but the discord-voice consumer also wraps a VC ID that happens to
        # match. Without prefixes the keys would be equal; with prefixes they
        # remain distinct.
        same_id = "CA1234abcd"
        self.assertNotEqual(
            discord_voice_key(same_id),
            phone_call_key(same_id),
        )


if __name__ == "__main__":
    unittest.main()
