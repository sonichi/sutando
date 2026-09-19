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

# A stealer unlinks only a stamp that is ITSELF abandoned — a fresh holder's is
# younger — and never removes the directory: an emptied one is taken by rename.
sentinel_lock_steal_abandoned() {
  local lock="$1" stamp
  for stamp in "$lock"/held.*; do
    [ -e "$stamp" ] || continue
    sentinel_lock_abandoned "$stamp" && rm -f "$stamp"
  done
}

# rename(2): atomic, and onto an existing directory only when that directory is
# EMPTY. `mv` would move the source INSIDE an existing target instead.
_sentinel_rename_dir() {
  local here py
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  # shellcheck source=../scripts/python-binary.sh
  . "$here/../scripts/python-binary.sh" || return 1
  py="$(require_python "$here/.." "take the sentinel lock")" || return 1
  "$py" -c 'import os, sys; os.rename(sys.argv[1], sys.argv[2])' "$1" "$2" 2>/dev/null
}

# A lock holds its taker's stamp <lock>/held.<token> from the instant it exists
# (built privately, renamed in), so a held lock is never empty for a rename to take.
SENTINEL_LOCK_TOKEN=""
sentinel_lock_acquire() {
  local lock deadline token tmp
  lock="$(sentinel_lock_path "$1")"
  deadline=$(( $(date +%s) + ${2:-10} ))
  token="$$.$(date +%s).${RANDOM:-0}${RANDOM:-0}"
  tmp="$(mktemp -d "${lock}.acq.XXXXXX")" || return 1
  : > "$tmp/held.$token" || { rm -rf "$tmp"; return 1; }
  while :; do
    if _sentinel_rename_dir "$tmp" "$lock"; then
      SENTINEL_LOCK_TOKEN="$token"
      return 0
    fi
    if sentinel_lock_abandoned "$lock"; then
      sentinel_lock_steal_abandoned "$lock"
      continue
    fi
    [ "$(date +%s)" -lt "$deadline" ] || { rm -rf "$tmp"; return 1; }
    sleep 0.05
  done
}

# Own stamp first, then the directory only if that left it empty: `rmdir`
# refuses one another taker has since renamed into place.
sentinel_lock_release() {
  local lock
  lock="$(sentinel_lock_path "$1")"
  [ -n "$SENTINEL_LOCK_TOKEN" ] && rm -f "$lock/held.$SENTINEL_LOCK_TOKEN"
  SENTINEL_LOCK_TOKEN=""
  rmdir "$lock" 2>/dev/null || true
}

# An incarnation names one START of one pid: `<epoch>-<pid>-<random>`. The pid
# inside is what lets a repair prove the live marker was written by that pid.
sentinel_new_incarnation() {   # <pid>
  printf '%s-%s-%s%s' "$(date +%s)" "$1" "${RANDOM:-0}" "${RANDOM:-0}"
}

# The pid an incarnation embeds, or empty when it is not of that shape.
sentinel_incarnation_pid() {   # <incarnation>
  local inc="$1" rest
  rest="${inc#*-}"
  [ "$rest" != "$inc" ] || return 1
  rest="${rest%%-*}"
  case "$rest" in ''|*[!0-9]*) return 1 ;; esac
  printf '%s' "$rest"
}

# The ONE record grammar. Both writers print through here; a second spelling is
# a second shape the strict reader will refuse.
_sentinel_record_body() {   # <pid> <instance> <inc> <code> <ver> <ws>
  printf '%s\n' "$1"
  printf 'instance=%s\n' "$2"
  printf 'incarnation=%s\n' "$3"
  printf 'code_path=%s\n' "$4"
  printf 'version=%s\n' "$5"
  printf 'started_at=%s\n' "$(date +%s)"
  printf 'workspace=%s\n' "$6"
}

# Temp+rename both files, marker first: a reader must never find a record whose
# code_path or marker has not landed, and would refuse a watcher that IS ours.
sentinel_write_record() {   # <pid_file> <pid> <instance> <inc> <code> <ver> <ws>
  local pid_file="$1" inc_file tmp inc_tmp
  inc_file="$(sentinel_incarnation_path "$pid_file")"
  tmp="$(mktemp "${pid_file}.new.XXXXXX")" || return 1
  inc_tmp="$(mktemp "${inc_file}.new.XXXXXX")" || { rm -f "$tmp"; return 1; }
  _sentinel_record_body "$2" "$3" "$4" "$5" "$6" "$7" > "$tmp" || { rm -f "$tmp" "$inc_tmp"; return 1; }
  printf '%s\n' "$4" > "$inc_tmp" || { rm -f "$tmp" "$inc_tmp"; return 1; }
  mv -f "$inc_tmp" "$inc_file" || { rm -f "$tmp" "$inc_tmp"; return 1; }
  mv -f "$tmp" "$pid_file" || { rm -f "$tmp"; return 1; }
  return 0
}

# Repair writer (health-check --fix): same grammar and lock; link(2) publishes, so an existing record wins.
# rc 0 written (stdout = the incarnation, taken from the live marker only when it embeds <pid>), 1 write failed, 3 exists, 4 no lock, 5 no marker of <pid>.
sentinel_stamp_absent() {   # <pid_file> <pid> <code> <ver> <ws>
  local pid_file="$1" pid="$2" marker inc inc_pid instance tmp rc=1
  marker="$(sentinel_incarnation_path "$pid_file")"
  instance="$(sentinel_instance_from_path "$pid_file")"
  sentinel_lock_acquire "$pid_file" || return 4
  if [ -e "$pid_file" ]; then
    sentinel_lock_release "$pid_file"; return 3
  fi
  inc="$(head -n1 "$marker" 2>/dev/null | tr -d '[:space:]' || true)"
  inc_pid="$(sentinel_incarnation_pid "$inc" 2>/dev/null || true)"
  if [ -z "$inc" ] || [ "$inc_pid" != "$pid" ]; then
    sentinel_lock_release "$pid_file"; return 5
  fi
  if tmp="$(mktemp "${pid_file}.new.XXXXXX")" \
     && _sentinel_record_body "$pid" "$instance" "$inc" "$3" "$4" "$5" > "$tmp"; then
    if ln "$tmp" "$pid_file" 2>/dev/null; then
      rc=0; printf '%s\n' "$inc"
    elif [ -e "$pid_file" ]; then
      rc=3
    fi
  fi
  [ -n "${tmp:-}" ] && rm -f "$tmp"
  sentinel_lock_release "$pid_file"
  return "$rc"
}

# The ONE conditional release, under the lock every publisher takes. The record
# goes only when it still names <pid> AND <inc>: a successor is a new incarnation
# even when the OS handed it the dead watcher's pid. <inc> empty = a pre-identity
# record, where the pid is all the file claims. The marker goes only with its own
# record. rc 0 released or already absent, 1 another watcher's record holds the
# path, 2 ours but not removable, 4 no lock.
sentinel_release_incarnation() {   # <pid_file> <pid> [<inc>]
  local pid_file="$1" pid="$2" inc="${3:-}" marker recorded held live rc=0
  marker="$(sentinel_incarnation_path "$pid_file")"
  sentinel_lock_acquire "$pid_file" || return 4
  if [ -f "$pid_file" ]; then
    held="$(sentinel_pid_in "$pid_file" 2>/dev/null || true)"
    recorded="$(sentinel_field_in "$pid_file" incarnation 2>/dev/null || true)"
    if [ "$held" != "$pid" ] || { [ -n "$inc" ] && [ "$recorded" != "$inc" ]; }; then
      rc=1
    else
      sentinel_release_if_owner "$pid_file" "$pid"
      [ -f "$pid_file" ] && rc=2
    fi
  fi
  if [ "$rc" -eq 0 ] && [ -n "$inc" ]; then
    live="$(head -n1 "$marker" 2>/dev/null | tr -d '[:space:]' || true)"
    [ "$live" = "$inc" ] && rm -f "$marker"
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

# Executed, not sourced: the repair path stamps and withdraws through THIS file.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  case "${1:-}" in
    stamp) shift; sentinel_stamp_absent "$@"; exit $? ;;
    release) shift; sentinel_release_incarnation "$@"; exit $? ;;
    *) echo "usage: watcher_sentinel.sh stamp <pid_file> <pid> <code_path> <version> <workspace>" >&2
       echo "       watcher_sentinel.sh release <pid_file> <pid> [<incarnation>]" >&2; exit 2 ;;
  esac
fi
