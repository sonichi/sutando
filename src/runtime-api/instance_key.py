"""Composite (agent_id, instance_id) identity encoding — the ONE owner shared
by the durable registry (flat manifest filenames) and the live run dir (the
directory holding this instance's socket and lock).

Both namespaces have to agree on what makes two instances distinct, on the
filesystems Sutando actually ships on. Three properties are load-bearing:

  * INJECTIVE — every component is percent-escaped against a fixed safe set,
    so any byte outside it (including `%` itself) becomes `%XX`; `DELIM` is
    outside that set, so the join is unambiguous and `decode_key` inverts
    `instance_key` exactly.
  * CASE-FOLD SAFE — the safe set contains NO uppercase ASCII, so `Blue` and
    `blue` encode to `%42lue` and `blue`. macOS (and Windows) default to
    case-insensitive filesystems, where the previous encoding let two accepted
    sibling instances silently become ONE manifest and ONE socket/lock dir.
    `urllib.parse.quote` cannot express this: its `safe=` only ADDS to an
    always-safe set that already contains A-Z, so the escaping is done here.
  * BOUNDED — a legal 128-character component can escape to 384+ bytes, past
    NAME_MAX and far past the AF_UNIX sun_path cap, which surfaced as
    ENAMETOOLONG at manifest and run-dir creation. Keys longer than
    MAX_KEY_BYTES collapse to `<head>~<digest>` over the whole exact key:
    bounded, deterministic on both ends of the connection, and still
    distinguishing. A bounded key is NOT reversible, and `decode_key` says so
    instead of guessing.

The `default` instance still collapses to the bare encoded actor so pre-M2
registries keep their filenames; that stays injective because a collapsed key
contains no `DELIM` while every explicit-instance key contains exactly one.
"""
from __future__ import annotations

import hashlib
from urllib.parse import unquote

# Both outside _SAFE by construction: an encoded component can contain neither.
DELIM = "+"
TRUNC = "~"

_SAFE = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._@:-")

DEFAULT_INSTANCE = "default"
MAX_PART_LEN = 128
MAX_KEY_BYTES = 64
_DIGEST_BYTES = 8


def validate_part(value: str | None, what: str) -> str:
    """Reject the forms no escaping can make safe, and return the raw value.
    Everything else is escaped rather than rejected, so a legal id never
    fails to register."""
    v = "" if value is None else str(value)
    if not v:
        raise ValueError(f"{what} is required")
    if v in (".", ".."):
        raise ValueError(f"{what} must not be {v!r}")
    if "\x00" in v:
        raise ValueError(f"{what} must not contain a NUL byte")
    if len(v) > MAX_PART_LEN:
        raise ValueError(f"{what} must be at most {MAX_PART_LEN} characters")
    return v


def _escape(value: str) -> str:
    out = []
    for byte in value.encode("utf-8"):
        ch = chr(byte)
        out.append(ch if ch in _SAFE else f"%{byte:02X}")
    return "".join(out)


def encode_part(value: str | None, what: str = "identity component") -> str:
    """One validated, reversible, case-fold-safe component of a composite key."""
    return _escape(validate_part(value, what))


def bound(name: str, max_bytes: int) -> str:
    """`name` when it fits, else a deterministic `<head>~<digest>` of the exact
    same input. TRUNC never occurs in an escaped component, so a bounded name
    is always recognizable as one."""
    raw = name.encode("utf-8")
    if len(raw) <= max_bytes:
        return name
    digest = hashlib.blake2b(raw, digest_size=_DIGEST_BYTES).hexdigest()
    keep = max_bytes - len(digest) - len(TRUNC)
    if keep < 0:
        raise ValueError(f"max_bytes must be at least {len(digest) + len(TRUNC)}")
    # Escaped output is pure ASCII, so a byte slice can never split a character.
    return f"{raw[:keep].decode('ascii')}{TRUNC}{digest}"


def instance_key(agent_id: str | None,
                 instance: str | None = None) -> str:
    """Bounded flat durable key for the (agent_id, instance_id) tuple."""
    aid = encode_part(agent_id, "agent_id")
    inst = encode_part(instance or DEFAULT_INSTANCE, "instance_id")
    key = aid if inst == encode_part(DEFAULT_INSTANCE) else f"{aid}{DELIM}{inst}"
    return bound(key, MAX_KEY_BYTES)


def decode_key(key: str) -> tuple[str, str]:
    """Inverse of `instance_key`: the exact tuple that produced `key`. A
    length-bounded key has no inverse — that raises rather than returning a
    plausible-looking wrong tuple."""
    if TRUNC in key:
        raise ValueError(f"{key!r} is a length-bounded key — not reversible")
    head, sep, tail = key.partition(DELIM)
    return unquote(head), (unquote(tail) if sep else DEFAULT_INSTANCE)
