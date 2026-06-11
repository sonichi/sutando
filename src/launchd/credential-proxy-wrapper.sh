#!/bin/bash
# Wrapper for launchd-managed credential-proxy.
#
# Runs the esbuild-bundled proxy (credential-proxy.mjs) that the installer
# vendors NEXT TO this script, outside any TCC-protected directory. launchd's
# bash has no Documents/Desktop TCC grant, so nothing in this job's runtime
# path may resolve into the repo checkout — not the wrapper, not the proxy
# script, not its imports. The bundle's only runtime dependency is `node`.
#
# Kills any stale holder of port 7846 before starting, preventing the
# EADDRINUSE crash-loop that occurs when a manually-started process is still
# running when launchd tries to bootstrap (issue #1086).
#
# Called by com.sutando.credential-proxy.plist as the ProgramArguments entry
# so the launchd job gets the wrapper's PID and KeepAlive tracks it correctly.

set -euo pipefail

# The installer copies this wrapper and the bundle into the same directory.
PROXY_BUNDLE="$(cd "$(dirname "$0")" && pwd)/credential-proxy.mjs"

# Resolve node — launchd doesn't inherit the user's shell PATH.
resolve_node() {
    for p in \
        /opt/homebrew/bin/node \
        /usr/local/bin/node \
        "$HOME/.nvm/versions/node/$(ls "$HOME/.nvm/versions/node/" 2>/dev/null | sort -V | tail -1)/bin/node" \
        "$HOME/.volta/bin/node"
    do
        [ -x "$p" ] && { echo "$p"; return; }
    done
    command -v node 2>/dev/null || { echo "ERROR: node not found" >&2; exit 1; }
}

PORT=7846

# Kill any stale process holding port 7846. Defensive: only kill if the
# holder is running credential-proxy (same target); avoids killing
# unrelated processes that coincidentally claimed the port.
kill_stale_holder() {
    local pid
    pid=$(lsof -ti :"$PORT" 2>/dev/null | head -1) || return 0
    [ -z "$pid" ] && return 0
    # Get the command line — only kill if it's a credential-proxy run.
    local cmd
    cmd=$(ps -p "$pid" -o args= 2>/dev/null || true)
    if echo "$cmd" | grep -q "credential-proxy"; then
        echo "[credential-proxy-wrapper] killing stale holder pid=$pid cmd='${cmd:0:80}'"
        kill "$pid" 2>/dev/null || true
        # Give it a moment to release the port.
        sleep 0.5
    fi
}

kill_stale_holder

if [ ! -f "$PROXY_BUNDLE" ]; then
    echo "ERROR: bundle not found at $PROXY_BUNDLE — re-run src/install-credential-proxy-launchd.sh" >&2
    exit 1
fi

NODE_BIN=$(resolve_node)
exec "$NODE_BIN" "$PROXY_BUNDLE"
