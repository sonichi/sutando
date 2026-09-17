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
PY="$(bash "$(cd "$(dirname "$0")/.." && pwd)/scripts/sutando-config.sh" python-bin)"
[ -x "$PY" ] || { echo "tmux-send-line: python interpreter not found ($PY) — cannot inspect the prompt, not sending" >&2; exit 7; }
# One sender at a time per socket+session: inspection and both send-keys run
# under a lock, so two callers cannot interleave payloads before either Enter.
LOCK="${TMPDIR:-/tmp}/tmux-send-line.$(printf '%s' "$SOCK:$SESSION" | "$PY" -c 'import sys,hashlib;print(hashlib.sha1(sys.stdin.read().encode()).hexdigest()[:12])').lock"
exec 9>"$LOCK"
"$PY" -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX)' || { echo "tmux-send-line: could not take the send lock" >&2; exit 7; }
# The current prompt is the LAST line starting with the runtime's glyph (Claude \u276f,
# Codex \u203a; scrollback holds old ones); its input is what follows the glyph and one
# optional space/nbsp. Both CLIs draw hints in the composer -- Codex a DIM placeholder,
# Claude a grey ghost suggestion -- while typed text is unstyled, so the pane is always
# read with -e and any styled run after the glyph is dropped before deciding "pending".
# A failed capture or parse is UNKNOWN, never "empty": refuse rather than send.
# One capture+parse, reused for the initial read and Codex's post-delay recheck below.
_capture() {
  "$TMUX" -S "$SOCK" capture-pane -e -p -t "$SESSION" 2>/dev/null
}
# The parsed text at the current prompt line (as before), from an ALREADY-captured pane.
_pending() {
  printf '%s\n' "$1" | "$PY" -c 'import sys,re
rt=sys.argv[1]; glyph={"claude":"\u276f","codex":"\u203a"}[rt]
SGR=re.compile(r"\x1b\[[0-9;]*m")
# dim (2) or a grey 256-colour foreground (38;5;2xx), up to the reset/normal-intensity
# code that ends it -- both CLIs use styled runs for placeholder/ghost text only.
GHOST=re.compile(r"\x1b\[(?:2|38;5;2[0-9]{2})m.*?(?=\x1b\[(?:0|22|39)m|$)")
last=""
for l in sys.stdin.read().splitlines():
    plain=SGR.sub("",l).lstrip(" \t")
    if not plain.startswith(glyph): continue
    idx=l.find(glyph)
    r=SGR.sub("",GHOST.sub("",l[idx+len(glyph):]))
    if r[:1] in (" ", "\u00a0"): r=r[1:]
    last=r.rstrip()
print(last)' "$RUNTIME"
}
# Everything BELOW the current prompt line, stripped of colour -- a fingerprint of what the
# rest of the pane shows. A stale prompt line matching the staged payload proves nothing if
# a gate/dialog has appeared beneath it; unchanged AFTER content is what proves it is live.
_after() {
  printf '%s\n' "$1" | "$PY" -c 'import sys,re
rt=sys.argv[1]; glyph={"claude":"\u276f","codex":"\u203a"}[rt]
SGR=re.compile(r"\x1b\[[0-9;]*m")
lines=sys.stdin.read().splitlines()
last_i=-1
for i,l in enumerate(lines):
    if SGR.sub("",l).lstrip(" \t").startswith(glyph): last_i=i
print("\n".join(SGR.sub("",x).strip() for x in lines[last_i+1:]) if last_i>=0 else "")' "$RUNTIME"
}
CAP="$(_capture)"; RC=$?
[ $RC -eq 0 ] || { echo "tmux-send-line: capture failed — prompt unknown, not sending" >&2; exit 7; }
PENDING="$(_pending "$CAP")"; RC=$?
[ $RC -eq 0 ] || { echo "tmux-send-line: prompt parse failed — not sending" >&2; exit 7; }
AFTER_BASELINE="$(_after "$CAP")"; RC=$?
[ $RC -eq 0 ] || { echo "tmux-send-line: after-prompt parse failed — not sending" >&2; exit 7; }
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
  RECAP="$(_capture)"; RC=$?
  [ $RC -eq 0 ] || { echo "tmux-send-line: capture failed during the delay — Enter withheld" >&2; exit 7; }
  RECHECK="$(_pending "$RECAP")"; RC=$?
  [ $RC -eq 0 ] || { echo "tmux-send-line: prompt parse failed during the delay — Enter withheld" >&2; exit 7; }
  if [ "$RECHECK" != "$LINE" ]; then echo "tmux-send-line: pane changed during the paste-burst delay (composer now '${RECHECK:0:60}', expected '$LINE') — Enter withheld" >&2; exit 5; fi
  # A matching prompt LINE is not proof the composer is still live: it can be a stale line
  # from before a gate/dialog appeared beneath it. Nothing may have changed below it either.
  AFTER_NOW="$(_after "$RECAP")"; RC=$?
  [ $RC -eq 0 ] || { echo "tmux-send-line: after-prompt parse failed during the delay — Enter withheld" >&2; exit 7; }
  if [ "$AFTER_NOW" != "$AFTER_BASELINE" ]; then echo "tmux-send-line: pane state changed below the prompt during the delay (was '${AFTER_BASELINE:0:60}', now '${AFTER_NOW:0:60}') — Enter withheld" >&2; exit 5; fi
fi
"$TMUX" -S "$SOCK" send-keys -t "$SESSION" Enter || { echo "tmux-send-line: send-keys failed" >&2; exit 1; }
echo "sent '$LINE' to $SESSION"
