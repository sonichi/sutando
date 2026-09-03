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

# `command -v` proves a PATH entry EXISTS; on a Mac without the Xcode CLT that
# entry is Apple's stub, and merely running it raises the install dialog.
# shellcheck source=../../scripts/python-binary.sh
. "$REPO/scripts/python-binary.sh"
PYBIN="$(require_python "$REPO" "run the ag2.space gateway bridge")" || exit 1

# Resolve + load the ag2space channel .env (holds REMOTE_TASK_TOKEN). Honor
# $CLAUDE_CONFIG_DIR if the plist exports it (claude-sutando installs); the
# config helper falls back to ~/.claude otherwise.
if _RELAY_ENV="$(bash "$REPO/scripts/sutando-config.sh" claude-home-path "channels/${REMOTE_TASK_CHANNEL_DIR:-ag2space}/.env" 2>/dev/null)"; then
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

# The bridge resolves env -> .env -> VAULT (_token_from_vault_ag2space, which
# reuses channel_token.token_from_vault), so an .env-only gate here is NARROWER
# than the thing it gates: a vault-only token parks the job before the bridge
# ever gets the chance to resolve it. Ask the same shared resolver the bridge
# would. rc 0 = usable, 3 = definitively absent; any other rc means the resolver
# itself is unrunnable, so fall through to the .env answer rather than taking
# the bridge down on a resolver bug.
_TOKEN_PRESENT="$REMOTE_TASK_TOKEN"
if [ -z "$_TOKEN_PRESENT" ]; then
    _tok_rc=0
    "$PYBIN" "$REPO/src/channel_token.py" --gateway >/dev/null 2>&1 || _tok_rc=$?
    if [ "$_tok_rc" -eq 0 ]; then
        _TOKEN_PRESENT="resolver"
    elif [ "$_tok_rc" -ne 3 ]; then
        echo "[gateway-bridge-wrapper] token resolver failed (rc=$_tok_rc) — using the lane file check" >&2
        [ -f "$_RELAY_ENV" ] && grep -qE '^(REMOTE_TASK_TOKEN|AG2_REMOTE_TOKEN)=.+' "$_RELAY_ENV" \
            && _TOKEN_PRESENT="lane-file"
    fi
fi

# If there's still no token, the bridge would FATAL-exit and KeepAlive would
# crash-loop. Exit 0 quietly instead — the install path only loads this job when
# a token is configured, so reaching here means the token was removed after
# install; don't hammer the system, just stop cleanly (launchd honors the clean
# exit under our KeepAlive.SuccessfulExit=false policy).
if [ -z "$_TOKEN_PRESENT" ]; then
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

exec "$PYBIN" "$REPO/src/remote-gateway-bridge.py"
