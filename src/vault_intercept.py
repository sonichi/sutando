from __future__ import annotations
"""Bridge-level vault secret interception.

Detects `vault set KEY VALUE` patterns in incoming messages BEFORE they
are written to task files on disk. Secrets go straight to macOS Keychain
(encrypted at rest) and the task file receives `[STORED-IN-KEYCHAIN]`
as a placeholder.

Secret lifecycle:
  Slack/Discord API → bridge (in-memory, SSL in transit)
  → Keychain (encrypted) — disk never sees plaintext.

Usage (in any bridge's message handler):

    from vault_intercept import intercept_vault_commands

    text, stored = intercept_vault_commands(raw_message)
    # `text` is sanitized; write it to the task file.
    # `stored` lists the keys that were persisted (for logging).

Supported syntax (all case-insensitive):
    vault set KEY value
    vault set KEY "value with spaces"
    vault set KEY 'value with spaces'

Multiple commands in one message are all intercepted in a single pass.
"""

import re
import subprocess
from typing import NamedTuple

_ACCOUNT = "sutando"

# Matches: vault set KEY <value>  where value is:
#   - double-quoted string   "foo bar"
#   - single-quoted string   'foo bar'
#   - bare token (no spaces) foobar
_VAULT_SET_RE = re.compile(
    r'vault\s+set\s+(\S+)\s+(?:"([^"]*)"|\'([^\']*)\'|(\S+))',
    re.IGNORECASE,
)


class InterceptResult(NamedTuple):
    text: str         # sanitized message text, safe to write to disk
    stored: list[str] # keys successfully stored to Keychain


def _store_in_keychain(key: str, value: str) -> None:
    result = subprocess.run(
        [
            "security", "add-generic-password",
            "-a", _ACCOUNT,
            "-s", key,
            "-w", value,
            "-U",   # update if already exists
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"vault: failed to store '{key}': "
            f"{result.stderr.decode(errors='replace').strip()}"
        )


def intercept_vault_commands(text: str) -> InterceptResult:
    """Detect vault-set commands in `text`, store secrets, return sanitized text.

    Raises RuntimeError if a Keychain write fails (so the bridge can log and
    optionally reply with an error rather than silently dropping the secret).
    """
    if not text:
        return InterceptResult(text=text, stored=[])

    stored: list[str] = []

    def _replacer(m: re.Match) -> str:
        key = m.group(1)
        # Groups 2/3/4 correspond to double-quoted / single-quoted / bare token.
        value = m.group(2) if m.group(2) is not None else (
            m.group(3) if m.group(3) is not None else m.group(4)
        )
        _store_in_keychain(key, value)
        stored.append(key)
        return f"vault set {key} [STORED-IN-KEYCHAIN]"

    sanitized = _VAULT_SET_RE.sub(_replacer, text)
    return InterceptResult(text=sanitized, stored=stored)
