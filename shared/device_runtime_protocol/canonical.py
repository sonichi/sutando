"""Sutando canonical JSON — the digest-input profile (S1.1 ruling).

Python's json.dumps(sort_keys=True) is test-stable inside one interpreter but
is NOT a cross-language security canonicalization. This profile is small,
strict, and documented so Swift/TypeScript/Kotlin runtimes can reproduce the
exact bytes:

  1. Values: object, array, string, integer, boolean ONLY.
  2. Floats are REJECTED in canonical space — integers or decimal STRINGS.
     (Number-formatting is where cross-language canonicalizations rot; we
     refuse the class instead of standardizing it.)
  3. Integers must satisfy |n| <= 2^53-1 (JSON interop bound).
  4. null is REJECTED: absent means absent. No null-vs-missing ambiguity.
  5. Object keys: strings only, unique, sorted by Unicode code point.
  6. Encoding: UTF-8, no ASCII escaping beyond JSON's mandatory set,
     separators "," and ":" with no whitespace.
  7. NaN/Infinity can never occur (floats are rejected wholesale).

Golden vectors in tests/device-runtime-protocol.test.py pin envelope →
canonical bytes → digest so any second implementation can self-verify.
"""

from __future__ import annotations

import hashlib
import json

_MAX_INT = 2**53 - 1


def _check(value, path: str) -> None:
    if value is None:
        raise ValueError(f"canonical JSON forbids null (absent means absent): {path}")
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        raise ValueError(
            f"canonical JSON forbids floats (use int or decimal string): {path}")
    if isinstance(value, int):
        if abs(value) > _MAX_INT:
            raise ValueError(f"integer exceeds 2^53-1 interop bound: {path}")
        return
    if isinstance(value, str):
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _check(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError(f"non-string object key at {path}: {k!r}")
            _check(v, f"{path}.{k}")
        return
    raise ValueError(f"type {type(value).__name__} not in canonical profile: {path}")


def canonical_json(value: dict) -> str:
    """Validate against the profile, then emit the canonical form. Absent
    fields must be OMITTED by the caller (this function rejects None)."""
    _check(value, "$")
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def canonical_digest(value: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def drop_absent(d: dict) -> dict:
    """Helper for envelope cores: remove keys whose value is None so the
    canonical form encodes absence by omission, per profile rule 4."""
    return {k: v for k, v in d.items() if v is not None}
