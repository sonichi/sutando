#!/bin/bash
# TASK_FILE emitters — sourceable so a test can invoke them in isolation.
# The caller owns fd 9; sourcing this file defines these functions and nothing else.

# Emit one task filename on the caller's stable stdout duplicate (fd 9).
# Never fatal: a failed shutdown emit must not abort the remaining cleanup — but
# it must not be silent either, or a dropped line leaves no trace anywhere.
# The message reached this device: a `queued` row for the room's card, before any turn has the task.
# Fire-and-forget through the skill's own writer (which decides whether the file names a room);
# it must never delay or fail the emit, and it is inert when TASKS_DIR is unset (tests, probes).
# The scheduler owns the task's lifecycle: QUEUED when the file lands, RUNNING once a live core was
# told, CANCELLED for the task a CANCEL_INSTRUCTION names. Through the activity bus (state, then rows),
# fire-and-forget: it can neither delay nor fail the emit, and it is inert when TASKS_DIR is unset.
# `$2` is a basename (joined to TASKS_DIR) or an absolute resolved path: rejoining
# an absolute one to TASKS_DIR would read an existing payload as missing.
_activity_task_file() {
	case "$1" in
		/*) printf '%s\n' "$1" ;;
		*) [ -n "${TASKS_DIR:-}" ] && printf '%s\n' "$TASKS_DIR/$1" ;;
	esac
}

activity_transition() {
	local to="$1" filename="$2" task_file
	task_file="$(_activity_task_file "$filename")"
	[ -n "$task_file" ] && [ -f "$task_file" ] || return 0
	local bus="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/activity_bus.py"
	[ -f "$bus" ] || return 0
	# Stamped now, so a QUEUED that lands after its RUNNING is reconciled by time, not dropped.
	( "${SUTANDO_PY_BIN:-python3}" "$bus" transition "$to" --task-file "$task_file" --ts "$(date +%s)" >/dev/null 2>&1 & ) 2>/dev/null || true
	return 0
}
activity_cancel_target() {
	local filename="$1" target task_file
	task_file="$(_activity_task_file "$filename")"
	[ -n "$task_file" ] && [ -f "$task_file" ] || return 0
	local bus="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/activity_bus.py"
	[ -f "$bus" ] || return 0
	target="$(grep -oE 'CANCEL_INSTRUCTION:[[:space:]]*stop processing[[:space:]]+task-[A-Za-z0-9._-]+' "$task_file" 2>/dev/null | grep -oE 'task-[A-Za-z0-9._-]+$' | head -1)"
	[ -n "$target" ] || return 0
	( "${SUTANDO_PY_BIN:-python3}" "$bus" transition CANCELLED --task-id "$target" --reason "cancel requested" >/dev/null 2>&1 & ) 2>/dev/null || true
	return 0
}
queued_activity_row() { activity_transition QUEUED "$1"; activity_cancel_target "$1"; }
# The second line of a dispatch, only when other tasks are waiting: `QUEUE: <n> pending after this`.
# The TASK_FILE line above it is byte-identical either way; the Monitor batches both into one
# notification. Synchronous (the count belongs to this dispatch), but never fatal and never a line
# it cannot vouch for: no counter, no python, a non-number — nothing is printed.
queue_line() {
	local filename="$1" task_file n
	task_file="$(_activity_task_file "$filename")"
	[ -n "$task_file" ] && [ -f "$task_file" ] || return 0
	local q="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/task_queue.py"
	[ -f "$q" ] || return 0
	# The count is what waits in THIS watcher's inbox: a worker's delivery folder holds its own
	# queue, while the payload it resolved to lives in the core's tasks/ beside everyone's.
	local inbox=()
	[ -z "${TASKS_DIR_ABS:-}" ] || inbox=(--inbox "$TASKS_DIR_ABS")
	n="$("${SUTANDO_PY_BIN:-python3}" "$q" waiting --task-file "$task_file" ${inbox[@]+"${inbox[@]}"} 2>/dev/null)" || return 0
	case "$n" in ''|*[!0-9]*) return 0 ;; esac
	[ "$n" -gt 0 ] && printf 'QUEUE: %s pending after this\n' "$n"
	return 0
}
emit_task_file() {
	local filename="$1"
	queued_activity_row "$filename"
	{ printf 'TASK_FILE: %s\n' "$filename" && queue_line "$filename"; } >&9 && { activity_transition RUNNING "$filename"; return 0; }
	echo "watch-tasks-stream: FAILED to emit TASK_FILE for $filename on fd 9 (rc=$?); the task file is written but the live core was not told" >&2
	return 0
}

# The handler-failed fallback, NOT a shutdown emit: it runs in normal drain on
# real stdout, so it must not borrow the shutdown emitter's fd 9.
# The watcher's ordinary dispatch: QUEUED was marked once by dispatch_task; here the live core is told
# (the printf failing means stdout is gone and the watcher exits, as before) and RUNNING follows.
emit_dispatch_task_file() {
	local filename="$1"
	printf 'TASK_FILE: %s\n' "$filename" || exit 0
	queue_line "$filename"
	activity_transition RUNNING "$filename"
}

emit_fallback_task_file() {
	local filename="$1"
	queued_activity_row "$filename"
	printf 'TASK_FILE: %s\n' "$filename" && { queue_line "$filename"; activity_transition RUNNING "$filename"; return 0; }
	echo "watch-tasks-stream: FAILED to emit TASK_FILE for $filename on stdout after a handler fallback (rc=$?); the fallback file is written but the live core was not told" >&2
	return 0
}
