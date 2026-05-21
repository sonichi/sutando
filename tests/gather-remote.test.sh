#!/bin/bash
# Tests for skills/self-diagnose/scripts/gather-remote.sh — CLI surface.
#
# Heavy end-to-end coverage (actual ssh + rsync + diff) requires a
# reachable peer with sutando installed; that's a manual smoke test
# (documented in skills/self-diagnose/SKILL.md). These tests cover
# the CLI argv/error paths, which are the most regression-prone.
#
# Run: bash tests/gather-remote.test.sh
# Exit: 0 on pass, 1 on fail.

set -u
SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/skills/self-diagnose/scripts/gather-remote.sh"

if [ ! -x "$SCRIPT" ]; then
    echo "FAIL: $SCRIPT not found or not executable"
    exit 1
fi

PASS=0
FAIL=0

run_case() {
    local name="$1"; shift
    if ( "$@" ); then
        echo "PASS: $name"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $name"
        FAIL=$((FAIL + 1))
    fi
}

case_help_prints_usage() {
    out="$(bash "$SCRIPT" --help 2>&1)"
    echo "$out" | grep -q "Usage: gather-remote.sh" || { echo "  expected 'Usage:' in help output"; return 1; }
    echo "$out" | grep -q "ssh-target" || { echo "  expected 'ssh-target' in help output"; return 1; }
    return 0
}

case_no_args_prints_usage_and_fails() {
    out="$(bash "$SCRIPT" 2>&1 || true)"
    echo "$out" | grep -q "Usage: gather-remote.sh" || { echo "  expected usage when no args"; return 1; }
    return 0
}

case_unreachable_peer_fails_with_clear_message() {
    # Use a host that DNS-fails fast so the test doesn't hang.
    rc=0
    out="$(bash "$SCRIPT" nonexistent-host.invalid 2>&1)" || rc=$?
    [ "$rc" -ne 0 ] || { echo "  expected non-zero exit on unreachable host (got $rc)"; return 1; }
    echo "$out" | grep -q "cannot reach" || { echo "  expected 'cannot reach' in stderr"; return 1; }
    echo "$out" | grep -q "ssh nonexistent-host.invalid" || { echo "  expected suggested manual ssh command in stderr"; return 1; }
    return 0
}

case_window_default_24h() {
    out="$(bash "$SCRIPT" nonexistent-host.invalid 2>&1)" || true
    echo "$out" | grep -q "window=24h" || { echo "  expected default window=24h in early log"; return 1; }
    return 0
}

case_window_overridden_via_positional() {
    out="$(bash "$SCRIPT" nonexistent-host.invalid 6h 2>&1)" || true
    echo "$out" | grep -q "window=6h" || { echo "  expected window=6h in early log"; return 1; }
    return 0
}

run_case "--help prints usage"                            case_help_prints_usage
run_case "no args prints usage and exits non-zero"        case_no_args_prints_usage_and_fails
run_case "unreachable peer fails with clear stderr"       case_unreachable_peer_fails_with_clear_message
run_case "window defaults to 24h"                         case_window_default_24h
run_case "window overridden via 2nd positional arg"       case_window_overridden_via_positional

echo ""
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
