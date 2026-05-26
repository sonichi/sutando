#!/usr/bin/env python3
"""
Tests for skills/discord-voice/scripts/join_trigger.py

Coverage:
  load_join_phrase      — workspace config, repo example, default fallback, missing file
  message_is_join_phrase — exact match, case-insensitive, boundary rules, empty/no-match
  _server_already_running — running process matches, different channel no-match, pgrep error
  _spawn_voice_server   — server script absent, Popen success, Popen failure
  _enqueue_context_prep_task — file written with correct fields, never raises
  handle_join_trigger   — not-in-voice-channel, already-running, spawn-fails, success path

Run: python3 skills/discord-voice/tests/join_trigger.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "join_trigger",
    REPO / "skills" / "discord-voice" / "scripts" / "join_trigger.py",
)
jt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jt)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_message(text="za warudo", access_tier="owner", has_voice=True, channel_id=111, guild_id=222):
    """Build a minimal fake Discord message object."""
    msg = MagicMock()
    msg.content = text

    # Author with voice state
    author = MagicMock()
    author.id = 999
    if has_voice:
        channel = MagicMock()
        channel.id = channel_id
        channel.name = "General Voice"
        guild = MagicMock()
        guild.id = guild_id
        channel.guild = guild
        author.voice = MagicMock()
        author.voice.channel = channel
    else:
        author.voice = None
    msg.author = author

    msg.channel = MagicMock()
    msg.channel.send = MagicMock()
    msg._join_trigger_guilds = None
    return msg


# ── load_join_phrase ──────────────────────────────────────────────────────────

class TestLoadJoinPhrase(unittest.TestCase):
    def test_default_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            tmpdir = Path(d)
            with patch.object(jt, "_config_path", return_value=tmpdir / "nonexistent.json"), \
                 patch.object(jt, "_REPO_ROOT", tmpdir):
                phrase = jt.load_join_phrase()
        self.assertEqual(phrase, jt.DEFAULT_JOIN_PHRASE)

    def test_workspace_config_wins(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "discord-voice.json"
            cfg.write_text(json.dumps({"join_phrase": "open sesame"}))
            with patch.object(jt, "_config_path", return_value=cfg):
                phrase = jt.load_join_phrase()
        self.assertEqual(phrase, "open sesame")

    def test_phrase_lowercased(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "discord-voice.json"
            cfg.write_text(json.dumps({"join_phrase": "ZA WARUDO"}))
            with patch.object(jt, "_config_path", return_value=cfg):
                phrase = jt.load_join_phrase()
        self.assertEqual(phrase, "za warudo")

    def test_blank_phrase_falls_through(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "discord-voice.json"
            cfg.write_text(json.dumps({"join_phrase": "   "}))
            with patch.object(jt, "_config_path", return_value=cfg), \
                 patch.object(jt, "_REPO_ROOT", Path(d)):
                phrase = jt.load_join_phrase()
        self.assertEqual(phrase, jt.DEFAULT_JOIN_PHRASE)

    def test_malformed_json_falls_through(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "discord-voice.json"
            cfg.write_text("not json{{{")
            with patch.object(jt, "_config_path", return_value=cfg), \
                 patch.object(jt, "_REPO_ROOT", Path(d)):
                phrase = jt.load_join_phrase()
        self.assertEqual(phrase, jt.DEFAULT_JOIN_PHRASE)


# ── message_is_join_phrase ────────────────────────────────────────────────────

class TestMessageIsJoinPhrase(unittest.TestCase):
    PHRASE = "za warudo"

    def _check(self, text, expected):
        result = jt.message_is_join_phrase(text, self.PHRASE)
        self.assertEqual(result, expected, f"text={text!r}")

    def test_exact_match(self):
        self._check("za warudo", True)

    def test_case_insensitive(self):
        self._check("ZA WARUDO", True)
        self._check("Za Warudo", True)

    def test_leading_trailing_whitespace(self):
        self._check("  za warudo  ", True)

    def test_trailing_punctuation_matches(self):
        self._check("za warudo!", True)
        self._check("za warudo,", True)
        self._check("za warudo.", True)

    def test_trailing_space_plus_words_matches(self):
        self._check("za warudo please", True)

    def test_word_boundary_prevents_partial(self):
        self._check("za warudonow", False)
        self._check("za warudo_extension", False)

    def test_empty_string_no_match(self):
        self._check("", False)

    def test_unrelated_text_no_match(self):
        self._check("hello world", False)
        self._check("za", False)

    def test_empty_phrase_no_match(self):
        result = jt.message_is_join_phrase("za warudo", "")
        self.assertFalse(result)

    def test_none_phrase_loads_configured(self):
        with patch.object(jt, "load_join_phrase", return_value="za warudo"):
            result = jt.message_is_join_phrase("za warudo", None)
        self.assertTrue(result)


# ── _server_already_running ───────────────────────────────────────────────────

class TestServerAlreadyRunning(unittest.TestCase):
    def _fake_run(self, stdout, returncode=0):
        m = MagicMock()
        m.stdout = stdout
        m.returncode = returncode
        return m

    def test_running_for_channel_returns_true(self):
        output = "12345 npx tsx discord-voice-server.ts --guild 1 --channel 111\n"
        with patch("subprocess.run", return_value=self._fake_run(output)):
            self.assertTrue(jt._server_already_running(111))

    def test_different_channel_returns_false(self):
        output = "12345 npx tsx discord-voice-server.ts --guild 1 --channel 999\n"
        with patch("subprocess.run", return_value=self._fake_run(output)):
            self.assertFalse(jt._server_already_running(111))

    def test_no_process_returns_false(self):
        with patch("subprocess.run", return_value=self._fake_run("", returncode=1)):
            self.assertFalse(jt._server_already_running(111))

    def test_pgrep_exception_returns_false(self):
        with patch("subprocess.run", side_effect=Exception("pgrep not found")):
            self.assertFalse(jt._server_already_running(111))


# ── _spawn_voice_server ───────────────────────────────────────────────────────

class TestSpawnVoiceServer(unittest.TestCase):
    def test_missing_server_script_returns_false(self):
        with patch.object(jt, "_DISCORD_VOICE_SERVER", Path("/nonexistent/discord-voice-server.ts")), \
             patch.object(jt, "_resolve_workspace", return_value=Path(tempfile.mkdtemp())):
            result = jt._spawn_voice_server(guild_id=1, channel_id=111)
        self.assertFalse(result)

    def test_successful_popen_returns_true(self):
        with tempfile.TemporaryDirectory() as d:
            fake_server = Path(d) / "discord-voice-server.ts"
            fake_server.touch()
            with patch.object(jt, "_DISCORD_VOICE_SERVER", fake_server), \
                 patch.object(jt, "_resolve_workspace", return_value=Path(d)), \
                 patch("subprocess.Popen") as mock_popen:
                mock_popen.return_value = MagicMock()
                result = jt._spawn_voice_server(guild_id=1, channel_id=111)
        self.assertTrue(result)

    def test_popen_exception_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            fake_server = Path(d) / "discord-voice-server.ts"
            fake_server.touch()
            with patch.object(jt, "_DISCORD_VOICE_SERVER", fake_server), \
                 patch.object(jt, "_resolve_workspace", return_value=Path(d)), \
                 patch("subprocess.Popen", side_effect=OSError("npx not found")):
                result = jt._spawn_voice_server(guild_id=1, channel_id=111)
        self.assertFalse(result)


# ── _enqueue_context_prep_task ────────────────────────────────────────────────

class TestEnqueueContextPrepTask(unittest.TestCase):
    def test_task_file_written(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            with patch.object(jt, "_resolve_workspace", return_value=ws):
                jt._enqueue_context_prep_task("za warudo", 111, "General Voice")
            task_files = list((ws / "tasks").glob("task-*.txt"))
        self.assertEqual(len(task_files), 1)

    def test_task_file_contains_required_fields(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            with patch.object(jt, "_resolve_workspace", return_value=ws):
                jt._enqueue_context_prep_task("za warudo", 111, "General Voice")
            task_files = list((ws / "tasks").glob("task-*.txt"))
            content = task_files[0].read_text()
        self.assertIn("access_tier: owner", content)
        self.assertIn("source: system-magic-word", content)
        self.assertIn("priority: urgent", content)
        self.assertIn("za warudo", content)
        self.assertIn("111", content)

    def test_never_raises_on_bad_workspace(self):
        with patch.object(jt, "_resolve_workspace", side_effect=Exception("boom")):
            try:
                jt._enqueue_context_prep_task("za warudo", 111, "Voice")
            except Exception as e:
                self.fail(f"_enqueue_context_prep_task raised: {e}")


# ── handle_join_trigger ───────────────────────────────────────────────────────

class TestHandleJoinTrigger(unittest.TestCase):
    def test_not_in_voice_channel_returns_message(self):
        msg = _make_message(has_voice=False)
        with patch.object(jt, "_server_already_running", return_value=False), \
             patch.object(jt, "_spawn_voice_server", return_value=False):
            result = jt.handle_join_trigger(msg)
        self.assertIn("not in a voice channel", result.lower())

    def test_already_running_returns_already_there_message(self):
        msg = _make_message(has_voice=True, channel_id=111)
        with patch.object(jt, "_server_already_running", return_value=True):
            result = jt.handle_join_trigger(msg)
        self.assertIn("already in", result.lower())

    def test_spawn_fails_returns_error_message(self):
        msg = _make_message(has_voice=True, channel_id=111, guild_id=222)
        with patch.object(jt, "_server_already_running", return_value=False), \
             patch.object(jt, "_spawn_voice_server", return_value=False), \
             patch.object(jt, "_enqueue_context_prep_task"):
            result = jt.handle_join_trigger(msg)
        self.assertIn("couldn't launch", result.lower())

    def test_success_returns_on_my_way_message(self):
        msg = _make_message(has_voice=True, channel_id=111, guild_id=222)
        with patch.object(jt, "_server_already_running", return_value=False), \
             patch.object(jt, "_spawn_voice_server", return_value=True), \
             patch.object(jt, "_enqueue_context_prep_task"):
            result = jt.handle_join_trigger(msg)
        self.assertIn("on my way", result.lower())

    def test_success_enqueues_context_prep(self):
        msg = _make_message(has_voice=True, channel_id=111, guild_id=222)
        with patch.object(jt, "_server_already_running", return_value=False), \
             patch.object(jt, "_spawn_voice_server", return_value=True), \
             patch.object(jt, "_enqueue_context_prep_task") as mock_enqueue:
            jt.handle_join_trigger(msg)
        mock_enqueue.assert_called_once()

    def test_voice_check_exception_returns_error(self):
        msg = _make_message(has_voice=True)
        with patch.object(jt, "_owner_voice_channel", side_effect=Exception("discord error")):
            result = jt.handle_join_trigger(msg)
        self.assertIn("couldn't check", result.lower())


if __name__ == "__main__":
    unittest.main()
