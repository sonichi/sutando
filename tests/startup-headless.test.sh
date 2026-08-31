#!/usr/bin/env bash
# Regression guard: the open-source core startup is headless. Desktop UI
# compilation, signing, process management, and browser opening belong to
# separately invoked product/app entry points, never src/startup.sh.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
STARTUP="${STARTUP:-$REPO/src/startup.sh}"
fail=0

check_absent() {
  local label="$1"
  local pattern="$2"
  if grep -nE "$pattern" "$STARTUP"; then
    echo "FAIL: $label"
    fail=1
  else
    echo "PASS: $label"
  fi
}

check_absent "core startup does not compile the macOS menu-bar app" \
  'swiftc .*main\.swift|Compiling Sutando'
check_absent "core startup does not sign or sync a macOS app bundle" \
  'SUT_APP=|codesign .*SUT_APP|cp .*SUT_BIN.*SUT_APP'
check_absent "core startup does not launch or terminate the menu-bar process" \
  'SUT_BIN=|src/Sutando/Sutando|Starting Sutando'
check_absent "core startup does not build the app-only accessibility helper" \
  'AXR_DIR=|Compiling public ax-read|skills/context-drop.*build\.sh'
check_absent "core startup does not open a browser" \
  '^[[:space:]]*open[[:space:]]'

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "Results: 5 passed, 0 failed"
