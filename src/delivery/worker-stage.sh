#!/bin/bash
# One pool delivery stage writer for the Claude watcher and Codex notifier.
# Usage: write_worker_stage <task filename> <pending|done|abandon> <workspace> <python>
write_worker_stage() {
  local task_id="${1%.txt}" stage="$2" ws="$3" py="$4"
  local writer="${SUTANDO_POOL_DELIVERY_SCRIPT:-}" reason=""
  # A best-effort done failure returns success; callers use this bit before pruning.
  WORKER_STAGE_WRITE_SUCCEEDED=0

  # The live core has no recipient record. A worker must have a usable writer:
  # without `pending`, a result could arrive with no attribution beside it.
  [ -n "${SUTANDO_INSTANCE_ID:-}" ] || return 0
  if [ ! -f "$writer" ]; then
    reason="pool delivery script is missing"
  elif [ -z "$py" ]; then
    reason="resolved Python interpreter is missing"
  elif "$py" "$writer" \
    --workspace "$ws" --recipient "$SUTANDO_INSTANCE_ID" \
    mark-done --task-id "$task_id" --stage "$stage" >/dev/null; then
    WORKER_STAGE_WRITE_SUCCEEDED=1
    return 0
  else
    reason="pool delivery script exited nonzero"
  fi

  printf 'worker-stage: could not record %s for %s (%s): %s\n' \
    "$stage" "$task_id" "$SUTANDO_INSTANCE_ID" "$reason" >&2
  # A published result already exists by `done`; logging is enough. `pending`
  # and `abandon` failures remain visible to callers so they can fail closed.
  [ "$stage" = done ]
}
