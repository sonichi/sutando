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
activity_transition() {
	local to="$1" filename="$2"
	[ -n "${TASKS_DIR:-}" ] && [ -f "$TASKS_DIR/$filename" ] || return 0
	local bus="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/activity_bus.py"
	[ -f "$bus" ] || return 0
	# Stamped now, so a QUEUED that lands after its RUNNING is reconciled by time, not dropped.
	( "${SUTANDO_PY_BIN:-python3}" "$bus" transition "$to" --task-file "$TASKS_DIR/$filename" --ts "$(date +%s)" >/dev/null 2>&1 & ) 2>/dev/null || true
	return 0
}
activity_cancel_target() {
	local filename="$1" target
	[ -n "${TASKS_DIR:-}" ] && [ -f "$TASKS_DIR/$filename" ] || return 0
	local bus="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/activity_bus.py"
	[ -f "$bus" ] || return 0
	target="$(grep -oE 'CANCEL_INSTRUCTION:[[:space:]]*stop processing[[:space:]]+task-[A-Za-z0-9._-]+' "$TASKS_DIR/$filename" 2>/dev/null | grep -oE 'task-[A-Za-z0-9._-]+$' | head -1)"
	[ -n "$target" ] || return 0
	( "${SUTANDO_PY_BIN:-python3}" "$bus" transition CANCELLED --task-id "$target" --reason "cancel requested" >/dev/null 2>&1 & ) 2>/dev/null || true
	return 0
}
queued_activity_row() { activity_transition QUEUED "$1"; activity_cancel_target "$1"; }
emit_task_file() {
	local filename="$1"
	queued_activity_row "$filename"
	printf 'TASK_FILE: %s\n' "$filename" >&9 && { activity_transition RUNNING "$filename"; return 0; }
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
	activity_transition RUNNING "$filename"
}

emit_fallback_task_file() {
	local filename="$1"
	queued_activity_row "$filename"
	printf 'TASK_FILE: %s\n' "$filename" && { activity_transition RUNNING "$filename"; return 0; }
	echo "watch-tasks-stream: FAILED to emit TASK_FILE for $filename on stdout after a handler fallback (rc=$?); the fallback file is written but the live core was not told" >&2
	return 0
}
