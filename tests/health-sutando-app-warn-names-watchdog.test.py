#!/usr/bin/env python3
"""The sutando-app 'not running' warn must name the watcher consequence.

THIS IS A WORDING PIN, NOT A SAFETY TEST, and the distinction matters: it cannot
detect the app being down, only that the message says what being down costs.

It exists because the old text — "not running — hotkeys disabled" — names the
visible consequence and omits the expensive one. The app also runs checkWatcher()
(src/Sutando/main.swift), which pgreps for the task watcher and pokes the CLI when
it is gone. On 2026-09-11 the task watcher died twice on a host where the app was
down; the probe warned on every pass, and the warn was skipped every time because
it read as a comfort feature.

Run: python3 tests/health-sutando-app-warn-names-watchdog.test.py
"""
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = (REPO / "src" / "health-check.py").read_text()
fails = 0


def ck(name, cond):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


# Isolate the ok-stopped branch's detail, so an unrelated "hotkeys" elsewhere in
# the file can neither satisfy nor break this.
m = re.search(r'"name":\s*"sutando-app",\s*"status":\s*"warn",\s*'
              r'"detail":\s*((?:"[^"]*"\s*)+)\}', SRC)
ck("the sutando-app warn branch is findable", m is not None)
detail = "".join(re.findall(r'"([^"]*)"', m.group(1))) if m else ""

ck("it still names the hotkey consequence", "hotkeys" in detail)
ck("it names checkWatcher by name", "checkWatcher" in detail)
ck("it says the watcher goes unrecovered",
   re.search(r"recovered by nothing", detail, re.I) is not None)
# The claim must carry the CLI-idle condition: checkWatcher defers to the loop
# whenever cliIsWorking(), so "the app recovers it" overstates the guarantee.
ck("and scopes it to when the CLI is busy", "while the CLI is busy" in detail)

# The GREEN branch had the same hotkey-only identity, which is why one-line
# fixes to the warn leave the probe still describing a keyboard convenience.
green = re.search(r'return f"running \(\{watch\}[^"]*"', SRC)
ck("the running-line names the watchdog too", green is not None)
ck("and the watchdog label is defined", 'watcher-watchdog' in SRC)

# The premise the message asserts must be true of the app, or the message lies.
SWIFT = REPO / "src" / "Sutando" / "main.swift"
ck("the app source exists", SWIFT.exists())
sw = SWIFT.read_text() if SWIFT.exists() else ""
ck("checkWatcher() is defined there", "func checkWatcher()" in sw)

# Scope the premise checks to checkWatcher's OWN body. File-wide substring
# presence would stay green if the guard moved to another function entirely.
_start = sw.find("func checkWatcher()")
_next = re.search(r"\n    func ", sw[_start + 1:]) if _start != -1 else None
body = sw[_start:_start + 1 + (_next.start() if _next else len(sw))] if _start != -1 else ""
ck("its body is isolatable (not the whole file)",
   0 < len(body) < len(sw) * 0.5)

ck("and it really pgreps for the watcher",
   re.search(r'"-f",\s*"watch-tasks"', body) is not None)
ck("cliIsWorking() gates the poke INSIDE checkWatcher, not merely somewhere in the file",
   "if cliIsWorking()" in body)
# `[^}]*` cannot cross a nested block: add any inner brace before the return and
# this goes RED on correct code. Widen the pattern then — do not delete the check.
ck("and that guard actually returns early",
   re.search(r"if cliIsWorking\(\)\s*\{[^}]*\breturn\b", body, re.S) is not None)

print("\nall ok" if fails == 0 else f"\n{fails} FAILED")
sys.exit(0 if fails == 0 else 1)
