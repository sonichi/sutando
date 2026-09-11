#!/usr/bin/env bash
# EXECUTES restart.sh with system operations faked and asserts what it PRINTS.
#
# Its companion (restart-warns-watcher-stopped.test.sh) scans source tokens and
# line order, so it cannot observe the behaviour it promises to protect: wrapping
# the three warning echoes in `if false; then ... fi` leaves every one of its six
# checks passing. Arm 3 below is that exact perturbation, and this file fails on
# it — which is the only reason to have both.
#
# The sandbox arms a CONFIRMED stop (full sentinel record + the recording fake),
# because the warning is only true when a watcher was stopped; arm 4 is the rest.
#
# Run: bash tests/restart-warns-watcher-stopped-behavior.test.sh
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
FAKE="$REPO/tests/fixtures/process-ops-fake.sh"
WPID=8801
fails=0
ck() { if [ "$2" = "0" ]; then echo "  ok   $1"; else echo "  FAIL $1"; fails=$((fails+1)); fi; }

# Build an isolated copy whose restart.sh resolves REPO to the sandbox (it uses
# `dirname "$0"/..`). Every process-touching call goes to the fake, so
# kill-vs-print ORDER is observable in one stream and no host process is touched.
build() {                       # build <sandbox> [perturbation]
  local sb="$1" perturb="${2:-none}"
  mkdir -p "$sb/src" "$sb/bin" "$sb/scripts" "$sb/workspace/state"
  cp "$REPO/src/restart.sh" "$sb/src/restart.sh"
  cp "$REPO/src/process-ops.sh" "$REPO/src/watcher_sentinel.sh" "$sb/src/"
  cp "$REPO/src/util_paths.py" "$REPO/src/sutando_config.py" "$sb/src/"
  cp -R "$REPO/src/runtime-api" "$sb/src/runtime-api"
  cp "$REPO/scripts/python-binary.sh" "$sb/scripts/python-binary.sh"
  cat > "$sb/scripts/sutando-config.sh" <<CFG
#!/bin/sh
case "\$1" in
  workspace) printf '%s' "$sb/workspace" ;;
  *) exit 1 ;;
esac
CFG
  # Stubs and the sentinel FIRST. A perturbation that aborts below used to return
  # with bin/ empty, and run()'s PATH fell through to the real pkill.
  printf '#!/bin/sh\necho "STUB-STARTUP-REACHED"\n' > "$sb/src/startup.sh"
  # Executing this is the ONLY way the marker appears, so its absence is evidence
  # rather than a regex that can never match. qingyun-wu proved the old one vacuous.
  printf '#!/bin/sh\necho "STUB-WATCHER-EXECUTED"\n' > "$sb/src/watch-tasks-stream.sh"
  printf '#!/bin/sh\necho "STUB-PKILL $*"\nexit 0\n'  > "$sb/bin/pkill"
  for c in pgrep launchctl ngrok lsof id open killall osascript tmux nohup; do
    printf '#!/bin/sh\nexit 1\n' > "$sb/bin/$c"
  done
  printf '#!/bin/sh\nexit 0\n' > "$sb/bin/sleep"
  # PATH stays stub-only, so every binary is named here explicitly. Adding /bin
  # instead would expose the real launchctl, which restart.sh uses to bootout jobs.
  # These are read-only or scoped to the sandbox; nothing here can signal a process.
  for c in bash sh dirname basename seq cat grep sed awk tr head tail wc \
           mktemp rm mkdir cp mv ls test env printf date stat; do
    [ -x "/bin/$c" ] && ln -sf "/bin/$c" "$sb/bin/$c"
    [ -x "/usr/bin/$c" ] && ln -sf "/usr/bin/$c" "$sb/bin/$c"
  done
  chmod +x "$sb/src/startup.sh" "$sb/src/watch-tasks-stream.sh" \
           "$sb/scripts/sutando-config.sh" "$sb/bin/"*
  arm_sentinel "$sb"
  case "$perturb" in
    disable-warning)
      "$REPO_PY" - "$sb/src/restart.sh" <<'PY' || return 1
import sys, re
p = sys.argv[1]; s = open(p).read()
# Wrap ONLY the three warning echoes, exactly as the reviewer's control did.
m = re.search(r'^ *echo "  ⚠ task watcher STOPPED.*?\n(?: *echo "      .*?\n)+', s, re.M | re.S)
assert m, "warning block not found — perturbation would be a no-op"
s = s[:m.start()] + "if false; then\n" + m.group(0) + "fi\n" + s[m.end():]
open(p, "w").write(s)
PY
      ;;
  esac
}

# A watcher this install can PROVE is its own — otherwise restart.sh refuses to
# signal and the "stopped" warning would be a lie.
arm_sentinel() {                # arm_sentinel <sandbox>
  local sb="$1" sent code
  code="$sb/src/watch-tasks-stream.sh"
  sent="$( . "$sb/src/watcher_sentinel.sh"; SUTANDO_PY="$REPO_PY" sentinel_path_for "$sb/workspace/state" )"
  printf '%s\ninstance=\nincarnation=inc1\ncode_path=%s\nversion=test\nworkspace=%s\n' \
    "$WPID" "$code" "$sb/workspace" > "$sent"
  printf 'inc1\n' > "${sent%.pid}.incarnation"
}

run() {                         # run <sandbox> -> stdout of a full restart
  # Refuse an unbuilt sandbox, and use ONLY the stub PATH. Both are load-bearing:
  # a missing stub must be `command not found`, never the host's real binary.
  if [ ! -x "$1/bin/pkill" ] || [ ! -x "$1/src/startup.sh" ] || [ ! -r "$1/src/process-ops.sh" ]; then
    echo "REFUSED-UNBUILT-SANDBOX"; return 0
  fi
  # Absolute interpreters + $SUTANDO_PY: that PATH must stay free of a python3,
  # which could signal anything, yet restart.sh needs one to resolve the sentinel.
  ( cd "$1" && PATH="$1/bin" /usr/bin/env \
      SUTANDO_PROCESS_OPS="$FAKE" POPS_LOG="$1/ops.log" POPS_ALIVE_PIDS="$WPID" \
      SUTANDO_PY="$REPO_PY" \
      "POPS_ARGV_$WPID=/bin/bash $1/src/watch-tasks-stream.sh" \
      /bin/bash "$1/src/restart.sh" 2>/dev/null )
}

# Assertions on one captured run. Returns 0 when the warning behaviour holds.
assert_warning_behaviour() {     # assert_warning_behaviour <output>
  local out="$1"
  grep -q "task watcher STOPPED" <<<"$out" || return 1
  grep -q "watch-tasks-stream.sh" <<<"$out" || return 1
  # ORDER, as executed: the stop must be announced before the warning. Printed
  # first, the warning would describe a watcher that is still running.
  local kill_n warn_n
  kill_n=$(grep -n "watcher stop:" <<<"$out" | head -1 | cut -d: -f1)
  warn_n=$(grep -n "task watcher STOPPED"      <<<"$out" | head -1 | cut -d: -f1)
  [ -n "$kill_n" ] && [ -n "$warn_n" ] && [ "$warn_n" -gt "$kill_n" ]
}

# The repo's own resolver, not an invented fallback: restart.sh itself sources this
# helper at :14-18, and a bare `|| echo /usr/bin/python3` reaches the CLT stub it exists
# to avoid. No runnable interpreter means the perturbation cannot be built, which must
# fail loudly rather than silently skip the control.
REPO_PY=""
if [ -r "$REPO/scripts/python-binary.sh" ]; then
  . "$REPO/scripts/python-binary.sh"
  REPO_PY="$(resolve_python "$REPO" 2>/dev/null || true)"
fi
[ -n "$REPO_PY" ]; ck "a runnable python3 resolved via scripts/python-binary.sh" $?
SB_ROOT="$(mktemp -d)"; trap 'rm -rf "$SB_ROOT"' EXIT

# --- arm 1: the script runs to completion under the fake (harness is sound) ---
sb="$SB_ROOT/head"; build "$sb"; out_head="$(run "$sb")"
grep -q "STUB-STARTUP-REACHED" <<<"$out_head"; ck "restart.sh reaches startup.sh under the fake" $?
grep -q "watcher stop:" <<<"$out_head"; ck "it really does reach the watcher stop (guards the rest)" $?
grep -q "^signal $WPID TERM$" "$sb/ops.log"; ck "and the stop it announces is a real signal to the sentinel's pid" $?

# --- arm 2: at this head, the warning behaviour holds -------------------------
assert_warning_behaviour "$out_head"; ck "warning is EMITTED, names the re-arm, and follows the kill" $?

# --- arm 2b: the warning's own claim, tested on the file under test ------------
# "nothing here re-arms it" is a claim about restart.sh, and this run reaches
# startup.sh (arm 1), so an absence here is a completed observation rather than a
# partial one. The stub pkill announces every kill; a re-arm would have to exec the
# watcher, and nothing in the captured stream does.
grep -q "watch-tasks-stream.sh" <<<"$out_head"
ck "the warning names the re-arm command" $?
! grep -q "STUB-WATCHER-EXECUTED" <<<"$out_head"
ck "restart.sh itself starts no watcher (the stub would announce itself)" $?

# --- arm 3: THE CONTROL — disable the warning, the assertion must FAIL --------
# The perturbation must APPLY. Without this check the arm below passes for the
# wrong reason on any tree that never had the warning (nothing to disable looks
# identical to disabled), which would let the control certify a missing feature.
sb="$SB_ROOT/off"
if build "$sb" disable-warning; then perturbed=0; else perturbed=1; fi
ck "the disabling perturbation applied (control is not vacuous)" "$perturbed"
if [ "$perturbed" = "0" ]; then out_off="$(run "$sb")"; else out_off=""; fi
grep -q "STUB-STARTUP-REACHED" <<<"$out_off"
ck "control still runs (perturbation is narrow, not a broken script)" $?
if [ "$perturbed" != "0" ]; then
  ck "DISABLED-WARNING CONTROL fails the assertion" 1
elif assert_warning_behaviour "$out_off"; then
  ck "DISABLED-WARNING CONTROL fails the assertion" 1
else
  ck "DISABLED-WARNING CONTROL fails the assertion" 0
fi

# --- arm 4: nothing was stopped, so nothing may claim it was ------------------
sb="$SB_ROOT/unowned"; build "$sb"
sed -i.bak "s|^code_path=.*|code_path=/some/other/checkout/watch-tasks-stream.sh|" \
  "$sb/workspace/state/watch-tasks-stream.pid"
out_unowned="$(run "$sb")"
! grep -q "task watcher STOPPED" <<<"$out_unowned"
ck "an UNCONFIRMED stop does not print 'task watcher STOPPED'" $?
grep -q "task watcher NOT stopped" <<<"$out_unowned"; ck "it warns that the watcher is still running instead" $?
grep -q "watch-tasks-stream.sh" <<<"$out_unowned"; ck "and still names the command that re-arms one" $?
! grep -q "^signal " "$sb/ops.log"; ck "and signalled nothing at all" $?

echo
[ "$fails" -eq 0 ] && { echo "all ok"; exit 0; } || { echo "$fails FAILED"; exit 1; }
