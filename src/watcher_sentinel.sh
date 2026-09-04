#!/bin/bash
# Ownership protocol for state/watch-tasks-stream.pid — the ONE writer contract.
#
# The sentinel names the watcher that owns it. Two callers remove it, and they
# ask DIFFERENT questions, which is why a single "atomic compare-and-delete"
# would fix one and silently leave the other broken:
#
#   watch-tasks-stream.sh cleanup()  — "is this still MY file?" It compares
#     against its own $$ while running, and a live pid cannot be reused, so the
#     value is unambiguous. What it lacks is atomicity: read, compare, unlink is
#     a window in which another watcher can stamp and lose its sentinel.
#
#   startup.sh reap_stale_task_watcher() — "is the pid in this file the watcher
#     that WROTE it?" That pid may belong to a process that already exited and
#     whose number the OS reissued. The bytes are identical either way, so no
#     amount of atomicity can answer it.
#
# The discriminator for the second question is the OS, not the file: a process
# that started AFTER the sentinel was written cannot be the one that wrote it.
# Elapsed time (`ps -o etime=`) is used rather than an absolute start time
# because `date -j -f` is BSD-only and CI runs ubuntu.
#
# The sentinel format is untouched — a bare pid. Three readers int() the whole
# file (health-check.py, services_status.py, and the tests), so a richer token
# would convert this bug into a different false "watcher is broken" signal.

# --- naming ------------------------------------------------------------------
# The stem only. The per-instance SUFFIX is not computed here: src/util_paths.py
# owns it and delegates to src/runtime-api/instance_key.py, so a shell mirror
# would be a second implementation of one contract.
WATCHER_SENTINEL_STEM="watch-tasks-stream"

# The sentinel THIS process writes. $1 = state dir. Asks the Python owner, which
# reads SUTANDO_INSTANCE_ID and the enrolled actor exactly as the run dir does.
# A failure is fatal: guessing a path here is how two instances share one file.
sentinel_path_for() {
  local state_dir="$1" here out
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if ! out="$(python3 "$here/util_paths.py" watcher-sentinel "$state_dir")"; then
    echo "watcher_sentinel: could not resolve the sentinel path" >&2
    return 1
  fi
  printf '%s' "$out"
}

# Every sentinel present, historic name first, one per line. A caller that asks
# about one file has asked about one watcher; on a pool host the others are
# equally real.
sentinel_paths_in() {
  local state_dir="$1" p
  [ -f "$state_dir/$WATCHER_SENTINEL_STEM.pid" ] && printf '%s/%s.pid\n' "$state_dir" "$WATCHER_SENTINEL_STEM"
  for p in "$state_dir/$WATCHER_SENTINEL_STEM"-*.pid; do
    [ -f "$p" ] && printf '%s\n' "$p"
  done
  return 0
}

# Seconds of elapsed time for a pid, or empty when it cannot be determined.
# `etime` is [[DD-]HH:]MM:SS on both BSD and GNU ps.
sentinel_pid_elapsed() {
  local pid="$1" raw
  raw="$(ps -p "$pid" -o etime= 2>/dev/null | tr -d ' ')" || return 1
  [ -n "$raw" ] || return 1
  printf '%s' "$raw" | awk -F'[-:]' '{
    if (NF == 4)      print ($1*86400) + ($2*3600) + ($3*60) + $4
    else if (NF == 3) print ($1*3600) + ($2*60) + $3
    else if (NF == 2) print ($1*60) + $2
    else              print ""
  }'
}

# PRECONDITION: the owning process creates the sentinel IN PLACE. A writer that
# builds it elsewhere and moves it in preserves the old mtime, and this stops
# reaping anything.
#
# True when <pid> could have written <pid_file>: it must have been alive when the
# file was stamped. A process younger than the file is a REISSUED pid — a
# different process wearing the dead owner's number.
#
# Fails SAFE. Anything unmeasurable (no ps, no stat, unparseable) returns true,
# so an unanswerable question never authorises killing or unlinking; the cost is
# leaving a stale sentinel one more boot, which is recoverable. The reverse
# would signal a live watcher.
# Tri-state, because "unmeasurable" is not "yes": rc 0 = this pid wrote the file,
# rc 1 = it demonstrably did not (reissued pid), rc 2 = UNKNOWN. Returning 0 for
# unknown made the reaper read it as confirmed ownership and kill a live watcher.
sentinel_pid_wrote_file() {
  local pid="$1" pid_file="$2" elapsed mtime now age
  local slack="${SUTANDO_SENTINEL_SLACK_SEC:-2}"

  elapsed="$(sentinel_pid_elapsed "$pid")" || return 2
  case "$elapsed" in ''|*[!0-9]*) return 2 ;; esac   # non-numeric => UNKNOWN, never "owner"

  # `stat -f %m` is BSD "modification time"; on GNU `-f` means FILESYSTEM status
  # and SUCCEEDS with a human-readable block, so an `||` chain never reaches the
  # GNU form and $mtime becomes text. Measured on the ubuntu runner: the value
  # started with "File:" and `$(( now - mtime ))` died as `File: unbound
  # variable`. macOS passed because BSD is correct there — the GNU path was
  # never exercised locally. So validate the RESULT rather than trusting the
  # exit status: a command that succeeds at a different question is the failure.
  mtime="$(stat -c %Y "$pid_file" 2>/dev/null || true)"
  case "$mtime" in ''|*[!0-9]*) mtime="$(stat -f %m "$pid_file" 2>/dev/null || true)" ;; esac
  case "$mtime" in ''|*[!0-9]*) return 2 ;; esac   # mtime unreadable => UNKNOWN

  now="$(date +%s)"
  age=$(( now - mtime ))

  # The true owner starts, then stamps, so elapsed >= age always. Only call it a
  # reissued pid when it is CLEARLY younger than the file.
  if [ "$(( elapsed + slack ))" -lt "$age" ]; then
    return 1
  fi
  return 0
}

# Remove <pid_file> only if it still names <expected_pid>, without a window in
# which a newly stamped sentinel can be destroyed.
#
# Claim by rename first: `mv` is atomic and exclusive, so exactly one caller wins
# and then inspects a copy nobody else can reach. After the claim the original
# path is FREE, so a watcher stamping concurrently creates its own file, and the
# restore below uses no-clobber precisely so it can never overwrite that.
sentinel_release_if_owner() {
  local pid_file="$1" expected_pid="$2" claim content
  [ -f "$pid_file" ] || return 0
  claim="${pid_file}.claim.$$"

  mv "$pid_file" "$claim" 2>/dev/null || return 0   # lost the race, or already gone
  content="$(cat "$claim" 2>/dev/null || true)"

  if [ "$content" = "$expected_pid" ]; then
    rm -f "$claim"
    return 0
  fi
  # Not ours. Put it back, but NEVER over a sentinel stamped since the claim.
  #
  # `mv -n` refuses silently: measured exit 0 with the SOURCE left in place when
  # the target exists. So the exit code cannot distinguish "restored" from
  # "refused" — test for the leftover claim instead, and drop it, because a
  # sentinel appearing at the path during our claim belongs to a live watcher.
  mv -n "$claim" "$pid_file" 2>/dev/null || true
  [ -e "$claim" ] && rm -f "$claim"
  return 0
}
