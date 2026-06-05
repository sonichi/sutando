"""Scoring for the voice-agent test harness: STT + LLM judge.

Latency is measured in audio.py (no judging). This module turns a captured
reply into {accuracy, clarity, rationale} via transcription + an LLM judge.

DRAFT: transcribe() and _call_judge() are stubbed at the model boundary; the
rubric, the judge contract, and the re-judge-on-fail logic are real.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict


# Version-pinned so judge drift is detectable across daily runs.
JUDGE_RUBRIC_VERSION = 1

_JUDGE_SYSTEM = """You are grading a voice assistant's spoken reply.
You receive the prompt it was given, what a correct reply should contain, and a
transcript of what it actually said. Grade only meaning and intelligibility —
NOT exact wording (the assistant may phrase things differently run-to-run).

Return STRICT JSON:
{
  "accuracy": "pass" | "partial" | "fail",
  "clarity": 1-5,        // 5 = fully intelligible, well-formed; 1 = garbled/truncated
  "rationale": "<one sentence>"
}

Rules:
- accuracy=pass: the reply satisfies the expectation (correct answer, or the
  right kind of response — e.g. a clarifying question where one was expected, a
  graceful decline where a refusal was expected).
- accuracy=partial: on the right track but incomplete or hedged.
- accuracy=fail: wrong, off-topic, hallucinated an action, or no real answer.
- Judge the MEANING against `expected`, not surface form."""


@dataclass
class Transcript:
    text: str
    confidence: float   # STT confidence 0..1; feeds the clarity signal


@dataclass
class Judgement:
    accuracy: str       # pass | partial | fail
    clarity: int        # 1..5
    rationale: str
    rubric_version: int = JUDGE_RUBRIC_VERSION

    def is_fail(self) -> bool:
        return self.accuracy == "fail"


def transcribe(wav_path: str | None) -> Transcript:
    """Speech-to-text on the captured reply.

    TODO(voice): run Whisper (local) or a hosted STT; return transcript + a
    real confidence. None wav (no response) -> empty transcript, confidence 0.
    """
    if wav_path is None:
        return Transcript(text="", confidence=0.0)
    raise NotImplementedError("TODO(voice): wire STT (Whisper/hosted)")


def _call_judge(prompt: str, expected: str, transcript: Transcript) -> dict:
    """Call the LLM judge once at temperature 0. Returns the parsed JSON dict.

    TODO(voice): send (_JUDGE_SYSTEM, user payload) to the judge model and parse
    its JSON. Kept as the single model-call boundary so it is easy to mock/test.
    """
    raise NotImplementedError("TODO(voice): wire LLM judge model call")


def judge(prompt: str, expected: str, transcript: Transcript) -> Judgement:
    """Score one reply. Re-judges once on a `fail` verdict to absorb judge
    non-determinism (a one-off fail is re-run; a persistent fail is trusted) so
    daily regression alerts don't fire on judge noise."""
    raw = _call_judge(prompt, expected, transcript)
    verdict = _coerce(raw)
    if verdict.is_fail():
        raw2 = _call_judge(prompt, expected, transcript)
        verdict2 = _coerce(raw2)
        # Trust fail only if the re-judge agrees; else take the kinder verdict.
        if not verdict2.is_fail():
            return verdict2
    return verdict


def _coerce(raw: dict) -> Judgement:
    acc = str(raw.get("accuracy", "fail")).lower()
    if acc not in ("pass", "partial", "fail"):
        acc = "fail"
    clarity = int(raw.get("clarity", 1))
    clarity = min(5, max(1, clarity))
    return Judgement(accuracy=acc, clarity=clarity,
                     rationale=str(raw.get("rationale", "")).strip())


def to_dict(j: Judgement) -> dict:
    return asdict(j)
