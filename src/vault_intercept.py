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
import sys
from datetime import datetime, timezone
from typing import NamedTuple

_ACCOUNT = "sutando"

# The manifest is the NON-SECRET index of key NAMES (values live in macOS
# Keychain, per-host, never synced). Canonical location is
# `<workspace>/state/secret-vault/keys.json` — under the workspace contract
# rather than a home dotdir. It indexes the per-host Keychain, so it stays
# PER-HOST and must NOT sync: `state/` is outside the sync carrier set
# (whitelist mode un-ignores only notes/ + hosts/<host>/ + memory/), so this
# path is non-synced by construction. Were it synced, every node would inherit
# key names its local Keychain can't resolve — a lying index.
_LEGACY_MANIFEST_PATH = os.path.expanduser("~/.sutando-secret-vault/keys.json")


def _manifest_path() -> str:
    """Canonical manifest path under the resolved workspace. Falls back to the
    legacy home-dir path if the workspace can't be resolved (preserves behavior
    in import contexts where workspace_default isn't importable)."""
    try:
        from workspace_default import resolve_workspace
        return os.path.join(str(resolve_workspace()), "state", "secret-vault", "keys.json")
    except Exception:
        return _LEGACY_MANIFEST_PATH


def _read_manifest() -> dict:
    """Load the manifest, preferring the canonical path and falling back to the
    legacy home-dir path for existing installs. The fallback makes the first
    write self-migrate: `_register_key` reads via this helper (inheriting any
    legacy keys), then writes the merged set to the canonical path."""
    canonical = _manifest_path()
    candidates = [canonical]
    if _LEGACY_MANIFEST_PATH != canonical:
        candidates.append(_LEGACY_MANIFEST_PATH)
    for path in candidates:
        try:
            with open(path) as f:
                data = json.load(f)
            if path == _LEGACY_MANIFEST_PATH and path != canonical:
                print(
                    "vault: read legacy manifest (~/.sutando-secret-vault/keys.json); "
                    "migrating to <workspace>/state/secret-vault/ on next write.",
                    file=sys.stderr, flush=True,
                )
            return data
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return {}

# Matches: vault set KEY <value>  where value is:
#   - double-quoted string   "foo bar"
#   - single-quoted string   'foo bar'
#   - backtick-quoted string `foo bar`  (Discord markdown; backticks stripped)
#   - bare token (no spaces) foobar
#
# Loose regex — finds candidate `vault set KEY VALUE` matches anywhere in
# the text (including mid-prose). FP prevention is delegated to detect-secrets
# (see _replacer): a candidate is only acted on if the VALUE is recognized as
# a known secret pattern. This trades the regex line-anchor approach for
# pattern-based validation, eliminating both:
#   - FP: "the vault set command works fine" → "works" is not a secret → skip
#   - FN: "hey vault set APOLLO_KEY sk-..." mid-prose → "sk-..." is OpenAI → store
# Key/value separator is whitespace OR `=` (with optional surrounding spaces),
# so `vault set KEY VALUE`, `vault set KEY=VALUE`, and `vault set KEY = VALUE`
# all intercept. The KEY group stops at the first space or `=` (`[^\s=]+`) so
# the `=` form isn't swallowed whole — that swallowing is what let an owner's
# `vault set X_BEARER_TOKEN=…` slip past uncaught and land in plaintext on disk
# (2026-06-22 incident). Group numbering preserved: key=group(1), value
# alternatives=groups 2-5 (separator is non-capturing).
_VAULT_SET_RE = re.compile(
    r'\bvault\s+set\s+([^\s=]+)(?:\s*=\s*|\s+)(?:"([^"]*)"|\'([^\']*)\'|`([^`]*)`|(\S+))(?=\s|$|[.,!?;])',
    re.IGNORECASE,
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
    path = _manifest_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Read via _read_manifest so the first write after the move inherits any
    # legacy keys (self-migration) rather than starting an empty index.
    manifest = _read_manifest()
    manifest[key] = {"stored_at": datetime.now(timezone.utc).isoformat()}
    # Atomic write — concurrent bridge processes won't corrupt keys.json.
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp, path)


def list_vault_keys() -> list[str]:
    """Return all key names stored in the vault manifest (no values)."""
    return sorted(_read_manifest().keys())


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
        # FP guard: validate the VALUE field is actually a known secret pattern
        # via detect-secrets. This filters out prose matches like
        # "the vault set command works fine" where regex would otherwise capture
        # key="command", value="works" — "works" is not a known secret → skip.
        # Quoted values bypass the guard (user explicitly delimited the value).
        is_quoted = m.group(2) is not None or m.group(3) is not None or m.group(4) is not None
        if not is_quoted:
            try:
                from secret_scanner import scan_secrets
            except ImportError:
                # detect-secrets (the FP backstop) isn't installed. The vault-set
                # regex is DELIBERATELY loose — it matches `vault set K V` anywhere,
                # including mid-prose — and delegates false-positive rejection to
                # detect-secrets. Without it we can't tell a real secret from prose,
                # so storing unconditionally would store every "vault set X Y" mention
                # as junk AND redact legitimate text. Fail safe: refuse to store and
                # leave a SELF-DOCUMENTING placeholder in the task. The instruction
                # lives in the placeholder itself (co-located with the event) rather
                # than in always-loaded CLAUDE.md — zero permanent context cost, and
                # the agent reading the task sees exactly what to do. Quoted values
                # never reach here (is_quoted branch stores directly). No bridge-side
                # pip-install (env mutation in a message handler) and no separate task:
                # the refusal rides the normal task to core, channel context intact.
                print(
                    f"vault: detect-secrets not installed — refused unquoted "
                    f"`vault set {key}` (can't validate). Core will install the dep; "
                    f"quote the value to store immediately.",
                    flush=True,
                )
                failed.append(key)
                return (
                    f"vault set {key} [vault: REFUSED — detect-secrets not installed, "
                    f"can't validate an unquoted value. AGENT: run "
                    f"'python3 -m pip install detect-secrets', then tell the owner to "
                    f"re-send this vault set (or quote the value next time). Never echo "
                    f"or ask for the value.]"
                )
            if not scan_secrets(value):
                # Not a known secret pattern — assume this is prose, leave it alone.
                return m.group(0)
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
