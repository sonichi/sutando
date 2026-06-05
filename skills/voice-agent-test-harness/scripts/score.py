"""Scoring for the voice-agent test harness — v1.

STT + judge both run on Gemini (Sutando's standard voice provider), via the REST
generateContent endpoint with the GEMINI_API_KEY from .env. No extra pip deps
(stdlib urllib). Latency is measured in audio.py and not judged here.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

JUDGE_RUBRIC_VERSION = 1
STT_MODEL = os.environ.get("VTH_STT_MODEL", "gemini-2.5-flash")
JUDGE_MODEL = os.environ.get("VTH_JUDGE_MODEL", "gemini-2.5-flash")
_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

_REPO = Path(__file__).resolve().parents[3]
_WS = Path(os.environ.get("SUTANDO_WORKSPACE", Path.home() / ".sutando" / "workspace"))

_JUDGE_SYSTEM = """You are grading a voice assistant's spoken reply.
You receive the prompt it was given, what a correct reply should contain, and a
transcript of what it actually said. Grade only meaning and intelligibility —
NOT exact wording (the assistant may phrase things differently run-to-run).

Return STRICT JSON only:
{"accuracy": "pass"|"partial"|"fail", "clarity": 1-5, "rationale": "<one sentence>"}

Rules:
- accuracy=pass: satisfies the expectation (correct answer, OR the right kind of
  response — a clarifying question where one was expected, a graceful decline
  where a refusal was expected).
- accuracy=partial: on the right track but incomplete or hedged.
- accuracy=fail: wrong, off-topic, hallucinated an action, or no real answer.
- clarity: 5 = fully intelligible/well-formed; 1 = garbled/truncated.
Judge MEANING against `expected`, not surface form."""


@dataclass
class Transcript:
    text: str
    confidence: float


@dataclass
class Judgement:
    accuracy: str
    clarity: int
    rationale: str
    rubric_version: int = JUDGE_RUBRIC_VERSION

    def is_fail(self) -> bool:
        return self.accuracy == "fail"


def _api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "")
    if key:
        return key
    for env in (_WS / ".env", _REPO / ".env"):
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("GEMINI_API_KEY="):
                    return line.split("=", 1)[1].strip()
    raise SystemExit("GEMINI_API_KEY not found (env or .env)")


def _post(model: str, parts: list[dict]) -> str:
    body = {"contents": [{"parts": parts}], "generationConfig": {"temperature": 0}}
    url = _ENDPOINT.format(model=model, key=_api_key())
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return data["candidates"][0]["content"]["parts"][0]["text"]


def transcribe(wav_path: str | None) -> Transcript:
    """Gemini STT on the captured reply (Sutando-standard provider)."""
    if wav_path is None or not Path(wav_path).exists():
        return Transcript(text="", confidence=0.0)
    audio_b64 = base64.b64encode(Path(wav_path).read_bytes()).decode()
    parts = [
        {"text": "Transcribe the spoken audio verbatim. Return only the words spoken, nothing else."},
        {"inline_data": {"mime_type": "audio/wav", "data": audio_b64}},
    ]
    try:
        text = _post(STT_MODEL, parts).strip()
    except Exception:
        return Transcript(text="", confidence=0.0)
    return Transcript(text=text, confidence=1.0 if text else 0.0)


def _call_judge(prompt: str, expected: str, transcript: Transcript) -> dict:
    user = (f"PROMPT: {prompt}\nEXPECTED: {expected}\n"
            f"TRANSCRIPT: {transcript.text or '(no speech detected)'}")
    raw = _post(JUDGE_MODEL, [{"text": _JUDGE_SYSTEM + "\n\n" + user}])
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0)) if m else {}


def judge(prompt: str, expected: str, transcript: Transcript) -> Judgement:
    """Score one reply; re-judge once on a `fail` to absorb judge noise."""
    verdict = _coerce(_call_judge(prompt, expected, transcript))
    if verdict.is_fail():
        second = _coerce(_call_judge(prompt, expected, transcript))
        if not second.is_fail():
            return second
    return verdict


def _coerce(raw: dict) -> Judgement:
    acc = str(raw.get("accuracy", "fail")).lower()
    if acc not in ("pass", "partial", "fail"):
        acc = "fail"
    clarity = min(5, max(1, int(raw.get("clarity", 1) or 1)))
    return Judgement(accuracy=acc, clarity=clarity,
                     rationale=str(raw.get("rationale", "")).strip())


def to_dict(j: Judgement) -> dict:
    return asdict(j)
