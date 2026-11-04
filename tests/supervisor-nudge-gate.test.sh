#!/usr/bin/env bash
# Integration for task-notifier-supervisor.sh's new pre-arm decision: a REAL
# tmux pane captured the same way the supervisor captures it, classified by the
# real pane_gate, fed to the real nudge_gate alongside a real beat file. This is
# the new plumbing end to end -- capture -> classify_pane -> nudge_gate -- with
# only the pane content and the beat's freshness varied.
#
#   idle-ready pane + fresh beat  -> nudge   (restore the session's own watcher)
#   idle-ready pane + stale beat  -> alert   (idle frame over a dead agent)
#   idle-ready pane + no beat     -> alert   (never beat)
#   busy pane      + fresh beat   -> arm     (a working turn needs no help)
#   idle-ready pane + no workspace-> arm     (unknown health never withholds)
#
# The supervisor's own decide_action is exercised by sourcing just its helper
# section (the main loop is skipped by running with no live target).
#
# Run: bash tests/supervisor-nudge-gate.test.sh
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
GATE="$REPO/src/delivery/nudge_gate.py"
PY="${SUTANDO_PY:-/usr/bin/python3}"
FOOTER="⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"

command -v tmux >/dev/null 2>&1 || { echo "SKIP: tmux not available"; exit 0; }

fail=0
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sut-sup-nudge.XXXXXX")"
SOCK="$WORK/tmux.sock"
trap 'tmux -S "$SOCK" kill-server 2>/dev/null; rm -rf "$WORK"' EXIT

check() { # desc expected actual
  if [ "$2" = "$3" ]; then
    echo "ok   $1"
  else
    echo "FAIL $1: expected [$2], got [$3]"; fail=1
  fi
}

# A pane classified through the same capture command the supervisor uses.
pane_state_of() { # target
  local cap
  cap="$(tmux -S "$SOCK" capture-pane -p -e -J -t "$1" 2>/dev/null)" || { echo unknown; return; }
  printf '%s' "$cap" | "$PY" -c \
    'import sys; sys.path.insert(0, sys.argv[1]); from delivery.pane_gate import classify_pane, CLAUDE; print(classify_pane(sys.stdin.read(), CLAUDE).state)' \
    "$REPO/src" 2>/dev/null || echo unknown
}

decision() { # pane-state beat-path-or-empty
  if [ -n "$2" ]; then
    "$PY" "$GATE" --pane-state "$1" --beat-path "$2" --stale-s 90 2>/dev/null || echo arm
  else
    "$PY" "$GATE" --pane-state "$1" --health unknown 2>/dev/null || echo arm
  fi
}

# An idle Claude pane: the footer classify_pane keys on, with an empty composer.
tmux -S "$SOCK" new-session -d -s idle -x 200 -y 50
tmux -S "$SOCK" send-keys -t idle:0 -l "clear; printf '%s\n%s\n' '❯ ' '$FOOTER'; cat"
tmux -S "$SOCK" send-keys -t idle:0 Enter
sleep 1
IDLE_STATE="$(pane_state_of idle:0)"
check "an idle Claude pane classifies idle-ready" "idle-ready" "$IDLE_STATE"

# A working pane: the thinking marker classify_pane reads as busy.
tmux -S "$SOCK" new-session -d -s busy -x 200 -y 50
tmux -S "$SOCK" send-keys -t busy:0 -l "clear; printf '%s\n%s\n%s\n' '✻ Thinking… (12s · esc to interrupt)' '❯ ' '$FOOTER'; cat"
tmux -S "$SOCK" send-keys -t busy:0 Enter
sleep 1
BUSY_STATE="$(pane_state_of busy:0)"
check "a working Claude pane classifies busy" "busy" "$BUSY_STATE"

# Beats.
mkdir -p "$WORK/state/cores"
FRESH="$WORK/state/cores/fresh.alive"; : > "$FRESH"
STALE="$WORK/state/cores/stale.alive"; : > "$STALE"; touch -t 202001010000 "$STALE"
MISSING="$WORK/state/cores/none.alive"

check "idle-ready + fresh beat -> nudge" "nudge" "$(decision "$IDLE_STATE" "$FRESH")"
check "idle-ready + stale beat -> alert" "alert" "$(decision "$IDLE_STATE" "$STALE")"
check "idle-ready + missing beat -> alert" "alert" "$(decision "$IDLE_STATE" "$MISSING")"
check "busy + fresh beat -> arm" "arm" "$(decision "$BUSY_STATE" "$FRESH")"
check "idle-ready + unresolvable health -> arm" "arm" "$(decision "$IDLE_STATE" "")"

# The supervisor's OWN decide_action, sourced with its standby loop skipped
# (SUTANDO_SUPERVISOR_SOURCE_ONLY), pointed at the real idle pane and a fresh
# core beat for the resolved host label. This exercises pane_state,
# beat_path_for_session and decide_action as the supervisor actually runs them.
HOST="$(bash "$REPO/scripts/sutando-config.sh" host-label 2>/dev/null)"
if [ -n "$HOST" ]; then
  : > "$WORK/state/cores/$HOST.alive"
  # The Claude notifier implements --nudge; a stub without it must never be nudged.
  CLAUDE_NOTIFIER="$REPO/src/agent/claude/cli/task-notifier.sh"
  NONUDGE="$WORK/nonudge-notifier.sh"; printf '#!/bin/bash\n[ "${1:-}" = "--event" ] && exit 0\nwhile :; do sleep 1; done\n' > "$NONUDGE"; chmod +x "$NONUDGE"
  run_decide() { # notifier-script
    (
      unset SUTANDO_INSTANCE_ID SUTANDO_TMUX_PANE SUTANDO_TASKS_DIR \
            SUTANDO_RESULTS_DIR SUTANDO_INBOX_KIND SUTANDO_INBOX_RESOLVER
      export SUTANDO_SUPERVISOR_SOURCE_ONLY=1
      export SUTANDO_TMUX_SOCKET="$SOCK" SUTANDO_TMUX_SESSION="idle" SUTANDO_TMUX_WINDOW="0"
      export SUTANDO_WORKSPACE_DIR="$WORK" SUTANDO_NOTIFIER_PY="$PY" SUTANDO_NOTIFIER_SCRIPT="$1"
      # shellcheck source=/dev/null
      source "$REPO/src/agent/codex/cli/task-notifier-supervisor.sh"
      printf 'ps=%s beat=%s action=%s\n' "$(pane_state)" "$(beat_path_for_session)" "$(decide_action)"
    ) 2>/dev/null
  }
  run_decide "$CLAUDE_NOTIFIER" > "$WORK/act.txt"
  ACT="$(sed -n 's/.*action=//p' "$WORK/act.txt")"
  check "decide_action (Claude notifier, idle-ready pane, fresh core beat) -> nudge" "nudge" "$ACT"
  BEATSEEN="$(sed -n 's/.*beat=\([^ ]*\).*/\1/p' "$WORK/act.txt")"
  check "beat_path_for_session resolves the core beat" "$WORK/state/cores/$HOST.alive" "$BEATSEEN"
  # A notifier without --nudge (the Codex default) can only ever arm, even on an
  # idle-ready pane with a live beat -- nudging it would wedge on its event loop.
  NNACT="$(run_decide "$NONUDGE" | sed -n 's/.*action=//p')"
  check "decide_action (no-nudge notifier, idle-ready + fresh) -> arm" "arm" "$NNACT"

  # notifier_supports_nudge is a grep for the entrypoint, so it is brittle to a
  # reword; pin that the REAL Claude notifier keeps matching it and the REAL
  # Codex notifier does not. A reword of either entrypoint fails here.
  supports() { # notifier-script -> yes/no
    ( export SUTANDO_SUPERVISOR_SOURCE_ONLY=1 SUTANDO_NOTIFIER_SCRIPT="$1"
      # shellcheck source=/dev/null
      source "$REPO/src/agent/codex/cli/task-notifier-supervisor.sh"
      notifier_supports_nudge && echo yes || echo no ) 2>/dev/null
  }
  check "the real Claude notifier passes notifier_supports_nudge" "yes" "$(supports "$REPO/src/agent/claude/cli/task-notifier.sh")"
  check "the real Codex notifier does not" "no" "$(supports "$REPO/src/agent/codex/cli/task-notifier.sh")"
else
  echo "ok   decide_action core-beat case skipped (no host-label)"
fi

[ "$fail" -eq 0 ] && echo "PASS" || echo "FAILED"
exit "$fail"
