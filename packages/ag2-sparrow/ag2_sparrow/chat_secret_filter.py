"""Fail-closed secret redaction for persisted inbound chat content.

The explicit ``vault set KEY VALUE`` path stores named values in Keychain.
This module covers the other common case: somebody pastes a token into ordinary
Discord/Slack prose. Known values are replaced before task files, owner-activity
state, prompt scratch files, or bridge logs are written.

Slack's token family is defined once here, as ``SLACK_TOKEN_PATTERN``, and
every other reader imports it (``secret_scanner`` for whole-secret redaction
and the vault value classifier, the report-feedback log scrub). The family is
wider than the bot/user tokens most scanners key on: ``xoxb-``/``xoxp-``/
``xoxa-``/``xoxs-``/``xoxo-`` bot, user, workspace, session and legacy tokens,
``xoxc-``/``xoxd-`` browser session tokens, ``xoxe-`` refresh tokens, ``xoxr-``
config refresh tokens, the rotated ``xoxe.xoxb-``/``xoxe.xoxp-`` access tokens,
and ``xapp-`` app-level (Socket Mode) tokens. A private ``xox[abps]`` copy let
a pasted app-level token reach a task file in plaintext while the bot token
beside it was redacted (user feedback, P1-43). Every kind carries a digit
right after its dash, so the family requires one: a ``xoxo-<name>`` sign-off
in prose is left alone.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, List, Tuple


SLACK_TOKEN_PATTERN = re.compile(
    r"(?:xoxe\.)?xox[abcdeoprs]-\d[A-Za-z0-9-]*|xapp-\d[A-Za-z0-9-]*"
)
PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
)

Span = Tuple[int, int, str]
Finder = Callable[[str], Iterator[Tuple[int, int]]]


def _all_matches(pattern: re.Pattern) -> Finder:
    def find(text: str) -> Iterator[Tuple[int, int]]:
        for m in pattern.finditer(text):
            yield m.start(), m.end()
    return find


def _once_per_run(start: str, run_end: str, pattern: str) -> Finder:
    """pattern.finditer for a pattern whose every start inside one run (up to run_end) fails
    alike: tried at the run's first start only, so matching stays linear in the text."""
    start_re, run_end_re, pattern_re = re.compile(start), re.compile(run_end), re.compile(pattern)

    def find(text: str) -> Iterator[Tuple[int, int]]:
        pos = 0
        while (s := start_re.search(text, pos)) is not None:
            m = pattern_re.match(text, s.start())
            if m:
                yield m.start(), m.end()
                pos = m.end()
            else:
                stop = run_end_re.search(text, s.end())
                pos = stop.start() if stop else len(text)
    return find


_FALLBACK_PATTERNS: Tuple[Tuple[str, Finder], ...] = (
    ("AWS Access Key", _all_matches(re.compile(r"AKIA[A-Z0-9]{16}"))),
    ("GitHub Token", _all_matches(re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}"))),
    # Google/Gemini API keys. This repo's own load-bearing GEMINI_API_KEY has
    # this shape, so without it an owner could paste the key into a room and
    # have it persisted to the task file and owner-activity state despite the
    # documented "tokens, keys" redaction guarantee. The 35-char tail makes it
    # high-precision: prose mentioning "AIza" cannot reach the length.
    ("Google API Key", _all_matches(re.compile(r"AIza[0-9A-Za-z_-]{35}"))),
    # Fine-grained PATs (2022+) use a distinct prefix + longer body — the
    # legacy ghp_/gho_/… pattern above never matches them (review P1).
    ("GitHub Fine-Grained PAT", _all_matches(re.compile(r"github_pat_[A-Za-z0-9_]{36,}"))),
    ("Matrix Access Token", _all_matches(re.compile(r"syt_[A-Za-z0-9_-]{20,}"))),
    # Onboarding tokens are `<url><sep><secret>`, and the separator is accepted
    # in BOTH forms by the parser that consumes them (`_SEPARATOR_RE` in
    # remote_gateway_bridge: `\||%7[Cc]`) — the desktop connect flow writes the
    # URL-encoded one. Matching only the literal `|` here let a valid
    # `…/relay%7C<secret>` paste reach disk unredacted, which is exactly what
    # this module exists to prevent (review blocker). The lookahead stops the
    # URL run from swallowing the encoded separator, mirroring the parser's
    # first-separator-wins `search()`.
    ("Remote Task Token", _once_per_run(
        r"https?://", r"[\s|]|%7[Cc]",
        r"https?://(?:(?!%7[Cc])[^\s|])+(?:\||%7[Cc])[A-Za-z0-9_+/=-]{20,}",
    )),
    ("JSON Web Token", _once_per_run(
        r"eyJ", r"[^A-Za-z0-9_-]", r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
    )),
    ("Slack Token", _all_matches(SLACK_TOKEN_PATTERN)),
    ("OpenAI Token", _all_matches(re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}"))),
    ("Stripe Access Key", _all_matches(re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{24,}"))),
    ("Discord Bot Token", _once_per_run(
        r"[MNO]", r"[^A-Za-z\d_-]", r"[MNO][A-Za-z\d_-]{23,}\.[A-Za-z\d_-]{6,}\.[A-Za-z\d_-]{27,}",
    )),
    ("Telegram Bot Token", _all_matches(re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}"))),
    ("Private Key", _all_matches(PRIVATE_KEY_PATTERN)),
)
_ENTROPY_TYPES = {"Base64 High Entropy String", "Hex High Entropy String"}
_TOKENISH_RUN = re.compile(r"[A-Za-z0-9_+/=-]{20,}")


@dataclass(frozen=True)
class ChatSecretFilterResult:
    text: str
    secret_types: Tuple[str, ...]

    @property
    def detected(self) -> bool:
        return bool(self.secret_types)


def _detector_spans(text: str) -> tuple[List[Span], set[str]]:
    spans: List[Span] = []
    for secret_type, find in _FALLBACK_PATTERNS:
        spans += [(start, end, secret_type) for start, end in find(text)]
    return spans, {span[2] for span in spans}


def _line_bounds(text: str) -> List[Tuple[int, int]]:
    bounds, start = [], 0
    for line in text.split("\n"):
        bounds.append((start, start + len(line)))
        start += len(line) + 1
    return bounds


def _covered(text: str, line: Tuple[int, int], value: str, spans: List[Span]) -> bool:
    """Whether the hit's detected value occurs on its line and every occurrence lies in a span."""
    i = text.find(value, *line) if value else -1
    if i == -1:
        return False
    while i != -1:
        if not any(s <= i and i + len(value) <= e for s, e, _ in spans):
            return False
        i = text.find(value, i + 1, line[1])
    return True


def _hit_spans(text: str, hits: Iterable[object], spans: List[Span]) -> List[Span]:
    """A span for each detector hit no full-secret span covers: its whole line, or for a private
    key its line through the END marker line (end of text when unterminated)."""
    bounds = _line_bounds(text)
    out: List[Span] = []
    for hit in hits:
        idx = int(getattr(hit, "line_number", 0)) - 1
        if not 0 <= idx < len(bounds):
            continue
        secret_type = str(getattr(hit, "secret_type", "Secret"))
        start, end = bounds[idx]
        if secret_type == "Private Key":
            end = next((e for s, e in bounds[idx:]
                        if "-----END " in text[s:e] and "PRIVATE KEY-----" in text[s:e]), len(text))
        elif _covered(text, bounds[idx], str(getattr(hit, "secret_value", "") or ""), spans):
            continue
        out.append((start, end, secret_type))
    return out


def _apply_spans(text: str, spans: Iterable[Span]) -> str:
    """Every span of the original text replaced at once; overlapping spans become one placeholder."""
    merged: List[List] = []
    for start, end, secret_type in sorted(spans, key=lambda span: (span[0], -span[1])):
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end, secret_type])
    parts, pos = [], 0
    for start, end, secret_type in merged:
        parts += [text[pos:start], f"[REDACTED-{secret_type}]"]
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


def _actionable_hits(text: str, hits: Iterable[object]) -> list[object]:
    """Drop entropy-only prose false positives.

    detect-secrets intentionally treats ordinary English fragments as possible
    base64 entropy. For chat persistence, an entropy hit is actionable only when
    its source line also contains a contiguous token-shaped run. Vendor-specific
    detectors remain actionable without this extra gate.
    """
    lines = text.split("\n")
    actionable = []
    for hit in hits:
        secret_type = str(getattr(hit, "secret_type", ""))
        idx = int(getattr(hit, "line_number", 0)) - 1
        if secret_type in _ENTROPY_TYPES:
            if not (0 <= idx < len(lines) and _TOKENISH_RUN.search(lines[idx])):
                continue
        actionable.append(hit)
    return actionable


_SCANNER_BROKEN_WARNED = False


def _warn_scanner_broken(exc) -> None:
    global _SCANNER_BROKEN_WARNED
    if not _SCANNER_BROKEN_WARNED:
        print(f"[chat-secret-filter] secret scanner import FAILED ({exc}) — "
              "detect-secrets install is broken, not absent; curated fallback "
              "rules only until the environment is repaired", file=sys.stderr)
        _SCANNER_BROKEN_WARNED = True


def filter_chat_secrets(text: str) -> ChatSecretFilterResult:
    """Return text safe for persistence plus detected secret type names."""
    if not text:
        return ChatSecretFilterResult(text=text, secret_types=())

    spans, found = _detector_spans(text)
    try:
        from secret_scanner import scan_and_redact
    except ImportError as e:
        # Scanner-module absence = expected install shape (silent);
        # any deeper ImportError = broken install (surface it).
        if not (isinstance(e, ModuleNotFoundError) and e.name == "secret_scanner"):
            _warn_scanner_broken(e)
        return ChatSecretFilterResult(text=_apply_spans(text, spans), secret_types=tuple(sorted(found)))
    try:
        hits = _actionable_hits(text, scan_and_redact(text)[0])
        spans = spans + _hit_spans(text, hits, spans)
        found.update(str(hit.secret_type) for hit in hits)
    except Exception:
        # Missing optional dependency or detector failure: keep the curated
        # fallback result and never crash the bridge.
        pass

    return ChatSecretFilterResult(text=_apply_spans(text, spans), secret_types=tuple(sorted(found)))


def secret_handling_instruction(surface: str, secret_types: Iterable[str]) -> str:
    """Trusted task instruction appended only after a bridge detects a secret."""
    types = tuple(sorted(set(secret_types)))
    if not types:
        return ""
    label = ", ".join(types)
    delete_advice = (
        f" In the final reply, advise the sender to delete the original {surface} "
        "message after the work is complete."
        if surface in ("Discord", "Slack")
        else ""
    )
    return (
        "\n\n===SUTANDO SECURITY NOTICE (bridge-generated; do not ignore)===\n"
        f"The bridge detected and redacted possible secret material ({label}) before "
        "persisting this task. Never reproduce or request the redacted value in plaintext."
        f"{delete_advice} If the value was not already supplied through `vault set` "
        "and is needed for future work, tell the owner to store it with "
        "`vault set KEY VALUE`; do not claim an unnamed redacted value was stored.\n"
        "===END SUTANDO SECURITY NOTICE===\n"
    )
