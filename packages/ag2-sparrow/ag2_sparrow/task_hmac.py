"""The task-envelope HMAC stamp, keyed by a file path: what the agent's own bridge signs each task with and
what codex-local checks, byte-compatible with Sutando's src/task_envelope.py and claude-local's verifier.

A stamp is `envelope_hmac: v1:<hex>` in the canonical header slot (line 0, or line 1 after `id:`): HMAC-SHA256
over the whole file without that line, under a 32-byte key kept as 64 hex characters in a 0600 file. Only a
`verified` verdict is proof; `unsigned`, `invalid` and `unverifiable` all fail a strict check. Stdlib only.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

STAMP_PREFIX = "envelope_hmac: v1:"
VERIFIED, UNSIGNED, INVALID, UNVERIFIABLE = "verified", "unsigned", "invalid", "unverifiable"
_MAC_CHARS = frozenset("0123456789abcdef")


def parse_key(text: str) -> bytes:
    """Exactly 64 hex characters, or ValueError: bytes.fromhex('') would key every stamp with nothing."""
    text = text.strip()
    key = bytes.fromhex(text)
    if len(text) != 64 or len(key) != 32:
        raise ValueError(f"a task-hmac key is 64 hex characters, not {len(text)}")
    return key


def load_key(path) -> bytes:
    """The key at `path`; OSError or ValueError when it is missing or not a key. Never creates one."""
    return parse_key(Path(path).read_text(encoding="utf-8"))


def create_key(path) -> bool:
    """Write a fresh key at `path`, 0600 from the open, unless one is already there: True when this call wrote it.
    A file at `path` that is not a key is left alone and reported (ValueError)."""
    p = Path(path)
    if p.exists() or p.is_symlink():
        load_key(p)
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(secrets.token_hex(32))
        try:
            os.link(tmp, p)      # never replaces a key another writer put there first
        except FileExistsError:
            load_key(p)
            return False
    finally:
        tmp.unlink(missing_ok=True)
    return True


def _strip_stamp(text: str) -> "tuple[str, str | None]":
    lines = text.split("\n")
    for i in (0, 1):
        if i < len(lines) and lines[i].startswith(STAMP_PREFIX) and (i == 0 or lines[0].startswith("id:")):
            return "\n".join(lines[:i] + lines[i + 1:]), lines[i][len(STAMP_PREFIX):].strip()
    return text, None


def _mac(text: str, key: bytes) -> str:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def stamp_text(text: str, key: bytes) -> str:
    """`text` with a fresh stamp after its `id:` line (or first); an existing stamp is replaced, never doubled."""
    body, _old = _strip_stamp(text)
    lines = body.split("\n")
    lines.insert(1 if lines and lines[0].startswith("id:") else 0, STAMP_PREFIX + _mac(body, key))
    return "\n".join(lines)


def verify_text(text: str, key_path) -> str:
    """verified | unsigned | invalid | unverifiable (no usable key at `key_path`, read now)."""
    body, mac = _strip_stamp(text)
    if mac is None:
        return UNSIGNED
    try:
        key = load_key(key_path)
    except (OSError, ValueError):
        return UNVERIFIABLE
    if not is_mac_shape(mac):
        return INVALID
    return VERIFIED if hmac.compare_digest(mac, _mac(body, key)) else INVALID


def is_mac_shape(mac: str) -> bool:
    """Exactly what the signer emits (64 lowercase hex): compare_digest raises TypeError on non-ASCII str."""
    return len(mac) == 64 and all(c in _MAC_CHARS for c in mac)


def stamper(key_path):
    """The bridge's task stamper for the key at `key_path`, read once now: OSError or ValueError when unusable."""
    key = load_key(key_path)
    return lambda text: stamp_text(text, key)


KEY_VAR = "AG2_HMAC_KEY_FILE"


def install_bridge_stamper(environ) -> str:
    """With AG2_HMAC_KEY_FILE (a key's path, set by a -local install that signs) in `environ`: every task file
    the bridge writes from now on is stamped with that key. '' or why not; with no such path, nothing changes
    (a host that injects its own stamper sets none)."""
    path = environ.get(KEY_VAR, "")
    if not path:
        return ""
    from .local_task_protocol import set_task_stamper
    try:
        set_task_stamper(stamper(path))
    except (OSError, ValueError) as e:
        return f"{KEY_VAR} names no usable key ({type(e).__name__}); tasks would go out unsigned"
    return ""
