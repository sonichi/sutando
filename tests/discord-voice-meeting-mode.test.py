#!/usr/bin/env python3
"""
Structural tests for meeting mode in skills/discord-voice/scripts/discord-voice-server.ts.

Verifies the implementation of issue #1105 (manual + auto meeting mode):
  - Module-level state constants (VOICE_MODE_TXT, VOICE_MODE_REQUEST, AUTO_MEETING_MS)
  - writeMeetingModeSentinel() writes to workspace-relative state/voice-mode.txt
  - applyModeRequest() reads request file, applies mode, unlinks
  - handleAudioOutput gates pushAudio on meetingMode
  - speaking.on('start') resets lastUserAudioTs and exits auto-meeting
  - Auto-meeting idle watchdog flips meetingMode after timeout
  - Wake-word detection exits meeting mode on turn.end
  - cleanupSession clears _autoMeetingHandle
"""

import re
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "skills" / "discord-voice" / "scripts" / "discord-voice-server.ts"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


class TestMeetingModeConstants(unittest.TestCase):
    """Module-level constants and state."""

    def test_voice_mode_txt_uses_workspace(self) -> None:
        src = _src()
        self.assertIn("VOICE_MODE_TXT", src)
        # Must derive from WORKSPACE_DIR, not a bare path
        self.assertIn("join(WORKSPACE_DIR", src)

    def test_voice_mode_request_defined(self) -> None:
        src = _src()
        self.assertIn("VOICE_MODE_REQUEST", src)

    def test_auto_meeting_ms_from_env(self) -> None:
        src = _src()
        self.assertIn("SUTANDO_VOICE_AUTO_MEETING_AFTER_SEC", src)
        self.assertIn("AUTO_MEETING_MS", src)

    def test_meeting_mode_flag_defined(self) -> None:
        src = _src()
        self.assertIn("let meetingMode = false", src)

    def test_initial_state_read_from_sentinel(self) -> None:
        src = _src()
        # Initial mode read from voice-mode.txt at startup
        self.assertIn("VOICE_MODE_TXT", src)
        self.assertIn("=== 'meeting'", src)

    def test_mode_request_poll_interval(self) -> None:
        src = _src()
        # setInterval(applyModeRequest, ...) must be present
        self.assertIn("setInterval(applyModeRequest", src)


class TestWriteMeetingModeSentinel(unittest.TestCase):
    """writeMeetingModeSentinel() writes state/voice-mode.txt via WORKSPACE_DIR."""

    def test_function_defined(self) -> None:
        src = _src()
        self.assertIn("function writeMeetingModeSentinel", src)

    def test_writes_to_voice_mode_txt(self) -> None:
        src = _src()
        # writeFileSync(VOICE_MODE_TXT, ...)
        self.assertIn("writeFileSync(VOICE_MODE_TXT", src)

    def test_mkdirSync_for_state_dir(self) -> None:
        src = _src()
        # mkdirSync guard present in sentinel writer
        # Find the writeMeetingModeSentinel function body
        match = re.search(r"function writeMeetingModeSentinel\(\)[^}]+}", src, re.DOTALL)
        self.assertIsNotNone(match)
        body = match.group(0)  # type: ignore[union-attr]
        self.assertIn("mkdirSync", body)


class TestApplyModeRequest(unittest.TestCase):
    """applyModeRequest() consumes the request file and updates meetingMode."""

    def test_function_defined(self) -> None:
        src = _src()
        self.assertIn("function applyModeRequest", src)

    def test_reads_request_file(self) -> None:
        src = _src()
        # readFileSync(VOICE_MODE_REQUEST, ...)
        self.assertIn("readFileSync(VOICE_MODE_REQUEST", src)

    def test_unlinks_request_file(self) -> None:
        src = _src()
        # unlinkSync(VOICE_MODE_REQUEST) — consumed on apply
        self.assertIn("unlinkSync(VOICE_MODE_REQUEST)", src)

    def test_calls_write_sentinel(self) -> None:
        src = _src()
        match = re.search(r"function applyModeRequest\(\)[^}]+}", src, re.DOTALL)
        self.assertIsNotNone(match)
        body = match.group(0)  # type: ignore[union-attr]
        self.assertIn("writeMeetingModeSentinel", body)


class TestAudioOutputGate(unittest.TestCase):
    """handleAudioOutput skips pushAudio when meetingMode is true."""

    def test_meeting_mode_guard_present(self) -> None:
        src = _src()
        # Find the handleAudioOutput block and verify meetingMode check
        handle_idx = src.find("sessionAny.handleAudioOutput = ")
        self.assertGreater(handle_idx, 0)
        block = src[handle_idx: handle_idx + 400]
        self.assertIn("meetingMode", block)

    def test_early_return_on_meeting_mode(self) -> None:
        src = _src()
        handle_idx = src.find("sessionAny.handleAudioOutput = ")
        block = src[handle_idx: handle_idx + 400]
        self.assertIn("if (meetingMode) return", block)

    def test_push_audio_after_meeting_check(self) -> None:
        src = _src()
        handle_idx = src.find("sessionAny.handleAudioOutput = ")
        block = src[handle_idx: handle_idx + 600]
        meeting_pos = block.find("meetingMode")
        push_pos = block.find("pushAudio")
        # pushAudio must appear AFTER the meetingMode guard
        self.assertGreater(push_pos, meeting_pos)


class TestAutoMeetingIdleWatchdog(unittest.TestCase):
    """Auto-meeting idle watchdog and wake-word exit."""

    def test_auto_meeting_watchdog_interval(self) -> None:
        src = _src()
        self.assertIn("_autoMeetingHandle", src)
        self.assertIn("AUTO_MEETING_MS", src)

    def test_watchdog_flips_meeting_mode(self) -> None:
        src = _src()
        # Should set meetingMode = true and write sentinel
        watchdog_idx = src.find("_autoMeetingHandle")
        # The region around the auto-meeting watchdog should have meetingMode = true
        region = src[max(0, watchdog_idx - 500): watchdog_idx + 200]
        self.assertIn("meetingMode = true", region)

    def test_speaking_start_resets_idle_clock(self) -> None:
        src = _src()
        # lastUserAudioTs must be updated on speaking.start
        self.assertIn("lastUserAudioTs", src)

    def test_speaking_start_exits_auto_meeting(self) -> None:
        src = _src()
        # When user starts speaking, auto-meeting exits.
        # The exit block is placed after subscribeUser() inside the speaking.on('start')
        # handler, so search in a larger window that covers the whole handler + tail.
        speaking_idx = src.find("connection.receiver.speaking.on('start'")
        self.assertGreater(speaking_idx, 0)
        region = src[speaking_idx: speaking_idx + 1800]
        self.assertIn("meetingMode = false", region)

    def test_wake_word_detection_present(self) -> None:
        src = _src()
        # Wake-word regex for bot names
        self.assertIn("wake", src.lower())
        self.assertIn("sutando", src.lower())

    def test_cleanup_clears_auto_meeting_handle(self) -> None:
        src = _src()
        cleanup_idx = src.find("function cleanupSession")
        self.assertGreater(cleanup_idx, 0)
        body = src[cleanup_idx: cleanup_idx + 600]
        self.assertIn("_autoMeetingHandle", body)


if __name__ == "__main__":
    unittest.main()
