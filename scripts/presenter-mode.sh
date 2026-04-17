#!/usr/bin/env bash
# presenter-mode.sh — silence Sutando's notification channels during a talk.
#
# Writes a sentinel file `state/presenter-mode.sentinel` containing an ISO
# timestamp for when the mode expires. Scripts that would otherwise ping the
# owner (discord-bridge poll_proactive, check-pending-questions, etc.) MUST
# check `is_presenter_mode_active()` and skip when active. Bridges poll the
# file on their own schedule — no signals, no coordination, just a file.
#
# Why: ICLR 2026-04-26 talk. While the owner is on screen presenting, any
# Discord DM / Telegram message / cron notification / voice-agent proactive
# would be a visible distraction — worst case, an audience-visible pop-up.
# Presenter mode is a single toggle that mutes all of them for N minutes,
# with a hard deadline so we can't leave it on after the talk ends.
#
# Usage:
#   bash scripts/presenter-mode.sh start [minutes]   # default 30
#   bash scripts/presenter-mode.sh stop
#   bash scripts/presenter-mode.sh status
#
# The sentinel file is gitignored (lives under state/) and is removed by
# `stop` or auto-expires at the ISO timestamp inside it. Any script reading
# it must handle a stale sentinel (ignore if expired).

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SENTINEL="$REPO/state/presenter-mode.sentinel"
mkdir -p "$REPO/state"

cmd="${1:-status}"

case "$cmd" in
	start)
		minutes="${2:-30}"
		# BSD date (macOS) and GNU date differ on +<minutes>M; compute epoch instead.
		now_epoch=$(date +%s)
		expire_epoch=$((now_epoch + minutes * 60))
		# Format ISO-8601 with trailing Z, both BSD and GNU support -u + -j (BSD) or -u -d (GNU).
		if date -u -r "$expire_epoch" +%Y-%m-%dT%H:%M:%SZ >/dev/null 2>&1; then
			expire_iso=$(date -u -r "$expire_epoch" +%Y-%m-%dT%H:%M:%SZ)
		else
			expire_iso=$(date -u -d "@$expire_epoch" +%Y-%m-%dT%H:%M:%SZ)
		fi
		echo "$expire_iso" > "$SENTINEL"
		echo "presenter-mode active until $expire_iso (${minutes} min)"
		echo "  Muted: discord-bridge proactive, check-pending-questions, voice proactive"
		echo "  Stop early: bash $0 stop"
		;;
	stop)
		if [ -f "$SENTINEL" ]; then
			rm -f "$SENTINEL"
			echo "presenter-mode stopped; notifications restored"
		else
			echo "presenter-mode was not active"
		fi
		;;
	status)
		if [ ! -f "$SENTINEL" ]; then
			echo "presenter-mode: inactive"
			exit 0
		fi
		expire_iso=$(cat "$SENTINEL")
		# Compare as strings — ISO-8601 with Z suffix sorts correctly.
		now_iso=$(date -u +%Y-%m-%dT%H:%M:%SZ)
		if [[ "$now_iso" > "$expire_iso" ]]; then
			echo "presenter-mode: expired at $expire_iso (sentinel stale, removing)"
			rm -f "$SENTINEL"
			exit 0
		fi
		echo "presenter-mode: ACTIVE until $expire_iso"
		;;
	*)
		echo "Usage: $0 {start [minutes] | stop | status}" >&2
		exit 1
		;;
esac
