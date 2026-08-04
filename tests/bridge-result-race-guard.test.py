#!/usr/bin/env python3
"""
Regression test: all three bridges guard against the empty-file race
condition in their result_watcher loops.

Root cause (2026-06-04): when a result file is written via shell redirect
`> file`, the OS creates the file empty before the process writes to it.
The bridge's result_watcher polls every ~2s; if it fires during that window
it reads an empty string, skips the send (because `if clean_text:` is False
in _send_slack_msg), but still archives the file — permanently losing the
reply with no error logged.

Fix: after `reply_text = result_file.read_text().strip()`, each bridge must
check `if not reply_text: continue` BEFORE popping from pending_replies.
Popping is irreversible; archiving is irreversible. The guard ensures we
only advance past the read if the file actually has content.

Two layers, per bridge:
  1. Structural: the four source-shape checks below (cheap smoke check,
     catches an accidental revert or reordering at a glance).
  2. Behavioral: extracts the ACTUAL `reply_text = ...` through `continue`
     lines out of the real source (not a hand-copied reimplementation —
     same technique as tests/watch-tasks-stream-trap-exit.test.sh), wraps
     them in a one-iteration-loop harness, and exercises them against a
     real empty-then-filled result file on disk. Asserts the guard actually
     fires (pending_replies untouched, doesn't fall through) on the empty
     read, and actually clears (falls through) once the file has content.

Run manually:
    python3 tests/bridge-result-race-guard.test.py
"""

from __future__ import annotations

import asyncio
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from workspace_default import resolve_workspace  # noqa: E402

# The fix was applied to the runtime source (app bundle / workspace symlink).
# Prefer a live workspace copy so CI on a running instance sees the live fix;
# fall back to the OSS repo path for upstream portability checks. Workspace
# location is resolved through the canonical loader (never a hardcoded
# legacy install-path literal — see scripts/lint-workspace-resolution.sh and
# scripts/lint-sutando-home-path.sh for why that's banned tree-wide).
try:
    _WORKSPACE_SRC = resolve_workspace(migrate=False) / "src"
except Exception:
    _WORKSPACE_SRC = None
_REPO_SRC = REPO / "src"


def _bridge_path(name: str) -> Path:
    if _WORKSPACE_SRC is not None:
        ws = _WORKSPACE_SRC / f"{name}.py"
        if ws.exists():
            return ws.resolve()  # follow symlink to actual file
    return _REPO_SRC / f"{name}.py"


BRIDGES = {
    "slack-bridge": _bridge_path("slack-bridge"),
    "discord-bridge": _bridge_path("discord-bridge"),
    "telegram-bridge": _bridge_path("telegram-bridge"),
}


# ---------------------------------------------------------------------------
# Layer 1: structural checks (source-shape smoke check)
# ---------------------------------------------------------------------------

async def _async_noop(*a, **k):
    """Awaitable no-op: the guard may await its bookkeeping (#2631)."""
    return None


class StructuralGuardTest(unittest.TestCase):
    def _check(self, name: str, path: Path) -> None:
        self.assertTrue(path.exists(), f"{name}: file not found at {path}")
        src = path.read_text()

        self.assertIn(
            "read_text().strip()", src,
            f"{name}: missing `read_text().strip()` — race guard has no effect without the read",
        )

        read_pat = re.compile(r"reply_text\s*=\s*result_file\.read_text\(\)\.strip\(\)")
        read_match = read_pat.search(src)
        self.assertIsNotNone(
            read_match, f"{name}: `reply_text = result_file.read_text().strip()` not found",
        )

        after_read = src[read_match.end():]
        guard_pat = re.compile(r"if\s+not\s+reply_text\s*:")
        guard_match = guard_pat.search(after_read)
        self.assertIsNotNone(
            guard_match, f"{name}: missing `if not reply_text:` guard after read_text().strip()",
        )

        guard_block = after_read[guard_match.end(): guard_match.end() + 120]
        self.assertIn(
            "continue", guard_block,
            f"{name}: `if not reply_text:` guard exists but does not `continue` — fix has no effect",
        )

        pop_pat = re.compile(r"\.pop\(")
        pop_match = pop_pat.search(after_read)
        if pop_match is not None:
            self.assertLessEqual(
                guard_match.start(), pop_match.start(),
                f"{name}: `if not reply_text:` guard appears AFTER `.pop(` — pending_replies "
                f"is consumed before the guard fires; race condition is NOT fixed",
            )

    def test_slack_bridge_structural(self):
        self._check("slack-bridge", BRIDGES["slack-bridge"])

    def test_discord_bridge_structural(self):
        self._check("discord-bridge", BRIDGES["discord-bridge"])

    def test_telegram_bridge_structural(self):
        self._check("telegram-bridge", BRIDGES["telegram-bridge"])


# ---------------------------------------------------------------------------
# Layer 2: behavioral — extract the real lines, run them against a real file
# ---------------------------------------------------------------------------

def _extract_race_guard_snippet(src: str) -> tuple[str, int]:
    """Pull `reply_text = result_file.read_text().strip()` through the end of
    the `if not reply_text: continue` block, out of the REAL source — not a
    hand-copied reimplementation, so this breaks if the fix regresses.

    Returns (snippet, start_line) — start_line (1-indexed) lets the caller
    compile the snippet with its TRUE line numbers in the source file, so
    coverage.py attributes execution back to the real file/lines instead of
    an anonymous exec namespace."""
    read_pat = re.compile(r"^(\s*)reply_text\s*=\s*result_file\.read_text\(\)\.strip\(\)\s*$", re.MULTILINE)
    m = read_pat.search(src)
    assert m, "reply_text = result_file.read_text().strip() not found in source"
    start_line = src.count("\n", 0, m.start()) + 1
    indent = m.group(1)
    lines = src[m.start():].splitlines()
    # First line is the read; keep consuming until we've captured the
    # `if not reply_text:` + its `continue` body (next differently- or
    # equally-indented line after the guard's own indented body).
    out = [lines[0]]
    i = 1
    guard_seen = False
    guard_indent = None
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not guard_seen:
            out.append(line)
            if re.match(r"if\s+not\s+reply_text\s*:", stripped):
                guard_seen = True
                guard_indent = len(line) - len(line.lstrip())
            i += 1
            continue
        # Inside/after the guard: keep lines that are part of its indented
        # body (deeper than guard_indent); stop at the first line back at
        # guard_indent or shallower (that's code AFTER the guard block).
        this_indent = len(line) - len(line.lstrip()) if stripped else guard_indent + 4
        if stripped and this_indent <= guard_indent:
            break
        out.append(line)
        i += 1
    snippet = "\n".join(out) + "\n" + f"{indent}_REACHED_PAST_GUARD.append(True)\n"
    return snippet, start_line


class BehavioralRaceGuardTest(unittest.TestCase):
    """Executes the ACTUAL extracted guard lines from each bridge against a
    real empty-then-filled result file, asserting the guard fires (skips)
    on empty and clears (proceeds) once content lands — the literal race
    this PR fixes, not a proxy for it."""

    def _run_harness(self, name: str, path: Path, file_content: str) -> bool:
        """Returns True iff the extracted guard let execution reach past it
        (i.e. reply_text was truthy and no `continue` fired).

        Compiles the snippet with the REAL bridge file as its `filename` and
        padded so its lines land at their TRUE line numbers in that file —
        not an anonymous exec namespace — so coverage.py credits this run
        against the actual source lines (needed for the diff-coverage gate;
        an anonymous exec namespace is invisible to it)."""
        src = path.read_text()
        snippet, start_line = _extract_race_guard_snippet(src)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(file_content)
            result_path = f.name
        try:
            class _ResultFile:
                def __init__(self, p):
                    self._p = p

                def read_text(self):
                    with open(self._p) as fh:
                        return fh.read()

            ns: dict = {
                "_ResultFile": _ResultFile,
                "result_file": _ResultFile(result_path),
                "_REACHED_PAST_GUARD": [],
                # The guard now calls the bridge's `_note_empty_result` to bound
                # how long a present-but-empty result can stall silently (#2631).
                # This harness execs the extracted region in an isolated
                # namespace, so that name must be provided or the snippet raises
                # NameError before the guard is exercised at all. A no-op stub is
                # correct here: this file asserts the guard FIRES, and the
                # announce-once policy has its own suites
                # (`result-router-empty-result-bound`, behavioural coverage in
                # `discord-bridge-empty-result-wiring`). Stubbing bookkeeping does
                # not weaken the assertion this file exists to make.
                "_note_empty_result": _async_noop,
                "task_id": "task-harness",
            }
            # `for _tick in [0]:` sits on the line immediately before the
            # snippet's true start; blank-line padding before it keeps the
            # snippet's own lines aligned to their real file line numbers.
            # ASYNC-CAPABLE WRAPPER. The guard may `await` (discord's bound
            # calls an async handler that DMs the owner and drains the task —
            # #2631), and `await` inside a plain `for` block is a SyntaxError.
            # Wrapping in `async def` and driving it with asyncio.run keeps the
            # snippet's own lines at their true file line numbers, which is what
            # lets coverage.py credit this run to the real source.
            wrapped = (
                "\n" * (start_line - 3)
                + "async def _snippet():\n"
                + " for _tick in [0]:\n"
                + "\n".join("  " + l for l in snippet.splitlines())
                + "\n"
            )
            code = compile(wrapped, str(path), "exec")
            exec(code, ns)  # noqa: S102 — trusted, in-repo source only
            asyncio.run(ns["_snippet"]())
            return bool(ns["_REACHED_PAST_GUARD"])
        finally:
            Path(result_path).unlink(missing_ok=True)

    def _assert_race_guard_behavior(self, name: str) -> None:
        path = BRIDGES[name]
        # Empty file (the race window): guard must fire — must NOT reach
        # past it (no pop, no archive — retry next poll instead).
        reached_on_empty = self._run_harness(name, path, "")
        self.assertFalse(
            reached_on_empty,
            f"{name}: guard did NOT fire on an empty result file — the race is NOT fixed "
            f"(this would pop + archive the task with the reply permanently lost)",
        )
        # Whitespace-only file: same as empty after .strip() — must also guard.
        reached_on_whitespace = self._run_harness(name, path, "   \n\n")
        self.assertFalse(
            reached_on_whitespace,
            f"{name}: guard did NOT fire on a whitespace-only result file",
        )
        # Real content: guard must NOT fire — execution must reach past it
        # so the reply actually gets delivered.
        reached_on_content = self._run_harness(name, path, "Here's your answer.")
        self.assertTrue(
            reached_on_content,
            f"{name}: guard fired on a NON-empty result file — this would silently "
            f"drop every real reply, not just the race window",
        )

    def test_slack_bridge_behavioral(self):
        self._assert_race_guard_behavior("slack-bridge")

    def test_discord_bridge_behavioral(self):
        self._assert_race_guard_behavior("discord-bridge")

    def test_telegram_bridge_behavioral(self):
        self._assert_race_guard_behavior("telegram-bridge")


if __name__ == "__main__":
    unittest.main()
