#!/bin/bash
# Wrapper for launchd-managed credential-proxy.
# 
# Kills any stale holder of port 7846 before starting, preventing the
# EADDRINUSE crash-loop that occurs when a manually-started process is still
# running when launchd tries to bootstrap (issue #1086).
#
# Called by com.sutando.credential-proxy.plist as the ProgramArguments entry
# so the launchd job gets the wrapper's PID and KeepAlive tracks it correctly.

set -euo pipefail

# Resolve the credential-proxy script path (tilde-expanded at run time).
PROXY_SCRIPT="$HOME/.claude/skills/quota-tracker/scripts/credential-proxy.ts"

# Resolve npx — launchd doesn't inherit the user's shell PATH.
resolve_npx() {
    for p in \
        /opt/homebrew/bin/npx \
        /usr/local/bin/npx \
        "$HOME/.nvm/versions/node/$(ls "$HOME/.nvm/versions/node/" 2>/dev/null | sort -V | tail -1)/bin/npx" \
        "$HOME/.volta/bin/npx"
    do
        [ -x "$p" ] && { echo "$p"; return; }
    done
    command -v npx 2>/dev/null || { echo "ERROR: npx not found" >&2; exit 1; }
}

# Resolve tsx — prefer direct binary to avoid npx overhead on restart paths.
resolve_tsx() {
    for p in \
        /opt/homebrew/bin/tsx \
        /usr/local/bin/tsx \
        "$HOME/.nvm/versions/node/$(ls "$HOME/.nvm/versions/node/" 2>/dev/null | sort -V | tail -1)/bin/tsx" \
        "$HOME/.volta/bin/tsx"
    do
        [ -x "$p" ] && { echo "$p"; return; }
    done
    return 1  # fall through to npx tsx
}

PORT=7846

# Kill any stale process holding port 7846. Defensive: only kill if the
# holder is running credential-proxy.ts (same script); avoids killing
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

# Resolve and run.
TSX_BIN=$(resolve_tsx 2>/dev/null) || true
if [ -n "${TSX_BIN:-}" ]; then
    exec "$TSX_BIN" "$PROXY_SCRIPT"
else
    NPX_BIN=$(resolve_npx)
    exec "$NPX_BIN" tsx "$PROXY_SCRIPT"
fi
