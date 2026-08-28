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

from vault_set_grammar import VAULT_SET_RE as _VAULT_SET_RE
from vault_set_grammar import redact_vault_commands as _grammar_redact_vault_commands

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
# a known secret pattern, OR the KEY isn't a single plain lowercase word
# (see _LOOKS_LIKE_PLAIN_LOWERCASE_WORD below — added for #2074). This trades the regex
# line-anchor approach for pattern-based validation, eliminating both:
#   - FP: "the vault set command works fine" → key="command" (not env-shaped),
#     value="works" (not a secret) → skip, left as prose
#   - FN: "hey vault set APOLLO_KEY sk-..." mid-prose → "sk-..." is OpenAI → store
# Grammar is canonical in vault_set_grammar.py, imported above as _VAULT_SET_RE — not
# redefined here. This file adds the storage half (Keychain, detect-secrets) on top.

# #2074: an unquoted value the FP guard doesn't recognize isn't proof of
# prose — it can be a real secret the classifier missed (a 32-char Discord
# client secret, a pa-/al-prefixed API key, ...). Used as a second signal
# alongside scan_secrets(): only treat the match as prose (leave it alone)
# when the key is a single plain lowercase-ASCII word; fail closed (redact +
# report failed) for every other key shape, so ordinary sentences that
# happen to match the loose regex still pass through untouched while any
# deliberately-named key gets the fail-closed treatment.
#
# This is an EXCLUSION test, not an inclusion list: "prose" = the key
# fullmatches `[a-z]+` and nothing else. Anything that isn't a single plain
# lowercase word — digits, underscores, dashes, uppercase letters, or ANY
# other punctuation (periods, slashes, colons, plus signs, @-signs, ...) —
# counts as a deliberate key. Enumerating "deliberate" characters instead
# (as the first version of this fix did) is an allowlist that will always
# miss some real-world key shape; enumerating "prose" is a much smaller,
# closed set (english words are just letters) so the exclusion is exhaustive
# by construction.
#
# PR #2052 review history:
# - qingyun-wu (2026-07-12, round 1): the original version only matched
#   SCREAMING_SNAKE_CASE (`^[A-Z][A-Z0-9_]{1,}$`), so `pr_triage_activity_secret`,
#   `PrTriageActivitySecret`, and `SOME-KEY` all still leaked.
# - qingyun-wu (2026-07-12, round 2): the round-1 fix's own docstring said
#   "deliberate = anything that isn't a single all-lowercase word," but the
#   regex (`[A-Z0-9_-]`, an inclusion list) didn't actually implement that —
#   lowercase keys with OTHER punctuation (`apikey.vault`, `apikey/vault`,
#   `user:id`, `token+name`, `@token`) still slipped through as "prose" and
#   leaked. This version finally matches the documented rule exactly: prose
#   is defined as the narrow case (plain lowercase word), everything else
#   fails closed.
_LOOKS_LIKE_PLAIN_LOWERCASE_WORD = re.compile(r"[a-z]+")


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


# Keys double as env-var names via the `env` verb / get_vault_key consumers, so
# the public setter holds them to env-var-safe naming (the chat-interception
# path has its own, looser matching + FP guards above).
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def set_vault_key(key: str, value: str) -> None:
    """Store a secret in Keychain + manifest — the public, non-chat write path.

    Same storage as the `vault set` chat interception (`security
    add-generic-password -a sutando -s KEY -U` + manifest registration), for
    callers that already hold the value programmatically (CLI `set` verb,
    desktop Settings' BYO-key entry). Raises ValueError on an invalid key or
    empty value; RuntimeError when the Keychain write fails.
    """
    if not _ENV_KEY_RE.match(key or ""):
        raise ValueError(f"vault: invalid key name '{key}' (want [A-Za-z_][A-Za-z0-9_]*)")
    if not value:
        raise ValueError("vault: refusing to store an empty value")
    _store_in_keychain(key, value)


def _deregister_key(key: str) -> None:
    # Reverse of _register_key. Absent key is a no-op (no write), so a re-run
    # does not churn the file. Atomic write, same as _register_key.
    manifest = _read_manifest()
    if key not in manifest:
        return
    del manifest[key]
    path = _manifest_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp, path)


_ERRSEC_ITEM_NOT_FOUND = 44  # errSecItemNotFound


def _delete_from_keychain(key: str) -> None:
    # rc 44 is errSecItemNotFound — already gone, so the manifest drop proceeds.
    # Any other non-zero may leave the item LIVE; dropping it then strands a secret.
    result = subprocess.run(
        ["security", "delete-generic-password", "-a", _ACCOUNT, "-s", key],
        capture_output=True,
    )
    if result.returncode not in (0, _ERRSEC_ITEM_NOT_FOUND):
        raise RuntimeError(
            f"vault: failed to delete '{key}' from Keychain "
            f"(rc={result.returncode}); manifest entry kept so the secret is not stranded"
        )
    _deregister_key(key)


def delete_vault_key(key: str) -> None:
    """Remove a secret from Keychain + manifest — the reverse of set_vault_key.

    Idempotent: deleting an absent key, or reconciling a half-state where only
    one of {Keychain item, manifest entry} survives, is SUCCESS, not an error —
    the desktop T2.8 teardown re-runs and must not fail on an already-gone key.
    Raises ValueError on an invalid key name (the same rule set_vault_key
    enforces — not loosened here), and RuntimeError when the Keychain delete
    fails for any reason other than the item already being absent.
    """
    if not _ENV_KEY_RE.match(key or ""):
        raise ValueError(f"vault: invalid key name '{key}' (want [A-Za-z_][A-Za-z0-9_]*)")
    _delete_from_keychain(key)


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
        # via detect-secrets. Genuine prose matches like "the vault set command
        # works fine" (key="command", value="works") are left alone — see the
        # _LOOKS_LIKE_PLAIN_LOWERCASE_WORD check below for why "not a known
        # secret" alone no longer means "assume prose" (#2074). Quoted values bypass the guard
        # entirely (user explicitly delimited the value).
        is_quoted = m.group(2) is not None or m.group(3) is not None or m.group(4) is not None
        if not is_quoted:
            try:
                from secret_scanner import DETECT_SECRETS_ACTIVE, scan_secrets
            except ImportError:
                DETECT_SECRETS_ACTIVE = False
            # Capability gate: the guarded import loads even when degraded,
            # so an ImportError gate would skip this refusal (yixuan, #3103).
            if not DETECT_SECRETS_ACTIVE:
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
                    f"vault: detect-secrets not installed in {sys.executable} — refused "
                    f"unquoted `vault set {key}` (can't validate). Quote the value to "
                    f"store immediately; see the task text for the install command.",
                    flush=True,
                )
                failed.append(key)
                # The install hint must name THIS interpreter (sys.executable),
                # not a bare `python3`: the bridges are launched with whichever
                # interpreter had their client library (see the probes in
                # src/startup.sh), which is frequently NOT the `python3` first on
                # PATH. Installing into the wrong one leaves the refusal in place
                # and looks like the fix silently failed.
                #
                # The --break-system-packages fallback is required, not optional
                # noise: on a stock Homebrew/macOS python both `pip install` AND
                # `pip install --user` are refused by PEP 668, so the previous
                # bare `python3 -m pip install detect-secrets` could not succeed
                # on the most common Sutando host. Verified 2026-07-20.
                return (
                    f"vault set {key} [vault: REFUSED — detect-secrets not installed, "
                    f"can't validate an unquoted value. AGENT: run "
                    f"'{sys.executable} -m pip install detect-secrets' — if that fails "
                    f"with 'externally-managed-environment' (PEP 668), retry the same "
                    f"command with --break-system-packages. Then tell the owner to "
                    f"re-send this vault set (or quote the value next time). Never echo "
                    f"or ask for the value.]"
                )
            if not scan_secrets(value):
                if _LOOKS_LIKE_PLAIN_LOWERCASE_WORD.fullmatch(key):
                    # Key is nothing but a plain lowercase word (no digits,
                    # underscores, dashes, uppercase, or other punctuation) —
                    # this is almost certainly prose ("the vault set command
                    # works fine" → key="command"), not a real command. Leave
                    # it alone; redacting it would mangle ordinary sentences.
                    # NOTE: this is a .fullmatch() exclusion test ("is the
                    # WHOLE key just a lowercase word?"), not a .search() for
                    # qualifying characters — the earlier version enumerated
                    # "deliberate" characters (digit/underscore/dash/upper)
                    # and missed lowercase-with-other-punctuation keys like
                    # "apikey.vault" or "user:id", which still leaked.
                    return m.group(0)
                # Key is NOT a plain lowercase word (SCREAMING_SNAKE_CASE,
                # lowercase snake_case, camelCase, PascalCase, dash-separated,
                # or any other punctuation-containing shape) but the value
                # wasn't recognized as a known secret shape. That's a classifier
                # miss, not proof of prose — issue #2074: a real Discord
                # client secret and pa-/al-prefixed API keys both slipped
                # through here as unrecognized-therefore-untouched, leaking
                # plaintext to disk (same root cause #2052 hit for bare
                # UUIDs, fixed by widening recognition — but recognition can
                # never be exhaustive, so THIS branch must fail closed too).
                # Never store an unvalidated value; redact and surface it so
                # the owner can resend quoted (which bypasses the guard
                # entirely) to store it for real.
                failed.append(key)
                return (
                    f"vault set {key} [vault: value not recognized as a secret, so it was "
                    f"NOT STORED **and the text you sent has been discarded** — nothing was "
                    f"kept anywhere, so you will need the value again. Resend it QUOTED: "
                    f"vault set {key} \"value\" — quoting skips this classifier and "
                    f"ATTEMPTS storage; you are stored only if the reply says "
                    f"[STORED-IN-KEYCHAIN].]"
                )
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
    while ensuring the Keychain is never written by an untrusted sender. Delegates
    to the canonical vault_set_grammar implementation (single source, see the
    module-level note above _VAULT_SET_RE) rather than reimplementing it here.
    """
    return _grammar_redact_vault_commands(text, placeholder="[vault: non-owner tier — ignored]")
