#!/bin/bash
# Shared boot-time admission gate for both runtime launchers' task notifiers.
# A notifier starts its own watcher independent of core's /startup Step 1.7,
# so it needs the same synchronous, fail-closed backfill boundary.

# Provider I/O stays at the edge: resolving $SUTANDO_POOL_BOOT_SWEEP is each
# adapter's own job, done once at launcher startup -- never here, to avoid
# re-globbing skills/*/manifest.json on every call.

# $1: absolute path to a runnable python3 interpreter.
# Returns 0 to proceed, non-zero to refuse starting the notifier. Prints its
# own diagnostic to stderr on refusal -- never on the skip-silently path.
notifier_boot_gate() {
  local py="$1" rc
  [ -n "${SUTANDO_POOL_BOOT_SWEEP:-}" ] || return 0
  [ -n "$py" ] || return 0
  "$py" "$SUTANDO_POOL_BOOT_SWEEP" --workspace "$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" --sweep --no-persist >&2
  rc=$?
  if [ "$rc" -ne 0 ]; then
    # A caller must not read the launcher's own exit code alone as "fully
    # up" -- the core can be fine while this run has no notifier intake path.
    echo "FATAL notifier-boot-gate: boot-time pool sweep exited $rc (not 0) -- refusing to start the task notifier's watcher, since there is no proof a live pool's routing declaration exists. The core is running WITHOUT a notifier intake path. Investigate the sweep's stderr, then re-run the launcher." >&2
    return 1
  fi
  return 0
}
