#!/bin/bash
# Shared policy for both runtime launchers: a managed task notifier starts its
# own watcher independent of the core session's own /startup Step 1.7, so it
# needs the SAME synchronous, fail-closed backfill boundary Step 1.7 enforces
# -- otherwise a notifier-delivered task can reach the unrestricted core
# before an existing pool's routing declaration is published (keweichen's
# review on PR #4503, round 5: an executable harness showed both launchers
# still `tmux new-session` the notifier even when the sweep path fails).
#
# Provider I/O stays at the edge: resolving $SUTANDO_POOL_BOOT_SWEEP (a
# skill's manifest.json "config" block) is each ADAPTER's job, done ONCE at
# launcher startup -- same as every other manifest-config key -- never here.
# A per-call resolution inside the gate would re-glob skills/*/manifest.json
# and spawn a python3 subprocess on every ensure_task_notifier() invocation
# (Codex alone has three call sites), which is exactly the redundant-copy
# shape this file exists to avoid, just moved from "policy" to "resolution".

# $1: absolute path to a runnable python3 interpreter.
# Returns 0 to proceed, non-zero to refuse starting the notifier. Prints its
# own diagnostic to stderr on refusal -- never on the skip-silently path.
notifier_boot_gate() {
  local py="$1" rc
  [ -n "${SUTANDO_POOL_BOOT_SWEEP:-}" ] || return 0   # same "skip silently" contract as Step 1.7
  [ -n "$py" ] || return 0   # no interpreter to run the sweep with -- ensure_task_notifier's own PY check already refused separately
  "$py" "$SUTANDO_POOL_BOOT_SWEEP" --workspace "$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" --sweep --no-persist >&2
  rc=$?
  if [ "$rc" -ne 0 ]; then
    # FATAL, not merely absent: the core process itself is fine, but this run
    # has NO task-notifier intake path -- a caller (Sutando.app, a script
    # wrapping this launcher) must not read the launcher's own exit code alone
    # as "fully up" (keweichen's review on PR #4503, round 7: the failure must
    # be observable, not just the watcher's absence).
    echo "FATAL notifier-boot-gate: boot-time pool sweep exited $rc (not 0) -- refusing to start the task notifier's watcher, since there is no proof a live pool's routing declaration exists. The core is running WITHOUT a notifier intake path. Investigate the sweep's stderr, then re-run the launcher." >&2
    return 1
  fi
  return 0
}
