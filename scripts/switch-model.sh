#!/usr/bin/env bash
# switch-model.sh <model> [--dry-run] [--state-dir DIR] [--session NAME] [--socket PATH]
# Change the core's model without the CLI: pin it in settings.json (next launch)
# and send `/model <model>` to the live core's tmux pane (this session). Every
# switch is recorded with a timestamp so the pin is visible, never silent.
set -u
MODEL=""; DRY=""; STATE_DIR=""; SESSION="${SUTANDO_TMUX_SESSION:-sutando-core}"
SOCK="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"
while [ $# -gt 0 ]; do case "$1" in
  --dry-run) DRY=1;; --state-dir) STATE_DIR="${2:?}"; shift;;
  --session) SESSION="${2:?}"; shift;; --socket) SOCK="${2:?}"; shift;;
  -*) echo "switch-model: unknown flag $1" >&2; exit 2;;
  *) [ -z "$MODEL" ] && MODEL="$1" || { echo "switch-model: one model, got '$1' too" >&2; exit 2; };;
esac; shift; done
[ -n "$MODEL" ] || { echo "usage: switch-model.sh <model> [--dry-run]" >&2; exit 2; }
# Aliases the CLI's /model accepts, or a full id with an optional context tag.
if ! printf '%s' "$MODEL" | grep -Eq '^(default|opus|sonnet|haiku|fable|claude-[a-z0-9.-]+(\[1m\])?)$'; then
  echo "switch-model: refused '$MODEL' — not a model alias or claude-* id" >&2; exit 2
fi
HERE="$(cd "$(dirname "$0")/.." && pwd)"
CFG="$(bash "$HERE/scripts/sutando-config.sh" claude-home-path settings.json)"
[ -n "$STATE_DIR" ] || STATE_DIR="$(bash "$HERE/scripts/sutando-config.sh" workspace)/state"
if [ -n "$DRY" ]; then
  echo "dry-run: would pin model=$MODEL in $CFG, record $STATE_DIR/model-switch.json, send '/model $MODEL' to tmux -S $SOCK -t $SESSION"; exit 0
fi
PREV="$(python3 - "$CFG" "$MODEL" "$STATE_DIR" <<'PY'
import json, os, sys, time, tempfile
cfg, model, state_dir = sys.argv[1], sys.argv[2], sys.argv[3]
try: data = json.load(open(cfg))
except (OSError, ValueError): data = {}
prev = data.get("model")
data["model"] = model
os.makedirs(os.path.dirname(cfg), exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(cfg), prefix=".settings.")
with os.fdopen(fd, "w") as f: json.dump(data, f, indent=2); f.write("\n")
os.replace(tmp, cfg)
os.makedirs(state_dir, exist_ok=True)
rec = {"model": model, "previous": prev, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
       "by": "scripts/switch-model.sh", "settings": cfg}
fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".model-switch.")
with os.fdopen(fd, "w") as f: json.dump(rec, f, indent=2); f.write("\n")
os.replace(tmp, os.path.join(state_dir, "model-switch.json"))
print(prev if prev else "-")
PY
)" || { echo "switch-model: settings pin FAILED — nothing sent to the live core" >&2; exit 1; }
echo "pinned: $CFG model=$MODEL (was $PREV); record: $STATE_DIR/model-switch.json"
if tmux -S "$SOCK" has-session -t "=$SESSION" 2>/dev/null \
   && tmux -S "$SOCK" send-keys -t "=$SESSION" -l "/model $MODEL" \
   && tmux -S "$SOCK" send-keys -t "=$SESSION" Enter; then
  echo "live: sent '/model $MODEL' to tmux session $SESSION"; exit 0
fi
echo "live: NOT sent — no tmux session '$SESSION' on $SOCK; the pin applies at the next launch" >&2
exit 3
