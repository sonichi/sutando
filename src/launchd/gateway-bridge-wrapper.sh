#!/bin/bash
# Wrapper for the launchd-managed ag2.space gateway bridge
# (src/remote-gateway-bridge.py).
#
# Why a wrapper (not a bare ProgramArguments python3 call): the bridge needs
# REMOTE_TASK_TOKEN (legacy AG2_REMOTE_TOKEN), which lives in the ag2space
# channel .env — NOT in the environment launchd hands the job. launchd doesn't
# source shell profiles or .env files, so we resolve + load the channel .env
# here, map the legacy token names to the ones the bridge reads, then exec the
# bridge. Mirrors the credential-proxy-wrapper.sh pattern.
#
# Called by com.sutando.gateway-bridge.plist as the ProgramArguments entry so
# the launchd job tracks THIS pid and KeepAlive restarts the bridge on death —
# the fix for the 2026-07-10 incident where the bridge died and stayed dead for
# 3 days with nothing restarting it (mobile messages stranded in the cloud).

set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"

# python3 resolves via PATH. The launchd plist sets PATH to
# "__BREW_BIN__:/usr/bin:/bin:/usr/sbin:/sbin", where __BREW_BIN__ is the
# interpreter dir the installer resolved from its own `command -v python3` — so
# the bridge runs under the same interpreter the installer validated, with no
# clone-, arch-, or user-specific fallback probe baked into this committed file.
if ! command -v python3 >/dev/null 2>&1; then
    echo "[gateway-bridge-wrapper] no python3 on PATH (check the plist PATH)" >&2
    exit 1
fi

# Resolve + load the ag2space channel .env (holds REMOTE_TASK_TOKEN). Honor
# $CLAUDE_CONFIG_DIR if the plist exports it (claude-sutando installs); the
# config helper falls back to ~/.claude otherwise.
if _RELAY_ENV="$(bash "$REPO/scripts/sutando-config.sh" claude-home-path channels/ag2space/.env 2>/dev/null)"; then
    [ -f "$_RELAY_ENV" ] && { set -a; . "$_RELAY_ENV"; set +a; }
fi

# Map legacy AG2_REMOTE_* → REMOTE_TASK_* (the names the bridge reads).
# Default tier is "owner" for the personal-agent model (PR #2018, 2026-07-08): a
# user's own gateway authenticates with their own owner bearer and the broker
# owner-scopes every pull, so its tasks are the owner's own. This MUST match
# src/startup.sh's inline launch and the bridge's own default — otherwise the
# launchd path would sandbox the owner's own mobile messages as team-tier
# (read-only). A shared / multi-user gateway sets REMOTE_TASK_TIER=team in the
# channel .env explicitly, which this honors.
REMOTE_TASK_TOKEN="${REMOTE_TASK_TOKEN:-${AG2_REMOTE_TOKEN:-}}"
REMOTE_TASK_TIER="${REMOTE_TASK_TIER:-${AG2_REMOTE_TIER:-owner}}"
# AG2 Space tags inbound image/file markers `ag2space-media`; the provider-
# neutral bridge defaults to `remote-media`, so without this the marker never
# matches and media URLs land unresolved in task bodies (owner-reported
# 2026-07-25; fixed on main in startup.sh's launch block). launchd jobs never
# see startup.sh's exports, so this AG2-specific launch site must default it
# too. Explicit REMOTE_MEDIA_MARKER from the channel .env still wins.
REMOTE_MEDIA_MARKER="${REMOTE_MEDIA_MARKER:-ag2space-media}"
export REMOTE_TASK_TOKEN REMOTE_TASK_TIER REMOTE_MEDIA_MARKER

# The supervised path must fence exactly like the bare launch in
# startup-runtime.sh; without this the default lane claims named-lane rooms.
# shellcheck source=../gateway-foreign-suffixes.sh
. "$REPO/src/gateway-foreign-suffixes.sh"
GATEWAY_FOREIGN_SUFFIXES="$(derive_foreign_suffixes)"
export GATEWAY_FOREIGN_SUFFIXES

# If there's still no token, the bridge would FATAL-exit and KeepAlive would
# crash-loop. Exit 0 quietly instead — the install path only loads this job when
# a token is configured, so reaching here means the token was removed after
# install; don't hammer the system, just stop cleanly (launchd honors the clean
# exit under our KeepAlive.SuccessfulExit=false policy).
if [ -z "$REMOTE_TASK_TOKEN" ]; then
    echo "[gateway-bridge-wrapper] no REMOTE_TASK_TOKEN configured — nothing to run; exiting cleanly." >&2
    exit 0
fi

# Evict an already-running gateway bridge that belongs to THIS checkout: it has no
# single-instance lock, so a straggler would double-poll and double-process.
_EVICT_HELPER="$REPO/src/launchd/evict-own-bridge.sh"
if [ -f "$_EVICT_HELPER" ]; then
  # shellcheck source=evict-own-bridge.sh
  . "$_EVICT_HELPER"
  # Same script path serves every gateway instance in this checkout, so scope by
  # GATEWAY_INSTANCE too — unset here means the primary/prod gateway.
  evict_own_bridge "remote-gateway" "$REPO" "GATEWAY_INSTANCE" "${GATEWAY_INSTANCE:-}"
  sleep 0.3
fi

exec python3 "$REPO/src/remote-gateway-bridge.py"
