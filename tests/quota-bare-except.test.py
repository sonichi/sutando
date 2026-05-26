#!/usr/bin/env python3
"""Structural test for the bare-except narrowing in read-quota.py (issue #1062).

## Problem (issue #1062)

`skills/quota-tracker/scripts/read-quota.py` had a bare `except Exception:`
around the workspace_default import:

    try:
        from workspace_default import status_read_path
        _canonical = status_read_path("quota-state.json")
    except Exception:           # ← too broad
        _canonical = None

When #1055 fixed the _REPO_ROOT path off-by-one, the original bug had been an
`ImportError` — "No module named 'workspace_default'". The broad except swallowed
it for ~12h, setting _canonical=None and emitting the misleading "No
quota-state.json found" fallback instead of surfacing the real error.

If status_read_path() raises any other exception (e.g. a future refactor adds an
unexpected error for a real config issue), we want it to propagate so it shows
in cron pass logs — not silently fall through to the same misleading message.

## Fix (this PR)

Narrowed to `except ImportError:` so only the "module not on sys.path" case is
suppressed; all other exceptions propagate.

Run: python3 tests/quota-bare-except.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
READ_QUOTA = REPO / "skills" / "quota-tracker" / "scripts" / "read-quota.py"

_FAILURES: list[str] = []


def fail(msg: str, ctx: str = "") -> None:
    _FAILURES.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("--- context ---", file=sys.stderr)
        print(ctx[:400], file=sys.stderr)


def expect(cond: bool, label: str, ctx: str = "") -> None:
    if not cond:
        fail(label, ctx)


def main() -> None:
    if not READ_QUOTA.exists():
        fail(f"file not found: {READ_QUOTA}")
        sys.exit(1)

    src = READ_QUOTA.read_text()
    lines = src.splitlines()

    # 1. No bare `except Exception:` around the workspace_default import block
    bare_except_lines = [
        ln.strip() for ln in lines
        if re.match(r"\s*except\s+Exception\s*:", ln)
    ]
    expect(
        len(bare_except_lines) == 0,
        f"read-quota.py: bare 'except Exception:' must be narrowed to 'except ImportError:' "
        f"(found {len(bare_except_lines)} site(s))",
        ctx="\n".join(bare_except_lines[:3]),
    )

    # 2. `except ImportError:` present — the correct narrow form
    import_error_lines = [
        ln.strip() for ln in lines
        if re.match(r"\s*except\s+ImportError\s*:", ln)
    ]
    expect(
        len(import_error_lines) >= 1,
        "read-quota.py: must have 'except ImportError:' to handle module-not-found case",
    )

    # 3. The import block structure is intact — try/except still wraps workspace_default import
    expect(
        "from workspace_default import status_read_path" in src,
        "read-quota.py: 'from workspace_default import status_read_path' must still be present",
    )

    # 4. _canonical = None fallback still present in except block
    expect(
        "_canonical = None" in src,
        "read-quota.py: '_canonical = None' fallback in except block must still be present",
    )

    # 5. No other broad except blocks introduced around status_read_path
    # (ensure the fix didn't accidentally add a new bare except elsewhere)
    broad_except_elsewhere = [
        ln.strip() for ln in lines
        if re.match(r"\s*except\s+Exception\s*:", ln)
        and "workspace_default" not in "\n".join(
            lines[max(0, lines.index(ln) - 5): lines.index(ln) + 2]
        )
    ]
    expect(
        len(broad_except_elsewhere) == 0,
        f"read-quota.py: no new bare 'except Exception:' blocks should be introduced "
        f"(found {len(broad_except_elsewhere)} site(s))",
        ctx="\n".join(broad_except_elsewhere[:3]),
    )

    # Summary
    if _FAILURES:
        print(f"\n{len(_FAILURES)} test(s) FAILED:", file=sys.stderr)
        for f in _FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    else:
        print("All 5 tests passed.")


if __name__ == "__main__":
    main()
