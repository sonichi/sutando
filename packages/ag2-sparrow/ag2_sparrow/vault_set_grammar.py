"""Pure, dependency-free `vault set KEY VALUE` grammar — regex + redact-only.

Canonical source for `vault_intercept.py` and the vendored ag2-sparrow copy; no imports beyond `re`.
"""
from __future__ import annotations

import re

# Separator is whitespace or `=`; KEY stops at the first space/`=` so `=` isn't swallowed. Unquoted
# value is lazy with an optional trailing-punctuation lookahead so `.,!?;` isn't captured into the secret.
VAULT_SET_RE = re.compile(
    r'\bvault\s+set\s+([^\s=]+)(?:\s*=\s*|\s+)(?:"([^"]*)"|\'([^\']*)\'|`([^`]*)`|(\S+?))'
    r'(?=[.,!?;]?(?:\s|$))',
    re.IGNORECASE,
)


def redact_vault_commands(text: str, *, placeholder: str = "[vault: non-owner tier — ignored]") -> str:
    """Scrub vault-set patterns from `text` without touching any store."""
    if not text:
        return text
    return VAULT_SET_RE.sub(
        lambda m: f"vault set {m.group(1)} {placeholder}",
        text,
    )
