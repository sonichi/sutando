#!/usr/bin/env bash
# shellcheck disable=SC2016  # stub bodies are written verbatim and expand when the
# stub RUNS, not when this file writes it; single quotes are required throughout.
# The installer's two dangerous paths, exercised against the real script.
#
# 1. Toolchain preflight. `command -v swiftc` passes against the Xcode-CLT stub
#    on a clean Mac (REVIEW.md lesson 7), so a bare swiftc then raises the system
#    install dialog instead of a diagnostic. The gate must be `xcode-select -p`,
#    the one probe that does not prompt, and it must run BEFORE swiftc.
#
# 2. Replacement scope. The Electron desktop app shares the executable NAME with
#    this menu-bar binary (#2038), so a name-scoped kill takes out the user's UI
#    and then reports success. Replacement must be scoped to this bundle's path.
#
# Runs the production script with a stubbed toolchain on PATH — not a copy of
# its logic — so the assertions bind to what actually ships.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO/scripts/install-menu-bar-app.sh"
fail=0
pass() { echo "PASS: $1"; }
flunk() { echo "FAIL: $1"; fail=1; }

[ "$(uname -s)" = "Darwin" ] || { echo "SKIP: macOS-only installer"; exit 0; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; [ -n "${DECOY:-}" ] && kill "$DECOY" 2>/dev/null; exit' EXIT

mkstub() {  # dir name body
    mkdir -p "$1"
    printf '#!/bin/sh\n%s\n' "$3" > "$1/$2"
    chmod +x "$1/$2"
}

# ---- 1. preflight: no developer tools -> refuse, and never reach swiftc ------
STUB="$WORK/stub-noclt"
mkstub "$STUB" xcode-select 'exit 1'
mkstub "$STUB" swiftc "touch '$WORK/SWIFTC_WAS_INVOKED'; exit 0"
out="$(PATH="$STUB:$PATH" bash "$SCRIPT" 2>&1)"; rc=$?

if [ "$rc" -ne 0 ]; then pass "refuses when xcode-select -p fails"
else flunk "refuses when xcode-select -p fails (exited $rc)"; fi

case "$out" in
    *"xcode-select --install"*) pass "names the actual remedy" ;;
    *) flunk "names the actual remedy (got: $out)" ;;
esac

# The whole point: the stub must never be invoked, because invoking it is what
# raises the install dialog on a clean Mac.
if [ -e "$WORK/SWIFTC_WAS_INVOKED" ]; then
    flunk "does NOT invoke swiftc when the toolchain is absent"
else
    pass "does NOT invoke swiftc when the toolchain is absent"
fi

# ---- 2. preflight: tools present but swiftc not runnable --------------------
STUB2="$WORK/stub-badswift"
mkstub "$STUB2" xcode-select 'exit 0'
mkstub "$STUB2" swiftc 'exit 1'
out2="$(PATH="$STUB2:$PATH" bash "$SCRIPT" 2>&1)"; rc2=$?
if [ "$rc2" -ne 0 ]; then pass "refuses when swiftc is present but not runnable"
else flunk "refuses when swiftc is present but not runnable (exited $rc2: $out2)"; fi

# ---- 3. replacement is path-scoped: a foreign 'Sutando' must SURVIVE --------
# Stand in for the Electron desktop app: same executable name, different bundle.
FOREIGN="$WORK/Applications/Sutando.app/Contents/MacOS"
mkdir -p "$FOREIGN"
# A COPY of a system binary is SIGKILLed by macOS (its signature no longer
# matches), so the stand-in has to be a script we own.
printf '#!/bin/sh\nexec sleep 600\n' > "$FOREIGN/Sutando"
chmod +x "$FOREIGN/Sutando"
"$FOREIGN/Sutando" &
DECOY=$!
sleep 0.3

if ! kill -0 "$DECOY" 2>/dev/null; then
    flunk "decoy setup (foreign Sutando did not start)"
else
    STUB3="$WORK/stub-build"
    mkstub "$STUB3" xcode-select 'exit 0'
    mkstub "$STUB3" swiftc 'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && { shift; touch "$1"; }; shift; done; exit 0'
    mkstub "$STUB3" codesign 'exit 0'
    mkstub "$STUB3" open "touch '$WORK/OPEN_CALLED'; exit 0"
    PATH="$STUB3:$PATH" bash "$SCRIPT" --launch >/dev/null 2>&1

    if kill -0 "$DECOY" 2>/dev/null; then
        pass "a foreign Sutando process SURVIVES --launch (not name-scoped)"
    else
        flunk "a foreign Sutando process SURVIVES --launch (it was killed)"
    fi
    kill "$DECOY" 2>/dev/null
fi

# ---- 4. success is verified, not assumed -----------------------------------
# `open` succeeded but nothing is running, so the script must NOT claim launch.
STUB4="$WORK/stub-noproc"
mkstub "$STUB4" xcode-select 'exit 0'
mkstub "$STUB4" swiftc 'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && { shift; touch "$1"; }; shift; done; exit 0'
mkstub "$STUB4" codesign 'exit 0'
mkstub "$STUB4" open 'exit 0'
out4="$(PATH="$STUB4:$PATH" bash "$SCRIPT" --launch 2>&1)"; rc4=$?
case "$out4" in
    *"✓ launched"*) flunk "never prints '✓ launched' when no process is running" ;;
    *) pass "never prints '✓ launched' when no process is running" ;;
esac
if [ "$rc4" -ne 0 ]; then pass "exits nonzero when the launch cannot be confirmed"
else flunk "exits nonzero when the launch cannot be confirmed (exited $rc4)"; fi

# ---- 5. the retired probes must not come back -------------------------------
# Strip comments first: the script explains WHY each probe was retired and names
# it, so a whole-file grep matches the explanation and reports the bug it fixed.
code="$(grep -vE '^[[:space:]]*#' "$SCRIPT")"
case "$code" in
    *'pkill -x Sutando'*) flunk "no name-scoped 'pkill -x Sutando' in CODE" ;;
    *) pass "no name-scoped 'pkill -x Sutando' in CODE" ;;
esac
case "$code" in
    *'command -v swiftc'*) flunk "no 'command -v swiftc' in CODE (passes against the CLT stub)" ;;
    *) pass "no 'command -v swiftc' in CODE (passes against the CLT stub)" ;;
esac

if [ "$fail" -ne 0 ]; then
    echo "FAIL: install-menu-bar-app"
    exit 1
fi
echo "PASS: installer refuses without a real toolchain and replaces only its own app."
