#!/usr/bin/env bash
# tmux-send-line.sh <session> <line> [--socket PATH] [--refuse-if-pending] [--skip-if-queued WORD] [--dry-run]
# The ONE sender for a line typed into a Sutando core pane: has-session, read
# the current prompt line, apply the queued-input policy, then send-keys -l + Enter.
# Exit: 0 sent · 3 no session · 4 no tmux · 5 pending text · 6 WORD already queued · 7 inspection failed (refused).
set -u -o pipefail
SESSION="${1:?session}"; LINE="${2:?line}"; shift 2
SOCK="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"; REFUSE=""; SKIPWORD=""; DRY=""
while [ $# -gt 0 ]; do case "$1" in
  --socket) SOCK="${2:?}"; shift;; --refuse-if-pending) REFUSE=1;; --skip-if-queued) SKIPWORD="${2:?}"; shift;;
  --dry-run) DRY=1;; *) echo "tmux-send-line: unknown flag $1" >&2; exit 2;; esac; shift; done
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
# The current prompt is the LAST line starting with ❯ (scrollback holds old
# ones); its input is what follows the glyph and one optional space/nbsp.
# A failed capture or parse is UNKNOWN, never "empty": refuse rather than send.
CAP="$("$TMUX" -S "$SOCK" capture-pane -p -t "$SESSION" 2>/dev/null)" || { echo "tmux-send-line: capture-pane failed — prompt unknown, not sending" >&2; exit 7; }
PENDING="$(printf '%s\n' "$CAP" | "$PY" -c 'import sys
last=""
for l in sys.stdin.read().splitlines():
    s=l.lstrip(" \t")
    if s.startswith("\u276f"):
        r=s[1:]
        if r[:1] in (" ", "\u00a0"): r=r[1:]
        last=r.rstrip()
print(last)')" || { echo "tmux-send-line: prompt parse failed — not sending" >&2; exit 7; }
if [ -n "$SKIPWORD" ] && [ "$PENDING" = "$SKIPWORD" ]; then echo "tmux-send-line: '$SKIPWORD' already queued at the prompt — not sent" >&2; exit 6; fi
if [ -n "$REFUSE" ] && [ -n "$PENDING" ]; then echo "tmux-send-line: prompt carries pending text (${PENDING:0:60}) — not sent" >&2; exit 5; fi
[ -n "$DRY" ] && { echo "dry-run: would send '$LINE' + Enter to $SESSION on $SOCK (pending: '${PENDING}')"; exit 0; }
"$TMUX" -S "$SOCK" send-keys -t "$SESSION" -l "$LINE" && "$TMUX" -S "$SOCK" send-keys -t "$SESSION" Enter || { echo "tmux-send-line: send-keys failed" >&2; exit 1; }
echo "sent '$LINE' to $SESSION"
