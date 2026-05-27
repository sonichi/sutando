#!/usr/bin/env python3
"""
Structural regression test for issue #1235 — phone-conversation task archiving.

Before fix: conversation-server.ts consumed the result file (unlinkSync) but
left the task file (task-phone-*.txt) in tasks/ forever.

After fix: archivePhoneTask() is called in all three poll-interval exit paths:
  1. Call hang-up / call not active  →  archive before return
  2. Result available (success path)  →  archive after unlinkSync(resultPath)
  3. Timeout (POLL_TIMEOUT_MS)        →  archive before logging timeout

Guards (structural — does not run the live server):
  A. archivePhoneTask() helper exists and uses TASKS_DIR + archive/YYYY-MM/
  B. Call in hang-up path (right before `return`)
  C. Call in success path (right after unlinkSync(resultPath))
  D. Call in timeout path (right before logging timeout)

Run: python3 tests/phone-task-archive.test.py
Exit: 0 = pass, 1 = fail.
"""

from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parent.parent
CONV = REPO / "skills" / "phone-conversation" / "scripts" / "conversation-server.ts"


def fail(msg: str, ctx: str = "") -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("---context---", file=sys.stderr)
        print(ctx[:1000], file=sys.stderr)
    return 1


def main() -> int:
    if not CONV.exists():
        return fail(f"{CONV} not found")

    src = CONV.read_text()

    # A. archivePhoneTask helper exists and uses archive/ + renameSync.
    # The function is small (~8 lines); scan a 400-char window starting at its def.
    helper_start = src.find("function archivePhoneTask(")
    if helper_start == -1:
        return fail("archivePhoneTask() helper function not found")
    helper_window = src[helper_start : helper_start + 400]
    if "archive" not in helper_window:
        return fail("archivePhoneTask must put files under archive/", helper_window)
    if "renameSync" not in helper_window:
        return fail("archivePhoneTask must use renameSync (not unlinkSync only)", helper_window)

    # B. Call in the hang-up / call-inactive early-exit path.
    # Pattern: the hangingUp guard block ends with archivePhoneTask then return
    if not re.search(
        r"callSession\.hangingUp[\s\S]{0,300}?archivePhoneTask\s*\(\s*taskPath\s*,\s*taskId\s*\)[\s\S]{0,60}?return\s*;",
        src,
    ):
        return fail(
            "archivePhoneTask() must be called in the hang-up exit path before `return`"
        )

    # C. Call in the success path — after unlinkSync(resultPath).
    # Pattern: unlinkSync(resultPath) ... archivePhoneTask(taskPath, taskId) within ~200 chars
    if not re.search(
        r"unlinkSync\s*\(\s*resultPath\s*\)[\s\S]{0,200}?archivePhoneTask\s*\(\s*taskPath\s*,\s*taskId\s*\)",
        src,
    ):
        return fail(
            "archivePhoneTask() must be called after unlinkSync(resultPath) in success path"
        )

    # D. Call in timeout path — before the timeout log line.
    if not re.search(
        r"archivePhoneTask\s*\(\s*taskPath\s*,\s*taskId\s*\)[\s\S]{0,100}?timeout for",
        src,
    ):
        return fail(
            "archivePhoneTask() must be called in the timeout path before logging timeout"
        )

    # E. renameSync is imported (needed by archivePhoneTask)
    if not re.search(r"\brenameSync\b", src.split("from 'node:fs'")[0] + "from 'node:fs'"):
        return fail("renameSync must be imported from 'node:fs'")
    fs_import = re.search(r"import\s*\{[^}]+\}\s*from\s*'node:fs'", src)
    if not fs_import or "renameSync" not in fs_import.group():
        return fail("renameSync must appear in the node:fs import statement")

    print("PASS: phone-conversation task archiving guards pass.")
    print("  - archivePhoneTask() helper found with renameSync + archive/ path")
    print("  - called in hang-up path before return")
    print("  - called in success path after unlinkSync(resultPath)")
    print("  - called in timeout path before timeout log")
    print("  - renameSync imported from node:fs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
