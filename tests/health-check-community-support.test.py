#!/usr/bin/env python3
"""Coverage + behavior for the issue-time community-support pointer (#2156).

health-check prints a link to the official Discord under its 'N issue(s)
found' summary so a stuck user has somewhere to go. The line is produced by
the pure helper community_support_line() (extracted so it's testable without
running the full main() health sweep).

Run: python3 tests/health-check-community-support.test.py  (exit 0/1)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("healthcheck_cs", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("  ok  " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


line = hc.community_support_line()
check("returns the official Discord invite", "discord.gg/uZHWXXmrCS" in line)
check("names real humans + community agents", "real humans" in line and "community agents" in line)
check("is indented to align under the issue list", line.startswith("  "))
check("is a single line", "\n" not in line)

# ── before/after (CR #2156): the health-check summary shows the Discord line
# ONLY when there are issues. Drive the real main() summary path with
# run_all_checks stubbed and capture stdout — this is the before/after output,
# and it exercises the otherwise-glue `print(community_support_line())` call.
import contextlib
import io
from unittest.mock import patch


def _run_main_capture(fake_checks):
    buf = io.StringIO()
    with patch.object(hc, "run_all_checks", return_value=fake_checks), \
         patch.object(sys, "argv", ["health-check.py"]), \
         contextlib.redirect_stdout(buf):
        try:
            hc.main()
        except SystemExit:
            pass
    return buf.getvalue()


# BEFORE: a clean run (all ok) → no issues → the Discord line is absent.
out_clean = _run_main_capture([{"name": "core", "status": "ok", "detail": "running"}])
check("BEFORE (clean run): summary shows NO Discord support line",
      "discord.gg/uZHWXXmrCS" not in out_clean and "All systems operational" in out_clean)
# AFTER: an issue present → the Discord line appears under the issue list.
out_issue = _run_main_capture([{"name": "voice-agent", "status": "down", "detail": "not running"}])
check("AFTER (issue present): summary shows the Discord support line",
      "discord.gg/uZHWXXmrCS" in out_issue and "ISSUE(S): voice-agent" in out_issue)

print()
if failures:
    print(f"FAIL — {len(failures)}: {failures}")
    sys.exit(1)
print("PASS — community-support line")
