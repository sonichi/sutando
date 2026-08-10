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

# Ensure `node` is on PATH. npx/tsx are launched by absolute path below, but
# they re-exec `node` via `#!/usr/bin/env node`, so the node install dir must be
# on PATH or the job dies with exit 127 ("env: node: No such file or directory").
# launchd's plist PATH is a fixed guess (often /opt/homebrew/bin on Apple Silicon)
# and won't match an Intel-Homebrew (/usr/local/bin) or version-manager install —
# so heal it here rather than depend on the plist being right for every machine.
#
# Resolve exactly ONE node dir, first match wins, in the same priority order (and
# with the same NEWEST-nvm selection) as resolve_npx/resolve_tsx below. The nvm
# candidate MUST use `sort -V | tail -1`, NOT a `*/bin` glob: glob order is
# lexicographic, so with prepend-then-continue a box with v9 + v10 would pick v9
# (`'1' < '9'` sorts v10 first, v9 last-wins) — an older node, not the latest.
# REPO_ROOT is resolved HERE (before the loop) so the app-bundle node dir is
# CONFIG-RESOLVED via sutando-config.sh app-node-dir (honors $SUTANDO_APP_NODE_DIR)
# on the supervised launchd path too — not just src/startup.sh (Codex #2154).
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
for _node_cand in \
    "${SUTANDO_NODE:-}" \
    /opt/homebrew/bin/node \
    /usr/local/bin/node \
    "$HOME/.nvm/versions/node/$(ls "$HOME/.nvm/versions/node/" 2>/dev/null | sort -V | tail -1)/bin/node" \
    "$HOME/.volta/bin/node" \
    "$(bash "$REPO_ROOT/scripts/sutando-config.sh" app-node-dir)/node"
do
    [ -x "$_node_cand" ] || continue
    _node_dir="$(dirname "$_node_cand")"
    case ":$PATH:" in *":$_node_dir:"*) ;; *) PATH="$_node_dir:$PATH"; export PATH ;; esac
    break   # first (highest-priority) node wins — don't stack multiple node dirs
done

# Prefer THIS checkout's copy so the proxy resolves the same workspace as the core;
# launchd inherits no env, so claude-home falls back to ~/.claude unless the plist pins it.
PROXY_SCRIPT="$REPO_ROOT/skills/quota-tracker/scripts/credential-proxy.ts"
if [ ! -f "$PROXY_SCRIPT" ]; then
    PROXY_SCRIPT="$(bash "$REPO_ROOT/scripts/sutando-config.sh" claude-home-path skills/quota-tracker/scripts/credential-proxy.ts)"
fi

# Test probe: print the resolved target and exit, so the suite can assert launchd-env
# resolution without exec'ing a real proxy. No production caller passes arguments.
if [ "${1:-}" = "--resolve-only" ]; then
    echo "$PROXY_SCRIPT"
    exit 0
fi

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
# The repo's own node_modules/.bin/tsx comes FIRST: it's the version the repo
# pins, and it's the only tsx on a host with no homebrew/nvm/volta node at all
# (the app-bundle runtime ships bare `node` — the PATH heal above covers the
# `#!/usr/bin/env node` re-exec).
resolve_tsx() {
    # Single source of truth: scripts/sutando-config.sh tsx-bin (repo-pinned
    # node_modules/.bin/tsx first, then global locations). Prints the path or
    # nothing; return 1 on empty lets the caller fall through to `npx tsx`.
    _tsx="$(bash "$REPO_ROOT/scripts/sutando-config.sh" tsx-bin 2>/dev/null)"
    [ -n "$_tsx" ] && { echo "$_tsx"; return 0; }
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

# G1.5 node-bundle (Codex re-review F1b): the wrapper enforces the SAME
# resolver + fail-closed + mode contract as startup.sh. node-bin exits 1 on a
# set-but-invalid SUTANDO_NODE — that is a desktop packaging error and the
# job must FAIL (KeepAlive backs off on ThrottleInterval), never silently
# slide to tsx/npx on whatever node the host has. Bundled context = explicit
# env OR this wrapper running from the packaged engine copy (repo inside the
# engine root that owns the at-rest runtime); in that context the dist
# artifact is REQUIRED.
NODE_BIN="$(bash "$REPO_ROOT/scripts/sutando-config.sh" node-bin)" || {
    echo "credential-proxy-wrapper: SUTANDO_NODE set but invalid — desktop packaging error; fail-closed (no PATH/tsx fallback)" >&2
    exit 78
}
_W_APP_NODE_DIR="$(bash "$REPO_ROOT/scripts/sutando-config.sh" app-node-dir)"
_W_ENGINE_ROOT="${_W_APP_NODE_DIR%/node/bin}"; _W_ENGINE_ROOT="${_W_ENGINE_ROOT%/runtime}"
_W_BUNDLED=0
if [ -n "${SUTANDO_NODE:-}" ]; then
    _W_BUNDLED=1
elif [ -x "$_W_APP_NODE_DIR/node" ] && [ "${REPO_ROOT#"$_W_ENGINE_ROOT"/}" != "$REPO_ROOT" ]; then
    _W_BUNDLED=1
fi
DIST_PROXY="$REPO_ROOT/dist/credential-proxy.js"
if [ "$_W_BUNDLED" = "1" ]; then
    if [ ! -f "$DIST_PROXY" ]; then
        echo "credential-proxy-wrapper: bundled mode but $DIST_PROXY missing — desktop packaging error; fail-closed" >&2
        exit 78
    fi
    exec "$NODE_BIN" "$DIST_PROXY"
fi

# Resolve and run.
TSX_BIN=$(resolve_tsx 2>/dev/null) || true
if [ -n "${TSX_BIN:-}" ]; then
    exec "$TSX_BIN" "$PROXY_SCRIPT"
else
    NPX_BIN=$(resolve_npx)
    exec "$NPX_BIN" tsx "$PROXY_SCRIPT"
fi
