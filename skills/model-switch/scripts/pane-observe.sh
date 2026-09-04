#!/usr/bin/env bash
# pane-observe.sh <session> --socket PATH (--count | --wait --baseline N [--timeout S] [--answer-enter] | --cancel)
# The model switch's only pane reader: counts CLI acceptance lines, waits for a
# new one after a send, detects the warm-cache confirm dialog, answers or cancels it.
set -u
SESSION="${1:?session}"; shift
SOCK=""; MODE=""; BASE=0; TIMEOUT=20; ANSWER=""
while [ $# -gt 0 ]; do case "$1" in
  --socket) SOCK="${2:?}"; shift;; --count) MODE=count;; --wait) MODE=wait;; --cancel) MODE=cancel;;
  --baseline) BASE="${2:?}"; shift;; --timeout) TIMEOUT="${2:?}"; shift;; --answer-enter) ANSWER=1;;
  *) echo "pane-observe: unknown arg $1" >&2; exit 2;;
esac; shift; done
[ -n "$SOCK" ] || { echo "pane-observe: --socket required" >&2; exit 2; }
ACCEPT='Set model to'; DIALOG='Yes, switch'
cap() { tmux -S "$SOCK" capture-pane -p -t "$SESSION" 2>/dev/null; }
count() { cap | grep -c -- "$ACCEPT"; }
case "$MODE" in
  count) count; exit 0;;
  cancel) tmux -S "$SOCK" send-keys -t "$SESSION" Escape; echo CANCELLED; exit 0;;
  wait) ;;
  *) echo "pane-observe: one of --count/--wait/--cancel" >&2; exit 2;;
esac
[ -n "$ANSWER" ] && tmux -S "$SOCK" send-keys -t "$SESSION" Enter
# Poll in 0.5 s steps: a NEW acceptance line (count above the baseline) is the
# switch; the dialog present means the CLI is waiting on a human.
deadline=$(( $(date +%s) + TIMEOUT ))
while :; do
  text="$(cap)"
  n=$(printf '%s\n' "$text" | grep -c -- "$ACCEPT")
  if [ "$n" -gt "$BASE" ]; then echo ACCEPTED; exit 0; fi
  if [ -z "$ANSWER" ] && printf '%s' "$text" | grep -q -- "$DIALOG"; then echo DIALOG; exit 10; fi
  [ "$(date +%s)" -ge "$deadline" ] && { echo TIMEOUT; exit 11; }
  sleep 0.5
done
