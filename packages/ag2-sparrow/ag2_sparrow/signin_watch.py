"""Which account a local CLI is signed in with, read from its own sign-in file, so its supervisor can
restart it after an account switch (a running CLI keeps the session it started with: Codex then fails
with "refresh token revoked", Claude Code stays on the old account).

Only non-secret identity fields are read and kept: Codex's `auth.json` account id and the email claim of
its id token, Claude Code's `.claude.json` `oauthAccount` uuid and email. No token is returned, kept or
logged; what a supervisor records is `fingerprint()`, a short hash of the identity.

The desktop's account switch can change only the Keychain item (Claude Code's `.claude.json` then keeps
the old email), so on every switch it also writes `.ag2-signin-changed` in the CLI's config dir
(CODEX_HOME, CLAUDE_CONFIG_DIR) as JSON `{"email": "<new email>", "at": <epoch seconds>}`. Exactly those
two fields are read; a change in either is an identity change. A missing or malformed marker is no
change: `SigninWatch` keeps the last marker it read.

An identity is `(account, email, marker)`; account and email are `SIGNED_OUT` when the file is absent or
names no account; the whole identity is None when the sign-in file could not be read (refused,
mid-write, not JSON), which never counts as a change. Stdlib.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os

from .bounded_file_read import read_regular

SIGNED_OUT = ("", "")
CODEX_AUTH = "auth.json"
MARKER = ".ag2-signin-changed"
MARKER_MAX = 4096
CLAUDE_CONFIG_MAX = 32 << 20     # `.claude.json` holds every project's history; still a bounded read


def fingerprint(identity) -> str:
    """A short, stable hash of an identity: what is recorded and compared, never the fields themselves."""
    fields = [f or "" for f in identity]
    fields[1] = fields[1].lower()
    return hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()[:16]


def marker(config_dir: str):
    """The desktop's switch marker in `config_dir`, `{"email": str, "at": number}`, as '<email>|<at>';
    None when it is missing or malformed (no change)."""
    text, _ = read_regular(os.path.join(config_dir, MARKER), MARKER_MAX)
    try:
        data = json.loads(text) if text else None
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    email, at = data.get("email"), data.get("at")
    if not isinstance(email, str) or not email or isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    return "%s|%r" % (email.strip().lower(), at)


def _with_marker(identity, config_dir: str):
    return None if identity is None else tuple(identity) + (marker(config_dir),)


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
    """Codex's account from `<CODEX_HOME>/auth.json` (its ChatGPT account id and email; an API-key login
    reads as ('api-key', '') without the key being looked at) and the switch marker beside it."""
    return _with_marker(_codex_account(home), home)


def _codex_account(home: str):
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


def claude_config_dir(claude_config_dir: str = "", home: str = "") -> str:
    """Where the desktop writes Claude Code's switch marker: CLAUDE_CONFIG_DIR, else the CLI's default."""
    return claude_config_dir or os.path.join(home or os.path.expanduser("~"), ".claude")


def claude_identity(config_file: str, config_dir: str):
    """Claude Code's account from its global config (`.claude.json`: `oauthAccount`'s uuid and email) and
    the switch marker in its config dir. The credential itself is in the Keychain, never read here."""
    return _with_marker(_claude_account(config_file), config_dir)


def _claude_account(config_file: str):
    data = _load(config_file, CLAUDE_CONFIG_MAX)
    if data is None:
        return None
    acct = data.get("oauthAccount")
    if not isinstance(acct, dict):
        return SIGNED_OUT
    account, email = acct.get("accountUuid"), acct.get("emailAddress")
    return (account if isinstance(account, str) else "", email if isinstance(email, str) else "")


class SigninWatch:
    """A sign-in file and the switch marker, re-read only when one's stat changes (`.claude.json` is
    rewritten often and can be large). `current()` is the identity now on disk, None when unreadable."""

    def __init__(self, paths, read, stat=os.stat):
        self.paths = [paths] if isinstance(paths, str) else list(paths)
        self.read, self.stat = read, stat
        self._sig, self._seen, self._marker = object(), None, ""

    def _stat(self, path):
        try:
            st = self.stat(path)
            return st.st_mtime_ns, st.st_size, st.st_ino
        except OSError:
            return None

    def current(self):
        sig = tuple(self._stat(p) for p in self.paths)
        if sig != self._sig:
            seen = self.read()
            if seen is None:
                return None             # not cached: a file caught mid-write is read again next time
            if len(seen) > 2:           # a missing or malformed marker keeps the last one read
                if seen[2] is None:
                    seen = tuple(seen[:2]) + (self._marker,)
                self._marker = seen[2]
            self._sig, self._seen = sig, seen
        return self._seen
