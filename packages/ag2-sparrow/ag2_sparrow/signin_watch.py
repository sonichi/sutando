"""Which account a local CLI is signed in with, read from its own sign-in file, so its supervisor can
restart it after an account switch (a running CLI keeps the session it started with: Codex then fails
with "refresh token revoked", Claude Code stays on the old account).

Only non-secret identity fields are read and kept: Codex's `auth.json` account id and the email claim of
its id token, Claude Code's `.claude.json` `oauthAccount` uuid and email. No token is returned, kept or
logged; what a supervisor records is `fingerprint()`, a short hash of the identity.

An identity is `(account, email)`; `SIGNED_OUT` when the file is absent or names no account; None when
the file could not be read (refused, mid-write, not JSON), which never counts as a change. Stdlib.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os

from .bounded_file_read import read_regular

SIGNED_OUT = ("", "")
CODEX_AUTH = "auth.json"
CLAUDE_CONFIG_MAX = 32 << 20     # `.claude.json` holds every project's history; still a bounded read


def fingerprint(identity) -> str:
    """A short, stable hash of an identity: what is recorded and compared, never the fields themselves."""
    account, email = identity
    return hashlib.sha256(("%s\0%s" % (account, email.lower())).encode("utf-8")).hexdigest()[:16]


def _load(path: str, limit: int):
    """The file's JSON object, {} when it is absent, None when it cannot be read as one."""
    text, why = read_regular(path, limit)
    if text is None:
        return {} if not why else None
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _jwt_email(token) -> str:
    """The email claim of an id token's payload ('' when there is none); the token is not kept."""
    if not isinstance(token, str) or token.count(".") != 2:
        return ""
    part = token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, TypeError):
        return ""
    if not isinstance(claims, dict):
        return ""
    profile = claims.get("https://api.openai.com/profile")
    email = claims.get("email") or (profile.get("email") if isinstance(profile, dict) else "")
    return email if isinstance(email, str) else ""


def codex_home(environ=None) -> str:
    env = os.environ if environ is None else environ
    return env.get("CODEX_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".codex")


def codex_identity(home: str):
    """Codex's account from `<CODEX_HOME>/auth.json`: its ChatGPT account id and email; an API-key login
    reads as ('api-key', '') without the key being looked at."""
    data = _load(os.path.join(home, CODEX_AUTH), 1 << 20)
    if data is None:
        return None
    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        account = tokens.get("account_id")
        account = account if isinstance(account, str) else ""
        email = _jwt_email(tokens.get("id_token"))
        if account or email:
            return account, email
    return ("api-key", "") if data.get("OPENAI_API_KEY") else SIGNED_OUT


def claude_identity(config_file: str):
    """Claude Code's account from its global config (`.claude.json`): `oauthAccount`'s uuid and email.
    The credential itself is in the Keychain, which is never read here."""
    data = _load(config_file, CLAUDE_CONFIG_MAX)
    if data is None:
        return None
    acct = data.get("oauthAccount")
    if not isinstance(acct, dict):
        return SIGNED_OUT
    account, email = acct.get("accountUuid"), acct.get("emailAddress")
    return (account if isinstance(account, str) else "", email if isinstance(email, str) else "")


class SigninWatch:
    """One sign-in file, re-read only when its stat changes (`.claude.json` is rewritten often and can be
    large). `current()` is the identity now on disk, None when unreadable."""

    def __init__(self, path: str, read, stat=os.stat):
        self.path, self.read, self.stat = path, read, stat
        self._sig, self._seen = object(), None

    def current(self):
        try:
            st = self.stat(self.path)
            sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            sig = None
        if sig != self._sig:
            seen = self.read()
            if seen is None:
                return None             # not cached: a file caught mid-write is read again next time
            self._sig, self._seen = sig, seen
        return self._seen
