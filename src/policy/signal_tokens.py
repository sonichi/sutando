"""Per-room Signal Room token registry — the read side (R2).

The desktop engine mints one `enqueue` and one `read` token per hosted room,
writes their salted hashes here BEFORE it publishes the roster, and hands the
plaintext only to the daemon. agent-api never sees a plaintext except on the
wire: a presented bearer is hashed against every live row and the match yields
the token's `{room_id, scope}` — the authoritative room for room-bearing routes.

File: `<workspace>/state/signal-room-tokens.json`, 0600, replaced atomically::

    {"v": 1, "tokens": [{"room_id": "!room:hs", "scope": "enqueue"|"read",
                         "salt": "<16 hex>", "sha256": "<hex sha256(salt+token)>",
                         "created_at": <epoch ms>, "revoked_at": <epoch ms|null>}]}

Revocation sets `revoked_at`; rotation appends a new row, republishes the
roster, then revokes the old row. The file is re-read whenever its mtime/size
changes, so a rotation lands without restarting the gateway.

Capability flip: `has_active_rows()` is True once the file exists with at least
one non-revoked row. Only then may a route refuse the legacy global token — an
older daemon keeps working until the engine has actually provisioned rows.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import threading
from pathlib import Path

REGISTRY_RELPATH = ("state", "signal-room-tokens.json")
SCOPE_ENQUEUE = "enqueue"
SCOPE_READ = "read"
SCOPES = frozenset({SCOPE_ENQUEUE, SCOPE_READ})
LEGACY_GLOBAL = "legacy_global"


def registry_path(workspace) -> Path:
    return Path(workspace).joinpath(*REGISTRY_RELPATH)


def token_digest(salt: str, token: str) -> str:
    return hashlib.sha256((salt + token).encode("utf-8")).hexdigest()


def _valid_row(row) -> bool:
    if not isinstance(row, dict):
        return False
    return (isinstance(row.get("room_id"), str) and bool(row.get("room_id"))
            and row.get("scope") in SCOPES
            and isinstance(row.get("salt"), str)
            and isinstance(row.get("sha256"), str))


class TokenRegistry:
    """mtime-cached view of the registry file; safe under the threaded server."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp = None
        self._rows: list[dict] = []

    def _refresh(self) -> None:
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            self._stamp, self._rows = None, []
            return
        if stamp == self._stamp:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Fail CLOSED on a torn/corrupt file: no rows verify, no flip fires.
            self._stamp, self._rows = None, []
            return
        rows = data.get("tokens") if isinstance(data, dict) and data.get("v") == 1 else None
        self._rows = [r for r in (rows or []) if _valid_row(r)]
        self._stamp = stamp

    def active_rows(self) -> list[dict]:
        with self._lock:
            self._refresh()
            return [r for r in self._rows if r.get("revoked_at") is None]

    def has_active_rows(self) -> bool:
        return bool(self.active_rows())

    def verify(self, token: str) -> dict | None:
        """`{room_id, scope}` for a live token, else None (revoked rows never match)."""
        if not isinstance(token, str) or not token:
            return None
        for row in self.active_rows():
            if hmac.compare_digest(token_digest(row["salt"], token), row["sha256"]):
                return {"room_id": row["room_id"], "scope": row["scope"]}
        return None


def write_registry(path, rows: list[dict]) -> None:
    """Atomic 0600 replace (temp + fsync + rename) — the engine's write shape."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".signal-room-tokens.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"v": 1, "tokens": list(rows)}, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def make_row(room_id: str, scope: str, token: str, *, created_at_ms: int,
             salt: str | None = None) -> dict:
    """One registry row for `token` (plaintext never stored)."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}")
    salt = salt or os.urandom(8).hex()
    return {"room_id": room_id, "scope": scope, "salt": salt,
            "sha256": token_digest(salt, token),
            "created_at": int(created_at_ms), "revoked_at": None}
