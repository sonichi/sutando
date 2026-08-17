#!/usr/bin/env bash
# --supervise must prove launchd owns a running process before reporting success.
#
# `launchctl kickstart` is invoked with `|| true` (it is idempotent-noisy), so the
# only thing standing between "registered" and "supervised" is a pid check. Without
# it the command prints "Sutando.app is now launchd-supervised" over a job launchd
# never started, and KeepAlive cannot restart what never ran.
#
# Isolation is the point of this file: HOME is redirected, so DEST resolves inside
# the temp tree and the real ~/Library/LaunchAgents is never written. Driving
# --supervise WITHOUT that redirect is how a live LaunchAgent pointing at a temp
# worktree got installed on a developer machine.
set -uo pipefail

# launchctl, ~/Library/LaunchAgents and codesign are macOS-only; the sibling
# installer test carries this same guard, and omitting it broke CI on ubuntu.
[ "$(uname -s)" = "Darwin" ] || { echo "SKIP: macOS-only installer"; exit 0; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO/src/install-sutando-app-launchd.sh"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/launchd-test-XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
fail=0

pass() { echo "PASS: $1"; }
flunk() { echo "FAIL: $1"; fail=1; }

mkstub() {  # dir name body
    mkdir -p "$1"
    printf '#!/bin/sh\n%s\n' "$3" > "$1/$2"
    chmod +x "$1/$2"
}

# A launchctl whose `print` reports a running pid == the healthy case.
mklaunchctl() {  # dir  pid-line-or-empty
    mkstub "$1" launchctl "
case \"\$1\" in
  print) [ -n \"$2\" ] && printf '\tstate = running\n\tpid = %s\n' \"$2\"; exit 0 ;;
  bootstrap|bootout|kickstart) exit 0 ;;
esac
exit 0"
}

run_supervise() {  # stubdir -> stdout+stderr, sets RC
    HOME="$WORK/home" PATH="$1:$PATH" bash "$SCRIPT" 2>&1
}

mkdir -p "$WORK/home/Library/LaunchAgents"
REAL_AGENTS="$HOME/Library/LaunchAgents"
agents_listing() { find "$REAL_AGENTS" -maxdepth 1 -type f 2>/dev/null | sort; }
BEFORE="$(agents_listing)"

# ---- 1. a job launchd never started must NOT report supervision -------------
STUB_DEAD="$WORK/stub-nopid"
mklaunchctl "$STUB_DEAD" ""
out_dead="$(run_supervise "$STUB_DEAD")"; rc_dead=$?
case "$out_dead" in
    *"now launchd-supervised"*) flunk "reports supervision with no launchd-owned pid" ;;
    *)                          pass "never reports supervision with no launchd-owned pid" ;;
esac
if [ "$rc_dead" -ne 0 ]; then pass "exits nonzero when launchd owns no process"
else flunk "exited 0 with no launchd-owned process (rc=$rc_dead)"; fi

# ---- 2. CONTROL: a running pid must still report success --------------------
# Without this, assertion 1 would pass simply by the script being broken.
STUB_LIVE="$WORK/stub-pid"
mklaunchctl "$STUB_LIVE" "4242"
out_live="$(run_supervise "$STUB_LIVE")"; rc_live=$?
case "$out_live" in
    *"now launchd-supervised"*) pass "control: a live launchd pid DOES report supervision" ;;
    *) flunk "control failed: live pid did not report supervision (rc=$rc_live)" ;;
esac
case "$out_live" in
    *"pid 4242"*) pass "names the launchd-owned pid it verified" ;;
    *)            flunk "did not name the verified pid" ;;
esac

# ---- 3. isolation: the REAL LaunchAgents dir must be untouched --------------
# The defect this guards is not hypothetical — it happened on a dev machine.
AFTER="$(agents_listing)"
if [ "$BEFORE" = "$AFTER" ]; then
    pass "the real ~/Library/LaunchAgents is unchanged by this test"
else
    flunk "this test wrote to the REAL LaunchAgents dir"
fi
if [ -f "$WORK/home/Library/LaunchAgents/com.sutando.menubar.plist" ]; then
    pass "the plist landed in the redirected HOME, proving the seam works"
else
    flunk "no plist in the redirected HOME — the run never reached install (seam unproven)"
fi

[ "$fail" -eq 0 ] || { echo "FAIL: install-sutando-app-launchd"; exit 1; }
echo "PASS: --supervise proves a live launchd-owned process before claiming it."
