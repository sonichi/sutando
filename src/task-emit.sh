#!/bin/bash
# Shutdown-path TASK_FILE emitter — sourceable so it can be tested in isolation.
#
# The caller owns fd 9 (`exec 9>&1` before anything can rebind fd 1). This unit
# only writes to it. Keeping the write here rather than inline is what lets a
# test invoke the real emitter one command-substitution deep, which is the only
# shape that reproduces the delivery bug: inside a $( ), fd 1 is the capture
# pipe, so an emit on fd 1 is written successfully into a discarded string.
#
# Sourcing this file must have no side effects — it defines one function.

# Emit one task filename on the caller's stable stdout duplicate (fd 9).
# Never fatal: a failed shutdown emit must not abort the remaining cleanup.
emit_task_file() {
	printf 'TASK_FILE: %s\n' "$1" >&9 || true
}
