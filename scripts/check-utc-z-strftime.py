#!/usr/bin/env python3
"""
CI gate: catch local-time timestamps mislabeled as UTC.

The bug (issue #908 / PR #909): `time.strftime('%Y-%m-%dT%H:%M:%S')` with no
time-tuple argument formats *local time*, but callers append a literal `Z`
implying UTC. On a non-UTC host every such timestamp is wrong by the host's
UTC offset. The bug survived ~7 weeks because nothing validated the field.

No stock linter catches this — Ruff's DTZ rules cover `datetime`, not
`time.strftime`. This AST scan flags the exact pattern.

FLAGGED — a `time.strftime(<fmt>)` call that is BOTH:
  1. single-argument (no time tuple → always local time), AND
  2. mislabeled as UTC: either the format string itself contains a literal
     `Z`, or the call sits inside an f-string that also contains a literal
     `Z` (the `f"...{time.strftime('%Y-%m-%dT%H:%M:%S')}Z..."` pattern).

NOT flagged:
  - `time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())` — 2 args, genuine UTC.
  - `time.strftime('%H:%M:%S')` in a console log line — local time, no UTC claim.

Exit 1 on any hit, 0 otherwise. Run from the repo root.
"""

import ast
import sys
from pathlib import Path

SCAN_DIRS = ("src", "scripts", "skills")

# A format string must look like a full ISO datetime to be a candidate.
ISO_DATETIME = "%Y-%m-%dT%H:%M:%S"


def _is_time_strftime(call: ast.Call) -> bool:
    """True iff `call` is `time.strftime(...)`."""
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "strftime"
        and isinstance(func.value, ast.Name)
        and func.value.id == "time"
    )


def _single_arg_iso_strftime(node: ast.expr) -> "str | None":
    """If `node` is a single-arg `time.strftime` with an ISO-datetime format
    string, return that format string; else None."""
    if not (isinstance(node, ast.Call) and _is_time_strftime(node)):
        return None
    if len(node.args) != 1:  # a 2nd arg (e.g. time.gmtime()) = explicit + OK
        return None
    arg = node.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        if ISO_DATETIME in arg.value:
            return arg.value
    return None


def scan_file(path: Path) -> list[tuple[int, str]]:
    """Return [(lineno, source_line)] for each offending call in `path`."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []
    lines = source.splitlines()
    hits: list[tuple[int, str]] = []

    def record(node: ast.AST) -> None:
        ln = getattr(node, "lineno", 0)
        src = lines[ln - 1].strip() if 0 < ln <= len(lines) else ""
        hits.append((ln, src))

    for node in ast.walk(tree):
        # Case A: f-string containing both an ISO single-arg strftime AND a
        # literal `Z` — the `{...strftime(...)}Z` mislabel pattern.
        if isinstance(node, ast.JoinedStr):
            iso_calls = [
                v.value
                for v in node.values
                if isinstance(v, ast.FormattedValue)
                and _single_arg_iso_strftime(v.value) is not None
            ]
            has_z = any(
                isinstance(v, ast.Constant)
                and isinstance(v.value, str)
                and "Z" in v.value
                for v in node.values
            )
            if iso_calls and has_z:
                for c in iso_calls:
                    record(c)
            continue
        # Case B: standalone single-arg strftime whose format string itself
        # embeds a `Z` — claims UTC, computes local.
        fmt = _single_arg_iso_strftime(node) if isinstance(node, ast.Call) else None
        if fmt is not None and "Z" in fmt:
            record(node)

    return hits


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    self_path = Path(__file__).resolve()
    offenders: list[str] = []
    for d in SCAN_DIRS:
        base = repo_root / d
        if not base.is_dir():
            continue
        for py in sorted(base.rglob("*.py")):
            if py.resolve() == self_path:
                continue
            for ln, src in scan_file(py):
                offenders.append(f"{py.relative_to(repo_root)}:{ln}: {src}")

    if offenders:
        print("UTC-timestamp check FAILED — local-time strftime mislabeled as UTC:")
        for o in offenders:
            print(f"  {o}")
        print()
        print("Fix: pass time.gmtime() so the value is genuinely UTC, e.g.")
        print("  time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())")
        return 1

    print("UTC-timestamp check passed — no local-time strftime mislabeled as UTC.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
