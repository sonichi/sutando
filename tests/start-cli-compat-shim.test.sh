#!/bin/bash
# Smoke test for the one-release compat shim added when the Claude-specific
# shell setup moved to src/agent/claude/cli/ (PR #1891).
#
# Guards against the "moved with no shim" regression: an already-installed /
# not-yet-rebuilt Sutando.app (and health-check --recover-core) still invoke the
# OLD scripts/sutando-shell-setup.sh path, and would hard-fail after a `git pull`
# if that path vanished. This asserts the remaining shim exists, is executable,
# parses, and exec-forwards to its canonical home. The generic core launcher has
# no old-path shim: all callers use src/agent/start-cli.sh directly.
# It does NOT run the shim — structural only.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fail=0

for name in sutando-shell-setup; do
    shim="$REPO/scripts/$name.sh"
    target="src/agent/claude/cli/$name.sh"

    [ -f "$shim" ] || { echo "  FAIL: compat shim $shim is missing"; fail=1; continue; }
    [ -x "$shim" ] || { echo "  FAIL: compat shim $shim is not executable"; fail=1; }
    bash -n "$shim" 2>/dev/null || { echo "  FAIL: $shim has a syntax error"; fail=1; }
    grep -q "exec bash .*$target" "$shim" \
        || { echo "  FAIL: $shim does not exec-forward to $target"; fail=1; }
    [ -f "$REPO/$target" ] || { echo "  FAIL: forward target $REPO/$target is missing"; fail=1; }
done

if [ "$fail" -eq 0 ]; then
    echo "PASS: shell-setup compat shim forwards to its canonical target"
else
    echo "compat-shim test FAILED"
    exit 1
fi
