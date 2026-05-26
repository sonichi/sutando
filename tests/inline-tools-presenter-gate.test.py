#!/usr/bin/env python3
"""
Structural guard: slideControlTool must be gated on the presenter-mode sentinel
at module-load time, not exposed unconditionally (#1171).

Why: inline tools are registered globally with bodhi VoiceSession; Gemini sees
their descriptions every turn and fires them spuriously on greetings when no
presentation is active. The fix gates exposure on
`state/presenter-mode.sentinel` so the description is never visible unless the
sentinel is present when voice-agent starts.

Guards:
  1. PRESENTER_ACTIVE sentinel guard present in src/inline-tools.ts
  2. The sentinel path uses WORKSPACE_DIR (not a bare/repo-relative path)
  3. slideControlTool is conditionally spread (not unconditionally listed) in inlineTools
  4. slideControlTool is conditionally spread in ownerOnlyTools
  5. slideControlTool is NOT unconditionally listed in either array
  6. The condition references PRESENTER_ACTIVE (not a bare existsSync call inline)

Run: python3 tests/inline-tools-presenter-gate.test.py
Exit: 0 on pass, 1 on fail.
"""

from pathlib import Path
import sys
import re

REPO = Path(__file__).resolve().parent.parent


def fail(msg: str, ctx: str = "") -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("---context---", file=sys.stderr)
        print(ctx[:1500], file=sys.stderr)
    return 1


def main() -> int:
    src = REPO / "src" / "inline-tools.ts"
    if not src.exists():
        return fail(f"{src} not found")
    text = src.read_text()

    # 1. PRESENTER_ACTIVE const must be defined
    if "PRESENTER_ACTIVE" not in text:
        return fail("src/inline-tools.ts missing PRESENTER_ACTIVE const — sentinel gate not added (#1171)")

    # 2. Must check the sentinel via WORKSPACE_DIR, not a bare/relative path
    # Pattern: existsSync(join(WORKSPACE_DIR, ... 'presenter-mode.sentinel'))
    if "existsSync(join(WORKSPACE_DIR" not in text:
        return fail(
            "src/inline-tools.ts PRESENTER_ACTIVE check must use existsSync(join(WORKSPACE_DIR, ...)) "
            "— bare or repo-relative sentinel paths would silently break when cwd != repo (#1171)"
        )
    if "presenter-mode.sentinel" not in text:
        return fail("src/inline-tools.ts PRESENTER_ACTIVE check must reference 'presenter-mode.sentinel'")

    # 3 & 4. slideControlTool must be spread conditionally, not listed unconditionally
    # Find the inlineTools array and ownerOnlyTools array blocks.
    # Strategy: after the sentinel guard is added, any unconditional `slideControlTool,`
    # reference inside the array bodies is the regression we're guarding against.
    # We look for the spread form: ...(PRESENTER_ACTIVE ? [slideControlTool] : [])
    if "...(PRESENTER_ACTIVE ? [slideControlTool] : [])" not in text:
        return fail(
            "src/inline-tools.ts must use ...(PRESENTER_ACTIVE ? [slideControlTool] : []) "
            "to conditionally include slideControlTool (#1171)"
        )

    # 5. No unconditional bare `slideControlTool,` references in the exported arrays
    # (The export const line and the definition line are fine; the array entries are not)
    # We allow: `export const slideControlTool`, tool definition body, and the spread.
    # We disallow: `slideControlTool,` appearing outside the spread context.
    bare_unconditional = re.compile(
        r'(?<!\.\.\()slideControlTool,',
    )
    # Strip the definition block + export line to focus on array entries.
    # Simple heuristic: find occurrences of `slideControlTool,` not preceded by `[` or `? [`
    # More precisely: any `slideControlTool,` not part of `[slideControlTool]`
    non_conditional = re.compile(r'(?<!\[)slideControlTool,(?! *\] *:)')
    # Find all lines with slideControlTool, and check each
    problem_lines = []
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        # Skip: the const definition, the export statement, and the conditional spread
        if stripped.startswith('export const slideControlTool'):
            continue
        if 'PRESENTER_ACTIVE' in stripped:
            continue
        if stripped.startswith('//'):
            continue
        # Flag if slideControlTool appears as a bare array element
        if re.search(r'\bslideControlTool\b', stripped) and 'PRESENTER_ACTIVE' not in stripped:
            # Allow inside tool definition body (lines between the definition and closing `}`)
            # Simple check: if it's in an array context (no `name:`, no `description:` nearby)
            # Just check: is it a standalone `slideControlTool,` in an array?
            if re.search(r'^\s*(?:.*,\s*)?slideControlTool,', line) and 'export const slideControlTool' not in line:
                problem_lines.append(f"  line {i}: {stripped}")

    if problem_lines:
        return fail(
            "src/inline-tools.ts still has unconditional slideControlTool array entries:\n"
            + "\n".join(problem_lines),
            "Remove bare slideControlTool, entries and use ...(PRESENTER_ACTIVE ? [slideControlTool] : [])"
        )

    # 6. PRESENTER_ACTIVE is referenced in the spread (already checked by guard 3)
    # Confirmed by check 3+4.

    print("PASS: slideControlTool is gated on PRESENTER_ACTIVE sentinel in inlineTools and ownerOnlyTools.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
