#!/usr/bin/env python3
"""PreToolUse: gate an Edit or Write to `MEMORY.md` on
`skills/proactive-loop/scripts/memory-index-budget.py`, for ANY caller — not
just proactive-loop's own step 7.5 checklist.

WHY THIS EXISTS. `memory-index-budget.py` answers a real, measured question:
would this addition to MEMORY.md silently push an entry already loading past
the session's read-budget cut? Step 7.5 chains it before an append — but that
chain only runs where a skill remembers to write it, and any Edit or Write
tool call that touches MEMORY.md (from any skill, any session, an ad hoc fix)
bypasses it entirely. This is the same architectural gap 3.45/9.5 had, and the
same fix: move enforcement from "a step I remember to chain" to the action
itself. Same pattern as gh-policy-gate.py (Bash + `gh`), here on
Edit/Write + a specific file path instead — `context-source-guard.py` already
proves file_path-based PreToolUse filtering works (live on this host, matched
on `Read`).

WHAT COUNTS AS "THE ADDITION". `memory-index-budget.py --adding TEXT` answers
"would TEXT, appended, drop something" — so TEXT must be the file's net
growth, not raw tool_input. Neither Edit nor Write has that as a single
field: an Edit's `new_string` is the whole replacement, not the delta over
`old_string` (sending it directly overstates the growth whenever
`old_string` is non-trivial), and a Write replaces the whole file with no
"addition" field at all. Both branches take the same shape: materialise the
post-edit text (Edit: `old_text.replace(old_string, new_string)`; Write: the
incoming `content` as-is), read what's currently on disk (BEFORE the write
happens, since after is too late), and diff — the lines present in the new
text but not the old are the addition. A Write to a file that does not exist
yet treats the whole `content` as the addition; an Edit whose pre-state can't
be read or located fails open (uncertain, not a positive finding). A
**shrink** (the diffed addition is empty, or an Edit's raw new_string is no
longer than old_string) is never refused — the guard exists to catch growth
pushing something out, not edits that remove or merely reorder text.

FAILS OPEN ON UNCERTAINTY, DENIES ONLY ON A POSITIVE FINDING. Same contract as
gh-policy-gate.py: an unresolvable path, an unreadable file, or an exit-2
"cannot answer" from the underlying script all allow the write. Only an
explicit exit-1 REFUSE denies.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HOOKS_DIR.parent
BUDGET_CHECK = _REPO_ROOT / "skills" / "proactive-loop" / "scripts" / "memory-index-budget.py"


def _is_memory_index(file_path: str) -> bool:
    """MEMORY.md specifically, not any memory/*.md file — the budget script's
    own index target, matched by basename so a symlink or relocated path still
    resolves."""
    if not file_path:
        return False
    return Path(file_path).name == "MEMORY.md"


def _new_lines(old_text: str, new_text: str) -> str:
    """Lines present in new_text but not old_text, in new_text's order — the
    heuristic 'addition' for a full-file Write. Order-preserving so a moved
    block (delete here, re-add there) is not double-counted as new."""
    old_lines = set(old_text.splitlines())
    added = [line for line in new_text.splitlines() if line not in old_lines]
    return "\n".join(added)


def _addition_for(tool_name: str, tool_input: dict) -> "str | None":
    """The text to pass as --adding, or None if this call is not a growth
    worth checking (a shrink, an unreadable pre-state, or a non-matching
    tool)."""
    file_path = tool_input.get("file_path") or ""
    if not _is_memory_index(file_path):
        return None

    if tool_name == "Edit":
        new_string = tool_input.get("new_string") or ""
        old_string = tool_input.get("old_string") or ""
        if not new_string or len(new_string) <= len(old_string):
            return None
        # Diff the materialised post-edit text against disk, same shape as
        # Write below — the raw new_string overstates growth (see PR body).
        try:
            old_text = Path(file_path).read_text(errors="ignore") if Path(file_path).is_file() else ""
        except OSError:
            return None
        if not old_text or old_string not in old_text:
            return None
        new_text = old_text.replace(old_string, new_string, 1)
        addition = _new_lines(old_text, new_text)
        return addition or None

    if tool_name == "Write":
        content = tool_input.get("content") or ""
        if not content:
            return None
        try:
            old_text = Path(file_path).read_text(errors="ignore") if Path(file_path).is_file() else ""
        except OSError:
            return None
        if not old_text:
            return content
        addition = _new_lines(old_text, content)
        return addition or None

    return None


def check_memory_write(tool_name: str, tool_input: dict):
    """Returns a deny reason string, or None to allow."""
    addition = _addition_for(tool_name, tool_input)
    if addition is None:
        return None
    file_path = tool_input.get("file_path") or ""
    try:
        r = subprocess.run(
            [sys.executable, str(BUDGET_CHECK), "--index", file_path, "--adding", addition],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        print(f"memory-index-guard: memory-index-budget.py did not run ({e}); "
              f"not enforcing", file=sys.stderr)
        return None
    if r.returncode == 1:
        return r.stdout.strip() or r.stderr.strip()
    if r.returncode not in (0, 1):
        print(f"memory-index-guard: memory-index-budget.py could not answer "
              f"(rc={r.returncode}); not enforcing", file=sys.stderr)
    return None


def main(argv):
    if os.environ.get("SUTANDO_ALLOW_UNGATED_MEMORY_WRITE") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    tool_name = payload.get("tool_name")
    if tool_name not in ("Edit", "Write"):
        return 0
    reason = check_memory_write(tool_name, payload.get("tool_input") or {})
    if not reason:
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"BLOCKED: {tool_name} to MEMORY.md refused by memory-index-budget.py — {reason} "
            f"Override once with SUTANDO_ALLOW_UNGATED_MEMORY_WRITE=1. [memory-index-guard]"),
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
