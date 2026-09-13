#!/bin/bash
# Inbox-entry resolver — sourceable so a test can invoke it in isolation.
# Sourcing this file defines resolve_inbox_entry and nothing else.

# An inbox entry need not be the task body: a recipient can be woken by a
# sentinel whose payload lives elsewhere, and only the adapter that wrote it
# knows the mapping — so the core runs the executable it was handed. The
# resolver MUST print an ABSOLUTE path: it is checked with `-f` against the
# watcher's own cwd, not the resolver's, so a relative answer is refused
# rather than dispatched (fails safe, but silently — name it if you hit it).
# Prints the path to dispatch (rc 0), or nothing and why on stderr (rc 3).
#
# Bounded, not because a resolver is untrusted, but because it runs inline in
# the single-threaded dispatch loop: a hung resolver would hang every future
# dispatch, for every entry, not just its own.
SUTANDO_INBOX_RESOLVER_TIMEOUT="${SUTANDO_INBOX_RESOLVER_TIMEOUT:-5}"
resolve_inbox_entry() {
	local entry="$1" out rc resolved
	if [ -z "${SUTANDO_INBOX_RESOLVER:-}" ]; then
		printf '%s\n' "$entry"
		return 0
	fi
	if [ ! -x "$SUTANDO_INBOX_RESOLVER" ]; then
		echo "watch-tasks-stream: SUTANDO_INBOX_RESOLVER names no executable ($SUTANDO_INBOX_RESOLVER); refusing to dispatch $entry unresolved" >&2
		return 3
	fi
	# Two explicit branches, not an optionally-empty array: `"${arr[@]}"` on an
	# empty array raises "unbound variable" under `set -u` on bash < 4.4 (the
	# macOS-shipped /bin/bash is 3.2), which would make a clean host with no
	# `timeout` binary fail this call instead of just skipping the bound.
	if command -v timeout >/dev/null 2>&1; then
		out="$(timeout "$SUTANDO_INBOX_RESOLVER_TIMEOUT" "$SUTANDO_INBOX_RESOLVER" "$entry" 2>/dev/null)"
	else
		out="$("$SUTANDO_INBOX_RESOLVER" "$entry" 2>/dev/null)"
	fi
	rc=$?
	# First line only, and it must BE a file: a resolver that printed a banner
	# ahead of its answer has not answered, and must not pass as one.
	resolved="$(printf '%s\n' "$out" | head -1)"
	if [ "$rc" -ne 0 ] || [ -z "$resolved" ] || [ ! -f "$resolved" ]; then
		echo "watch-tasks-stream: resolver $SUTANDO_INBOX_RESOLVER did not name an existing file for $entry (rc=$rc, first line: ${resolved:-<empty>}); not dispatching it" >&2
		return 3
	fi
	printf '%s\n' "$resolved"
	return 0
}
