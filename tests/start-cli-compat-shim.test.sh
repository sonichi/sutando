#!/bin/bash
# Smoke test for the one-release compat shims added when generic scripts moved
# under src/agent/ (PR #1891 for sutando-shell-setup; the start-cli shim
# followed later, closing the gap #1891 deliberately left — see
# sonichi/sutando#3362).
#
# Guards against the "moved with no shim" regression: an already-installed /
# not-yet-rebuilt Sutando.app still invokes the OLD scripts/*.sh paths, and
# would hard-fail — or, for start-cli specifically, silently no-op with no
# logging (ag2space-cinny-desktop's backend-supervisor.mjs launchCore,
# sonichi/sutando#3362) — after a `git pull` if those paths vanished. This
# asserts each remaining shim exists, is executable, parses, and
# exec-forwards to its own canonical home (the two shims have DIFFERENT
# targets, so this is keyed per name, not a single shared target string).
# It does NOT run the shims — structural only.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fail=0

target_for() {
    case "$1" in
        sutando-shell-setup) echo "src/agent/claude/cli/sutando-shell-setup.sh" ;;
        start-cli)           echo "src/agent/start-cli.sh" ;;
        *)                   echo "" ;;
    esac
}

for name in sutando-shell-setup start-cli; do
    shim="$REPO/scripts/$name.sh"
    target="$(target_for "$name")"

    [ -f "$shim" ] || { echo "  FAIL: compat shim $shim is missing"; fail=1; continue; }
    [ -x "$shim" ] || { echo "  FAIL: compat shim $shim is not executable"; fail=1; }
    bash -n "$shim" 2>/dev/null || { echo "  FAIL: $shim has a syntax error"; fail=1; }
    grep -q "exec bash .*$target" "$shim" \
        || { echo "  FAIL: $shim does not exec-forward to $target"; fail=1; }
    [ -f "$REPO/$target" ] || { echo "  FAIL: forward target $REPO/$target is missing"; fail=1; }
done

if [ "$fail" -eq 0 ]; then
    echo "PASS: compat shims forward to their canonical targets"
else
    echo "compat-shim test FAILED"
    exit 1
fi
