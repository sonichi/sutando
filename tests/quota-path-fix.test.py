#!/usr/bin/env python3
"""Structural tests for quota-tracker read-quota path fix (PR #1055).

## Background

Before PR #1055, `read-quota.py` computed `_REPO_ROOT` with three `.parent`
calls from `__file__`. The path `skills/quota-tracker/scripts/read-quota.py`
is four levels deep when resolved through the `~/.claude/skills` symlink into
the repo root — three `.parent` calls landed on `<repo>/skills/`, which has no
`src/` subdirectory. This caused `from workspace_default import status_read_path`
to silently fail, so quota was always reported as missing regardless of whether
the credential proxy had written a fresh `quota-state.json`.

Fix: use exactly four `.parent` calls so `_REPO_ROOT / "src"` resolves to
`<repo>/src`, where `workspace_default.py` lives.

Also verifies:
  - No stale cwd / skill-dir fallback (the old shadow-file bug path)
  - `status_read_path("quota-state.json")` is called to find the canonical file
  - Graceful exit when quota file is absent (not a silent swallow)

Run: python3 tests/quota-path-fix.test.py
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
        print(ctx[:500], file=sys.stderr)


def expect(cond: bool, label: str, ctx: str = "") -> None:
    if not cond:
        fail(label, ctx)


def main() -> None:
    if not READ_QUOTA.exists():
        fail(f"file not found: {READ_QUOTA}")
        sys.exit(1)

    src = READ_QUOTA.read_text()

    # ── 1. Exactly four .parent calls for _REPO_ROOT ─────────────────────────
    # The pattern: Path(__file__).resolve().parent.parent.parent.parent
    four_parent = re.search(
        r"Path\(__file__\)\.resolve\(\)" + r"(?:\.parent){4}",
        src,
    )
    expect(
        four_parent is not None,
        "_REPO_ROOT must use exactly four .parent calls: "
        "Path(__file__).resolve().parent.parent.parent.parent",
    )

    # ── 2. Three .parent calls must NOT be the final count ────────────────────
    # Protect against regression: three .parent lands on skills/, not src/.
    three_parent_only = re.search(
        r"Path\(__file__\)\.resolve\(\)" + r"(?:\.parent){3}(?!\.parent)",
        src,
    )
    expect(
        three_parent_only is None,
        "_REPO_ROOT must NOT use only three .parent calls (lands on skills/ not repo root)",
        ctx=src[:400],
    )

    # ── 3. _REPO_ROOT is joined with "src" to locate workspace_default ───────
    expect(
        '_REPO_ROOT / "src"' in src or "_REPO_ROOT / 'src'" in src,
        '_REPO_ROOT must be joined with "src" to build the sys.path insert',
    )

    # ── 4. sys.path.insert used (not sys.path.append) ────────────────────────
    # insert(0, ...) ensures our src/ takes priority over any installed package.
    expect(
        "sys.path.insert(0," in src,
        "sys.path.insert(0, ...) must be used to add <repo>/src to path",
    )

    # ── 5. workspace_default imported via status_read_path ───────────────────
    expect(
        "from workspace_default import status_read_path" in src
        or "from workspace_default import" in src,
        "workspace_default must be imported (status_read_path) for canonical path resolution",
    )

    # ── 6. status_read_path("quota-state.json") called ───────────────────────
    expect(
        'status_read_path("quota-state.json")' in src
        or "status_read_path('quota-state.json')" in src,
        'status_read_path("quota-state.json") must be called to find the canonical quota file',
    )

    # ── 7. No bare cwd / script-relative fallback for quota file ─────────────
    # The old code fell back to cwd or skills/quota-tracker/ which caused
    # stale-file shadowing. Verify no such fallback remains.
    expect(
        "Path.cwd()" not in src and 'cwd()' not in src,
        "read-quota.py must NOT fall back to cwd to locate quota-state.json (stale-shadow bug)",
        ctx=src[:400],
    )

    # ── 8. No skills/ directory fallback as a Path construct ─────────────────
    # The comment explaining the old bug mentions "skills/quota-tracker/" — that's OK.
    # What must NOT exist is an actual Path construction pointing there for quota-state.json.
    import re as _re
    skill_path_construct = _re.search(
        r'Path\b.*skills.*quota.tracker|"skills".*"quota-tracker"|skills.*quota-tracker.*quota-state',
        src,
    )
    expect(
        skill_path_construct is None,
        "read-quota.py must NOT construct a skills/quota-tracker path for quota-state.json (stale-shadow bug)",
    )

    # ── 9. Graceful exit when quota file not found ────────────────────────────
    expect(
        "sys.exit(1)" in src,
        "read-quota.py must call sys.exit(1) when no quota-state.json found",
    )

    # ── 10. Informative message printed before exit ───────────────────────────
    expect(
        "not found" in src or "No quota-state.json" in src or "credential proxy" in src,
        "read-quota.py must print a helpful message when quota file is absent",
    )

    # ── 11. Human-readable output: remaining % shown ─────────────────────────
    expect(
        "remaining" in src,
        "read-quota.py human-readable output must show remaining %",
    )

    # ── 12. --json flag produces machine-readable output ─────────────────────
    expect(
        '"--json"' in src or "'--json'" in src,
        "read-quota.py must support --json flag for machine-readable output",
    )

    # ── 13. --gate flag for scripted quota gating ────────────────────────────
    expect(
        '"--gate"' in src or "'--gate'" in src,
        "read-quota.py must support --gate flag (exit 1 when quota exhausted)",
    )

    # Summary
    if _FAILURES:
        print(f"\n{len(_FAILURES)} test(s) FAILED:", file=sys.stderr)
        for f in _FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    else:
        total = 13
        print(f"All {total} tests passed.")


if __name__ == "__main__":
    main()
