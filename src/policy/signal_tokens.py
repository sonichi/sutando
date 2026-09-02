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

Registry state drives the capability flip:

* `unprovisioned` — no file, or a valid document that never held a row: the
  legacy global token is still accepted (an older daemon keeps working);
* `provisioned` — at least one row, live OR revoked: only room tokens are
  accepted. Revoking every row does NOT re-admit the global token — a fully
  revoked registry is a locked door, not a missing one;
* `invalid` — unreadable, wrong version, any malformed row, OR a registry that
  is missing/empty AFTER it was once provisioned: every scoped route fails
  closed until the engine rewrites the file.

Provisioning is irreversible. The first time a reader observes a row it writes
`<workspace>/state/signal-room-tokens.provisioned` (0600, atomic); from then
on — across restarts — a missing or emptied registry is a fault, never a
return to the legacy gate. `authorize()` is the one entry point: state and
match are computed from a single locked snapshot, so a file replaced between
two reads can never split the verdict.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
from pathlib import Path

REGISTRY_RELPATH = ("state", "signal-room-tokens.json")
MARKER_SUFFIX = ".provisioned"
SCOPE_ENQUEUE = "enqueue"
SCOPE_READ = "read"
SCOPES = frozenset({SCOPE_ENQUEUE, SCOPE_READ})
LEGACY_GLOBAL = "legacy_global"
STATE_UNPROVISIONED = "unprovisioned"
STATE_PROVISIONED = "provisioned"
STATE_INVALID = "invalid"
_HEX16 = re.compile(r"^[0-9a-f]{16}\Z")
_HEX64 = re.compile(r"^[0-9a-f]{64}\Z")


def registry_path(workspace) -> Path:
    return Path(workspace).joinpath(*REGISTRY_RELPATH)


def marker_path(workspace) -> Path:
    """`<workspace>/state/signal-room-tokens.provisioned` — written once, never removed."""
    return registry_path(workspace).with_suffix(MARKER_SUFFIX)


def token_digest(salt: str, token: str) -> str:
    return hashlib.sha256((salt + token).encode("utf-8")).hexdigest()


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_row(row) -> bool:
    """Every contract field, strictly: one bad row makes the whole file invalid."""
    if not isinstance(row, dict):
        return False
    return (isinstance(row.get("room_id"), str) and bool(row.get("room_id"))
            and row.get("scope") in SCOPES
            and isinstance(row.get("salt"), str) and bool(_HEX16.match(row["salt"]))
            and isinstance(row.get("sha256"), str) and bool(_HEX64.match(row["sha256"]))
            and _is_int(row.get("created_at"))
            and (row.get("revoked_at") is None or _is_int(row.get("revoked_at"))))


def _atomic_write(path: Path, payload: str, prefix: str) -> None:
    """0600 temp + fsync + rename, then fsync the directory — the engine's write shape."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=prefix, suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class TokenRegistry:
    """mtime-cached view of the registry file; safe under the threaded server."""

    def __init__(self, path, marker=None):
        self.path = Path(path)
        self.marker = self.path.with_suffix(MARKER_SUFFIX) if marker is None else Path(marker)
        self._lock = threading.Lock()
        self._stamp = None
        self._rows: list[dict] = []
        self._state = STATE_UNPROVISIONED
        self._reason = ""
        self._marked = False

    def _set_invalid(self, reason: str) -> None:
        # Unstamped so a rewrite is re-read at once; no row verifies meanwhile.
        self._stamp, self._rows, self._state, self._reason = None, [], STATE_INVALID, reason

    def _provisioned_before(self) -> bool:
        """Has any row ever been observed — in this process, or by the marker on disk?"""
        if self._marked:
            return True
        try:
            os.lstat(self.marker)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        self._marked = True
        return True

    def _mark_provisioned(self) -> None:
        """Ensure the marker exists — re-checked on every registry change, so it self-heals."""
        try:
            os.lstat(self.marker)
        except FileNotFoundError:
            _atomic_write(self.marker, "provisioned\n", ".signal-room-tokens.provisioned.")
        self._marked = True

    def _refresh(self) -> None:
        marked = self._provisioned_before()
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            if marked:
                self._set_invalid("registry missing after provisioning")
            else:
                self._stamp, self._rows, self._state, self._reason = None, [], STATE_UNPROVISIONED, ""
            return
        except OSError as exc:
            self._set_invalid(f"unreadable: {exc.__class__.__name__}")
            return
        if stamp == self._stamp:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._set_invalid(f"unparseable: {exc.__class__.__name__}")
            return
        if not isinstance(data, dict) or data.get("v") != 1:
            self._set_invalid("wrong document version")
            return
        rows = data.get("tokens")
        if not isinstance(rows, list):
            self._set_invalid("tokens is not a list")
            return
        for index, row in enumerate(rows):
            if not _valid_row(row):
                self._set_invalid(f"malformed row {index}")
                return
        if rows:
            try:
                self._mark_provisioned()
            except OSError as exc:
                self._set_invalid(f"provisioning marker unwritable: {exc.__class__.__name__}")
                return
        elif marked:
            self._set_invalid("registry emptied after provisioning")
            return
        self._rows = list(rows)
        self._state = STATE_PROVISIONED if rows else STATE_UNPROVISIONED
        self._reason = ""
        self._stamp = stamp

    def authorize(self, token) -> dict:
        """One locked snapshot -> {"state", "match": {room_id, scope} | None, "reason"}.

        The only read path: state and match come from the same parse of the
        same file, so a registry replaced between two calls cannot flip a verdict.
        `reason` says why the file is `invalid` (server log only; never token material).
        """
        with self._lock:
            self._refresh()
            match = None
            if isinstance(token, str) and token and self._state == STATE_PROVISIONED:
                for row in self._rows:
                    if row.get("revoked_at") is None and hmac.compare_digest(
                            token_digest(row["salt"], token), row["sha256"]):
                        match = {"room_id": row["room_id"], "scope": row["scope"]}
                        break
            return {"state": self._state, "match": match, "reason": self._reason}

    def state(self) -> str:
        return self.authorize(None)["state"]

    def invalid_reason(self) -> str:
        return self.authorize(None)["reason"]

    def active_rows(self) -> list[dict]:
        with self._lock:
            self._refresh()
            return [r for r in self._rows if r.get("revoked_at") is None]

    def verify(self, token: str) -> dict | None:
        """`{room_id, scope}` for a live token, else None (revoked rows never match)."""
        return self.authorize(token)["match"]


def write_registry(path, rows: list[dict]) -> None:
    """Atomic 0600 replace (temp + fsync + rename) — the engine's write shape."""
    _atomic_write(Path(path), json.dumps({"v": 1, "tokens": list(rows)}, sort_keys=True),
                  ".signal-room-tokens.")


def make_row(room_id: str, scope: str, token: str, *, created_at_ms: int,
             salt: str | None = None) -> dict:
    """One registry row for `token` (plaintext never stored)."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}")
    salt = salt or os.urandom(8).hex()
    return {"room_id": room_id, "scope": scope, "salt": salt,
            "sha256": token_digest(salt, token),
            "created_at": int(created_at_ms), "revoked_at": None}
