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
# LINE 1 is a bare pid, and stays that way: every reader takes the pid from it
# (health-check.py, services_status.py, the startup reaper, the tests). Lines 2+
# are optional `key=value` identity claims — instance, incarnation, code_path,
# version, started_at, workspace — which a signaller reads to establish that the
# process wearing that pid is the watcher THIS install started. A record carrying
# no such lines is a pre-identity sentinel and proves nothing beyond the number.

# --- the identity record -----------------------------------------------------
# READ in one place only: src/util_paths.py:read_sentinel_record, which
# src/watcher_identity.py turns into the ownership verdict. A shell copy of that
# grammar is the second parser this contract exists to prevent.

# The pid on line 1, through that reader. Empty + rc 1 when the file names none:
# `cat` fed whole records to `ps -p`, which answers "Invalid process id".
sentinel_pid_in() {
  local pid_file="$1" here py
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  # shellcheck source=../scripts/python-binary.sh
  . "$here/../scripts/python-binary.sh" || return 1
  py="$(require_python "$here/.." "read the watcher sentinel")" || return 1
  "$py" "$here/util_paths.py" sentinel-pid "$pid_file" 2>/dev/null
}

# One identity claim from lines 2+, through the SAME reader. Empty + rc 1 when
# the field is absent, so a pid-only sentinel yields nothing for every key.
sentinel_field_in() {
  local pid_file="$1" key="$2" here py
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  # shellcheck source=../scripts/python-binary.sh
  . "$here/../scripts/python-binary.sh" || return 1
  py="$(require_python "$here/.." "read the watcher sentinel")" || return 1
  "$py" "$here/util_paths.py" sentinel-field "$pid_file" "$key" 2>/dev/null
}

# The marker a live watcher exposes its incarnation through, beside its own
# sentinel: <stem>[-<instance>].pid -> <stem>[-<instance>].incarnation.
sentinel_incarnation_path() {
  local pid_file="$1"
  printf '%s' "${pid_file%.pid}.incarnation"
}

# The instance key ENCODED IN A RESOLVED SENTINEL PATH, never a raw id: that
# suffix is the canonical form the writer and every signaller both derive.
sentinel_instance_from_path() {
  local key
  key="$(basename "$1")"
  key="${key#"$WATCHER_SENTINEL_STEM"}"
  key="${key%.pid}"
  printf '%s' "${key#-}"
}

# --- the writer --------------------------------------------------------------

# Start and cleanup mutate two files that must agree, so they serialise on one
# mkdir lock — the repo's portable test-and-set, `flock(1)` being Linux-only.
sentinel_lock_path() {
  printf '%s' "${1%.pid}.lock"
}

# Abandoned = untouched for a full minute (`find -mmin`, whose unit is minutes)
# — a DIFFERENT number from the ${2:-10}s a caller waits to acquire, below.
sentinel_lock_abandoned() {
  find "$1" -maxdepth 0 -mmin +1 2>/dev/null | grep -q .
}

# A holder killed mid-section would wedge every later start, so an ABANDONED
# lock is removed under a SECOND lock, which only one stealer can hold.
sentinel_lock_acquire() {
  local lock steal deadline
  lock="$(sentinel_lock_path "$1")"
  steal="${lock}.steal"
  deadline=$(( $(date +%s) + ${2:-10} ))
  while ! mkdir "$lock" 2>/dev/null; do
    if sentinel_lock_abandoned "$lock"; then
      if mkdir "$steal" 2>/dev/null; then
        # RE-probe under the steal lock. `$lock` is never renamed away, so a
        # winner that took it since the probe above is still there to be seen.
        if sentinel_lock_abandoned "$lock"; then
          rm -rf "$lock"
        fi
        rmdir "$steal" 2>/dev/null || true
        continue
      fi
      # A stealer killed between those two lines wedges the steal, not the lock.
      if sentinel_lock_abandoned "$steal"; then
        rm -rf "$steal"
      fi
    fi
    [ "$(date +%s)" -lt "$deadline" ] || return 1
    sleep 0.05
  done
  return 0
}

sentinel_lock_release() {
  rmdir "$(sentinel_lock_path "$1")" 2>/dev/null || true
}

# Temp+rename both files, marker first: a reader must never find a record whose
# code_path or marker has not landed, and would refuse a watcher that IS ours.
sentinel_write_record() {   # <pid_file> <pid> <instance> <inc> <code> <ver> <ws>
  local pid_file="$1" inc_file tmp inc_tmp
  inc_file="$(sentinel_incarnation_path "$pid_file")"
  tmp="$(mktemp "${pid_file}.new.XXXXXX")" || return 1
  inc_tmp="$(mktemp "${inc_file}.new.XXXXXX")" || { rm -f "$tmp"; return 1; }
  {
    printf '%s\n' "$2"
    printf 'instance=%s\n' "$3"
    printf 'incarnation=%s\n' "$4"
    printf 'code_path=%s\n' "$5"
    printf 'version=%s\n' "$6"
    printf 'started_at=%s\n' "$(date +%s)"
    printf 'workspace=%s\n' "$7"
  } > "$tmp" || { rm -f "$tmp" "$inc_tmp"; return 1; }
  printf '%s\n' "$4" > "$inc_tmp" || { rm -f "$tmp" "$inc_tmp"; return 1; }
  mv -f "$inc_tmp" "$inc_file" || { rm -f "$tmp" "$inc_tmp"; return 1; }
  mv -f "$tmp" "$pid_file" || { rm -f "$tmp"; return 1; }
  return 0
}

# cleanup()'s release, under the lock a start takes. The marker holds no pid, so
# only its own content says whether a live successor wrote it. rc 1 = it did.
sentinel_release_incarnation() {
  local pid_file="$1" pid="$2" inc="$3" marker recorded live rc=0
  marker="$(sentinel_incarnation_path "$pid_file")"
  sentinel_lock_acquire "$pid_file" || return 1
  recorded="$(sentinel_field_in "$pid_file" incarnation 2>/dev/null || true)"
  if [ -n "$inc" ] && [ -f "$pid_file" ] && [ "$recorded" != "$inc" ]; then
    sentinel_lock_release "$pid_file"
    return 1
  fi
  sentinel_release_if_owner "$pid_file" "$pid"
  live="$(head -n1 "$marker" 2>/dev/null | tr -d '[:space:]' || true)"
  if [ -z "$inc" ] || [ "$live" = "$inc" ]; then
    rm -f "$marker"
  else
    rc=1
  fi
  sentinel_lock_release "$pid_file"
  return "$rc"
}

# --- naming ------------------------------------------------------------------
# The stem only. The per-instance SUFFIX is not computed here: src/util_paths.py
# owns it and delegates to src/runtime-api/instance_key.py, so a shell mirror
# would be a second implementation of one contract.
WATCHER_SENTINEL_STEM="watch-tasks-stream"

# The sentinel THIS process writes. $1 = state dir. Asks the Python owner, which
# reads SUTANDO_INSTANCE_ID and the enrolled actor exactly as the run dir does.
# A failure is fatal: guessing a path here is how two instances share one file.
# $2, when given, names ANOTHER instance's sentinel instead of this process's —
# the only way a caller can address a worker without guessing the filename.
sentinel_path_for() {
  local state_dir="$1" instance="${2:-}" here out
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  local py
  # shellcheck source=../scripts/python-binary.sh
  . "$here/../scripts/python-binary.sh" || return 1
  py="$(require_python "$here/.." "resolve the watcher sentinel")" || return 1
  if ! out="$("$py" "$here/util_paths.py" watcher-sentinel "$state_dir" ${instance:+"$instance"})"; then
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
  # Through the process-ops seam when a caller has loaded one (restart.sh), so a
  # test's injected fake reaches this read too; the direct `ps` is the default.
  if command -v pops_elapsed >/dev/null 2>&1; then
    raw="$(pops_elapsed "$pid" | tr -d ' ')"
  else
    raw="$(ps -p "$pid" -o etime= 2>/dev/null | tr -d ' ')" || return 1
  fi
  [ -n "$raw" ] || return 1
  printf '%s' "$raw" | awk -F'[-:]' '{
    if (NF == 4)      print ($1*86400) + ($2*3600) + ($3*60) + $4
    else if (NF == 3) print ($1*3600) + ($2*60) + $3
    else if (NF == 2) print ($1*60) + $2
    else              print ""
  }'
}

# PRECONDITION: the published mtime IS the stamp time. sentinel_write_record
# renames a temp made moments earlier; a file staged long before reaps nothing.
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
  # Line 1 only: the identity lines below it are claims ABOUT the owner, not the
  # owner's name, and comparing the whole file would never match a record.
  content="$(head -n1 "$claim" 2>/dev/null | tr -d '[:space:]' || true)"

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
