#!/usr/bin/env python3
"""Structural test for quota-tracker predictive burn-rate (issue #1087).

## What this checks

`skills/quota-tracker/scripts/read-quota.py` gains EWMA burn-rate prediction:
- History file at <workspace>/state/quota-burn-history.json
- EWMA alpha=0.3, samples capped at 60 entries
- New output keys: burn_rate_pct_per_min, estimated_minutes_to_exhaustion,
  estimated_passes_remaining
- Human-readable: "Burn rate: X.XXX%/min  (~N passes left)"
- Positive-only delta filter + 30-min gap guard prevent resets skewing the rate

Run: python3 tests/quota-burn-rate.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import ast
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

    # 1. resolve_workspace imported alongside status_read_path
    expect(
        "resolve_workspace" in src,
        "read-quota.py: must import resolve_workspace from workspace_default",
    )

    # 2. _BURN_HISTORY_FILE defined using _workspace
    expect(
        "_BURN_HISTORY_FILE" in src,
        "read-quota.py: _BURN_HISTORY_FILE variable must be defined",
    )
    expect(
        "_workspace" in src,
        "read-quota.py: _workspace variable must be resolved from resolve_workspace()",
    )

    # 3. EWMA alpha constant present
    ewma_lines = [ln for ln in lines if re.search(r"_EWMA_ALPHA\s*=\s*0\.\d+", ln)]
    expect(
        len(ewma_lines) >= 1,
        "read-quota.py: _EWMA_ALPHA constant must be defined",
    )

    # 4. History capped (_MAX_HISTORY or similar slicing)
    expect(
        "_MAX_HISTORY" in src,
        "read-quota.py: _MAX_HISTORY cap must be defined to bound history size",
    )

    # 5. Positive-delta guard present (ignore resets/decreases)
    expect(
        "delta_pct > 0" in src,
        "read-quota.py: positive-delta guard (delta_pct > 0) must be present to filter resets",
    )

    # 6. Gap guard present (ignore long pauses to prevent rate-spike)
    expect(
        "dt_min < 30" in src,
        "read-quota.py: gap guard (dt_min < 30) must be present to ignore long pauses",
    )

    # 7. burn_rate_pct_per_min in result dict
    expect(
        "burn_rate_pct_per_min" in src,
        "read-quota.py: 'burn_rate_pct_per_min' key must appear in output",
    )

    # 8. estimated_minutes_to_exhaustion in result dict
    expect(
        "estimated_minutes_to_exhaustion" in src,
        "read-quota.py: 'estimated_minutes_to_exhaustion' key must appear in output",
    )

    # 9. estimated_passes_remaining in result dict
    expect(
        "estimated_passes_remaining" in src,
        "read-quota.py: 'estimated_passes_remaining' key must appear in output",
    )

    # 10. Human-readable output includes "Burn rate:"
    expect(
        "Burn rate:" in src,
        "read-quota.py: human-readable output must include 'Burn rate:' line",
    )

    # 11. passes left shown in human-readable output
    expect(
        "passes left" in src,
        "read-quota.py: human-readable output must include 'passes left' forecast",
    )

    # 12. History file path lives in state/ subdir
    expect(
        '"state"' in src or "'state'" in src or 'state/quota-burn-history.json' in src,
        "read-quota.py: burn history file must be under state/ directory",
    )

    # 13. _load_history guards against missing file and JSON decode errors
    expect(
        "_load_history" in src,
        "read-quota.py: _load_history() function must be defined",
    )
    expect(
        "json.JSONDecodeError" in src or "JSONDecodeError" in src,
        "read-quota.py: _load_history must guard against JSONDecodeError",
    )

    # 14. _save_history uses mkdir(parents=True) to create state/ if needed
    expect(
        "_save_history" in src,
        "read-quota.py: _save_history() function must be defined",
    )
    expect(
        "mkdir" in src,
        "read-quota.py: _save_history must mkdir parents before writing",
    )

    # 15. Still has except ImportError: (from #1165 fix — carried forward)
    import_error_lines = [
        ln.strip() for ln in lines
        if re.match(r"\s*except\s+ImportError\s*:", ln)
    ]
    expect(
        len(import_error_lines) >= 1,
        "read-quota.py: must retain 'except ImportError:' from #1165 (no bare except Exception:)",
    )

    # 16. No bare except Exception: reintroduced
    bare_except_lines = [
        ln.strip() for ln in lines
        if re.match(r"\s*except\s+Exception\s*:", ln)
    ]
    expect(
        len(bare_except_lines) == 0,
        f"read-quota.py: bare 'except Exception:' must not appear "
        f"(found {len(bare_except_lines)} site(s))",
        ctx="\n".join(bare_except_lines[:3]),
    )

    # 17. File is valid Python (syntax check)
    try:
        ast.parse(src)
    except SyntaxError as exc:
        fail(f"read-quota.py: syntax error — {exc}")

    # Summary
    if _FAILURES:
        print(f"\n{len(_FAILURES)} test(s) FAILED:", file=sys.stderr)
        for f in _FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    else:
        print("All 17 tests passed.")


if __name__ == "__main__":
    main()
