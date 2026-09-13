#!/bin/bash
# Inbox-entry resolver — sourceable so a test can invoke it in isolation.
# Sourcing this file defines resolve_inbox_entry and nothing else.

# Only the adapter that wrote a sentinel knows where its payload lives, so
# the core just runs the executable it was handed.

# The resolver MUST print an ABSOLUTE path: checked with `-f` against the
# watcher's cwd, not the resolver's, so a relative one fails safe, silently.

# Bounded: this runs inline in the single-threaded dispatch loop, so a hung
# resolver would hang every future dispatch, not just its own.
SUTANDO_INBOX_RESOLVER_TIMEOUT="${SUTANDO_INBOX_RESOLVER_TIMEOUT:-5}"
resolve_inbox_entry() {
	local entry="$1" out rc resolved not_absolute out_file resolver_pid watchdog_pid
	if [ -z "${SUTANDO_INBOX_RESOLVER:-}" ]; then
		printf '%s\n' "$entry"
		return 0
	fi
	if [ ! -x "$SUTANDO_INBOX_RESOLVER" ]; then
		echo "watch-tasks-stream: SUTANDO_INBOX_RESOLVER names no executable ($SUTANDO_INBOX_RESOLVER); refusing to dispatch $entry unresolved" >&2
		return 3
	fi
	# Two explicit branches, not an optionally-empty array: `"${arr[@]}"` on an
	# empty array raises "unbound variable" under `set -u` on bash < 4.4.
	if command -v timeout >/dev/null 2>&1; then
		out="$(timeout "$SUTANDO_INBOX_RESOLVER_TIMEOUT" "$SUTANDO_INBOX_RESOLVER" "$entry" 2>/dev/null)"
		rc=$?
	else
		# No GNU timeout on this host (e.g. macOS's shipped /bin): bound it by
		# hand, a background killer racing the resolver, so this path is not
		# "bounded" in name only.
		out_file="$(mktemp)"
		"$SUTANDO_INBOX_RESOLVER" "$entry" > "$out_file" 2>/dev/null &
		resolver_pid=$!
		# The sleep runs in the BACKGROUND of the watchdog under a TERM trap: bash
		# defers a signal while a foreground child runs, so a foreground sleep made
		# `wait "$watchdog_pid"` below stall every successful resolution to the full
		# deadline. TERM then KILL, because a resolver that traps TERM is otherwise
		# unbounded -- the same escalation the startup reaper uses.
		( trap 'kill "$_s" 2>/dev/null; exit 0' TERM
		  sleep "$SUTANDO_INBOX_RESOLVER_TIMEOUT" & _s=$!; wait "$_s"
		  kill -TERM "$resolver_pid" 2>/dev/null; sleep 1
		  kill -KILL "$resolver_pid" 2>/dev/null ) &
		watchdog_pid=$!
		wait "$resolver_pid" 2>/dev/null
		rc=$?
		kill -TERM "$watchdog_pid" 2>/dev/null
		wait "$watchdog_pid" 2>/dev/null
		out="$(cat "$out_file" 2>/dev/null)"
		rm -f "$out_file"
	fi
	# First line only, and it must BE a file: a resolver that printed a banner
	# ahead of its answer has not answered, and must not pass as one.
	resolved="$(printf '%s\n' "$out" | head -1)"
	# The contract says ABSOLUTE; a relative one that happens to name a file
	# in the watcher's own cwd would otherwise pass `-f` and misdispatch.
	case "$resolved" in
		/*) not_absolute=0 ;;
		*) not_absolute=1 ;;
	esac
	if [ "$rc" -ne 0 ] || [ -z "$resolved" ] || [ "$not_absolute" -eq 1 ] || [ ! -f "$resolved" ]; then
		echo "watch-tasks-stream: resolver $SUTANDO_INBOX_RESOLVER did not name an existing ABSOLUTE file for $entry (rc=$rc, first line: ${resolved:-<empty>}); not dispatching it" >&2
		return 3
	fi
	printf '%s\n' "$resolved"
	return 0
}
