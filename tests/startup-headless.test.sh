#!/usr/bin/env bash
# Regression guard: --headless / SUTANDO_HEADLESS=1 makes core startup skip the
# desktop app entirely, and every app action stays inside that gate. Asserting
# the strings were absent instead made the default unrestorable without deleting
# this file, so the gate is what is pinned here, not the absence.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
STARTUP="${STARTUP:-$REPO/src/startup.sh}"

python3 - "$STARTUP" <<'PY'
import re, sys

src = open(sys.argv[1]).read()
lines = src.split("\n")
fail = 0

def report(ok, label):
    global fail
    print(("PASS: " if ok else "FAIL: ") + label)
    if not ok:
        fail = 1

report("--headless)" in src, "startup accepts --headless")
report('SUTANDO_HEADLESS="${SUTANDO_HEADLESS:-0}"' in src,
       "SUTANDO_HEADLESS defaults to 0 (app on by default for OSS installs)")

# Locate the guarded region: `if [ "${SUTANDO_HEADLESS:-0}" = "1" ]; then` ... matching `fi`.
start = next((i for i, l in enumerate(lines)
              if re.match(r'\s*if \[ "\$\{SUTANDO_HEADLESS:-0\}" = "1" \]; then', l)), None)
report(start is not None, "the app section is wrapped in a SUTANDO_HEADLESS gate")

guarded = set()
if start is not None:
    depth = 0
    for i in range(start, len(lines)):
        stripped = lines[i].strip()
        if re.match(r'(if |elif .*; then$)', stripped) or stripped.endswith("; then"):
            if stripped.startswith("if "):
                depth += 1
        if stripped == "fi":
            depth -= 1
            if depth == 0:
                guarded = set(range(start, i + 1))
                break
    report(bool(guarded), "the SUTANDO_HEADLESS gate is balanced")

# Every app action must live inside the gate — an ungated one is the regression.
for label, pattern in [
    ("menu-bar app compilation", r'swiftc .*main\.swift|Compiling Sutando'),
    ("app bundle signing/sync", r'SUT_APP=|codesign .*SUT_APP'),
    ("menu-bar process launch/terminate", r'SUT_BIN=|src/Sutando/Sutando|Starting Sutando'),
    ("app-only accessibility helper build", r'AXR_DIR=|Compiling public ax-read'),
]:
    hits = [i for i, l in enumerate(lines) if re.search(pattern, l)]
    outside = [i + 1 for i in hits if i not in guarded]
    if outside:
        print(f"   ungated at line(s): {outside}")
    report(bool(hits) and not outside, f"{label} happens only inside the headless gate")

# Unchanged from the original guard: startup never opens a browser for you.
report(not [l for l in lines
            if re.match(r'^\s*open\s', l) and not l.strip().startswith("#")],
       "core startup does not open a browser")

sys.exit(1 if fail else 0)
PY

echo "startup-headless: all checks passed"
