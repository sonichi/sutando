#!/usr/bin/env bash
# tmux stand-in rendering a Codex 0.146 pane driven by the keys it received: bare /model
# opens "Select Model and Effort"; a digit picks a row and opens "Select Reasoning Level
# for <model>"; a digit or Enter there prints "• Model changed to <model> <effort>" and,
# like the CLI, rewrites TMUX_CODEX_CONFIG. Escape closes the pickers. Every argv goes to TMUX_LOG.
printf '%s\n' "$*" >> "$TMUX_LOG"
[ -n "${TMUX_FAIL:-}" ] && exit 1
case " $* " in *" capture-pane "*) ;; *) exit 0;; esac
n=$(( $(cat "$TMUX_LOG.caps" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$TMUX_LOG.caps"; [ "${TMUX_FAIL_CAPTURE_N:-0}" = "$n" ] && exit 1
[ -n "${TMUX_CAP_DELAY:-}" ] && sleep "$TMUX_CAP_DELAY"
ROWS="${TMUX_CODEX_ROWS:-  1. gpt-5.6-sol (default)  Reliable agentic coding
  2. gpt-5.6-luna  Fast frontier model
  3. gpt-5.6-mini  Small and quick
› 4. gpt-5.5 (current)  Proven previous-generation model}"
EFFORTS='  1. Low
› 2. Medium (default)
  3. High
  4. Extra high
  5. More reasoning…'
COMPOSER="${TMUX_PANE_TEXT:-$(printf '\033[1m›\033[0m \033[2mImprove documentation in @filename\033[0m\n  gpt-5.5 xhigh fast\n')}"
# Keys after the LAST "/model" send, minus that send's own Enter, are the picker drive.
picks="$(awk '/-l \/model$/{p=""; seen=1; skip=1; next} seen && /( -l [0-9]| Enter| Escape)$/{ if (skip && / Enter$/) {skip=0; next}; sub(/.* -l /,""); sub(/.* /,""); p=p $0 "\n"} END{printf "%s", p}' "$TMUX_LOG")"
model_of() { printf '%s\n' "$ROWS" | sed 's/›/ /' | awk -v d="$1" '$1==d"."{print $2; exit}'; }
effort_of() { case "$1" in 1) echo low;; 2|Enter) echo medium;; 3) echo high;; 4) echo xhigh;; *) echo "";; esac; }
body=""
if grep -q -- '-l /model$' "$TMUX_LOG" && ! printf '%s' "$picks" | grep -q Escape; then
  k=$(printf '%s' "$picks" | grep -c .); p1="$(printf '%s\n' "$picks" | sed -n 1p)"; p2="$(printf '%s\n' "$picks" | sed -n 2p)"
  if [ "$k" -eq 0 ]; then body="  Select Model and Effort\n$ROWS\n"
  elif [ "$k" -eq 1 ]; then body="  Select Reasoning Level for $(model_of "$p1")\n$EFFORTS\n"
  elif [ -z "${TMUX_NO_ACCEPT:-}" ]; then
    m="$(model_of "$p1")"; e="${TMUX_ACCEPT_EFFORT_AS:-$(effort_of "$p2")}"; body="• Model changed to $m $e\n"
    [ -n "${TMUX_CODEX_CONFIG:-}" ] && printf 'model = "%s"\nmodel_reasoning_effort = "%s"\n' "$m" "$e" > "$TMUX_CODEX_CONFIG"
  fi
fi
printf '%b' "${TMUX_CODEX_PRIOR:-}$body"; printf '%s\n' "$COMPOSER"
exit 0
