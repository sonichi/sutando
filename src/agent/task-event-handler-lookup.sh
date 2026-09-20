#!/bin/bash
# Optional capability lookup for the watcher's task-event handler. A skill that
# provides one DECLARES it in its own manifest.json:
#
#     {"config": {"SUTANDO_TASK_EVENT_HANDLER_SCRIPT": "scripts/route_handler.py"}}
#
# skill-relative, and it must stay inside the declaring skill. This helper names
# no skill: exactly one declarer wins; none sets nothing; several is ambiguous
# and sets nothing, loudly, so the operator pins SUTANDO_TASK_EVENT_HANDLER.
#
# MIGRATION: a published executable at skills/<skill>/task-event-handler was the
# previous contract and is NO LONGER RESOLVED. It could not be shipped -- it is
# gitignored and had to be created at runtime -- so a correct checkout resolved
# nothing until something published it. A skill that still publishes one and
# declares no config key is named on stderr rather than silently ignored.
#
# resolve_task_event_handler <repo>  -> prints the path (rc 0) | rc 1 none | rc 2 cannot tell

TASK_EVENT_HANDLER_CAPABILITY="SUTANDO_TASK_EVENT_HANDLER_SCRIPT"

# Kept as a -c program, not a heredoc inside a command substitution: the nested
# form works but re-nests wrongly under a small edit, and does so silently.
read -r -d '' __TASK_EVENT_HANDLER_PROG <<'PYPROG' || true
import json, os, sys
repo, key = sys.argv[1], sys.argv[2]
skills = os.path.join(repo, "skills")
declared_by = set()
for name in sorted(os.listdir(skills) if os.path.isdir(skills) else []):
    manifest = os.path.join(skills, name, "manifest.json")
    try:
        with open(manifest, encoding="utf-8") as fh:
            config = json.load(fh).get("config") or {}
    except (OSError, ValueError):
        continue
    declared = config.get(key) if isinstance(config, dict) else None
    if not isinstance(declared, str) or not declared:
        continue
    path = declared if os.path.isabs(declared) else os.path.join(skills, name, declared)
    # realpath, not normpath: normpath folds `..` but follows no symlink, and the
    # executable test does, so a link out of the skill would pass a string check.
    path = os.path.realpath(path)
    own = os.path.realpath(os.path.join(skills, name))
    if not path.startswith(own + os.sep):
        print(f"task-event-handler: {manifest}: {key} escapes its skill", file=sys.stderr)
        continue
    if os.access(path, os.X_OK):
        print(path)
        declared_by.add(name)
for name in sorted(os.listdir(skills) if os.path.isdir(skills) else []):
    legacy = os.path.join(skills, name, "task-event-handler")
    if name not in declared_by and os.path.exists(legacy):
        print(f"task-event-handler: {legacy}: the published-file contract is no longer "
              f"resolved; declare {key} in {name}/manifest.json", file=sys.stderr)
PYPROG

# Cheap staleness probe for the cache below: existence + mtime of every
# skills/*/manifest.json, concatenated. An add/edit/remove changes this string,
# so a fingerprint match is exactly as fresh as a full rescan -- only cheaper
# when nothing changed.
#
# ONE batched `stat` call, not one per file: an early version stat'd each
# manifest in a per-file loop (up to two spawns per file, GNU attempt then BSD
# fallback) and measured SLOWER than the python resolve it was meant to avoid
# -- spawn count, not per-file work, is what costs here. `-c` (GNU) is a clean,
# fast failure on BSD (`illegal option`), unlike the `-f`-means-something-else
# trap watcher_sentinel.sh documents, so an exit-code check is enough here.
_task_event_handler_fingerprint() {
  local repo="$1" skills="$repo/skills" out="" files=() f
  for f in "$skills"/*/manifest.json; do
    [ -e "$f" ] && files+=("$f")
  done
  [ "${#files[@]}" -eq 0 ] && { printf '\n'; return 0; }
  out="$(stat -c '%n %Y' -- "${files[@]}" 2>/dev/null)"
  [ -n "$out" ] || out="$(stat -f '%N %m' -- "${files[@]}" 2>/dev/null)"
  printf '%s\n' "$out"
}

# In-process cache for resolve_task_event_handler's two SETTLED outcomes.
# Scoped to this sourced file, so it lives exactly as long as the watcher
# process that sourced it -- never written to disk, never read by another
# process. This is not the boot-time-freeze bug the "call fresh for every
# task" comment above guards against: that bug SKIPPED calling this function
# at all past boot. Every call here still runs, still re-checks the fingerprint
# against the real filesystem, and a changed fingerprint always forces a real
# rescan -- see tests/task-event-handler-live-resolution.test.py's same-process
# case for the property this must not break.
_SUTANDO_TEH_CACHE_FP=""
_SUTANDO_TEH_CACHE_RC=""
_SUTANDO_TEH_CACHE_OUT=""

# resolve_task_event_handler <repo> -> path (rc 0) | rc 1 none | rc 2 cannot tell
#
# An interpreter that cannot run takes rc 2, the fail-closed code: a reader that
# cannot read is not evidence that nobody declared one.
resolve_task_event_handler() {
  local repo="$1" py="${SUTANDO_PY_BIN:-python3}" out prc h fp

  fp="$(_task_event_handler_fingerprint "$repo")"
  if [ -n "$_SUTANDO_TEH_CACHE_RC" ] && [ "$fp" = "$_SUTANDO_TEH_CACHE_FP" ]; then
    [ "$_SUTANDO_TEH_CACHE_RC" -eq 0 ] && printf '%s\n' "$_SUTANDO_TEH_CACHE_OUT"
    return "$_SUTANDO_TEH_CACHE_RC"
  fi

  out="$("$py" -c "$__TASK_EVENT_HANDLER_PROG" "$repo" "$TASK_EVENT_HANDLER_CAPABILITY")"
  prc=$?
  if [ "$prc" -ne 0 ]; then
    printf 'task-event-handler: cannot read skill manifests (%s exited %s); refusing to report "none"\n' "$py" "$prc" >&2
    # Not cached: an interpreter failure is an execution-environment problem the
    # manifest fingerprint cannot observe, so a fixed interpreter must be
    # re-tried on the very next call, never held stale behind an unrelated key.
    return 2
  fi
  set --
  while IFS= read -r h; do
    [ -n "$h" ] && set -- "$@" "$h"
  done <<EOF
$out
EOF
  case $# in
    0)
      _SUTANDO_TEH_CACHE_FP="$fp"; _SUTANDO_TEH_CACHE_RC=1; _SUTANDO_TEH_CACHE_OUT=""
      return 1
      ;;
    1)
      _SUTANDO_TEH_CACHE_FP="$fp"; _SUTANDO_TEH_CACHE_RC=0; _SUTANDO_TEH_CACHE_OUT="$1"
      printf '%s\n' "$1"
      return 0
      ;;
    *)
      # Not cached: ambiguity is a misconfiguration dispatch_task refuses to
      # route around (rc 2 below), so every dispatch re-warns until an operator
      # fixes it -- continuous visibility matters more than latency here, and
      # this is the rare/broken case, not the one this change targets.
      printf 'task-event-handler: %s skills declare %s (%s); set SUTANDO_TASK_EVENT_HANDLER explicitly\n' "$#" "$TASK_EVENT_HANDLER_CAPABILITY" "$*" >&2
      return 2
      ;;
  esac
}

# task_event_handler <repo> -> path (rc 0) | rc 1 none/broken pin | rc 2 cannot tell
#
# Call fresh for every task, never once at process start or cached in a var
# that outlives the call: an explicit pin always wins over resolution. This is
# a rule for CALLERS (a launcher exporting the result into a long-lived env var
# is exactly the boot-time-freeze bug tests/task-event-handler-live-resolution
# .test.py guards); it is not violated by resolve_task_event_handler's own
# internal fingerprint cache above, which still runs and still re-checks the
# real filesystem on every single call -- it only skips the python spawn when
# that check proves nothing changed.
task_event_handler() {
  local repo="$1"
  if [ -n "${SUTANDO_TASK_EVENT_HANDLER:-}" ]; then
    [ -x "$SUTANDO_TASK_EVENT_HANDLER" ] || return 1
    printf '%s\n' "$SUTANDO_TASK_EVENT_HANDLER"
    return 0
  fi
  resolve_task_event_handler "$repo"
}
