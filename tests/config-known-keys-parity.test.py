#!/usr/bin/env python3
"""The three declarations of "which top-level config keys exist" must agree.

There are three, and nothing kept them in sync:
  1. `_KNOWN_TOP_LEVEL_KEYS` in `src/sutando_config.py`  (warn-only loader)
  2. `KNOWN_TOP_LEVEL_KEYS`  in `src/sutando_config.ts`  (warn-only loader)
  3. `properties`            in `docs/sutando-config.schema.json`

The schema declares `additionalProperties: false`, so a key known to the loaders
but absent from the schema HARD-FAILS validation for anyone whose editor honours
the schema — while the loaders happily accept it. That asymmetry has now shipped
twice: `stand` (#2486) and `migrate` (added by #1454, caught by Sutando-Pro's
review of #2486). Both were fixed as instances; this asserts the AXIS.

Deliberately fails LOUDLY if a declaration cannot be located, rather than
returning an empty set — an empty set would compare equal to another empty set
and the check would pass while measuring nothing.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Extraction FLOOR. Not an exact count — adding a key must not break this test.
# It exists because "the extractor silently narrowed" is the one degradation an
# equality check alone cannot catch reliably: three extractors that all failed the
# same way would still compare equal. Set well below the real count (7, as of
# `stand` + `migrate`) so it only fires on catastrophic narrowing, never on growth.
MIN_KEYS = 5


def _floor(name: str, keys: set) -> set:
    if len(keys) < MIN_KEYS:
        raise AssertionError(
            f"{name} yielded only {len(keys)} key(s) ({sorted(keys)}) — below the "
            f"MIN_KEYS={MIN_KEYS} extraction floor. The declaration moved or the "
            f"parse narrowed; fix the extractor rather than lowering the floor."
        )
    return keys


def python_keys() -> set[str]:
    """Import the real module — no regex over source we can execute."""
    sys.path.insert(0, str(REPO / "src"))
    from sutando_config import _KNOWN_TOP_LEVEL_KEYS  # noqa: E402

    keys = set(_KNOWN_TOP_LEVEL_KEYS)
    if not keys:
        raise AssertionError("python _KNOWN_TOP_LEVEL_KEYS is empty — refusing to compare nothing")
    return _floor("python _KNOWN_TOP_LEVEL_KEYS", keys)


def ts_keys() -> set[str]:
    """Scope the match to the declaration, and fail if its shape moved."""
    src = (REPO / "src" / "sutando_config.ts").read_text()
    m = re.search(r"KNOWN_TOP_LEVEL_KEYS[^=]*=\s*(?:new Set\()?\[(.*?)\]", src, re.S)
    if not m:
        raise AssertionError(
            "could not locate KNOWN_TOP_LEVEL_KEYS in src/sutando_config.ts — "
            "the declaration shape changed; update this test rather than deleting it"
        )
    keys = set(re.findall(r"['\"]([A-Za-z0-9_]+)['\"]", m.group(1)))
    if not keys:
        raise AssertionError("parsed an EMPTY key set from sutando_config.ts — parse is broken")
    return _floor("ts KNOWN_TOP_LEVEL_KEYS", keys)


def schema_keys() -> tuple[set[str], bool]:
    schema = json.loads((REPO / "docs" / "sutando-config.schema.json").read_text())
    props = set(schema.get("properties", {}))
    if not props:
        raise AssertionError("schema has no `properties` — refusing to compare nothing")
    return _floor("schema properties", props), schema.get("additionalProperties") is False


def main() -> int:
    py, ts = python_keys(), ts_keys()
    schema, closed = schema_keys()

    failures: list[str] = []
    if py != ts:
        failures.append(f"python vs ts differ: only-python={sorted(py - ts)} only-ts={sorted(ts - py)}")
    if py != schema:
        missing, extra = sorted(py - schema), sorted(schema - py)
        if missing:
            failures.append(
                f"keys accepted by the loaders but MISSING from the schema: {missing}"
                + (" — schema is additionalProperties:false, so setting these fails validation" if closed else "")
            )
        if extra:
            failures.append(f"keys in the schema the loaders do not know: {extra}")

    print(f"python ({len(py)}): {sorted(py)}")
    print(f"ts     ({len(ts)}): {sorted(ts)}")
    print(f"schema ({len(schema)}): {sorted(schema)}  additionalProperties:false={closed}")

    if failures:
        print("\nFAIL — the three key declarations disagree:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nPASS — all three declarations agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
