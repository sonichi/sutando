#!/usr/bin/env bash
# pane-observe.sh <session> --socket PATH --model M [--settings FILE] [--expected-pid PID] (--count | --wait --baseline N [--timeout S] [--answer-enter] | --cancel)
# Acceptance = a "Set model to <family> [<version>]" line for the REQUESTED model; any other model's line does not count.
set -u
SESSION="${1:?session}"; shift
SOCK=""; MODE=""; BASE=0; TIMEOUT=20; ANSWER=""; REQ=""; SETTINGS=""; EXPECT=()
while [ $# -gt 0 ]; do case "$1" in
  --socket) SOCK="${2:?}"; shift;; --count) MODE=count;; --wait) MODE=wait;; --cancel) MODE=cancel;;
  --baseline) BASE="${2:?}"; shift;; --timeout) TIMEOUT="${2:?}"; shift;; --answer-enter) ANSWER=1;; --model) REQ="${2:?}"; shift;; --settings) SETTINGS="${2:?}"; shift;; --expected-pid) EXPECT=(--expected-pid "${2:?}"); shift;;
  *) echo "pane-observe: unknown arg $1" >&2; exit 2;;
esac; shift; done
[ -n "$SOCK" ] || { echo "pane-observe: --socket required" >&2; exit 2; }
[ -n "$REQ" ] || [ "$MODE" = cancel ] || { echo "pane-observe: --model required" >&2; exit 2; }
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
PANE_KEYS="$REPO_ROOT/src/tmux-pane-keys.sh"
PY="$(bash "$REPO_ROOT/scripts/sutando-config.sh" python-bin)" || exit 1
DIALOG="$("$PY" "$REPO_ROOT/src/claude_model_switch.py" confirm-text)" || exit 1
# The CLI echoes display names ("Set model to Fable 5.1"), not ids. The line that is this model's
# is src/claude_model_switch.py's rule, shared with ag2-sparrow's claude-local.
ACCEPT=""
if [ "$MODE" != cancel ]; then ACCEPT="$("$PY" "$REPO_ROOT/src/claude_model_switch.py" accept "$REQ")" || { echo "pane-observe: '$REQ' is not a model" >&2; exit 2; }; fi
# A failed capture is a failed observation, never a zero.
cap() { tmux -S "$SOCK" capture-pane -p -t "$SESSION" 2>/dev/null || { echo "CAPTURE-FAILED"; return 12; }; }
# With --settings (the CLI's settings.json) the baseline is "<strict> <omitted> <mark>": a [1m] line without
# its size counts only after a fresh save of that file naming the model (claude_model_switch.py settled).
COUNT_ARGS=(count "$REQ"); [ -n "$SETTINGS" ] && COUNT_ARGS=(baseline "$REQ" --settings "$SETTINGS")
count() { out="$(cap)" || { echo "CAPTURE-FAILED"; return 12; }; printf '%s\n' "$out" | "$PY" "$REPO_ROOT/src/claude_model_switch.py" "${COUNT_ARGS[@]}"; return 0; }
case "$MODE" in
  count) count; exit $?;;
  cancel) bash "$PANE_KEYS" --python "$PY" -S "$SOCK" -t "$SESSION" ${EXPECT[@]+"${EXPECT[@]}"} -- Escape || exit $?; echo CANCELLED; exit 0;;
  wait) ;;
  *) echo "pane-observe: one of --count/--wait/--cancel" >&2; exit 2;;
esac
# The sender's status (1 not sent, 75 busy, 124 revoked, 125 may have applied) is the verdict, not a timeout.
if [ -n "$ANSWER" ]; then bash "$PANE_KEYS" --python "$PY" -S "$SOCK" -t "$SESSION" ${EXPECT[@]+"${EXPECT[@]}"} -- Enter || exit $?; fi
# A NEW matching line (count above baseline) is the switch; the dialog means the CLI waits on a human.
deadline=$(( $(date +%s) + TIMEOUT ))
while :; do
  if ! text="$(cap)"; then
    # Unobservable pane: keep polling, but never let a blind poll count as acceptance.
    [ "$(date +%s)" -ge "$deadline" ] && { echo TIMEOUT; exit 11; }; sleep 0.5; continue
  fi
  if [ -n "$SETTINGS" ]; then
    n=$(printf '%s\n' "$text" | "$PY" "$REPO_ROOT/src/claude_model_switch.py" settled "$REQ" --settings "$SETTINGS" --baseline "$BASE")
    [ "$n" = 1 ] && { echo ACCEPTED; exit 0; }
  else
    n=$(printf '%s\n' "$text" | "$PY" "$REPO_ROOT/src/claude_model_switch.py" count "$REQ")
    if [ "$n" -gt "$BASE" ]; then echo ACCEPTED; exit 0; fi
  fi
  if [ -z "$ANSWER" ] && printf '%s' "$text" | grep -q -- "$DIALOG"; then echo DIALOG; exit 10; fi
  [ "$(date +%s)" -ge "$deadline" ] && { echo TIMEOUT; exit 11; }
  sleep 0.5
done
