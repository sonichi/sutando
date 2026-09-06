#!/bin/bash
# Dead-man's-switch ping: the outbound heartbeat proving this Mac is alive.
# Silence at the external monitor is the alert.

set -u

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"

# Shared resolver: a launchd PATH reaches the Xcode-CLT stub, which passes an
# existence check and raises the install dialog when run.
. "$REPO/scripts/python-binary.sh"
PY_BIN="$(resolve_python "$REPO")"

# Per-host label, lockstep with `_host_label()` in src/util_paths.py: the
# heartbeat file is per-host and both sides must agree on the name.
_host_label() {
    local env="${SUTANDO_HOST_LABEL:-${SUTANDO_HOST_OVERRIDE:-}}"
    if [ -n "$env" ]; then
        printf '%s\n' "$env"
        return
    fi
    local lhn=""
    if command -v scutil >/dev/null 2>&1; then
        lhn="$(scutil --get LocalHostName 2>/dev/null)"
    fi
    if [ -n "$lhn" ]; then
        printf '%s\n' "$lhn"
    else
        hostname | sed 's/\..*//'
    fi
}

URL="${HEALTHCHECKS_PING_URL:-}"
if [ -z "$URL" ]; then
    URL="$([ -n "$PY_BIN" ] && "$PY_BIN" "$REPO/skills/secret-vault/secret-vault.py" get HEALTHCHECKS_PING_URL 2>/dev/null || true)"
fi
if [ -z "$URL" ]; then
    # Not configured — installed-but-unarmed is a supported state.
    exit 0
fi

ALIVE="${SUTANDO_DEADMAN_ALIVE_FILE:-}"
if [ -z "$ALIVE" ]; then
    WORKSPACE="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" || WORKSPACE=""
    if [ -z "$WORKSPACE" ]; then
        echo "deadman-ping: workspace resolution failed — reporting core-down (/fail)" >&2
        WORKSPACE=""
    fi
    HOST="$(_host_label)"
    ALIVE="$WORKSPACE/state/cores/$HOST.alive"
fi

# Fresh heartbeat (<90s) = alive. Portable mtime via python3 (stat -f/-c differ
# across BSD/GNU). Missing file or unreadable mtime = down.
suffix="/fail"
if [ -f "$ALIVE" ]; then
    age="$([ -n "$PY_BIN" ] && "$PY_BIN" -c 'import os,sys,time; print(int(time.time() - os.path.getmtime(sys.argv[1])))' "$ALIVE" 2>/dev/null || echo 99999)"
    if [ "$age" -le 90 ] 2>/dev/null; then
        suffix=""
    fi
fi

if ! curl -fsS -m 10 --retry 2 -o /dev/null "$URL$suffix" 2>/dev/null; then
    echo "deadman-ping: curl to monitor endpoint failed (fail-open, exit 0)" >&2
fi
exit 0
