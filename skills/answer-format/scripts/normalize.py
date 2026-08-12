#!/usr/bin/env python3
"""Deterministic final-answer normalizer. Conservative by design: every transform
is lossless or pattern-gated, and ambiguous input passes through unchanged."""
from __future__ import annotations

import argparse
import re
import sys
from decimal import Decimal, InvalidOperation

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
_CURRENCY_PREFIX = re.compile(r"^(?:[$€£¥]|USD|EUR|GBP|JPY)\s*", re.IGNORECASE)
_LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
# Grouping is a property of the whole token, not of one comma: no grouped
# number has a four-digit lead group, so "1000,200" separates elements.
_GROUPED_NUMBER = re.compile(r"(?<![\d,])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?!\d)")


def _split_list_elements(text: str) -> list[str]:
    """Split on element commas, keeping grouping commas inside their number."""
    grouped = [m.span() for m in _GROUPED_NUMBER.finditer(text)]
    parts, start = [], 0
    for i, ch in enumerate(text):
        if ch == "," and not any(a <= i < b for a, b in grouped):
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def _expand_magnitude(text: str) -> str | None:
    m = _MAGNITUDE_RE.match(text)
    if not m:
        return None
    # Exact decimal arithmetic — binary float would emit artifacts like
    # "2.01 million" -> 2009999.9999999998 instead of 2010000.
    try:
        value = Decimal(m.group(1)) * _WORD_MAGNITUDES[m.group(2).lower()]
    except InvalidOperation:
        return None
    # Emit a bare integer when whole (the common case), else a plain decimal
    # with no exponent or trailing zeros (Decimal.normalize can yield "2E+6").
    if value == value.to_integral_value():
        return str(int(value))
    normalized = value.normalize()
    return f"{normalized:f}"


def _strip_currency_prefix(text: str) -> str:
    """Strip one currency wrapper around a numeric core. The minus may sit either
    side of the symbol ("-$1,000" / "$-1,000"), so peel the sign before matching."""
    sign = ""
    body = text
    if body.startswith("-"):
        sign, body = "-", body[1:]
    match = _CURRENCY_PREFIX.match(body)
    if not match:
        return text
    core = sign + body[match.end():]
    numeric = core[:-1] if core.endswith("%") else core
    if (
        _MAGNITUDE_RE.match(numeric)
        or _PURE_NUMBER_COMMAS.match(numeric)
        or re.fullmatch(r"-?\d+(?:\.\d+)?", numeric)
    ):
        return core
    return text


def normalize_number(text: str) -> str:
    """Bare numeric form: expand worded magnitudes, strip thousands separators,
    drop a trailing/leading currency or unit token only when unambiguous."""
    s = _strip_currency_prefix(text.strip())
    expanded = _expand_magnitude(s)
    if expanded is not None:
        return expanded
    # "1,234,567" -> "1234567" (only when the whole token is a comma-grouped number)
    if _PURE_NUMBER_COMMAS.match(s):
        return s.replace(",", "")
        # Currency, magnitude, percent and comma-stripping must compose, in
        # that order; applying any one alone leaves "$1,000" unnormalised.
    stripped = s
    if stripped[-1:] == "%" and (
            re.fullmatch(r"-?\d+(?:\.\d+)?", stripped[:-1]) or _PURE_NUMBER_COMMAS.match(stripped[:-1])):
        stripped = stripped[:-1]
    if _PURE_NUMBER_COMMAS.match(stripped):
        stripped = stripped.replace(",", "")
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
    """Comma-separated, one space after each comma, elements trimmed. Sorting is
    opt-in (graders rarely want it) and case-insensitive."""
    parts = [p.strip() for p in _split_list_elements(text)]
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
    # A grouped number keeps its commas even inside a supported symbol wrapper,
    # so strip the wrapper before testing the numeric core.
    core = _strip_currency_prefix(s)
    # An unknown ISO-shaped wrapper stays a string even with a grouping
    # comma, sign included: "-AUD 1,000" must not become ["-AUD 1", "000"].
    if core == s and re.match(r"^-?[A-Z]{3}\s+", s):
        return "string"
    if core[-1:] == "%":
        core = core[:-1]
    if "," in s and not _PURE_NUMBER_COMMAS.match(core):
        return "list"
    if _MAGNITUDE_RE.match(core) or re.fullmatch(r"-?\d[\d,]*(?:\.\d+)?", core):
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
