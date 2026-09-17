#!/usr/bin/env bash
# startup's watch-tasks-stream reaper must only delete the sentinel it inspected,
# signal only a pid it has PROVED is this checkout's watcher, and keep the
# sentinel unless that signal delivered and the exit was confirmed.
#
# Unlinking a sentinel this reap did not inspect strands a live watcher untrackable.
# Case 3 makes that window deterministic: the `ps` shim re-stamps the file mid-reap.
#
# The reaper is driven from a SANDBOX CHECKOUT (tests/fixtures/sandbox-checkout.sh):
# every src/ entry is the real one, linked, except watch-tasks-stream.sh, which is
# a sleeper. "This checkout's watcher" is therefore a process executing that
# sleeper, and a watcher at any other path is a foreign checkout's.
#
# Run: bash tests/startup-watcher-reaper-ownership.test.sh
# Exit: 0 = all pass, 1 = failure
set -uo pipefail

REAL_REPO="${REPO_UNDER_TEST:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
fails=0

ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s — %s\n' "$1" "${2:-}"; fails=$((fails + 1)); }

echo "startup watch-tasks-stream reaper ownership:"

TMP="$(mktemp -d)"
SPAWNED=""
cleanup() {
  for p in $SPAWNED; do kill "$p" 2>/dev/null; done
  rm -rf "$TMP"
}
trap cleanup EXIT

# shellcheck source=fixtures/sandbox-checkout.sh
. "$REAL_REPO/tests/fixtures/sandbox-checkout.sh"
SB="$TMP/checkout"
make_sandbox_checkout "$SB" "$REAL_REPO"
CODE="$SB/src/watch-tasks-stream.sh"
# The function under test is the production one, sourced, not a copy — from the
# sandbox, so sutando_repo_root() names the sandbox as this checkout.
REPO="$SB"
# shellcheck source=../src/startup-runtime.sh
source "$REPO/src/startup-runtime.sh"

# Bailing on a missing helper would make this suite skip on base rather than fail,
# proving nothing; the wiring assertions below are what name the defect pre-fix.
have_fn=1
if declare -F reap_stale_task_watcher > /dev/null; then
  ok "reap_stale_task_watcher is defined in src/startup-runtime.sh"
else
  bad "reap_stale_task_watcher is defined in src/startup-runtime.sh" "not found"
  have_fn=0
fi

# The sandbox is only a checkout if the reaper's own resolver says so.
if [ "$(sutando_repo_root)" = "$(cd -P "$SB" && pwd -P)" ]; then
  ok "harness: sutando_repo_root resolves the sandbox checkout"
else
  bad "harness: sutando_repo_root resolves the sandbox checkout" "got $(sutando_repo_root)"
fi

if [ "$have_fn" -eq 1 ]; then

dead_pid() {
  # A pid that is certainly not running: spawn and reap one.
  local p
  bash -c 'exit 0' &
  p=$!
  wait "$p" 2>/dev/null
  echo "$p"
}

# A complete record + marker naming <pid> as <code>'s watcher, under <dir>.
record() {   # record <sentinel> <pid> <code_path> <workspace>
  printf '%s\ninstance=\nincarnation=inc-%s\ncode_path=%s\nversion=test\nworkspace=%s\n' \
    "$2" "$2" "$3" "$4" > "$1"
  printf 'inc-%s\n' "$2" > "${1%.pid}.incarnation"
}
spawn() { local p; p="$(spawn_sandbox_watcher "$1")"; SPAWNED="$SPAWNED $p"; printf '%s' "$p"; }

# --- case 1: sentinel names a dead pid -> removed (pre-existing behavior) -----
f="$TMP/case1.pid"
echo "$(dead_pid)" > "$f"
out="$(reap_stale_task_watcher "$f" 2>&1)"
if [ ! -e "$f" ]; then
  ok "dead pid: sentinel removed"
else
  bad "dead pid: sentinel removed" "still present ($out)"
fi

# --- case 2: sentinel names a LIVE watcher of THIS checkout -> signaled, removed
# Real `ps` here: the process genuinely executes the checkout's script.
live="$(spawn "$CODE")"
mkdir -p "$TMP/case2"
f="$TMP/case2/watch-tasks-stream.pid"
record "$f" "$live" "$CODE" "$TMP/case2"
out="$(reap_stale_task_watcher "$f" 2>&1)"
wait "$live" 2>/dev/null
if [ ! -e "$f" ]; then
  ok "live stale watcher: sentinel removed"
else
  bad "live stale watcher: sentinel removed" "still present ($out)"
fi
if ! kill -0 "$live" 2>/dev/null; then
  ok "live stale watcher: signaled"
else
  kill -KILL "$live" 2>/dev/null
  bad "live stale watcher: signaled" "still running"
fi
case "$out" in *"reaped stale"*) ok "live stale watcher: the reap is reported" ;;
  *) bad "live stale watcher: the reap is reported" "got: $out" ;; esac

# --- case 3 (the guard): a live watcher re-stamps DURING the reap -------------
# The `ps` shim re-stamps where a real watcher would: after the read, before delete.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/ps" << 'SH'
#!/bin/bash
printf '%s' "$SHIM_NEW_OWNER" > "$SHIM_PID_FILE"
exit 1
SH
chmod +x "$TMP/bin/ps"

f="$TMP/case3.pid"
stale="$(dead_pid)"
echo "$stale" > "$f"
export SHIM_PID_FILE="$f" SHIM_NEW_OWNER=424242
out="$(PATH="$TMP/bin:$PATH" reap_stale_task_watcher "$f" 2>&1)"
unset SHIM_PID_FILE SHIM_NEW_OWNER

if [ -e "$f" ]; then
  ok "re-stamped mid-reap: live watcher's sentinel survives"
else
  bad "re-stamped mid-reap: live watcher's sentinel survives" \
    "deleted a sentinel the reap never inspected ($out)"
fi
if [ "$(cat "$f" 2>/dev/null)" = "424242" ]; then
  ok "re-stamped mid-reap: sentinel still names the live watcher"
else
  bad "re-stamped mid-reap: sentinel still names the live watcher" \
    "content='$(cat "$f" 2>/dev/null)'"
fi

# --- case 5: a pid-only sentinel names a LIVE watcher -> refuse ---------------
# Case 2 is the control: the SAME fixture carrying a record IS reaped.
legacy="$(spawn "$CODE")"
mkdir -p "$TMP/case5"
f5="$TMP/case5/watch-tasks-stream.pid"
echo "$legacy" > "$f5"
out5="$(reap_stale_task_watcher "$f5" 2>&1)"
if kill -0 "$legacy" 2>/dev/null && [ -f "$f5" ]; then
  ok "pid-only sentinel: live watcher untouched, sentinel left in place"
else
  bad "pid-only sentinel: live watcher untouched" "killed or unlinked ($out5)"
fi
case "$out5" in *"records a pid only"*) ok "pid-only sentinel: the refusal is reported" ;;
  *) bad "pid-only sentinel: the refusal is reported" "got: $out5" ;;
esac
kill "$legacy" 2>/dev/null

# --- case 4: no sentinel -> no-op, success ------------------------------------
if reap_stale_task_watcher "$TMP/absent.pid" > /dev/null 2>&1; then
  ok "absent sentinel: no-op, rc 0"
else
  bad "absent sentinel: no-op, rc 0" "non-zero rc"
fi

# --- case 6: the watcher path as a DATA argument of another program -----------
# A complete, current record names a pid whose argv merely CARRIES the script
# path. Containment matched it; the executed slot is python, not our watcher.
PY_BIN="$(. "$REAL_REPO/scripts/python-binary.sh"; resolve_python "$REAL_REPO")"
"$PY_BIN" -c 'import time; time.sleep(120)' "$CODE" >/dev/null 2>&1 & data_pid=$!
SPAWNED="$SPAWNED $data_pid"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  ps -p "$data_pid" -o args= 2>/dev/null | grep -q "watch-tasks-stream" && break; sleep 0.1
done
mkdir -p "$TMP/case6"
f6="$TMP/case6/watch-tasks-stream.pid"
record "$f6" "$data_pid" "$CODE" "$TMP/case6"
out6="$(reap_stale_task_watcher "$f6" 2>&1)"
if kill -0 "$data_pid" 2>/dev/null; then
  ok "data-argument argv: the process is NOT signalled"
else
  bad "data-argument argv: the process is NOT signalled" "it was killed ($out6)"
fi
[ -f "$f6" ] && ok "data-argument argv: the sentinel is retained" \
             || bad "data-argument argv: the sentinel is retained" "unlinked ($out6)"
case "$out6" in *"reaped"*) bad "data-argument argv: nothing claims a reap" "got: $out6" ;;
  *) ok "data-argument argv: nothing claims a reap" ;; esac
case "$out6" in *"argv:"*) ok "data-argument argv: the refusal names the executed-slot check" ;;
  *) bad "data-argument argv: the refusal names the executed-slot check" "got: $out6" ;; esac
kill "$data_pid" 2>/dev/null

# --- case 7: a self-consistent FOREIGN checkout ------------------------------
# A real watcher executing another checkout's script, with a record that names
# exactly that script, our instance and our workspace. Record and argv agree
# with each other; neither is this checkout's, and shared workspaces are allowed.
mkdir -p "$TMP/foreign/src" "$TMP/case7"
write_sandbox_watcher "$TMP/foreign/src/watch-tasks-stream.sh"
foreign="$(spawn "$TMP/foreign/src/watch-tasks-stream.sh")"
f7="$TMP/case7/watch-tasks-stream.pid"
record "$f7" "$foreign" "$TMP/foreign/src/watch-tasks-stream.sh" "$TMP/case7"
out7="$(reap_stale_task_watcher "$f7" 2>&1)"
if kill -0 "$foreign" 2>/dev/null; then
  ok "foreign checkout: its watcher is NOT signalled"
else
  bad "foreign checkout: its watcher is NOT signalled" "it was killed ($out7)"
fi
[ -f "$f7" ] && ok "foreign checkout: its sentinel is retained" \
             || bad "foreign checkout: its sentinel is retained" "unlinked ($out7)"
case "$out7" in *"code_path:"*"this checkout runs"*) ok "foreign checkout: the refusal names this checkout's script" ;;
  *) bad "foreign checkout: the refusal names this checkout's script" "got: $out7" ;; esac
kill "$foreign" 2>/dev/null
# The control for case 7 is case 2: the same record shape naming THIS checkout's
# script IS reaped, so the refusal above is the checkout comparison and not a
# harness that refuses everything.

# --- case 8: the signal FAILS -> nothing is reported reaped, sentinel stays ---
# Ownership confirms (the case-2 fixture again); only delivery fails. The
# sentinel is the only record of a watcher still running, so it must remain.
own8="$(spawn "$CODE")"
mkdir -p "$TMP/case8"
f8="$TMP/case8/watch-tasks-stream.pid"
record "$f8" "$own8" "$CODE" "$TMP/case8"
SIGLOG="$TMP/case8/signals"; : > "$SIGLOG"
pops_signal() { printf 'signal %s %s\n' "$1" "${2:-TERM}" >> "$SIGLOG"; return 1; }
out8="$(reap_stale_task_watcher "$f8" 2>&1)"
# shellcheck source=../src/process-ops.sh
. "$SB/src/process-ops.sh"            # the real seam back
grep -q "^signal $own8 TERM$" "$SIGLOG" \
  && ok "failed signal: the reaper did reach the signal (the case is not vacuous)" \
  || bad "failed signal: the reaper did reach the signal" "no signal recorded ($out8)"
[ -f "$f8" ] && ok "failed signal: the sentinel is RETAINED" \
             || bad "failed signal: the sentinel is RETAINED" "unlinked ($out8)"
case "$out8" in *"reaped"*) bad "failed signal: the output does NOT say reaped" "got: $out8" ;;
  *) ok "failed signal: the output does NOT say reaped" ;; esac
case "$out8" in *"SIGNAL FAILED"*) ok "failed signal: the failure is reported" ;;
  *) bad "failed signal: the failure is reported" "got: $out8" ;; esac
kill -0 "$own8" 2>/dev/null && ok "failed signal: the watcher is still running (the fake sent nothing)" \
  || bad "failed signal: the watcher is still running" "it died, so the fake was bypassed"
kill "$own8" 2>/dev/null

# --- case 9: the signal delivers but the watcher does not exit ---------------
own9="$(spawn "$CODE")"
mkdir -p "$TMP/case9"
f9="$TMP/case9/watch-tasks-stream.pid"
record "$f9" "$own9" "$CODE" "$TMP/case9"
pops_signal() { return 0; }            # "delivered", to a process that ignores it
out9="$(SUTANDO_WATCHER_STOP_TICKS=2 reap_stale_task_watcher "$f9" 2>&1)"
. "$SB/src/process-ops.sh"
[ -f "$f9" ] && ok "survived TERM: the sentinel is RETAINED" \
             || bad "survived TERM: the sentinel is RETAINED" "unlinked ($out9)"
case "$out9" in *"reaped"*) bad "survived TERM: the output does NOT say reaped" "got: $out9" ;;
  *"STILL ALIVE"*) ok "survived TERM: the output says the watcher is still alive" ;;
  *) bad "survived TERM: the output says the watcher is still alive" "got: $out9" ;; esac
kill "$own9" 2>/dev/null

# --- case 10: the Codex notifier's exact launch, `/bin/bash <script> <tasks-dir>`
# The operand after the script is what a flattened `ps` string cannot split; the
# seam's vector read is the real one here (KERN_PROCARGS2 / /proc), not a fake.
mkdir -p "$TMP/case10/tasks"
own10="$(spawn_notifier_watcher "$CODE" "$TMP/case10/tasks")"; SPAWNED="$SPAWNED $own10"
case "$(pops_argv "$own10")" in *"$CODE $TMP/case10/tasks"*) ok "notifier form: the flattened argv carries the operand (the case is not vacuous)" ;;
  *) bad "notifier form: the flattened argv carries the operand" "got: $(pops_argv "$own10")" ;; esac
want10="$("$PY_BIN" -c 'import json, sys; print(json.dumps(sys.argv[1:]))' /bin/bash "$CODE" "$TMP/case10/tasks")"
got10="$(pops_argv_vector "$own10")"; rc10=$?
if [ "$rc10" -eq 0 ] && [ "$got10" = "$want10" ]; then
  ok "notifier form: the real seam reads the kernel's argv LIST for a live process"
else
  bad "notifier form: the real seam reads the kernel's argv LIST" "rc=$rc10 got=$got10 want=$want10"
fi
f10="$TMP/case10/watch-tasks-stream.pid"
record "$f10" "$own10" "$CODE" "$TMP/case10"
out10="$(reap_stale_task_watcher "$f10" 2>&1)"
wait "$own10" 2>/dev/null
kill -0 "$own10" 2>/dev/null && bad "notifier form: the watcher is signalled" "still running ($out10)" \
                             || ok "notifier form: the watcher is signalled"
[ ! -e "$f10" ] && ok "notifier form: the sentinel is released" \
                || bad "notifier form: the sentinel is released" "still present ($out10)"
case "$out10" in *"reaped stale"*) ok "notifier form: the reap is reported" ;;
  *) bad "notifier form: the reap is reported" "got: $out10" ;; esac

# --- case 11: the core Monitor's launch, `bash <script> <tasks-dir>` ----------
mkdir -p "$TMP/case11/tasks"
own11="$(spawn "$CODE" "$TMP/case11/tasks")"
f11="$TMP/case11/watch-tasks-stream.pid"
record "$f11" "$own11" "$CODE" "$TMP/case11"
out11="$(reap_stale_task_watcher "$f11" 2>&1)"
wait "$own11" 2>/dev/null
kill -0 "$own11" 2>/dev/null && bad "Monitor form: the watcher is signalled" "still running ($out11)" \
                             || ok "Monitor form: the watcher is signalled"
[ ! -e "$f11" ] && ok "Monitor form: the sentinel is released" \
                || bad "Monitor form: the sentinel is released" "still present ($out11)"

# --- case 12: the vector reader on a pid that is gone -> nothing, rc 1 --------
gone="$(dead_pid)"
if out12="$(pops_argv_vector "$gone")"; then
  bad "dead pid: the vector reader fails, rather than inventing a list" "rc 0, printed: $out12"
else
  [ -z "$out12" ] && ok "dead pid: the vector reader prints nothing and fails" \
                  || bad "dead pid: the vector reader prints nothing" "printed: $out12"
fi

fi  # have_fn

# --- wiring: startup.sh must delegate, and reap only THIS instance -------------
# This used to assert `sentinel_paths_in` — the enumeration of EVERY instance's
# sentinel. The reaper deliberately kills a watcher that owns its sentinel, so
# that loop killed a peer's live watcher and removed its record.
if grep -q 'reap_stale_task_watcher "\$__sentinel"' "$REAL_REPO/src/startup.sh" \
   && grep -q 'sentinel_path_for "\$WORKSPACE/state"' "$REAL_REPO/src/startup.sh"; then
  ok "startup.sh delegates to the shared reaper for its OWN sentinel"
else
  bad "startup.sh delegates to the shared reaper" "call site not found"
fi
if grep -q 'sentinel_paths_in "\$WORKSPACE/state"' "$REAL_REPO/src/startup.sh"; then
  bad "startup.sh reaps only its own identity" "it still enumerates every instance's sentinel"
else
  ok "startup.sh reaps only its own identity"
fi

# --- peer survival: the property the enumeration assertion could not express ---
# LIMIT: calls the reaper directly, so it does NOT re-fail if startup.sh regresses to enumeration.
if [ -n "${have_fn:-}" ] && command -v sentinel_path_for >/dev/null 2>&1; then
  _pt="$(mktemp -d)"; mkdir -p "$_pt/state"
  _a="$(spawn "$CODE")"
  _b="$(spawn "$CODE")"
  sleep 2
  _rec() {   # <sentinel> <pid> <instance>
    printf '%s\ninstance=%s\nincarnation=inc-%s\ncode_path=%s\nversion=test\nworkspace=%s\n' \
      "$2" "$3" "$2" "$CODE" "$_pt" > "$1"
    printf 'inc-%s\n' "$2" > "${1%.pid}.incarnation"
  }
  _rec "$_pt/state/watch-tasks-stream.pid" "$_a" ""
  _rec "$_pt/state/watch-tasks-stream-peer-b+w2.pid" "$_b" "peer-b+w2"
  if kill -0 "$_a" 2>/dev/null && kill -0 "$_b" 2>/dev/null; then
    if _s="$(sentinel_path_for "$_pt/state")" && [ -n "$_s" ]; then
      reap_stale_task_watcher "$_s" >/dev/null 2>&1
    fi
    sleep 1
    if kill -0 "$_b" 2>/dev/null && [ -f "$_pt/state/watch-tasks-stream-peer-b+w2.pid" ]; then
      ok "a peer instance's live watcher and sentinel both survive this startup"
    else
      bad "a peer instance survives this startup" "the peer was killed or its sentinel removed"
    fi
    if kill -0 "$_a" 2>/dev/null; then
      bad "this instance's own stale watcher is still reaped" "it survived, so the reap did nothing"
    else
      ok "this instance's own stale watcher is still reaped"
    fi
  else
    bad "peer-survival fixture" "a fixture watcher was not alive; the case measured nothing"
  fi
  kill "$_a" "$_b" 2>/dev/null; rm -rf "$_pt"
fi
if grep -q 'rm -f "\$WATCHER_PID_FILE"' "$REAL_REPO/src/startup.sh"; then
  bad "startup.sh keeps no unguarded copy" "the inline rm -f is still there"
else
  ok "startup.sh keeps no unguarded copy"
fi

# --- case 5: an OBSERVER whose argv merely NAMES the script is not a watcher --
# Identity belongs to src/watcher_identity.py: argv[1] is not the script.
cat > "$TMP/observer.sh" << 'SH'
#!/bin/bash
# argv carries the watcher's name as an OPERAND, which is what used to match.
sleep 30
SH
chmod +x "$TMP/observer.sh"
"$TMP/observer.sh" src/watch-tasks-stream.sh "$TMP/inbox" &
obs_pid=$!
f="$TMP/case5.pid"
echo "$obs_pid" > "$f"
out="$(reap_stale_task_watcher "$f" 2>&1)"
if kill -0 "$obs_pid" 2>/dev/null; then
  ok "observer naming the script is NOT killed"
else
  bad "observer naming the script is NOT killed" "reaped an unrelated process ($out)"
fi
kill "$obs_pid" 2>/dev/null; wait "$obs_pid" 2>/dev/null

# --- case 6: an UNOBSERVED ps must not release the sentinel -------------------
# A silent non-zero `ps` (no stderr) used to fall through to release; hazard is a LIVE, unreadable watcher.
sleep 30 &
live6=$!
f="$TMP/case6.pid"
echo "$live6" > "$f"
shimdir="$TMP/shim6"; mkdir -p "$shimdir"
printf '#!/bin/sh\nexit 1\n' > "$shimdir/ps"; chmod +x "$shimdir/ps"
out="$(PATH="$shimdir:$PATH" reap_stale_task_watcher "$f" 2>&1)"
if [ -f "$f" ]; then
  ok "unobserved ps: sentinel left in place"
else
  bad "unobserved ps: sentinel left in place" "released on an unproven pid ($out)"
fi
kill "$live6" 2>/dev/null; wait "$live6" 2>/dev/null


# --- case 7: stdout NOISE ahead of the verdict must not be read as one --------
# A startup hook can prepend a line; unrecognised output is not an answer.
sleep 30 &
live7=$!
f="$TMP/case7.pid"
echo "$live7" > "$f"
noisy="$TMP/noisy"; mkdir -p "$noisy"
cat > "$noisy/python3" << 'SH'
#!/bin/sh
echo "site-banner"
echo "watcher"
echo "why=bash /x/watch-tasks-stream.sh /inbox"
exit 0
SH
chmod +x "$noisy/python3"
out="$(PATH="$noisy:$PATH" SUTANDO_PY="$noisy/python3" reap_stale_task_watcher "$f" 2>&1)"
if [ -f "$f" ] && kill -0 "$live7" 2>/dev/null; then
  ok "banner ahead of the verdict: neither killed nor released"
else
  bad "banner ahead of the verdict: neither killed nor released" \
      "sentinel present=$([ -f "$f" ] && echo yes || echo no) alive=$(kill -0 "$live7" 2>/dev/null && echo yes || echo no) ($out)"
fi
kill "$live7" 2>/dev/null; wait "$live7" 2>/dev/null


# --- case 8: `dead` from a SUCCEEDING helper still licenses the release -------
# On the record contract the helper is the seam's `pops_alive`, and the record is
# COMPLETE, so nothing but the pid's absence licenses the release. Case 1 is the
# pid-only sibling: a GONE pid's record is stale whatever its shape.
mkdir -p "$TMP/case8m"
f="$TMP/case8m/watch-tasks-stream.pid"
record "$f" "$(dead_pid)" "$CODE" "$TMP/case8m"
out="$(reap_stale_task_watcher "$f" 2>&1)"; rc8=$?
if [ "$rc8" -eq 0 ] && [ ! -f "$f" ]; then
  ok "dead from a succeeding helper (full record): sentinel released, rc 0"
else
  bad "dead from a succeeding helper (full record): sentinel released, rc 0" \
      "rc=$rc8 present=$([ -f "$f" ] && echo yes || echo no) ($out)"
fi

# --- case 9: `dead` from a FAILED helper licenses nothing ---------------------
# A failed run is not a verdict, whatever it printed.
sleep 30 &
live9=$!
f="$TMP/case9.pid"
echo "$live9" > "$f"
bad9="$TMP/bad9"; mkdir -p "$bad9"
cat > "$bad9/python3" << 'SH'
#!/bin/sh
echo "dead"
echo "why=no such process"
exit 42
SH
chmod +x "$bad9/python3"
out="$(PATH="$bad9:$PATH" SUTANDO_PY="$bad9/python3" reap_stale_task_watcher "$f" 2>&1)"
if [ -f "$f" ] && kill -0 "$live9" 2>/dev/null; then
  ok "dead from a FAILED helper: neither killed nor released"
else
  bad "dead from a FAILED helper: neither killed nor released" \
      "sentinel present=$([ -f "$f" ] && echo yes || echo no) alive=$(kill -0 "$live9" 2>/dev/null && echo yes || echo no) ($out)"
fi
kill "$live9" 2>/dev/null; wait "$live9" 2>/dev/null


# --- case 10: a watcher that does not exit on TERM is surfaced, never KILLed ---
# The reaper reports it and keeps the sentinel; it never escalates, because a
# KILL lands on whoever wears the pid by then. TERM is ignored across the exec,
# so the process still EXECUTES this checkout's script and ownership confirms.
mkdir -p "$TMP/case10m"
bash -c 'trap "" TERM; exec bash "$1"' _ "$CODE" >/dev/null 2>&1 &
live10=$!; SPAWNED="$SPAWNED $live10"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  ps -p "$live10" -o args= 2>/dev/null | grep -q "watch-tasks-stream" && break; sleep 0.1
done
f="$TMP/case10m/watch-tasks-stream.pid"
record "$f" "$live10" "$CODE" "$TMP/case10m"
SIGLOG10="$TMP/case10m/signals"; : > "$SIGLOG10"
# The real signal, logged: the assertion below is about which signals were SENT.
pops_signal() { printf 'signal %s %s\n' "$1" "${2:-TERM}" >> "$SIGLOG10"; kill -"${2:-TERM}" "$1" 2>/dev/null; }
out="$(SUTANDO_WATCHER_STOP_TICKS=3 reap_stale_task_watcher "$f" 2>&1)"
grep -q "^signal $live10 TERM$" "$SIGLOG10" \
  && ok "watcher ignoring TERM: TERM was sent through the seam (the case is not vacuous)" \
  || bad "watcher ignoring TERM: TERM was sent through the seam" "log: $(cat "$SIGLOG10") ($out)"
if kill -0 "$live10" 2>/dev/null && [ -f "$f" ]; then
  ok "watcher ignoring TERM: still running, sentinel RETAINED so it stays tracked"
else
  bad "watcher ignoring TERM: still running, sentinel RETAINED" \
      "alive=$(kill -0 "$live10" 2>/dev/null && echo yes || echo no) sentinel=$([ -f "$f" ] && echo present || echo gone) ($out)"
fi
case "$out" in *"STILL ALIVE"*) ok "watcher ignoring TERM: the survival is reported" ;;
  *) bad "watcher ignoring TERM: the survival is reported" "got: $out" ;; esac
if grep -qE '^signal [0-9]+ (KILL|9|-9|SIGKILL)$' "$SIGLOG10"; then
  bad "watcher ignoring TERM: NO kill -KILL was issued" "log: $(cat "$SIGLOG10")"
else
  ok "watcher ignoring TERM: NO kill -KILL was issued"
fi
# CONTROL: the same seam, a watcher that honours TERM -> released on one TERM.
ctl10="$(spawn "$CODE")"
mkdir -p "$TMP/case10c"
f="$TMP/case10c/watch-tasks-stream.pid"
record "$f" "$ctl10" "$CODE" "$TMP/case10c"
: > "$SIGLOG10"
out="$(reap_stale_task_watcher "$f" 2>&1)"
if [ ! -e "$f" ] && [ "$(cat "$SIGLOG10")" = "signal $ctl10 TERM" ]; then
  ok "watcher ignoring TERM, CONTROL: one honouring TERM is released on one TERM"
else
  bad "watcher ignoring TERM, CONTROL: one honouring TERM is released on one TERM" \
      "sentinel=$([ -e "$f" ] && echo present || echo gone) log: $(cat "$SIGLOG10") ($out)"
fi
. "$SB/src/process-ops.sh"            # the real seam back
kill -KILL "$live10" 2>/dev/null; wait "$live10" 2>/dev/null

# --- case 11: a verdict-shaped line from a startup hook is still not a verdict -
# Case 7 covers an unrecognised prelude; this one uses protocol vocabulary.
sleep 30 &
live11=$!
f="$TMP/case11.pid"
echo "$live11" > "$f"
vocab="$TMP/vocab11"; mkdir -p "$vocab"
cat > "$vocab/python3" << 'SH'
#!/bin/sh
printf 'watcher\nnot-watcher\nwhy=python3 observer.py src/watch-tasks-stream.sh /inbox\n'
SH
chmod +x "$vocab/python3"
out="$(PATH="$vocab:$PATH" SUTANDO_PY="$vocab/python3" reap_stale_task_watcher "$f" 2>&1)"
if [ -f "$f" ] && kill -0 "$live11" 2>/dev/null; then
  ok "vocabulary prelude: neither killed nor released"
else
  bad "vocabulary prelude: neither killed nor released" \
      "sentinel=$([ -f "$f" ] && echo present || echo gone) alive=$(kill -0 "$live11" 2>/dev/null && echo yes || echo no) ($out)"
fi
kill "$live11" 2>/dev/null; wait "$live11" 2>/dev/null


# --- case 12: the refusal is the reason, not an earlier return ----------------
# With no KILL there is no re-proof window: the identity refusal is the last
# word, and nothing may be signalled on the way to it. Measured through the seam.
SIGLOG12="$TMP/siglog12"; : > "$SIGLOG12"
pops_signal() { printf 'signal %s %s\n' "$1" "${2:-TERM}" >> "$SIGLOG12"; return 0; }
# (a) this checkout's live watcher behind a pid-only record
p12a="$(spawn "$CODE")"
mkdir -p "$TMP/case12a"
f="$TMP/case12a/watch-tasks-stream.pid"
echo "$p12a" > "$f"
out="$(reap_stale_task_watcher "$f" 2>&1)"
case "$out" in *"records a pid only"*) ok "refusal is the reason: a pid-only record names the record check" ;;
  *) bad "refusal is the reason: a pid-only record names the record check" "got: $out" ;; esac
[ -f "$f" ] && ok "refusal is the reason: the pid-only sentinel is retained" \
            || bad "refusal is the reason: the pid-only sentinel is retained" "unlinked ($out)"
# (b) a COMPLETE record whose pid carries the script as DATA
"$PY_BIN" -c 'import time; time.sleep(120)' "$CODE" >/dev/null 2>&1 & p12b=$!
SPAWNED="$SPAWNED $p12b"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  ps -p "$p12b" -o args= 2>/dev/null | grep -q "watch-tasks-stream" && break; sleep 0.1
done
mkdir -p "$TMP/case12b"
f="$TMP/case12b/watch-tasks-stream.pid"
record "$f" "$p12b" "$CODE" "$TMP/case12b"
out="$(reap_stale_task_watcher "$f" 2>&1)"
case "$out" in *"argv:"*) ok "refusal is the reason: a data-argument pid names the executed-slot check" ;;
  *) bad "refusal is the reason: a data-argument pid names the executed-slot check" "got: $out" ;; esac
[ -f "$f" ] && ok "refusal is the reason: the data-argument sentinel is retained" \
            || bad "refusal is the reason: the data-argument sentinel is retained" "unlinked ($out)"
. "$SB/src/process-ops.sh"            # the real seam back
if [ ! -s "$SIGLOG12" ]; then
  ok "refusal is the reason: nothing was signalled on the way to either refusal"
else
  bad "refusal is the reason: nothing was signalled on the way to either refusal" "log: $(cat "$SIGLOG12")"
fi
if kill -0 "$p12a" 2>/dev/null && kill -0 "$p12b" 2>/dev/null; then
  ok "refusal is the reason: both processes still run"
else
  bad "refusal is the reason: both processes still run" "a=$(kill -0 "$p12a" 2>/dev/null && echo yes || echo no) b=$(kill -0 "$p12b" 2>/dev/null && echo yes || echo no)"
fi
kill "$p12a" "$p12b" 2>/dev/null

if [ "$fails" -eq 0 ]; then
  echo "ALL PASS"
  exit 0
fi
echo "FAILED ($fails)"
exit 1
