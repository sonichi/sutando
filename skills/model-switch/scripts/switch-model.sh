#!/usr/bin/env bash
# switch-model.sh <model> [--dry-run] [--state-dir DIR] [--session NAME] [--socket PATH]
# Change the core's model without the CLI: record the switch, pin it in the
# runtime's settings.json (next launch), send `/model <model>` to the live core
# pane (now). The record is written first so a pin can never exist unrecorded.
set -u
MODEL=""; DRY=""; STATE_DIR=""; SESSION="${SUTANDO_TMUX_SESSION:-}"; SOCK="${SUTANDO_TMUX_SOCKET:-}"
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
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
CFG="$(bash "$REPO/scripts/sutando-config.sh" claude-home-path settings.json)"
PY="$(bash "$REPO/scripts/sutando-config.sh" python-bin)"
[ -n "$SOCK" ] || SOCK="$(bash "$REPO/scripts/sutando-config.sh" tmux-socket)"
[ -n "$SESSION" ] || SESSION="sutando-core"
[ -n "$STATE_DIR" ] || STATE_DIR="$(bash "$REPO/scripts/sutando-config.sh" workspace)/state"
if [ -n "$DRY" ]; then
  echo "dry-run: would record $STATE_DIR/model-switch.json, pin model=$MODEL in $CFG, send '/model $MODEL' to tmux -S $SOCK -t $SESSION (python: $PY)"; exit 0
fi
# Record first, then pin; a failed pin removes the record it would have described.
OUT="$("$PY" - "$CFG" "$MODEL" "$STATE_DIR" <<'PYEOF'
import json, os, sys, time, tempfile
cfg, model, state_dir = sys.argv[1], sys.argv[2], sys.argv[3]
try: data = json.load(open(cfg))
except (OSError, ValueError): data = {}
prev = data.get("model")
rec = {"model": model, "previous": prev, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
       "by": "skills/model-switch/scripts/switch-model.sh", "settings": cfg}
rec_path = os.path.join(state_dir, "model-switch.json")
try:
    os.makedirs(state_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".model-switch.")
    with os.fdopen(fd, "w") as f: json.dump(rec, f, indent=2); f.write("\n")
    os.replace(tmp, rec_path)
except OSError as e:
    print(f"RECORD-FAILED {e}"); sys.exit(1)
try:
    data["model"] = model
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(cfg), prefix=".settings.")
    with os.fdopen(fd, "w") as f: json.dump(data, f, indent=2); f.write("\n")
    os.replace(tmp, cfg)
except OSError as e:
    try: os.remove(rec_path)
    except OSError: pass
    print(f"PIN-FAILED {e}"); sys.exit(1)
print(f"OK {prev if prev else '-'}")
PYEOF
)"; RC=$?
case "$OUT" in
  RECORD-FAILED*) echo "switch-model: could not write the switch record ($STATE_DIR) — nothing changed: ${OUT#RECORD-FAILED }" >&2; exit 1;;
  PIN-FAILED*)    echo "switch-model: settings pin FAILED ($CFG) — record removed, nothing changed: ${OUT#PIN-FAILED }" >&2; exit 1;;
  OK*) ;;
  *) echo "switch-model: unexpected python outcome (rc=$RC): $OUT" >&2; exit 1;;
esac
echo "pinned: $CFG model=$MODEL (was ${OUT#OK }); record: $STATE_DIR/model-switch.json"
if tmux -S "$SOCK" has-session -t "=$SESSION" 2>/dev/null \
   && tmux -S "$SOCK" send-keys -t "$SESSION" -l "/model $MODEL" \
   && tmux -S "$SOCK" send-keys -t "$SESSION" Enter; then
  echo "live: sent '/model $MODEL' to tmux session $SESSION"; exit 0
fi
echo "live: NOT sent — no tmux session '$SESSION' on $SOCK; the pin applies at the next launch" >&2
exit 3
