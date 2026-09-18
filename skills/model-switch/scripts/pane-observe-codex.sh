#!/usr/bin/env bash
# pane-observe-codex.sh <session> --socket PATH --model M [--effort low|medium|high|xhigh] (--count | --wait --baseline N [--timeout S] | --cancel)
# Codex's bare /model opens two pickers; a digit key selects AND confirms a row. Acceptance = a new
# "Model changed to <M> <effort>" line for the REQUESTED id, whose effort word must equal --effort when given.
# Exit: 0 "ACCEPTED <effort>" · 11 TIMEOUT · 12 capture failed · 13 model not offered (picker cancelled) · 14 effort not offered · 15 "EFFORT-MISMATCH <seen>" · 16 EFFORT-UNREADABLE (no effort word in the acknowledgement).
set -u
SESSION="${1:?session}"; shift
SOCK=""; MODE=""; BASE=0; TIMEOUT=20; REQ=""; EFFORT=""
while [ $# -gt 0 ]; do case "$1" in
  --socket) SOCK="${2:?}"; shift;; --count) MODE=count;; --wait) MODE=wait;; --cancel) MODE=cancel;;
  --baseline) BASE="${2:?}"; shift;; --timeout) TIMEOUT="${2:?}"; shift;; --model) REQ="${2:?}"; shift;; --effort) EFFORT="${2:?}"; shift;;
  *) echo "pane-observe-codex: unknown arg $1" >&2; exit 2;;
esac; shift; done
[ -n "$SOCK" ] || { echo "pane-observe-codex: --socket required" >&2; exit 2; }
[ -n "$REQ" ] || [ "$MODE" = cancel ] || { echo "pane-observe-codex: --model required" >&2; exit 2; }
case "$EFFORT" in "") LABEL="";; low) LABEL=Low;; medium) LABEL=Medium;; high) LABEL=High;; xhigh) LABEL="Extra high";;
  *) echo "pane-observe-codex: --effort must be low|medium|high|xhigh, got '$EFFORT'" >&2; exit 2;; esac
# The id is followed by the effort word: "gpt-5.5 xhigh" must not be matched by a request for "gpt-5".
ESC="$(printf '%s' "$REQ" | sed 's/[.[\*^$]/\\&/g')"
ACCEPT="Model changed to ${ESC}( |\$)"
MODEL_PICKER='Select Model and Effort'; EFFORT_PICKER="Select Reasoning Level for ${ESC}( |\$)"
# A failed capture is a failed observation, never a zero.
cap() { tmux -S "$SOCK" capture-pane -p -t "$SESSION" 2>/dev/null || { echo "CAPTURE-FAILED"; return 12; }; }
count() { out="$(cap)" || { echo "CAPTURE-FAILED"; return 12; }; printf '%s\n' "$out" | grep -Ec -- "$ACCEPT"; return 0; }
# Rows below the LAST occurrence of a picker title, as "<n> <label...>"; the › marker of the selected row is dropped.
rows_after() { printf '%s\n' "$2" | awk -v t="$1" 'index($0,t){buf=""; on=1; next} on{buf=buf $0 "\n"} END{printf "%s", buf}' \
  | sed 's/›/ /g' | sed -nE 's/^[[:space:]]*([0-9]+)\. ([^[:space:]].*)$/\1 \2/p'; }
case "$MODE" in
  count) count; exit $?;;
  cancel) tmux -S "$SOCK" send-keys -t "$SESSION" Escape; echo CANCELLED; exit 0;;
  wait) ;;
  *) echo "pane-observe-codex: one of --count/--wait/--cancel" >&2; exit 2;;
esac
deadline=$(( $(date +%s) + TIMEOUT ))
# poll_for TITLE: print the pane once TITLE is on screen; TIMEOUT past the deadline (a blind poll never counts).
poll_for() { while :; do
  if text="$(cap)" && printf '%s\n' "$text" | grep -Eq -- "$1"; then printf '%s\n' "$text"; return 0; fi
  [ "$(date +%s)" -ge "$deadline" ] && return 11; sleep 0.3
done; }
# The title can be drawn a frame before its rows: an empty row list is "not yet", not "none".
while :; do
  text="$(poll_for "$MODEL_PICKER")" || { echo TIMEOUT; exit 11; }
  rows="$(rows_after "$MODEL_PICKER" "$text")"; [ -n "$rows" ] && break
  [ "$(date +%s)" -ge "$deadline" ] && { echo TIMEOUT; exit 11; }; sleep 0.3
done
# Exact id match on the row's first word: "gpt-5.5" is not a match for "gpt-5.5-x".
digit="$(printf '%s\n' "$rows" | awk -v m="$REQ" '$2==m{print $1; exit}')"
if ! printf '%s' "$digit" | grep -Eq '^[1-9]$'; then
  tmux -S "$SOCK" send-keys -t "$SESSION" Escape
  echo "pane-observe-codex: '$REQ' is not a selectable row of the model picker; picker cancelled. Rows seen: $(printf '%s\n' "$rows" | awk '{print $2}' | paste -sd, -)" >&2
  echo NOT-OFFERED; exit 13
fi
tmux -S "$SOCK" send-keys -t "$SESSION" -l "$digit"
text="$(poll_for "$EFFORT_PICKER")" || { echo TIMEOUT; exit 11; }
if [ -z "$LABEL" ]; then tmux -S "$SOCK" send-keys -t "$SESSION" Enter
else
  rows="$(rows_after "Select Reasoning Level for" "$text")"
  digit="$(printf '%s\n' "$rows" | awk -v l="$LABEL" '{s=$0; sub(/^[0-9]+ /,"",s)} index(s,l)==1{print $1; exit}')"
  if ! printf '%s' "$digit" | grep -Eq '^[1-9]$'; then
    tmux -S "$SOCK" send-keys -t "$SESSION" Escape; tmux -S "$SOCK" send-keys -t "$SESSION" Escape
    echo "pane-observe-codex: reasoning level '$LABEL' is not a row of the effort picker; pickers cancelled. Rows seen: $(printf '%s\n' "$rows" | cut -d' ' -f2- | paste -sd, -)" >&2
    echo NOT-OFFERED; exit 14
  fi
  tmux -S "$SOCK" send-keys -t "$SESSION" -l "$digit"
fi
# The NEWEST matching line (count above baseline) is the switch; its effort word is what the CLI applied.
while :; do
  if text="$(cap)" && [ "$(printf '%s\n' "$text" | grep -Ec -- "$ACCEPT")" -gt "$BASE" ]; then
    ack="$(printf '%s\n' "$text" | grep -E -- "$ACCEPT" | tail -1)"
    seen="$(printf '%s\n' "$ack" | sed -E "s/.*Model changed to ${ESC} ?([[:alnum:]_-]*).*/\\1/")"
    # An acknowledgement with no readable effort word leaves the applied reasoning level
    # unknown; there is nothing to record, so it is never an acceptance.
    if [ -z "$seen" ]; then
      echo "pane-observe-codex: the acknowledgement for $REQ carries no readable effort word (${ack}); the applied reasoning level is unknown, not accepted." >&2
      echo EFFORT-UNREADABLE; exit 16
    fi
    if [ -n "$EFFORT" ] && [ "$seen" != "$EFFORT" ]; then
      echo "pane-observe-codex: requested effort '$EFFORT' but the CLI applied '${seen:-none}' (Model changed to $REQ ${seen}); not accepted." >&2
      echo "EFFORT-MISMATCH $seen"; exit 15
    fi
    echo "ACCEPTED${seen:+ $seen}"; exit 0
  fi
  [ "$(date +%s)" -ge "$deadline" ] && { echo TIMEOUT; exit 11; }
  sleep 0.3
done
