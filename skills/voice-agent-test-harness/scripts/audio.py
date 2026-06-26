"""Audio transport for the voice-agent test harness (prober side) — v1, macOS.

The ONLY transport-coupled module. Same-room speaker->mic:
  speak()     -> gemini-tts (mp3) played via afplay; `say` fallback.
  listen()    -> sox `rec` (CoreAudio) mic capture to wav + RMS voice-onset.
  calibrate() -> 1s ambient capture; confirms the mic produces usable signal.

Latency note: sox/CoreAudio takes ~constant time to open the input device, which adds a
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
# Recorder: sox `rec`. ffmpeg's avfoundation input drops ~75% of mic samples on
# this Mac (it captured ~1s for an 8s request, 2026-06-06), which truncated every
# reply and made the whole suite score 0. sox/CoreAudio records the full duration.
REC = next((p for p in ("/opt/homebrew/bin/rec", "/usr/local/bin/rec", "rec")
            if os.path.sep not in p or os.path.exists(p)), "rec")


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
    """Capture `seconds` of mic audio (mono, SR Hz) to wav via sox `rec`
    (CoreAudio). Reliable full-duration capture — see REC note above."""
    Path(wav_path).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [REC, "-q", "-r", str(SR), "-c", "1", "-b", "16", wav_path,
         "trim", "0", f"{seconds:.2f}"],
        check=True, capture_output=True,
    )


def _frames(wav_path: str) -> np.ndarray:
    with wave.open(wav_path, "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _onset(samples: np.ndarray, onset_rms: float, win_ms: int = 30,
           guard_ms: int = 200, min_run: int = 4,
           skip_samples: int = 0) -> tuple[int, int, float]:
    """Return (onset_idx, end_idx, peak_rms) over RMS windows. onset_idx == -1 if
    no SUSTAINED energy crosses the threshold after the guard window.

    skip_samples blanks an EXACT leading span — prompt_and_listen passes the
    end-of-playback offset so the prompt audio captured on the same continuous
    recording is masked precisely, instead of guessing with a fixed guard.

    guard_ms skips the very start of the recording so the prober's own playback
    REVERB TAIL isn't mistaken for the subject's reply. listen() now records only
    AFTER speak() has fully finished playing (sequential, not overlapped), so the
    guard only needs to cover the room's decay + sox/CoreAudio spawn settling —
    ~200ms. The old 600ms value (from when capture overlapped playback, 2026-06-05)
    blanked the first 600ms of the reply window and ATE fast acknowledgements like
    "Working on it…" that voice agents emit within ~300-900ms — a false no-response
    (owner-caught 2026-06-06). min_run still requires several consecutive loud
    windows so a single transient blip doesn't count."""
    win = max(1, int(SR * win_ms / 1000))
    n = len(samples) // win
    if n == 0:
        return -1, -1, 0.0
    rms = np.sqrt((samples[: n * win].reshape(n, win) ** 2).mean(axis=1))
    peak = float(rms.max()) if n else 0.0
    loud = rms > onset_rms
    blank = max(min(int(guard_ms / win_ms), n), min(skip_samples // win, n))
    loud[:blank] = False   # ignore prober playback / echo tail
    onset, run = -1, 0
    for i in range(n):
        run = run + 1 if loud[i] else 0
        if run >= min_run:
            onset = i - min_run + 1
            break
    if onset < 0:
        return -1, -1, peak
    tail = np.where(loud[onset:])[0]
    end = onset + int(tail[-1]) + 1 if tail.size else onset + 1
    return int(onset * win), int(end * win), peak


def _write_wav(path: str, samples: np.ndarray) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def listen(timeout_s: float, onset_rms: float = 0.012) -> CapturedReply:
    """Record up to `timeout_s`, detect the subject's reply, and return a clip
    TRIMMED to the detected speech span (+150ms pad). Sending only the speech to
    STT/judge — instead of a mostly-silent window — stops the model from
    answering the instruction instead of transcribing, and gives the audio judge
    a clean signal. The 600ms echo-guard in _onset keeps the prober's own
    playback tail from registering as the reply (2026-06-05/06)."""
    WORKDIR.mkdir(parents=True, exist_ok=True)
    raw = str(WORKDIR / f"_raw-{int(time.time()*1000)}.wav")
    rec_start = time.time()
    record_window(timeout_s, raw)
    samples = _frames(raw)
    onset_i, end_i, peak = _onset(samples, onset_rms)
    wav = str(WORKDIR / f"reply-{int(rec_start*1000)}.wav")
    if onset_i < 0:
        _write_wav(wav, samples)
        return CapturedReply(onset_at=None, ended_at=None, wav_path=wav, peak_rms=peak)
    pad = int(0.15 * SR)
    a = max(0, onset_i - pad)
    b = min(len(samples), end_i + pad)
    _write_wav(wav, samples[a:b])
    return CapturedReply(
        onset_at=rec_start + onset_i / SR,
        ended_at=rec_start + end_i / SR,
        wav_path=wav,
        peak_rms=peak,
    )


def _audio_dur(path: str, default: float = 8.0) -> float:
    """Best-effort duration (seconds) of an audio file via macOS afinfo."""
    try:
        out = subprocess.run(["afinfo", path], capture_output=True, text=True,
                             timeout=10).stdout
        for line in out.splitlines():
            if "estimated duration" in line:
                return float(line.split(":")[1].strip().split()[0])
    except Exception:
        pass
    return default


def prompt_and_listen(text: str, timeout_s: float, onset_rms: float = 0.010,
                      warmup_s: float = 0.35, settle_s: float = 0.12,
                      ) -> tuple[SpokenPrompt, CapturedReply]:
    """Speak `text` and capture the reply on a SINGLE continuous recording that
    starts BEFORE playback. The old path played the prompt, THEN spawned the
    recorder — a ~100-400ms capture-open gap in which a fast reply's opening
    ("Working on it…") was never recorded, yielding a false no-response
    (owner-caught 2026-06-06). Here the mic is already running when playback
    ends, so a reply that begins the instant the prompt stops is captured in
    full. We then mask EXACTLY up to end-of-playback (known precisely) plus a
    short speaker-decay settle, and detect the subject's onset after that.

    Returns (prompt, reply) so latency_ms(prompt, reply) still works — and the
    latency is now honest (onset relative to true end-of-playback)."""
    WORKDIR.mkdir(parents=True, exist_ok=True)
    ms = int(time.time() * 1000)
    out = WORKDIR / f"prompt-{ms}.mp3"
    started = time.time()
    used_tts = True
    try:
        subprocess.run(["bash", str(TTS), "--out", str(out), "--", text],
                       check=True, capture_output=True, timeout=30)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        used_tts = False
    tts_dur = _audio_dur(str(out)) if used_tts else 6.0
    raw = str(WORKDIR / f"_raw-{ms}.wav")
    total = warmup_s + tts_dur + float(timeout_s) + 0.5
    rec_start = time.time()
    proc = subprocess.Popen(
        [REC, "-q", "-r", str(SR), "-c", "1", "-b", "16", raw,
         "trim", "0", f"{total:.2f}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(warmup_s)              # let CoreAudio actually open before playback
    if used_tts:
        _afplay(str(out))            # blocks for the prompt's duration
    else:
        subprocess.run(["say", text], check=True)
    play_end = time.time()
    prompt = SpokenPrompt(text=text, started_at=started, ended_at=play_end)
    proc.wait()                      # rec self-stops at `total`
    samples = _frames(raw)
    skip = int((play_end - rec_start + settle_s) * SR)
    onset_i, end_i, peak = _onset(samples, onset_rms, skip_samples=skip)
    wav = str(WORKDIR / f"reply-{ms}.wav")
    if onset_i < 0:
        _write_wav(wav, samples[min(skip, len(samples)):])   # post-playback span only
        return prompt, CapturedReply(onset_at=None, ended_at=None,
                                     wav_path=wav, peak_rms=peak)
    pad = int(0.15 * SR)
    a = max(0, onset_i - pad)
    b = min(len(samples), end_i + pad)
    _write_wav(wav, samples[a:b])
    return prompt, CapturedReply(
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
