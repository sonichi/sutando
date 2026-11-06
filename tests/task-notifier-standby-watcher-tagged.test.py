#!/usr/bin/env python3
"""Both notifiers must start their standby watcher TAGGED `--role standby`.

THE DEFECT (found in review, 2026-09-22): each notifier launches the watcher
through a python shim that re-execs bash, and the shim forwarded only
`sys.argv[1]` and `sys.argv[2]` — the script path and the inbox. Any flag after
those was dropped on the floor, silently: the launch line read
`--role standby --inbox <dir>` and the process that appeared carried neither. A
grep of the launcher would have "confirmed" the tag; only the shim's own argv
handling decides it, so this test execs the real shim text out of each notifier.

Run: python3 tests/task-notifier-standby-watcher-tagged.test.py  (exit 0/1)
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NOTIFIERS = (
    REPO / "src/agent/claude/cli/task-notifier.sh",
    REPO / "src/agent/codex/cli/task-notifier.sh",
)
LAUNCH = re.compile(
    r"-c\s*\\?\s*\n?\s*'(?P<shim>[^']*os\.execv[^']*)'\s*\\?\s*\n?\s*"
    r"(?P<args>[^\n]*watch-tasks-stream\.sh[^\n]*)")

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  PASS " if ok else "  FAIL ") + label + ("" if ok else f" — {detail}"))
    if not ok:
        failures.append(label)


print("notifier standby watcher is tagged:")
for notifier in NOTIFIERS:
    name = notifier.relative_to(REPO)
    src = notifier.read_text(encoding="utf-8")
    m = LAUNCH.search(src)
    if not m:
        check(f"{name}: the watcher launch is found", False,
              "shape changed — this test cannot vouch for the tag any more")
        continue

    args = m.group("args")
    check(f"{name}: the launch line asks for --role standby",
          "--role standby" in args, args.strip()[:120])
    check(f"{name}: the launch line asks for --inbox",
          "--inbox" in args, args.strip()[:120])

    # The shim is what decides whether those flags survive. Run the real text with
    # a stand-in for bash that prints the argv it was handed.
    shim = m.group("shim").replace("os.setsid(); ", "")
    printer = "import sys; print(' '.join(sys.argv[1:]))"
    out = subprocess.run(
        [sys.executable, "-c", shim.replace('"/bin/bash"', f'"{sys.executable}"')
                                   .replace('["bash"', f'["{sys.executable}", "-c", "{printer}"'),
         "SCRIPT", "INBOX", "--role", "standby", "--inbox", "INBOX"],
        capture_output=True, text=True)
    forwarded = out.stdout.strip()
    check(f"{name}: the shim forwards the script and the inbox",
          "SCRIPT" in forwarded and "INBOX" in forwarded, f"got {forwarded!r}")
    check(f"{name}: the shim forwards the tag as well",
          "--role standby" in forwarded and "--inbox" in forwarded,
          f"got {forwarded!r} — the flags were dropped between the launch line and bash")

if failures:
    print(f"\nFAILED ({len(failures)})")
    sys.exit(1)
print("\n  ok  both notifiers hand the standby watcher its role and inbox")
