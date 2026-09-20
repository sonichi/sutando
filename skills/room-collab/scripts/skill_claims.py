"""Claims in a skill's text about what the service refuses, and their age.

A sentence that says the service "refuses", "will not", or does something "by
design" is a statement about a server that changes without the file changing.
It is allowed here only with the date it was last measured, and it expires:
after MAX_AGE_DAYS the test that reads this fails until someone measures it
again and moves the date. The words that make a sentence a claim are listed
below; ordinary prose about the client's own behaviour is not one.

Imports nothing: pure text rules, testable without the service.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

MAX_AGE_DAYS = 30

# A rule word AND a credential/account subject: the client's own refusals
# name a command, not an account, so the second group excludes them.
_RULE_WORD = re.compile(
    r"\b(by design|deliberate(?:ly)?|is refused|are refused|refuses|will not|cannot)\b", re.I)
_SUBJECT = re.compile(
    r"\b(bearer|token|ticket|credential|agent|member|account|grant|authoriz\w*)\b", re.I)
_TABLE_ROW = re.compile(r"^\|\s*(?:4\d{3}|HTTP\s+\d{3}[^|]*)\s*\|", re.I)
_DATED = re.compile(r"\(verified (\d{4}-\d{2}-\d{2})\)")


@dataclass(frozen=True)
class Claim:
    line: int
    text: str
    verified: dt.date | None
    problem: str = ""


def _sentences(text: str) -> list[tuple[int, str]]:
    """(line the paragraph starts on, one sentence). Sentences wrap across
    lines, so a paragraph is joined first and split on its terminators; table
    rows are one claim each."""
    out: list[tuple[int, str]] = []
    para: list[str] = []
    start = 1

    def flush() -> None:
        if para:
            joined = " ".join(s.strip() for s in para)
            for sentence in re.split(r"(?<=[.!?])\s+", joined):
                if sentence.strip():
                    out.append((start, sentence.strip()))
            para.clear()

    for n, raw in enumerate(text.splitlines(), 1):
        if raw.startswith("|"):
            flush()
            out.append((n, raw.strip()))
        elif raw.strip() == "":
            flush()
        else:
            if not para:
                start = n
            para.append(raw)
    flush()
    return out


def refusal_claims(text: str) -> list[Claim]:
    """Every sentence (or refusal-table row) that states what the service refuses."""
    out = []
    for line, sentence in _sentences(text):
        is_row = bool(_TABLE_ROW.match(sentence))
        is_claim = bool(_RULE_WORD.search(sentence) and _SUBJECT.search(sentence))
        if not (is_row or is_claim):
            continue
        m = _DATED.search(sentence)
        verified = dt.date.fromisoformat(m.group(1)) if m else None
        out.append(Claim(line, sentence, verified))
    return out


def stale_claims(text: str, today: dt.date) -> list[Claim]:
    """The claims that no longer count: undated, or measured too long ago."""
    out = []
    for c in refusal_claims(text):
        if c.verified is None:
            out.append(Claim(c.line, c.text, None, "undated claim about the service"))
        elif (today - c.verified).days > MAX_AGE_DAYS:
            out.append(Claim(c.line, c.text, c.verified,
                             f"expired: verified {c.verified}, older than {MAX_AGE_DAYS} days"))
    return out
