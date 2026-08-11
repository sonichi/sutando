"""Pure, dependency-free `vault set KEY VALUE` grammar — regex + redact-only.

Extracted from `vault_intercept.py` (2026-08-11) so the pattern has one canonical
source. `vault_intercept.py`'s own `intercept_vault_commands`/`redact_vault_commands`
import from here rather than duplicating the regex, and this module is also the one
`packages/ag2-sparrow` vendors verbatim (see `tools/sync_from_src.py`) — the standalone
package needs the redaction shape but never the Keychain-writing storage path, which
stays monorepo-only in `vault_intercept.py` (system Keychain access, not a pure utility).

No imports beyond `re`. Any change to the accepted `vault set` syntax belongs here,
not in a copy — a hand-copied grammar silently diverges the moment this file changes.
"""
from __future__ import annotations

import re

# Key/value separator is whitespace OR `=` (with optional surrounding spaces), so
# `vault set KEY VALUE`, `vault set KEY=VALUE`, and `vault set KEY = VALUE` all
# intercept. The KEY group stops at the first space or `=` (`[^\s=]+`) so the `=`
# form isn't swallowed whole. Group numbering: key=group(1), value alternatives=
# groups 2-5 (separator is non-capturing). See vault_intercept.py's own history
# for the FP/FN incidents this exact shape was tuned against.
VAULT_SET_RE = re.compile(
    r'\bvault\s+set\s+([^\s=]+)(?:\s*=\s*|\s+)(?:"([^"]*)"|\'([^\']*)\'|`([^`]*)`|(\S+))'
    r'(?=\s|$|[.,!?;])',
    re.IGNORECASE,
)


def redact_vault_commands(text: str, *, placeholder: str = "[vault: non-owner tier — ignored]") -> str:
    """Scrub vault-set patterns from `text` WITHOUT touching any store.

    Pure string transform, no I/O, no external dependency — safe for any caller
    that only needs "don't let this reach disk," not "actually store it."
    `placeholder` lets callers customize the reason without re-deriving the regex.
    """
    if not text:
        return text
    return VAULT_SET_RE.sub(
        lambda m: f"vault set {m.group(1)} {placeholder}",
        text,
    )
