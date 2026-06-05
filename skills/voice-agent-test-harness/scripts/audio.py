"""Audio transport for the voice-agent test harness (prober side).

This is the ONLY transport-coupled module. Swapping same-room speaker->mic for
Twilio/WebRTC is a change confined to this file; the suite, scoring, baseline,
and reporting do not change.

DRAFT: the three hardware-touching functions are stubbed with TODO(voice). Their
signatures + return contracts are final so the orchestrator is reviewable now.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class SpokenPrompt:
    """Result of speaking a prompt aloud."""
    text: str
    started_at: float   # epoch seconds, start of TTS playback
    ended_at: float     # epoch seconds, end of TTS playback (latency clock starts here)


@dataclass
class CapturedReply:
    """Result of listening for the subject's reply."""
    onset_at: float | None    # epoch seconds of first reply audio, or None if no response
    ended_at: float | None    # epoch seconds reply audio ended
    wav_path: str | None      # captured audio for STT, or None
    peak_rms: float           # loudness, for the calibration / level gate


def speak(text: str) -> SpokenPrompt:
    """Play `text` through the prober speaker (subject hears it via the room).

    TODO(voice): reuse the gemini-tts / openai-tts skill to synthesize, play it,
    and return precise start/end timestamps. `ended_at` is the latency origin, so
    it must be the real end-of-audio, not the synth-request time.
    """
    now = time.time()
    raise NotImplementedError("TODO(voice): wire TTS playback via gemini-tts skill")
    # return SpokenPrompt(text=text, started_at=now, ended_at=now + tts_duration)


def listen(timeout_s: float, onset_rms: float = 0.02) -> CapturedReply:
    """Capture the subject's spoken reply from the prober mic.

    Voice-onset detection: first window whose RMS crosses `onset_rms` marks
    `onset_at` (this minus the prompt's `ended_at` is the response latency).
    Capture continues until ~700ms of trailing silence or `timeout_s`.

    TODO(voice): implement with sounddevice/ffmpeg capture + a rolling-RMS
    onset/endpoint detector; write the reply to a wav and return its path.
    """
    raise NotImplementedError("TODO(voice): wire mic capture + onset detection")
    # return CapturedReply(onset_at=..., ended_at=..., wav_path=..., peak_rms=...)


def calibrate(tone_hz: int = 440, seconds: float = 1.0) -> tuple[bool, str]:
    """Pre-run audio-level gate: play a known tone, confirm the return path is
    in range. Returns (ok, reason). Out-of-range -> the run reports SKIPPED
    rather than a false regression.

    TODO(voice): play tone, capture, compare measured RMS to expected band.
    """
    raise NotImplementedError("TODO(voice): implement tone calibration")
    # return (True, "levels nominal")


def latency_ms(prompt: SpokenPrompt, reply: CapturedReply) -> float | None:
    """Response latency: prompt-end -> reply-onset, in ms. None if no response."""
    if reply.onset_at is None:
        return None
    return round((reply.onset_at - prompt.ended_at) * 1000.0, 1)
