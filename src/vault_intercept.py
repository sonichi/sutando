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

    result = intercept_vault_commands(raw_message)
    # result.text  — sanitized, safe to write to disk (plaintext always gone)
    # result.stored — keys successfully stored to Keychain
    # result.failed — keys that failed to store (still redacted from text)

Supported syntax (all case-insensitive):
    vault set KEY value
    vault set KEY "value with spaces"
    vault set KEY 'value with spaces'
    vault set KEY `value`         (backtick-quoted — backticks stripped)
    vault set KEY `value with spaces`  (backtick-quoted with spaces)

Multiple commands in one message are all intercepted in a single pass.

`redact_vault_commands(text)` is the non-storing variant: it scrubs vault-set
patterns from text without touching the Keychain.  Use it for non-owner-tier
messages where we want to prevent accidental secret exposure in task files but
must not write to the owner's Keychain.
"""

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import NamedTuple

_ACCOUNT = "sutando"
_MANIFEST_PATH = os.path.expanduser("~/.sutando-vault/keys.json")

# Matches: vault set KEY <value>  where value is:
#   - double-quoted string   "foo bar"
#   - single-quoted string   'foo bar'
#   - backtick-quoted string `foo bar`  (Discord markdown; backticks stripped)
#   - bare token (no spaces) foobar
#
# Anchored to start-of-line (or after a newline) to prevent conversational
# false-positives like "the vault set command works" from storing garbage.
# The (?:\s|$) tail avoids partial-token corruption on bare values.
_VAULT_SET_RE = re.compile(
    r'(?:^|\n)\s*vault\s+set\s+(\S+)\s+(?:"([^"]*)"|\'([^\']*)\'|`([^`]*)`|(\S+))(?:\s|$)',
    re.IGNORECASE | re.MULTILINE,
)


class InterceptResult(NamedTuple):
    text: str          # sanitized message text, safe to write to disk
    stored: list[str]  # keys successfully stored to Keychain
    failed: list[str]  # keys that could NOT be stored (secret still redacted)


def _store_in_keychain(key: str, value: str) -> None:
    # Note: value is passed as an argv element — briefly visible in `ps` to
    # the same user. Acceptable on a single-user Mac; not a multi-user safe API.
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
    _register_key(key)


def _register_key(key: str) -> None:
    os.makedirs(os.path.dirname(_MANIFEST_PATH), exist_ok=True)
    try:
        with open(_MANIFEST_PATH) as f:
            manifest = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        manifest = {}
    manifest[key] = {"stored_at": datetime.now(timezone.utc).isoformat()}
    # Atomic write — concurrent bridge processes won't corrupt keys.json.
    tmp = _MANIFEST_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp, _MANIFEST_PATH)


def list_vault_keys() -> list[str]:
    """Return all key names stored in the vault manifest (no values)."""
    try:
        with open(_MANIFEST_PATH) as f:
            return sorted(json.load(f).keys())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def get_vault_key(key: str) -> str:
    """Retrieve a secret value from Keychain. Raises KeyError if not found."""
    result = subprocess.run(
        ["security", "find-generic-password", "-a", _ACCOUNT, "-s", key, "-w"],
        capture_output=True,
    )
    if result.returncode != 0:
        raise KeyError(f"vault: key '{key}' not found in Keychain")
    return result.stdout.decode().strip()


def intercept_vault_commands(text: str) -> InterceptResult:
    """Detect vault-set commands in `text`, store secrets, return sanitized text.

    Fail-closed: the plaintext secret is ALWAYS redacted from the returned text,
    even when the Keychain write fails. Failed keys are reported in result.failed
    so the bridge can notify the user without leaking the secret.
    """
    if not text:
        return InterceptResult(text=text, stored=[], failed=[])

    stored: list[str] = []
    failed: list[str] = []

    def _replacer(m: re.Match) -> str:
        key = m.group(1)
        # Groups 2/3/4/5: double-quoted / single-quoted / backtick / bare token.
        value = next(
            (g for g in (m.group(2), m.group(3), m.group(4), m.group(5)) if g is not None),
            "",
        )
        if not value:
            # Reject empty value — ambiguous and almost certainly a mistake.
            failed.append(key)
            return f"vault set {key} [VAULT-EMPTY-VALUE]"
        try:
            _store_in_keychain(key, value)
            stored.append(key)
            return f"vault set {key} [STORED-IN-KEYCHAIN]"
        except RuntimeError:
            # Store failed — redact anyway so plaintext never reaches disk.
            failed.append(key)
            return f"vault set {key} [VAULT-STORE-FAILED]"

    sanitized = _VAULT_SET_RE.sub(_replacer, text)
    return InterceptResult(text=sanitized, stored=stored, failed=failed)


def redact_vault_commands(text: str) -> str:
    """Scrub vault-set patterns from text WITHOUT touching the Keychain.

    Use for non-owner-tier messages: prevents secrets from landing in task files
    while ensuring the Keychain is never written by an untrusted sender.
    """
    if not text:
        return text
    return _VAULT_SET_RE.sub(
        lambda m: f"vault set {m.group(1)} [vault: non-owner tier — ignored]",
        text,
    )
