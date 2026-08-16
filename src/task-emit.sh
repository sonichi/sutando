#!/bin/bash
# Shutdown-path TASK_FILE emitter — sourceable so a test can invoke it in isolation.
# The caller owns fd 9; sourcing this file defines one function and nothing else.

# Emit one task filename on the caller's stable stdout duplicate (fd 9).
# Never fatal: a failed shutdown emit must not abort the remaining cleanup.
emit_task_file() {
	printf 'TASK_FILE: %s\n' "$1" >&9 || true
}
