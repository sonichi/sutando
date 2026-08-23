"""Composite (agent_id, instance_id) identity encoding — the ONE owner shared
by the durable registry (flat manifest filenames) and the live run dir (nested
socket/lock paths).

Both namespaces have to agree on what makes two instances distinct. When they
did not, a lossy sanitizer plus an unescaped `--` join let distinct tuples
resolve to the same manifest filename, and the run dir keyed on instance alone
meant two tuples the registry listed as separate could not run together.

The encoding is validated, injective and reversible:

  * every component is percent-escaped against a fixed safe set, so any byte
    outside it (including `%` itself) becomes `%XX` — no two distinct
    components can encode to the same string;
  * `DELIM` is deliberately OUTSIDE that safe set, so it can never occur
    inside an encoded component and the join is unambiguous;
  * `decode_key` inverts `instance_key` exactly, which is what makes the
    injectivity claim checkable rather than asserted.

The `default` instance still collapses to the bare encoded actor so pre-M2
registries keep their filenames; that stays injective because a collapsed key
contains no `DELIM` while every explicit-instance key contains exactly one.
"""
from __future__ import annotations

from urllib.parse import quote, unquote

# Outside _SAFE by construction: an encoded component can never contain it.
DELIM = "+"

_SAFE = ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
         "0123456789._@:-")

DEFAULT_INSTANCE = "default"
MAX_PART_LEN = 128


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


def encode_part(value: str | None, what: str = "identity component") -> str:
    """One validated, reversible component of a composite identity key."""
    return quote(validate_part(value, what), safe=_SAFE)


def instance_key(agent_id: str | None,
                 instance: str | None = None) -> str:
    """Flat durable key for the (agent_id, instance_id) tuple."""
    aid = encode_part(agent_id, "agent_id")
    inst = encode_part(instance or DEFAULT_INSTANCE, "instance_id")
    return aid if inst == encode_part(DEFAULT_INSTANCE) else f"{aid}{DELIM}{inst}"


def decode_key(key: str) -> tuple[str, str]:
    """Inverse of `instance_key`: the exact tuple that produced `key`."""
    head, sep, tail = key.partition(DELIM)
    return unquote(head), (unquote(tail) if sep else DEFAULT_INSTANCE)
