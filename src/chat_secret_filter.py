"""Fail-closed secret redaction for persisted inbound chat content.

The explicit ``vault set KEY VALUE`` path stores named values in Keychain.
This module covers the other common case: somebody pastes a token into ordinary
Discord/Slack prose. Known values are replaced before task files, owner-activity
state, prompt scratch files, or bridge logs are written.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Iterable, Tuple


_FALLBACK_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("AWS Access Key", re.compile(r"AKIA[A-Z0-9]{16}")),
    ("GitHub Token", re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}")),
    # Google/Gemini API keys. This repo's own load-bearing GEMINI_API_KEY has
    # this shape, so without it an owner could paste the key into a room and
    # have it persisted to the task file and owner-activity state despite the
    # documented "tokens, keys" redaction guarantee. The 35-char tail makes it
    # high-precision: prose mentioning "AIza" cannot reach the length.
    ("Google API Key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    # Fine-grained PATs (2022+) use a distinct prefix + longer body — the
    # legacy ghp_/gho_/… pattern above never matches them (review P1).
    ("GitHub Fine-Grained PAT", re.compile(r"github_pat_[A-Za-z0-9_]{36,}")),
    ("Matrix Access Token", re.compile(r"syt_[A-Za-z0-9_-]{20,}")),
    # Onboarding tokens are `<url><sep><secret>`, and the separator is accepted
    # in BOTH forms by the parser that consumes them (`_SEPARATOR_RE` in
    # remote_gateway_bridge: `\||%7[Cc]`) — the desktop connect flow writes the
    # URL-encoded one. Matching only the literal `|` here let a valid
    # `…/relay%7C<secret>` paste reach disk unredacted, which is exactly what
    # this module exists to prevent (review blocker). The lookahead stops the
    # URL run from swallowing the encoded separator, mirroring the parser's
    # first-separator-wins `search()`.
    ("Remote Task Token", re.compile(
        r"https?://(?:(?!%7[Cc])[^\s|])+(?:\||%7[Cc])[A-Za-z0-9_+/=-]{20,}"
    )),
    ("JSON Web Token", re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")),
    ("Slack Token", re.compile(r"xox[abps]-[A-Za-z0-9-]+")),
    ("OpenAI Token", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("Stripe Access Key", re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{24,}")),
    ("Discord Bot Token", re.compile(
        r"[MNO][A-Za-z\d_-]{23,}\.[A-Za-z\d_-]{6,}\.[A-Za-z\d_-]{27,}"
    )),
    ("Telegram Bot Token", re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}")),
    ("Private Key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"
    )),
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


def _replace_patterns(text: str) -> tuple[str, set[str]]:
    redacted = text
    found: set[str] = set()
    for secret_type, pattern in _FALLBACK_PATTERNS:
        redacted, count = pattern.subn(f"[REDACTED-{secret_type}]", redacted)
        if count:
            found.add(secret_type)
    return redacted, found


def _redact_unhandled_hit_lines(text: str, redacted: str, hits: Iterable[object]) -> str:
    """Redact a whole line when detection found a secret but no full-span
    pattern changed it. Losing the line is safer than persisting a partial hit."""
    source_lines = text.split("\n")
    output_lines = redacted.split("\n")
    for hit in hits:
        idx = int(getattr(hit, "line_number", 0)) - 1
        if 0 <= idx < len(source_lines) and idx < len(output_lines):
            if output_lines[idx] == source_lines[idx]:
                secret_type = str(getattr(hit, "secret_type", "Secret"))
                output_lines[idx] = f"[REDACTED-{secret_type}]"
    return "\n".join(output_lines)


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

    redacted, found = _replace_patterns(text)
    try:
        from secret_scanner import scan_and_redact
    except ImportError as e:
        # Scanner-module absence = expected install shape (silent);
        # any deeper ImportError = broken install (surface it).
        if not (isinstance(e, ModuleNotFoundError) and e.name == "secret_scanner"):
            _warn_scanner_broken(e)
        return ChatSecretFilterResult(text=redacted, secret_types=tuple(sorted(found)))
    try:
        hits, library_redacted = scan_and_redact(text)
        hits = _actionable_hits(text, hits)
        # Recompute when the library changed a line for an entropy-only false
        # positive that we just discarded; otherwise ordinary prose would stay
        # redacted even though no actionable hit remains.
        if not hits:
            library_redacted = text
        # Generic detection does not store unnamed values; make the placeholder
        # truthful instead of inheriting the scanner's vault-oriented wording.
        library_redacted = library_redacted.replace(
            "[STORED-IN-KEYCHAIN-", "[REDACTED-"
        )
        library_redacted = _redact_unhandled_hit_lines(text, library_redacted, hits)
        redacted, fallback_found = _replace_patterns(library_redacted)
        found.update(fallback_found)
        found.update(str(hit.secret_type) for hit in hits)
    except Exception:
        # Missing optional dependency or detector failure: keep the curated
        # fallback result and never crash the bridge.
        pass

    return ChatSecretFilterResult(text=redacted, secret_types=tuple(sorted(found)))


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
