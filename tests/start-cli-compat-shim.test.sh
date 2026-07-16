#!/bin/bash
# Smoke test for the one-release compat shims added when the Claude core-agent
# launchers moved to src/agent/claude/cli/ (PR #1891).
#
# Guards against the "moved with no shim" regression: an already-installed /
# not-yet-rebuilt Sutando.app (and health-check --recover-core) still invoke the
# OLD scripts/{start-cli,sutando-shell-setup}.sh paths, and would hard-fail after
# a `git pull` if those paths vanished. This asserts the shims exist, are
# executable, parse, and exec-forward to their canonical homes. start-cli now
# targets the runtime dispatcher; sutando-shell-setup remains Claude-specific.
# It does NOT run the shims (that would launch the live core) — structural only.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fail=0

for name in start-cli sutando-shell-setup; do
    shim="$REPO/scripts/$name.sh"
    if [ "$name" = "start-cli" ]; then
        target="src/agent/start-cli.sh"
    else
        target="src/agent/claude/cli/$name.sh"
    fi

    [ -f "$shim" ] || { echo "  FAIL: compat shim $shim is missing"; fail=1; continue; }
    [ -x "$shim" ] || { echo "  FAIL: compat shim $shim is not executable"; fail=1; }
    bash -n "$shim" 2>/dev/null || { echo "  FAIL: $shim has a syntax error"; fail=1; }
    grep -q "exec bash .*$target" "$shim" \
        || { echo "  FAIL: $shim does not exec-forward to $target"; fail=1; }
    [ -f "$REPO/$target" ] || { echo "  FAIL: forward target $REPO/$target is missing"; fail=1; }
done

if [ "$fail" -eq 0 ]; then
    echo "PASS: launcher compat shims forward to their canonical targets"
else
    echo "compat-shim test FAILED"
    exit 1
fi
