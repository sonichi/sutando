#!/usr/bin/env python3
"""One x.com page is addressed by ID for a whole reply, so focus cannot retarget it.

A public reply is irreversible: if the fill lands in one composer and the
Cmd+Return lands in another, the post goes out from the wrong page under the
user's account. Every phase must name the SAME window/tab ids.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "skills" / "x-twitter" / "x-browser.py"
_spec = importlib.util.spec_from_file_location("x_browser", _SRC)
xb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(xb)

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f" — {extra}"))
    if not cond:
        FAILED.append(name)


# The page the operation starts on, and the one a focus change would steal it to.
OURS = (1, 100)
INTRUDER = (2, 200)
scripts: list[str] = []


def fake_osascript(script: str, timeout: int = 20) -> str:
    scripts.append(script)
    if "make new tab" in script:                       # ensure_tab resolves + reports
        return f"{OURS[0]},{OURS[1]}"
    if "__NO_X_TAB__" in script:                       # a bare re-scan would land here
        return f"{INTRUDER[0]},{INTRUDER[1]}"          # focus moved: first match is now theirs
    if "readyState" in script:
        return "complete"
    return "ok"


xb._osascript = fake_osascript                          # type: ignore[assignment]

xb._chrome_running = lambda: True                       # type: ignore[assignment]
xb.time.sleep = lambda *_a, **_k: None                  # type: ignore[assignment]

xb.ensure_tab("https://x.com/i/status/123", settle=0, max_wait=0.01)
check("ensure_tab records the window+tab it actually used",
      xb._TARGET == {"win": OURS[0], "tab": OURS[1]}, f"got {xb._TARGET}")

after_resolve = len(scripts)
xb.run_js("document.title")
xb._os_submit_via_keystroke()
later = scripts[after_resolve:]
check("the reply's later phases each issued a script", len(later) >= 2, f"got {len(later)}")

ids_ok = all(re.search(rf"\(id of w\) is {OURS[0]}\b", s)
             and re.search(rf"\(id of t\) is {OURS[1]}\b", s) for s in later)
check("every phase after resolution addresses OUR ids", ids_ok,
      "a phase addressed something other than the recorded window/tab")

no_steal = not any(str(INTRUDER[0]) in re.sub(r"eval\(atob\('[^']*'\)\)", "", s)
                   and str(INTRUDER[1]) in s for s in later)
check("no phase addresses the intruder tab a focus change would surface", no_steal)

no_rescan = not any('contains "x.com"' in s for s in later)
check("no phase re-scans for the first matching URL", no_rescan,
      "a URL scan is exactly what lets a focus change retarget the operation")

submit = later[-1]
check("the keystroke is sent to the recorded window, not the frontmost one",
      "keystroke return using command down" in submit
      and "set index of theWin to 1" in submit
      and f"(id of w) is {OURS[0]}" in submit)

# Fail closed rather than guess: a vanished tab must not fall back to a scan.
def gone(script: str, timeout: int = 20) -> str:
    return "__TARGET_GONE__"


xb._osascript = gone                                    # type: ignore[assignment]
try:
    xb.run_js("document.title")
    raised = False
except xb.BrowserError as e:
    raised = "refusing to retarget" in str(e)
check("a vanished target raises instead of retargeting", raised)

xb._TARGET = None
xb._osascript = fake_osascript                          # type: ignore[assignment]
try:
    xb._os_submit_via_keystroke()
    blind = False
except xb.BrowserError as e:
    blind = "refusing to submit blind" in str(e)
check("submitting with no recorded target refuses rather than posting", blind)

print("\n" + ("PASS — stable window/tab identity across a reply" if not FAILED
              else f"FAIL — {len(FAILED)} check(s): {FAILED}"))
sys.exit(1 if FAILED else 0)
