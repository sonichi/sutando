#!/bin/bash
# TASK_FILE emitters — sourceable so a test can invoke them in isolation.
# The caller owns fd 9; sourcing this file defines these functions and nothing else.

# Emit one task filename on the caller's stable stdout duplicate (fd 9).
# Never fatal: a failed shutdown emit must not abort the remaining cleanup — but
# it must not be silent either, or a dropped line leaves no trace anywhere.
emit_task_file() {
	local filename="$1"
	printf 'TASK_FILE: %s\n' "$filename" >&9 && return 0
	echo "watch-tasks-stream: FAILED to emit TASK_FILE for $filename on fd 9 (rc=$?); the task file is written but the live core was not told" >&2
	return 0
}

# The handler-failed fallback, NOT a shutdown emit: it runs in normal drain on
# real stdout, so it must not borrow the shutdown emitter's fd 9.
emit_fallback_task_file() {
	local filename="$1"
	printf 'TASK_FILE: %s\n' "$filename" && return 0
	echo "watch-tasks-stream: FAILED to emit TASK_FILE for $filename on stdout after a handler fallback (rc=$?); the fallback file is written but the live core was not told" >&2
	return 0
}
