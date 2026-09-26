#!/usr/bin/env bash
# task-notifier-supervisor.sh: an idle-ready pane over a STALE beat (a dead or
# hung agent) must ALERT ONCE and then ARM the standby -- not loop the alert.
# This pins the review fix end to end: real tmux idle pane + a stale beat + a
# nudge-capable stub notifier, with osascript mocked to count notifications.
#
# Run: bash tests/supervisor-nudge-alert-arms.test.sh
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
SUPERVISOR="$REPO/src/agent/codex/cli/task-notifier-supervisor.sh"
FOOTER="⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"

command -v tmux >/dev/null 2>&1 || { echo "SKIP: tmux not available"; exit 0; }
HOST="$(bash "$REPO/scripts/sutando-config.sh" host-label 2>/dev/null)"
[ -n "$HOST" ] || { echo "SKIP: no host-label"; exit 0; }

fail=0
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sut-nudge-alert.XXXXXX")"
SOCK="$WORK/tmux.sock"
trap 'tmux -S "$SOCK" kill-server 2>/dev/null; pkill -f "$WORK" 2>/dev/null; rm -rf "$WORK"' EXIT

# A nudge-capable stub notifier: it contains the literal "--nudge" so the
# supervisor's notifier_supports_nudge grep passes; armed (no args) it records
# the arm and holds; --nudge would exit 0, but a STALE beat means we alert+arm,
# never nudge, so that branch should not be reached.
STUB="$WORK/stub-notifier.sh"
cat > "$STUB" <<'EOS'
#!/bin/bash
if [ "${1:-}" = "--nudge" ]; then echo nudge >> "$SUTANDO_STUB_NUDGE"; exit 0; fi
echo "$$" > "$SUTANDO_STUB_MARKER"
trap 'exit 0' TERM
while true; do sleep 1; done
EOS
chmod +x "$STUB"

# osascript mocked to count notifications.
BIN="$WORK/bin"; mkdir -p "$BIN"
OSA_COUNT="$WORK/osascript.count"; : > "$OSA_COUNT"
printf '#!/bin/bash\necho x >> "%s"\nexit 0\n' "$OSA_COUNT" > "$BIN/osascript"; chmod +x "$BIN/osascript"

# A stale core beat: idle-looking pane, dead agent.
mkdir -p "$WORK/state/cores" "$WORK/inbox"
touch -t 202001010000 "$WORK/state/cores/$HOST.alive"

# The target pane shows the Claude idle footer with an empty composer.
tmux -S "$SOCK" new-session -d -s target -x 200 -y 50
tmux -S "$SOCK" send-keys -t target:0 -l "clear; printf '%s\n%s\n' '❯ ' '$FOOTER'; sleep 600"
tmux -S "$SOCK" send-keys -t target:0 Enter
sleep 1

MARK="$WORK/armed.marker"
NUDGES="$WORK/nudges.log"; : > "$NUDGES"
tmux -S "$SOCK" new-session -d -s supervisor -c "$REPO" \
  "env -u SUTANDO_INSTANCE_ID PATH=$BIN:\$PATH \
     SUTANDO_TMUX_SOCKET=$SOCK SUTANDO_TMUX_SESSION=target SUTANDO_TMUX_WINDOW=0 \
     SUTANDO_WORKSPACE_DIR=$WORK SUTANDO_TASKS_DIR=$WORK/inbox \
     SUTANDO_NOTIFIER_SCRIPT=$STUB SUTANDO_STUB_MARKER=$MARK SUTANDO_STUB_NUDGE=$NUDGES \
     SUTANDO_NOTIFIER_GRACE_PERIOD=2 SUTANDO_NOTIFIER_ROLE_POLL=1 \
     bash $SUPERVISOR > $WORK/sup.log 2>&1"

# Wait for the standby to arm (proves alert fell through to arm, not looped).
armed=0
for _ in $(seq 1 120); do [ -s "$MARK" ] && { armed=1; break; }; sleep 0.1; done
if [ "$armed" = 1 ]; then
  echo "ok   idle-ready over a stale beat armed the standby (did not loop)"
else
  echo "FAIL the standby never armed on an idle-ready pane over a stale beat"; fail=1
  cat "$WORK/sup.log" 2>/dev/null | tail -5
fi

# Give it well past two more grace periods; the notification must stay at one,
# and the nudge branch must never have run (stale beat -> alert, not nudge).
sleep 6
OSA_N="$(wc -l < "$OSA_COUNT" | tr -d ' ')"
if [ "$OSA_N" = "1" ]; then
  echo "ok   the health alert fired exactly once across multiple grace periods"
else
  echo "FAIL health alert fired $OSA_N times (expected 1: the one-shot latch)"; fail=1
fi
NUD_N="$(wc -l < "$NUDGES" | tr -d ' ')"
if [ "$NUD_N" = "0" ]; then
  echo "ok   no nudge was attempted on a stale beat"
else
  echo "FAIL the stub was nudged $NUD_N time(s) on a stale beat (expected 0)"; fail=1
fi

[ "$fail" -eq 0 ] && echo "PASS" || echo "FAILED"
exit "$fail"
