#!/bin/bash
# Task routing gate — sourceable so a test can drive it without a watcher.
# Sourcing defines these functions and nothing else; call task_route_init once.
#
# The task directory is shared, but a task file names who owns it. Handing
# every appearance to every session put the filtering in prose the agent was
# asked to follow, so a session with no acquisition step could execute a task
# concurrently with the worker it was routed to.

# Resolve this session's routing identity and whether workers are installed.
# A worker's id comes from the launchd plist that started it; a session
# without one is not a pool member.
task_route_init() {
	POOL_WORKER="${SUTANDO_POOL_WORKER:-}"
	# NUMERIC only, matching the installer's com.sutando.core-<N> plists and the
	# same gate in core-status.sh: a main core carries 'legacy', not a seat.
	if [ -z "$POOL_WORKER" ]; then
		case "${SUTANDO_CORE_ID-}" in
			'' | *[!0-9]* ) ;;
			* ) POOL_WORKER="core-${SUTANDO_CORE_ID}" ;;
		esac
	fi
	# Same glob startup.sh uses to decide whether a lead is needed, and
	# install-core-pool.sh to decide what to tear down — one signal, not a
	# third opinion. No array: bash 3.2 under `set -u`.
	local agents plist
	agents="${SUTANDO_POOL_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
	POOL_INSTALLED=0
	for plist in "$agents"/com.sutando.core-[0-9]*.plist; do
		if [ -e "$plist" ]; then
			POOL_INSTALLED=1
		fi
		break
	done
}

# True when this session may act on the named task file.
#
#   worker id present    unassigned tasks, plus its OWN assignments
#   none, workers live   nothing — the queue is routed, and executing from
#                        it is the double-run
#   none, no workers     unassigned only (the single-session case, unchanged)
#
# A claimed file is never routed anywhere: it is already executing in its
# claimer's session, and recovering one whose claimer died is the lead's job.
task_routed_here() {
	case "$1" in
		*.claimed-*.txt)
			return 1
			;;
		*.assigned-*.txt)
			if [ -z "${POOL_WORKER:-}" ]; then
				return 1
			fi
			if [ "$1" = "${1%.assigned-*}.assigned-${POOL_WORKER}.txt" ]; then
				return 0
			fi
			return 1
			;;
	esac
	if [ -z "${POOL_WORKER:-}" ] && [ "${POOL_INSTALLED:-0}" -eq 1 ]; then
		return 1
	fi
	return 0
}
