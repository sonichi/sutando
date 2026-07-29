#!/usr/bin/env python3
"""answer-format: deterministic final-answer normalizer.

A last-step pass for any task that ends in a *precise* answer (a number, a
short string, a list). It applies the formatting conventions that graders and
downstream consumers expect — bare digits, no thousands separators, worded
magnitudes expanded, list spacing normalized, prose wrappers stripped — WITHOUT
changing the answer's meaning.

Design principle: conservative. A normalizer that mangles a correct answer is
worse than none, so every transform is either lossless or gated on a confident
pattern. Ambiguous input passes through unchanged.

CLI:
  echo "100 million" | python3 normalize.py --kind number   -> 100000000
  python3 normalize.py --kind list -- "b,  a , c"            -> a, b, c   (with --sort)
"""
from __future__ import annotations

import argparse
import re
import sys

_WORD_MAGNITUDES = {
    "hundred": 100,
    "thousand": 1_000,
    "million": 1_000_000,
    "billion": 1_000_000_000,
    "trillion": 1_000_000_000_000,
}

# "100 million", "3.5 billion", "2 thousand" — a leading number then a magnitude.
_MAGNITUDE_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s+(" + "|".join(_WORD_MAGNITUDES) + r")s?\s*$",
    re.IGNORECASE,
)
_PURE_NUMBER_COMMAS = re.compile(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)


def _expand_magnitude(text: str) -> str | None:
    m = _MAGNITUDE_RE.match(text)
    if not m:
        return None
    value = float(m.group(1)) * _WORD_MAGNITUDES[m.group(2).lower()]
    # Emit an integer when the result is whole (the common case), else a plain decimal.
    return str(int(value)) if value == int(value) else repr(value)


def normalize_number(text: str) -> str:
    """Bare numeric form: expand worded magnitudes, strip thousands separators,
    drop a trailing/leading currency or unit token only when unambiguous."""
    s = text.strip()
    expanded = _expand_magnitude(s)
    if expanded is not None:
        return expanded
    # "1,234,567" -> "1234567" (only when the whole token is a comma-grouped number)
    if _PURE_NUMBER_COMMAS.match(s):
        return s.replace(",", "")
    # "$1234" / "1234%" -> strip a single leading/trailing symbol when the rest is numeric
    stripped = s
    if stripped[:1] in "$€£¥" and re.fullmatch(r"-?\d+(?:\.\d+)?", stripped[1:]):
        stripped = stripped[1:]
    if stripped[-1:] == "%" and re.fullmatch(r"-?\d+(?:\.\d+)?", stripped[:-1]):
        stripped = stripped[:-1]
    return stripped


def normalize_string(text: str, drop_article: bool = False) -> str:
    """Trim whitespace and surrounding quotes; optionally drop a leading article.
    Never touches interior content — capitalization and word choice are preserved."""
    s = text.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    if drop_article:
        s = _LEADING_ARTICLE.sub("", s)
    return re.sub(r"\s+", " ", s)


def normalize_list(text: str, sort: bool = False, number_items: bool = False) -> str:
    """Comma-separated, single space after each comma. Per-element trimming; each
    element optionally normalized as a number. Sorting is opt-in (graders rarely
    want it) and case-insensitive."""
    parts = [p.strip() for p in text.split(",")]
    parts = [p for p in parts if p]
    if number_items:
        parts = [normalize_number(p) for p in parts]
    if sort:
        parts = sorted(parts, key=str.casefold)
    return ", ".join(parts)


def normalize_answer(text: str, kind: str | None = None, *,
                     sort_list: bool = False, drop_article: bool = False,
                     number_items: bool = False) -> str:
    """Dispatch by declared kind; 'auto' (or None) infers from shape."""
    if kind in (None, "auto"):
        kind = _infer_kind(text)
    if kind == "number":
        return normalize_number(text)
    if kind == "list":
        return normalize_list(text, sort=sort_list, number_items=number_items)
    if kind == "string":
        return normalize_string(text, drop_article=drop_article)
    return text.strip()


def _infer_kind(text: str) -> str:
    s = text.strip()
    if "," in s and not _PURE_NUMBER_COMMAS.match(s):
        return "list"
    if _MAGNITUDE_RE.match(s) or re.fullmatch(r"[-$€£¥]?\d[\d,]*(?:\.\d+)?%?", s):
        return "number"
    return "string"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Normalize a final answer for grading/consumers.")
    ap.add_argument("--kind", choices=["auto", "number", "string", "list"], default="auto")
    ap.add_argument("--sort", action="store_true", help="list: sort elements (case-insensitive)")
    ap.add_argument("--number-items", action="store_true", help="list: normalize each element as a number")
    ap.add_argument("--drop-article", action="store_true", help="string: drop a leading the/a/an")
    ap.add_argument("text", nargs="*", help="answer text; if omitted, read stdin")
    args = ap.parse_args(argv)
    raw = " ".join(args.text) if args.text else sys.stdin.read()
    out = normalize_answer(raw, kind=args.kind, sort_list=args.sort,
                           drop_article=args.drop_article, number_items=args.number_items)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
