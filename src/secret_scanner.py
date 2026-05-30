"""Library-based secret detection for inbound bridge messages.

Phase 1 of issue #1354: provides a scanner module that detects known secret
patterns in arbitrary chat text via `detect-secrets`. Phase 2 (bridge
integration: pending-secrets buffer, ask-for-KEY follow-up, Keychain store)
lands in a separate PR.

Why a library, not hand-written regex: see PR description for the empirical
comparison. Briefly: detect-secrets ships 25+ secret-type plugins (AWS,
GitHub, Slack, JWT, PEM, OpenAI, Stripe, Twilio, Discord, etc.) maintained
externally, far broader than what a hand-maintained regex could realistically
cover; and it adds entropy checks that the regex path can't easily reproduce.

One patch on top of stock detect-secrets:

**Whole-secret redaction** — stock `s.secret_value` from detect-secrets can
contain only the matched-prefix segment for some secret types (e.g. for a
40-char GitHub PAT, the value field carries the `ghp_` head but not the rest
of the token). Naive `text.replace(secret_value, placeholder)` leaves the
suffix exposed in the bridge's task file. `redact_secrets` re-runs the
per-type pattern against the source line and replaces the full secret span.
"""

import os
import re
import tempfile
from dataclasses import dataclass
from typing import Iterable

from detect_secrets import SecretsCollection
from detect_secrets.settings import transient_settings


# Plugins enabled for the scan. detect-secrets v1.5+ ships these out of the
# box; we explicitly enumerate them so the bridge behavior doesn't drift if
# the library default set changes upstream.
_PLUGINS_CONFIG = [
    {"name": "AWSKeyDetector"},
    {"name": "AzureStorageKeyDetector"},
    {"name": "DiscordBotTokenDetector"},
    {"name": "GitHubTokenDetector"},
    {"name": "GitLabTokenDetector"},
    {"name": "JwtTokenDetector"},
    {"name": "MailchimpDetector"},
    {"name": "NpmDetector"},
    {"name": "OpenAIDetector"},
    {"name": "PrivateKeyDetector"},
    {"name": "PypiTokenDetector"},
    {"name": "SendGridDetector"},
    {"name": "SlackDetector"},
    {"name": "SquareOAuthDetector"},
    {"name": "StripeDetector"},
    {"name": "TelegramBotTokenDetector"},
    {"name": "TwilioKeyDetector"},
    {"name": "Base64HighEntropyString", "limit": 4.5},
    {"name": "HexHighEntropyString", "limit": 3.0},
]


# Per-type fallback regex for whole-secret redaction. detect-secrets' internal
# patterns aren't all stably exposed, so we keep our own anchored patterns
# here keyed by `secret_type` strings. Adding a new type means enabling its
# plugin above AND adding an entry here.
_FULL_PATTERNS: dict[str, re.Pattern] = {
    "AWS Access Key": re.compile(r"AKIA[A-Z0-9]{16}"),
    "GitHub Token": re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}"),
    "JSON Web Token": re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    "Slack Token": re.compile(r"xox[abps]-[A-Za-z0-9-]+"),
    "Private Key": re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"
    ),
    "OpenAI Token": re.compile(r"sk-[A-Za-z0-9_-]*[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}"),  # matches detect-secrets OpenAIDetector pattern
    "Stripe Access Key": re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{24,}"),
    "Discord Bot Token": re.compile(
        r"[MNO][A-Za-z\d_-]{23,}\.[A-Za-z\d_-]{6,}\.[A-Za-z\d_-]{27,}"
    ),
    "Telegram Bot Token": re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}"),
}


@dataclass
class SecretHit:
    """One detected secret. `secret_type` is e.g. "GitHub Token"."""

    secret_type: str
    line_number: int


def scan_secrets(text: str) -> list[SecretHit]:
    """Return list of SecretHit for every known-secret-pattern match in `text`.

    Scanning runs against the full text (multi-line); each hit carries the
    line number where the secret was found so `redact_secrets` can rewrite
    precisely that line without disturbing the rest of the message.
    """
    with transient_settings({"plugins_used": _PLUGINS_CONFIG}):
        sc = SecretsCollection()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(text)
            fname = f.name
        try:
            sc.scan_file(fname)
            return [
                SecretHit(secret_type=s.type, line_number=s.line_number)
                for fileset in sc.data.values()
                for s in fileset
            ]
        finally:
            os.unlink(fname)


def redact_secrets(text: str, hits: Iterable[SecretHit]) -> str:
    """Replace each hit's full secret span with `[STORED-IN-KEYCHAIN-{type}]`.

    Single-line secrets: re-run the per-type pattern against the source line
    of each hit (catches the full secret value even when detect-secrets only
    returned a matched-prefix segment).

    Multi-line PEM blocks: detect-secrets hits on the `-----BEGIN ... -----`
    marker line; the actual key bytes span subsequent lines until the
    matching `-----END ... -----` marker. Redact the entire BEGIN..END span,
    since a partial redact would leave the key payload exposed.

    Lines without a matching pattern are left alone (safer than partial
    redaction of an unknown secret format).
    """
    lines = text.split("\n")
    redact_ranges: list[tuple[int, int, str]] = []  # (begin_idx, end_idx, type)
    for h in hits:
        idx = h.line_number - 1
        if not (0 <= idx < len(lines)):
            continue
        if h.secret_type == "Private Key":
            # Find END marker on or after the hit line.
            end_idx = idx
            for j in range(idx, len(lines)):
                if "-----END " in lines[j] and "PRIVATE KEY-----" in lines[j]:
                    end_idx = j
                    break
            redact_ranges.append((idx, end_idx, h.secret_type))
        else:
            redact_ranges.append((idx, idx, h.secret_type))
    # Apply highest-line-number first so earlier indexes stay valid.
    for begin_idx, end_idx, stype in sorted(redact_ranges, key=lambda r: -r[0]):
        if begin_idx == end_idx and stype != "Private Key":
            pattern = _FULL_PATTERNS.get(stype)
            if pattern is not None:
                lines[begin_idx] = pattern.sub(
                    f"[STORED-IN-KEYCHAIN-{stype}]", lines[begin_idx]
                )
        else:
            # Multi-line redact: replace the whole span with one placeholder.
            lines[begin_idx : end_idx + 1] = [f"[STORED-IN-KEYCHAIN-{stype}]"]
    return "\n".join(lines)


def scan_and_redact(text: str) -> tuple[list[SecretHit], str]:
    """Convenience wrapper for the common scan-then-redact path."""
    hits = scan_secrets(text)
    return hits, redact_secrets(text, hits)
