#!/usr/bin/env bash
# EXECUTES restart.sh with system operations stubbed and asserts what it PRINTS.
#
# Its companion (restart-warns-watcher-stopped.test.sh) scans source tokens and
# line order, so it cannot observe the behaviour it promises to protect: wrapping
# the three warning echoes in `if false; then ... fi` leaves every one of its six
# checks passing. Arm 3 below is that exact perturbation, and this file fails on
# it — which is the only reason to have both.
#
# Run: bash tests/restart-warns-watcher-stopped-behavior.test.sh
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fails=0
ck() { if [ "$2" = "0" ]; then echo "  ok   $1"; else echo "  FAIL $1"; fails=$((fails+1)); fi; }

# Build an isolated copy whose restart.sh resolves REPO to the sandbox (it uses
# `dirname "$0"/..`), with every process-touching command replaced by a stub that
# announces itself on stdout, so kill-vs-print ORDER is observable in one stream.
build() {                       # build <sandbox> [perturbation]
  local sb="$1" perturb="${2:-none}"
  mkdir -p "$sb/src" "$sb/bin"
  cp "$REPO/src/restart.sh" "$sb/src/restart.sh"
  # Stubs FIRST. A perturbation that aborts below used to return with bin/ empty,
  # and run()'s PATH fell through to the real pkill — killing the host's watchers.
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
  chmod +x "$sb/src/startup.sh" "$sb/src/watch-tasks-stream.sh" "$sb/bin/"*
  case "$perturb" in
    disable-warning)
      "$REPO_PY" - "$sb/src/restart.sh" <<'PY' || return 1
import sys, re
p = sys.argv[1]; s = open(p).read()
# Wrap ONLY the three warning echoes, exactly as the reviewer's control did.
m = re.search(r'^echo "  ⚠ task watcher STOPPED.*?\n(?:echo "      .*?\n)+', s, re.M | re.S)
assert m, "warning block not found — perturbation would be a no-op"
s = s[:m.start()] + "if false; then\n" + m.group(0) + "fi\n" + s[m.end():]
open(p, "w").write(s)
PY
      ;;
  esac
}

run() {                         # run <sandbox> -> stdout of a full restart
  # Refuse an unbuilt sandbox, and use ONLY the stub PATH. Both are load-bearing:
  # a missing stub must be `command not found`, never the host's real binary.
  if [ ! -x "$1/bin/pkill" ] || [ ! -x "$1/src/startup.sh" ]; then
    echo "REFUSED-UNBUILT-SANDBOX"; return 0
  fi
  # /bin/bash by absolute path: the stub-only PATH applies to the script's own
  # lookups, and would otherwise hide the interpreter running it.
  ( cd "$1" && PATH="$1/bin" /bin/bash "$1/src/restart.sh" 2>/dev/null )
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

# --- arm 1: the script runs to completion under stubs (harness is sound) ------
sb="$SB_ROOT/head"; build "$sb"; out_head="$(run "$sb")"
grep -q "STUB-STARTUP-REACHED" <<<"$out_head"; ck "restart.sh reaches startup.sh under stubs" $?
grep -q "watcher stop:" <<<"$out_head"; ck "it really does reach the watcher stop (guards the rest)" $?

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

echo
[ "$fails" -eq 0 ] && { echo "all ok"; exit 0; } || { echo "$fails FAILED"; exit 1; }
