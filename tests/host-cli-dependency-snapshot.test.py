#!/usr/bin/env python3
"""Host-CLI dependency snapshot — issue #864.

Sutando reads and writes paths under `~/.claude/` (Claude Code's per-user
home). If Anthropic renames a directory, or a contributor hard-codes a new
`~/.claude/` path outside the canonical helper, this test fails before users
hit the drift.

## What is checked

1. **claudeHomePath helper correctness** — `claude_home_path()` must produce
   a Path inside `~/.claude/` (expanded). Verified for both the Python helper
   in `src/util_paths.py` and by asserting the TS/Python API surface is in
   sync (both helpers must exist).

2. **Hardcoded-path anti-pattern detection** — scan `src/` for non-comment
   code that constructs `~/.claude/` paths via `Path.home() / ".claude"` or
   similar hand-rolled patterns outside the canonical modules. New violations
   fail CI immediately. The KNOWN_VIOLATIONS allowlist documents existing debt
   to be migrated (each entry is a (file, reason) pair — shrink over time).

## What is NOT checked here

- Existence of actual `~/.claude/` paths at runtime — path layout is
  user/environment-specific and unsuitable for CI assertion.
- TypeScript static analysis — the Python scanner is authoritative for
  the Python layer; TS files are scanned for the same anti-pattern strings
  but not compiled.

Read-only static analysis + one Python unit call. Runs in <200ms.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC  = REPO / "src"


# ---------------------------------------------------------------------------
# 1. Helper-correctness check
# ---------------------------------------------------------------------------

def check_helper_correctness() -> list[str]:
    """Verify claude_home_path() produces a ~/.claude/... Path."""
    errs = []

    # Python helper must exist
    if not (SRC / "util_paths.py").exists():
        errs.append("src/util_paths.py is missing — claude_home_path() helper not found")
        return errs

    sys.path.insert(0, str(SRC))
    try:
        from util_paths import claude_home_path  # type: ignore[import]
    except ImportError as exc:
        errs.append(f"import claude_home_path from util_paths failed: {exc}")
        return errs

    expected_prefix = Path.home() / ".claude"
    result = claude_home_path("channels", "discord", "access.json")
    if not str(result).startswith(str(expected_prefix)):
        errs.append(
            f"claude_home_path('channels','discord','access.json') → {result!r}; "
            f"expected a path under {expected_prefix}"
        )

    # TS counterpart must exist
    if not (SRC / "util_paths.ts").exists():
        errs.append("src/util_paths.ts is missing — claudeHomePath() TS helper not found")
    else:
        ts_src = (SRC / "util_paths.ts").read_text()
        if "export function claudeHomePath" not in ts_src:
            errs.append(
                "src/util_paths.ts exists but exports no claudeHomePath() function"
            )

    return errs


# ---------------------------------------------------------------------------
# 2. Hardcoded-path anti-pattern detection
# ---------------------------------------------------------------------------

# Pattern: Path.home() / ".claude" or homedir() / ".claude" (Python)
# or Path(home) / ".claude" or join(homedir(), ".claude") (TS/JS)
PY_RAW_RE = re.compile(r'Path\.home\(\)\s*/\s*["\']\.claude["\']')
TS_RAW_RE = re.compile(r'(?:homedir\(\)|Path\.home\(\))\s*/\s*["\']\.claude["\']|'
                       r'join\([^)]*homedir\(\)[^)]*,\s*["\']\.claude["\']')

# Allowlist — known existing violations that use Path.home() directly.
# Format: (relative_path_str, short_reason).
# These MUST be migrated to claude_home_path() / claudeHomePath() in follow-up PRs.
# Removing a file from this list is always safe (it means the migration landed).
KNOWN_VIOLATIONS: list[tuple[str, str]] = [
    ("src/health-check.py",    "uses Path.home()/'.claude' × 2 — migrate to claude_home_path() (tracked in #864)"),
    ("src/discord-bridge.py",  "uses Path.home()/'.claude' for channels .env + ACCESS_FILE — migrate (#864)"),
    ("src/slack-bridge.py",    "uses Path.home()/'.claude' for ACCESS_FILE — migrate (#864)"),
    ("src/telegram-bridge.py", "uses Path.home()/'.claude' × 2 — migrate (#864)"),
    ("src/dm-result.py",       "uses Path.home()/'.claude' × 2 — migrate (#864)"),
]
KNOWN_VIOLATION_PATHS = {v[0] for v in KNOWN_VIOLATIONS}


def _non_comment_lines_py(path: Path) -> list[tuple[int, str]]:
    lines = []
    for i, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
        if not raw.lstrip().startswith("#"):
            lines.append((i, raw))
    return lines


def _non_comment_lines_ts(path: Path) -> list[tuple[int, str]]:
    lines = []
    for i, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
        stripped = raw.lstrip()
        if not (stripped.startswith("//") or stripped.startswith("*")):
            lines.append((i, raw))
    return lines


def check_hardcoded_paths() -> list[str]:
    failures = []

    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(REPO))
        if rel in KNOWN_VIOLATION_PATHS:
            continue
        if path.name in ("util_paths.py",):
            continue
        for lineno, line in _non_comment_lines_py(path):
            if PY_RAW_RE.search(line):
                failures.append(
                    f"{rel}:{lineno}: hardcoded Path.home()/'.claude' — "
                    "use claude_home_path() from src/util_paths.py instead"
                )

    for path in sorted(SRC.rglob("*.ts")) + sorted(SRC.rglob("*.tsx")):
        rel = str(path.relative_to(REPO))
        if rel in KNOWN_VIOLATION_PATHS:
            continue
        if path.name in ("util_paths.ts",):
            continue
        for lineno, line in _non_comment_lines_ts(path):
            if TS_RAW_RE.search(line):
                failures.append(
                    f"{rel}:{lineno}: hardcoded homedir()/'.claude' — "
                    "use claudeHomePath() from src/util_paths.ts instead"
                )

    return failures


def check_allowlist_integrity() -> list[str]:
    """Verify each allowlisted file still exists and still needs the allowlist."""
    errs = []
    for rel, _ in KNOWN_VIOLATIONS:
        p = REPO / rel
        if not p.exists():
            errs.append(
                f"KNOWN_VIOLATIONS entry '{rel}' does not exist on disk — "
                "remove it from the allowlist"
            )
    return errs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    all_failures: list[str] = []

    # Allowlist integrity must pass before the other checks are meaningful.
    integrity = check_allowlist_integrity()
    if integrity:
        print("ALLOWLIST INTEGRITY FAILURES (clean up KNOWN_VIOLATIONS):")
        for e in integrity:
            print(f"  {e}")
        sys.exit(1)

    helper_errs = check_helper_correctness()
    hardcoded_errs = check_hardcoded_paths()
    all_failures = helper_errs + hardcoded_errs

    if all_failures:
        print(f"host-cli-dependency-snapshot: {len(all_failures)} violation(s):\n")
        for f in all_failures:
            print(f"  {f}")
        print(
            "\nFix: replace Path.home()/'.claude'/... with "
            "claude_home_path(...) from src/util_paths.py (Python) or "
            "claudeHomePath(...) from src/util_paths.ts (TypeScript).\n"
            "If this is an existing file being migrated, remove it from "
            "KNOWN_VIOLATIONS in tests/host-cli-dependency-snapshot.test.py."
        )
        sys.exit(1)

    py_count = len(list(SRC.rglob("*.py")))
    ts_count = len(list(SRC.rglob("*.ts"))) + len(list(SRC.rglob("*.tsx")))
    print(
        f"host-cli-dependency-snapshot: OK — {ts_count} TS files, {py_count} Python files, "
        f"0 new violations ({len(KNOWN_VIOLATIONS)} known allowlisted)"
    )
