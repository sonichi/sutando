"""Audio transport for the voice-agent test harness (prober side) — v1, macOS.

The ONLY transport-coupled module. Same-room speaker->mic:
  speak()     -> gemini-tts (mp3) played via afplay; `say` fallback.
  listen()    -> ffmpeg avfoundation mic capture to wav + RMS voice-onset.
  calibrate() -> 1s ambient capture; confirms the mic produces usable signal.

Latency note: ffmpeg takes ~constant time to open the input device, which adds a
fixed offset to every measured latency. It cancels in the baseline diff (we
compare to the previous green run on the same machine), so absolute numbers carry
that offset but day-over-day deltas do not.
"""
from __future__ import annotations

import os
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]          # ~/GitHub/sutando
SKILL = Path(__file__).resolve().parents[1]
TTS = REPO / "skills" / "gemini-tts" / "scripts" / "synthesize.sh"
WORKDIR = SKILL / "results" / "audio"
SR = 16000                                          # capture sample rate
AUDIO_DEVICE = os.environ.get("VTH_AUDIO_DEVICE", ":0")  # avfoundation: no video, audio dev 0


@dataclass
class SpokenPrompt:
    text: str
    started_at: float
    ended_at: float          # latency clock origin


@dataclass
class CapturedReply:
    onset_at: float | None
    ended_at: float | None
    wav_path: str | None
    peak_rms: float


def _afplay(path: str) -> None:
    subprocess.run(["afplay", path], check=True)


def speak(text: str) -> SpokenPrompt:
    """Synthesize `text` and play it through the speaker; block until done so
    `ended_at` is the true end-of-audio (the latency origin)."""
    WORKDIR.mkdir(parents=True, exist_ok=True)
    out = WORKDIR / f"prompt-{int(time.time()*1000)}.mp3"
    started = time.time()
    try:
        subprocess.run(["bash", str(TTS), "--out", str(out), "--", text],
                       check=True, capture_output=True, timeout=30)
        _afplay(str(out))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        # offline fallback: macOS `say`
        subprocess.run(["say", text], check=True)
    return SpokenPrompt(text=text, started_at=started, ended_at=time.time())


def record_window(seconds: float, wav_path: str) -> None:
    """Capture `seconds` of mic audio (mono, 16k) to wav via ffmpeg avfoundation."""
    Path(wav_path).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "avfoundation", "-i", AUDIO_DEVICE,
         "-t", f"{seconds:.2f}", "-ac", "1", "-ar", str(SR), wav_path],
        check=True, capture_output=True,
    )


def _frames(wav_path: str) -> np.ndarray:
    with wave.open(wav_path, "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _onset(samples: np.ndarray, onset_rms: float, win_ms: int = 30) -> tuple[int, int, float]:
    """Return (onset_idx, end_idx, peak_rms) over RMS windows. onset_idx == -1 if
    nothing crossed the threshold."""
    win = max(1, int(SR * win_ms / 1000))
    n = len(samples) // win
    if n == 0:
        return -1, -1, 0.0
    rms = np.sqrt((samples[: n * win].reshape(n, win) ** 2).mean(axis=1))
    peak = float(rms.max()) if n else 0.0
    loud = np.where(rms > onset_rms)[0]
    if loud.size == 0:
        return -1, -1, peak
    return int(loud[0] * win), int((loud[-1] + 1) * win), peak


def listen(timeout_s: float, onset_rms: float = 0.02) -> CapturedReply:
    """Record up to `timeout_s` and detect the subject's reply onset/end."""
    WORKDIR.mkdir(parents=True, exist_ok=True)
    wav = str(WORKDIR / f"reply-{int(time.time()*1000)}.wav")
    rec_start = time.time()
    record_window(timeout_s, wav)
    samples = _frames(wav)
    onset_i, end_i, peak = _onset(samples, onset_rms)
    if onset_i < 0:
        return CapturedReply(onset_at=None, ended_at=None, wav_path=wav, peak_rms=peak)
    return CapturedReply(
        onset_at=rec_start + onset_i / SR,
        ended_at=rec_start + end_i / SR,
        wav_path=wav,
        peak_rms=peak,
    )


def calibrate(seconds: float = 1.0) -> tuple[bool, str]:
    """Confirm the mic path is alive (non-silent, non-clipping ambient)."""
    WORKDIR.mkdir(parents=True, exist_ok=True)
    wav = str(WORKDIR / "calib.wav")
    try:
        record_window(seconds, wav)
    except subprocess.CalledProcessError as e:
        return False, f"mic capture failed ({e.returncode})"
    _, _, peak = _onset(_frames(wav), onset_rms=0.0)
    if peak <= 1e-4:
        return False, "mic silent (check input device / permissions)"
    if peak >= 0.98:
        return False, "mic clipping (lower input gain)"
    return True, f"levels nominal (ambient peak rms {peak:.3f})"


def latency_ms(prompt: SpokenPrompt, reply: CapturedReply) -> float | None:
    if reply.onset_at is None:
        return None
    return round((reply.onset_at - prompt.ended_at) * 1000.0, 1)
