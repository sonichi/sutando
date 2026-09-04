#!/usr/bin/env bash
# switch-model.sh <model> [--dry-run] [--state-dir DIR] [--brain DIR] [--session NAME] [--socket PATH]
# Change the core's model without the CLI: record the switch, pin it in the
# runtime's settings.json (next launch), send `/model <model>` to the live core
# pane (now). The record is written first so a pin can never exist unrecorded.
set -u
MODEL=""; DRY=""; STATE_DIR=""; BRAIN=""; DESCF=""; SESSION="${SUTANDO_TMUX_SESSION:-}"; SOCK="${SUTANDO_TMUX_SOCKET:-}"
while [ $# -gt 0 ]; do case "$1" in
  --dry-run) DRY=1;; --state-dir) STATE_DIR="${2:?}"; shift;;
  --session) SESSION="${2:?}"; shift;; --socket) SOCK="${2:?}"; shift;; --brain) BRAIN="${2:?}"; shift;; --descriptor-file) DESCF="${2:?}"; shift;;
  -*) echo "switch-model: unknown flag $1" >&2; exit 2;;
  *) [ -z "$MODEL" ] && MODEL="$1" || { echo "switch-model: one model, got '$1' too" >&2; exit 2; };;
esac; shift; done
[ -n "$MODEL" ] || { echo "usage: switch-model.sh <model> [--dry-run]" >&2; exit 2; }
# Aliases the CLI's /model accepts, or a full id with an optional context tag.
if ! printf '%s' "$MODEL" | grep -Eq '^(default|opus|sonnet|haiku|fable|claude-[a-z0-9.-]+(\[1m\])?)$'; then
  echo "switch-model: refused '$MODEL' — not a model alias or claude-* id" >&2; exit 2
fi
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
PY="$(bash "$REPO/scripts/sutando-config.sh" python-bin)"
# Defaults come from the configured core's runtime descriptor (brain, socket,
# session — runtime-authored, foreign-caller safe), never from ambient env.
if [ -n "$DESCF" ]; then DESC="$(cat "$DESCF")"; else DESC="$(bash "$REPO/scripts/sutando-config.sh" runtime 2>/dev/null)"; fi
# One field per line, read whole-line: a brain under "Application Support" or a
# spaced socket/session must survive the hop from JSON into shell variables.
{ IFS= read -r DBRAIN; IFS= read -r DSOCK; IFS= read -r DSESSION; } <<EOF_DESC
$(printf '%s' "$DESC" | "$PY" -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: d={}
for k in ("brain","socket","session"): print(str(d.get(k) or "").replace("\n"," "))')
EOF_DESC
[ -n "$BRAIN" ] || BRAIN="${DBRAIN:-$(bash "$REPO/scripts/sutando-config.sh" claude-sutando-config-dir)}"
CFG="$BRAIN/settings.json"
[ -n "$SOCK" ] || SOCK="${DSOCK:-/tmp/sutando-tmux.sock}"
[ -n "$SESSION" ] || SESSION="${DSESSION:-sutando-core}"
[ -n "$STATE_DIR" ] || STATE_DIR="$(bash "$REPO/scripts/sutando-config.sh" workspace)/state"
if [ -n "$DRY" ]; then
  echo "dry-run: would record $STATE_DIR/model-switch.json, pin model=$MODEL in $CFG, send '/model $MODEL' to tmux -S $SOCK -t $SESSION (python: $PY)"; exit 0
fi
# One switch at a time per brain: the lock spans preflight, the settings/record
# transaction and the live send, so two invocations cannot interleave.
LOCK="$STATE_DIR/.model-switch.lock"; mkdir -p "$STATE_DIR" 2>/dev/null
if ! { exec 9>"$LOCK"; } 2>/dev/null; then echo "switch-model: could not open the switch lock ($LOCK) — nothing changed" >&2; exit 1; fi
"$PY" -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX)' || { echo "switch-model: could not take the switch lock — nothing changed" >&2; exit 1; }
# Claude Code only: /model and the settings.json pin are its. The Codex core takes
# its model at launch (codex -m from SUTANDO_CORE_MODEL), so the fix there is a restart.
RUNTIME="$(bash "$REPO/scripts/sutando-config.sh" core-runtime 2>/dev/null || echo claude)"
if [ "$RUNTIME" = "codex" ]; then
  echo "switch-model: refused — the configured core runtime is codex; its model is a launch argument: SUTANDO_CORE_MODEL=$MODEL bash src/agent/codex/cli/start-cli.sh --restart (owner-gated restart). Nothing changed." >&2; exit 4
fi
# Preflight the live pane before any write through the ONE sender
# (scripts/tmux-send-line.sh): its --dry-run inspects the prompt under the
# socket lock and refuses (5) on pending text, (7) on a failed inspection.
SENDER="$REPO/scripts/tmux-send-line.sh"
LIVE=""
if [ -x "$SENDER" ] || [ -f "$SENDER" ]; then
  bash "$SENDER" "$SESSION" "/model $MODEL" --socket "$SOCK" --refuse-if-pending --dry-run > /dev/null 2> "$STATE_DIR/.send-preflight.err"; PRC=$?
  case $PRC in
    0) LIVE=1;;
    3) LIVE="";;
    5) echo "switch-model: refused — $(cat "$STATE_DIR/.send-preflight.err"); nothing changed" >&2; exit 5;;
    *) echo "switch-model: refused — $(cat "$STATE_DIR/.send-preflight.err"); nothing changed" >&2; exit 7;;
  esac
else
  echo "switch-model: shared sender missing ($SENDER) — nothing changed" >&2; exit 7
fi
# Record first, then pin; a failed pin removes the record it would have described.
OUT="$("$PY" - "$CFG" "$MODEL" "$STATE_DIR" <<'PYEOF'
import json, os, sys, time, tempfile
cfg, model, state_dir = sys.argv[1], sys.argv[2], sys.argv[3]
try: prior_bytes = open(cfg, "rb").read()
except OSError: prior_bytes = None
try: data = json.loads(prior_bytes) if prior_bytes is not None else {}
except ValueError: data = {}
prev = data.get("model")
rec = {"model": model, "previous": prev, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
       "by": "skills/model-switch/scripts/switch-model.sh", "settings": cfg}
rec_path = os.path.join(state_dir, "model-switch.json")
# Stage the record beside the old one; the prior record is never touched until
# the pin has succeeded, so a failed pin leaves the last trusted record intact.
try:
    os.makedirs(state_dir, exist_ok=True)
    fd, staged = tempfile.mkstemp(dir=state_dir, prefix=".model-switch.staged.")
    with os.fdopen(fd, "w") as f: json.dump(rec, f, indent=2); f.write("\n")
except OSError as e:
    print(f"RECORD-FAILED {e}"); sys.exit(1)
try:
    data["model"] = model
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(cfg), prefix=".settings.")
    with os.fdopen(fd, "w") as f: json.dump(data, f, indent=2); f.write("\n")
    os.replace(tmp, cfg)
except OSError as e:
    try: os.remove(staged)
    except OSError: pass
    print(f"PIN-FAILED {e}"); sys.exit(1)
try:
    os.replace(staged, rec_path)
except OSError as e:
    # The pin landed but cannot be recorded: an unrecorded pin is the one state
    # this transaction forbids, so restore the exact prior settings bytes.
    try:
        if prior_bytes is None:
            os.remove(cfg)
        else:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(cfg), prefix=".settings.")
            with os.fdopen(fd, "wb") as f: f.write(prior_bytes)
            os.replace(tmp, cfg)
        rolled = "settings rolled back to the prior bytes"
    except OSError as e2:
        rolled = f"ROLLBACK FAILED ({e2}) — settings.json is pinned and UNRECORDED"
    try: os.remove(staged)
    except OSError: pass
    print(f"RECORD-COMMIT-FAILED {e}; {rolled}"); sys.exit(1)
print(f"OK {prev if prev else '-'}")
PYEOF
)"; RC=$?
case "$OUT" in
  RECORD-FAILED*) echo "switch-model: could not write the switch record ($STATE_DIR) — nothing changed: ${OUT#RECORD-FAILED }" >&2; exit 1;;
  PIN-FAILED*)    echo "switch-model: settings pin FAILED ($CFG) — prior switch record kept, nothing changed: ${OUT#PIN-FAILED }" >&2; exit 1;;
  RECORD-COMMIT-FAILED*) echo "switch-model: switch record could not be committed ($STATE_DIR/model-switch.json) — ${OUT#RECORD-COMMIT-FAILED }" >&2; exit 1;;
  OK*) ;;
  *) echo "switch-model: unexpected python outcome (rc=$RC): $OUT" >&2; exit 1;;
esac
echo "pinned: $CFG model=$MODEL (was ${OUT#OK }); record: $STATE_DIR/model-switch.json"
if [ -n "$LIVE" ] && bash "$SENDER" "$SESSION" "/model $MODEL" --socket "$SOCK" --refuse-if-pending > /dev/null; then
  echo "live: sent '/model $MODEL' to tmux session $SESSION (if a turn is running, the CLI queues it and switches when the turn ends)"; exit 0
fi
echo "live: NOT sent — no tmux session '$SESSION' on $SOCK; the pin applies at the next launch" >&2
exit 3
