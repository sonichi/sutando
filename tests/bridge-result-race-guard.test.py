#!/usr/bin/env python3
"""
Structural regression test: all three bridges guard against the empty-file
race condition in their result_watcher loops.

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

Guards (each bridge):
  1. `read_text().strip()` call exists (the file read).
  2. `if not reply_text` guard exists immediately after.
  3. The guard uses `continue` (skip this cycle, retry next poll).
  4. The guard appears BEFORE `pop(` — confirms pending_replies is NOT
     consumed on an empty read.

Run manually:
    python3 tests/bridge-result-race-guard.test.py
"""

from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parent.parent

# The fix was applied to the runtime source (app bundle / workspace symlink).
# Prefer the workspace path so CI on the running instance sees the live fix;
# fall back to the OSS repo path for upstream portability checks.
_WORKSPACE_SRC = Path.home() / ".sutando" / "workspace" / "src"
_REPO_SRC = REPO / "src"


def _bridge_path(name: str) -> Path:
    ws = _WORKSPACE_SRC / f"{name}.py"
    if ws.exists():
        return ws.resolve()  # follow symlink to actual file
    return _REPO_SRC / f"{name}.py"


BRIDGES = {
    "slack-bridge": _bridge_path("slack-bridge"),
    "discord-bridge": _bridge_path("discord-bridge"),
    "telegram-bridge": _bridge_path("telegram-bridge"),
}

errors = 0


def fail(msg: str, context: str = "") -> None:
    global errors
    print(f"FAIL: {msg}", file=sys.stderr)
    if context:
        print("  context:", context[:300], file=sys.stderr)
    errors += 1


def check_bridge(name: str, path: Path) -> None:
    if not path.exists():
        fail(f"{name}: file not found at {path}")
        return

    src = path.read_text()

    # 1. read_text().strip() call exists
    if "read_text().strip()" not in src:
        fail(f"{name}: missing `read_text().strip()` — race guard has no effect without the read")
        return

    # Locate the result_watcher / poll section that reads result files.
    # Find the first occurrence of read_text().strip() assigned to reply_text.
    read_pat = re.compile(r"reply_text\s*=\s*result_file\.read_text\(\)\.strip\(\)")
    read_match = read_pat.search(src)
    if not read_match:
        fail(f"{name}: `reply_text = result_file.read_text().strip()` not found")
        return

    # 2. `if not reply_text` guard exists after the read
    after_read = src[read_match.end():]
    guard_pat = re.compile(r"if\s+not\s+reply_text\s*:")
    guard_match = guard_pat.search(after_read)
    if not guard_match:
        fail(
            f"{name}: missing `if not reply_text:` guard after read_text().strip()",
            after_read[:400],
        )
        return

    # 3. `continue` follows the guard (within the next 120 chars)
    guard_block = after_read[guard_match.end(): guard_match.end() + 120]
    if "continue" not in guard_block:
        fail(
            f"{name}: `if not reply_text:` guard exists but does not `continue` — fix has no effect",
            guard_block,
        )
        return

    # 4. guard appears BEFORE `.pop(` — pending_replies not consumed on empty read
    pop_pat = re.compile(r"\.pop\(")
    pop_match = pop_pat.search(after_read)
    if not pop_match:
        # No pop at all after read — unusual, but not the bug we're guarding.
        pass
    elif guard_match.start() > pop_match.start():
        fail(
            f"{name}: `if not reply_text:` guard appears AFTER `.pop(` — pending_replies "
            f"is consumed before the guard fires; race condition is NOT fixed",
            after_read[:500],
        )
        return

    print(f"  ok  {name}: empty-file race guard present and correctly ordered")


print("bridge-result-race-guard — checking all 3 bridges:")
for bridge_name, bridge_path in BRIDGES.items():
    check_bridge(bridge_name, bridge_path)

if errors:
    print(f"\nFAILED: {errors} check(s) failed", file=sys.stderr)
    sys.exit(1)
else:
    print(f"\nPASSED: all {len(BRIDGES)} bridges have the race guard")
