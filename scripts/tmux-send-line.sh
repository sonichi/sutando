#!/usr/bin/env bash
# tmux-send-line.sh <session> <line> [--socket PATH] [--runtime claude|codex] [--refuse-if-pending] [--skip-if-queued WORD] [--dry-run]
# The ONE sender for a line typed into a Sutando core pane: has-session, read
# the current prompt line, apply the queued-input policy, then send-keys -l + Enter.
# Exit: 0 sent · 3 no session · 4 no tmux · 5 pending text · 6 WORD already queued · 7 inspection failed (refused).
set -u -o pipefail
SESSION="${1:?session}"; LINE="${2:?line}"; shift 2; RUNTIME=claude
SOCK="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"; REFUSE=""; SKIPWORD=""; DRY=""
while [ $# -gt 0 ]; do case "$1" in
  --socket) SOCK="${2:?}"; shift;; --refuse-if-pending) REFUSE=1;; --skip-if-queued) SKIPWORD="${2:?}"; shift;;
  --runtime) RUNTIME="${2:?}"; shift;; --dry-run) DRY=1;; *) echo "tmux-send-line: unknown flag $1" >&2; exit 2;; esac; shift; done
case "$RUNTIME" in claude|codex) ;; *) echo "tmux-send-line: unknown --runtime '$RUNTIME' (claude|codex)" >&2; exit 2;; esac
# A launchd-launched caller (the menu-bar app) has a bare PATH; path_helper
# restores /etc/paths.d, where Homebrew registers itself — no literal prefix.
TMUX="$(command -v tmux 2>/dev/null)"
if [ -z "$TMUX" ] && [ -x /usr/libexec/path_helper ]; then eval "$(/usr/libexec/path_helper -s)"; TMUX="$(command -v tmux 2>/dev/null)"; fi
[ -n "$TMUX" ] || { echo "tmux-send-line: no tmux binary" >&2; exit 4; }
"$TMUX" -S "$SOCK" has-session -t "=$SESSION" 2>/dev/null || { echo "tmux-send-line: no session '$SESSION' on $SOCK" >&2; exit 3; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(bash "$REPO/scripts/sutando-config.sh" python-bin)"
[ -x "$PY" ] || { echo "tmux-send-line: python interpreter not found ($PY) — cannot inspect the prompt, not sending" >&2; exit 7; }
# One sender at a time per socket+session: inspection and both send-keys run
# under a lock, so two callers cannot interleave payloads before either Enter.
LOCK="${TMPDIR:-/tmp}/tmux-send-line.$(printf '%s' "$SOCK:$SESSION" | "$PY" -c 'import sys,hashlib;print(hashlib.sha1(sys.stdin.read().encode()).hexdigest()[:12])').lock"
exec 9>"$LOCK"
"$PY" -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX)' || { echo "tmux-send-line: could not take the send lock" >&2; exit 7; }
# The text pending at the current prompt is read by src/delivery/pane_gate.py, which
# needs attributes on EITHER runtime: Codex draws a dim placeholder, Claude a grey
# ghost suggestion, and pane_gate.py's own prompt_line() strips whichever applies.
# A failed capture or parse is UNKNOWN, never "empty": refuse rather than send.
# One capture, reused for the initial read and Codex's post-delay recheck below.
_capture() {
  "$TMUX" -S "$SOCK" capture-pane -e -p -t "$SESSION" 2>/dev/null
}
# Pane width. A composer line longer than this wraps onto rows with no prompt glyph;
# without it they cannot be told apart from content below the prompt.
_width() {
  "$TMUX" -S "$SOCK" display-message -p -t "$SESSION" '#{pane_width}' 2>/dev/null | tr -dc '0-9'
}
WIDTH="$(_width)"
CAP="$(_capture)" || { echo "tmux-send-line: capture-pane failed — prompt unknown, not sending" >&2; exit 7; }
# pane_gate owns the parse for both runtimes, including rejoining wrapped continuation rows;
# --width 0 simply disables the rejoin, so a missing width still fails closed below.
PENDING="$(printf '%s\n' "$CAP" | "$PY" "$REPO/src/delivery/pane_gate.py" pending --runtime "$RUNTIME" --width "${WIDTH:-0}")"; PRC=$?
# Exit 3 = no prompt line for this runtime: UNKNOWN, not "empty composer". Only a caller
# that asked for the composer guard is refused; a plain delivery to a pane that never had
# a CLI prompt (a shell, a pager) is unaffected and still sends.
if [ "$PRC" = 3 ]; then
  [ -n "$REFUSE" ] && { echo "tmux-send-line: no $RUNTIME prompt line in the pane — composer unknown, not sent" >&2; exit 5; }
  PENDING=""
elif [ "$PRC" != 0 ]; then
  echo "tmux-send-line: prompt unknown or unparseable — not sending" >&2; exit 7
fi
AFTER_BASELINE="$(printf '%s\n' "$CAP" | "$PY" "$REPO/src/delivery/pane_gate.py" after --runtime "$RUNTIME" --width "${WIDTH:-0}")" || { echo "tmux-send-line: prompt unknown or unparseable — not sending" >&2; exit 7; }
if [ -n "$SKIPWORD" ] && [ "$PENDING" = "$SKIPWORD" ]; then echo "tmux-send-line: '$SKIPWORD' already queued at the prompt — not sent" >&2; exit 6; fi
if [ -n "$REFUSE" ] && [ -n "$PENDING" ]; then echo "tmux-send-line: prompt carries pending text (${PENDING:0:60}) — not sent" >&2; exit 5; fi
[ -n "$DRY" ] && { echo "dry-run: would send '$LINE' + Enter to $SESSION on $SOCK (pending: '${PENDING}')"; exit 0; }
"$TMUX" -S "$SOCK" send-keys -t "$SESSION" -l "$LINE" || { echo "tmux-send-line: send-keys failed" >&2; exit 1; }
# Codex reads an Enter within 120ms of a typed burst as a pasted newline (PASTE_ENTER_SUPPRESS_WINDOW), not a submit.
[ "$RUNTIME" = codex ] && sleep 0.25
# The lock excludes cooperating senders, not operator keystrokes: a picker or dialog
# can appear in the pane during Codex's delay. Re-read and only Enter if the composer
# still shows exactly the payload THIS invocation staged; otherwise abort without Enter.
if [ "$RUNTIME" = codex ]; then
  RECAP="$("$TMUX" -S "$SOCK" capture-pane -e -p -t "$SESSION" 2>/dev/null)" || { echo "tmux-send-line: capture-pane failed — Enter withheld" >&2; exit 7; }
  RECHECK="$(printf '%s\n' "$RECAP" | "$PY" "$REPO/src/delivery/pane_gate.py" pending --runtime "$RUNTIME" --width "${WIDTH:-0}")" || { echo "tmux-send-line: prompt unknown or unparseable — Enter withheld" >&2; exit 7; }
  if [ "$RECHECK" != "$LINE" ]; then echo "tmux-send-line: pane changed during the paste-burst delay (composer now '${RECHECK:0:60}', expected '$LINE') — Enter withheld" >&2; exit 5; fi
  # A matching prompt LINE is not proof the composer is still live: it can be a stale line
  # from before a gate/dialog appeared beneath it. Nothing may have changed below it either.
  AFTER_NOW="$(printf '%s\n' "$RECAP" | "$PY" "$REPO/src/delivery/pane_gate.py" after --runtime "$RUNTIME" --width "${WIDTH:-0}")" || { echo "tmux-send-line: prompt unknown or unparseable — Enter withheld" >&2; exit 7; }
  if [ "$AFTER_NOW" != "$AFTER_BASELINE" ]; then echo "tmux-send-line: pane state changed below the prompt during the delay (was '${AFTER_BASELINE:0:60}', now '${AFTER_NOW:0:60}') — Enter withheld" >&2; exit 5; fi
fi
"$TMUX" -S "$SOCK" send-keys -t "$SESSION" Enter || { echo "tmux-send-line: send-keys failed" >&2; exit 1; }
echo "sent '$LINE' to $SESSION"
