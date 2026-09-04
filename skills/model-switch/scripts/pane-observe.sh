#!/usr/bin/env bash
# pane-observe.sh <session> --socket PATH --model M (--count | --wait --baseline N [--timeout S] [--answer-enter] | --cancel)
# Acceptance = a "Set model to <family> [<version>]" line for the REQUESTED model; any other model's line does not count.
set -u
SESSION="${1:?session}"; shift
SOCK=""; MODE=""; BASE=0; TIMEOUT=20; ANSWER=""; REQ=""
while [ $# -gt 0 ]; do case "$1" in
  --socket) SOCK="${2:?}"; shift;; --count) MODE=count;; --wait) MODE=wait;; --cancel) MODE=cancel;;
  --baseline) BASE="${2:?}"; shift;; --timeout) TIMEOUT="${2:?}"; shift;; --answer-enter) ANSWER=1;; --model) REQ="${2:?}"; shift;;
  *) echo "pane-observe: unknown arg $1" >&2; exit 2;;
esac; shift; done
[ -n "$SOCK" ] || { echo "pane-observe: --socket required" >&2; exit 2; }
[ -n "$REQ" ] || [ "$MODE" = cancel ] || { echo "pane-observe: --model required" >&2; exit 2; }
# The CLI echoes display names ("Set model to Fable 5.1"), not ids: derive the
# family from an alias or claude-<family>-<ver>, and the version with dots.
fam="$(printf '%s' "$REQ" | sed -E 's/^claude-([a-z]+)-?.*/\1/; s/\[1m\]$//')"
ver="$(printf '%s' "$REQ" | sed -nE 's/^claude-[a-z]+-([0-9]+(-[0-9]+)*).*/\1/p' | tr '-' '.')"
ACCEPT="Set model to ${fam}"; [ -n "$ver" ] && ACCEPT="${ACCEPT} ${ver}"
DIALOG='Yes, switch'
cap() { tmux -S "$SOCK" capture-pane -p -t "$SESSION" 2>/dev/null; }
count() { cap | grep -ci -- "$ACCEPT"; }
case "$MODE" in
  count) count; exit 0;;
  cancel) tmux -S "$SOCK" send-keys -t "$SESSION" Escape; echo CANCELLED; exit 0;;
  wait) ;;
  *) echo "pane-observe: one of --count/--wait/--cancel" >&2; exit 2;;
esac
[ -n "$ANSWER" ] && tmux -S "$SOCK" send-keys -t "$SESSION" Enter
# A NEW matching line (count above baseline) is the switch; the dialog means the CLI waits on a human.
deadline=$(( $(date +%s) + TIMEOUT ))
while :; do
  text="$(cap)"
  n=$(printf '%s\n' "$text" | grep -ci -- "$ACCEPT")
  if [ "$n" -gt "$BASE" ]; then echo ACCEPTED; exit 0; fi
  if [ -z "$ANSWER" ] && printf '%s' "$text" | grep -q -- "$DIALOG"; then echo DIALOG; exit 10; fi
  [ "$(date +%s)" -ge "$deadline" ] && { echo TIMEOUT; exit 11; }
  sleep 0.5
done
