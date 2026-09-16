#!/usr/bin/env bash
# switch-model.sh <model> [--dry-run] [--confirm] [--effort low|medium|high|xhigh] [--accept-timeout S] [--state-dir DIR] [--brain DIR] [--session NAME] [--socket PATH]
# Sends /model through the shared sender and records only after the CLI accepts THAT model; the CLI's own
# file (Claude settings.json, Codex config.toml) is the CLI's to write.
set -u
MODEL=""; DRY=""; STATE_DIR=""; BRAIN=""; DESCF=""; SESSION="${SUTANDO_TMUX_SESSION:-}"; SOCK="${SUTANDO_TMUX_SOCKET:-}"; CONFIRM=""; ACCEPT_TIMEOUT=20; EFFORT=""; EFFORT_SEEN=""
while [ $# -gt 0 ]; do case "$1" in
  --dry-run) DRY=1;; --state-dir) STATE_DIR="${2:?}"; shift;; --confirm) CONFIRM=1;; --accept-timeout) ACCEPT_TIMEOUT="${2:?}"; shift;; --effort) EFFORT="${2:?}"; shift;;
  --session) SESSION="${2:?}"; shift;; --socket) SOCK="${2:?}"; shift;; --brain) BRAIN="${2:?}"; shift;; --descriptor-file) DESCF="${2:?}"; shift;;
  -*) echo "switch-model: unknown flag $1" >&2; exit 2;;
  *) [ -z "$MODEL" ] && MODEL="$1" || { echo "switch-model: one model, got '$1' too" >&2; exit 2; };;
esac; shift; done
[ -n "$MODEL" ] || { echo "usage: switch-model.sh <model> [--dry-run]" >&2; exit 2; }
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
# Fail closed on the runtime gate (a resolver that errors, prints nothing or names an unknown
# runtime refuses before any record or pane action); the runtime decides which ids are valid.
RUNTIME="$(bash "$REPO/scripts/sutando-config.sh" core-runtime 2>/dev/null)"; RRC=$?
if [ $RRC -ne 0 ] || [ -z "$RUNTIME" ]; then echo "switch-model: refused — core runtime could not be resolved (rc=$RRC, value='$RUNTIME'); nothing changed" >&2; exit 4; fi
case "$RUNTIME" in
  claude)
    # Aliases the CLI's /model accepts, or a full id with an optional context tag.
    if ! printf '%s' "$MODEL" | grep -Eq '^(default|opus|sonnet|haiku|fable|claude-[a-z0-9.-]+(\[1m\])?)$'; then
      echo "switch-model: refused '$MODEL' — not a model alias or claude-* id" >&2; exit 2
    fi
    [ -z "$EFFORT" ] || { echo "switch-model: refused — --effort is a Codex reasoning level; the claude runtime has none" >&2; exit 2; };;
  codex)
    if ! printf '%s' "$MODEL" | grep -Eq '^gpt-[a-z0-9.-]+$'; then
      echo "switch-model: refused '$MODEL' — the core runtime is codex, which takes gpt-* ids" >&2; exit 2
    fi
    case "$EFFORT" in ""|low|medium|high|xhigh) ;; *) echo "switch-model: refused — --effort must be low|medium|high|xhigh, got '$EFFORT'" >&2; exit 2;; esac;;
  *) echo "switch-model: refused — unrecognized core runtime '$RUNTIME'; nothing changed" >&2; exit 4;;
esac
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
if [ "$RUNTIME" = codex ]; then
  # Codex's brain is its CODEX_HOME: the launcher's configured dir, else the env, else ~/.codex.
  [ -n "$BRAIN" ] || BRAIN="$(bash "$REPO/scripts/sutando-config.sh" core-config-dir-value codex 2>/dev/null)"
  [ -n "$BRAIN" ] || BRAIN="${CODEX_HOME:-$HOME/.codex}"
  CFG="$BRAIN/config.toml"; LINE="/model"
else
  [ -n "$BRAIN" ] || BRAIN="${DBRAIN:-$(bash "$REPO/scripts/sutando-config.sh" claude-sutando-config-dir)}"
  CFG="$BRAIN/settings.json"; LINE="/model $MODEL"
fi
[ -n "$SOCK" ] || SOCK="${DSOCK:-/tmp/sutando-tmux.sock}"
[ -n "$SESSION" ] || SESSION="${DSESSION:-sutando-core}"
[ -n "$STATE_DIR" ] || STATE_DIR="$(bash "$REPO/scripts/sutando-config.sh" workspace)/state"
if [ -n "$DRY" ]; then
  echo "dry-run: would record $STATE_DIR/model-switch.json (previous read from $CFG), send '$LINE' to tmux -S $SOCK -t $SESSION (python: $PY)$([ "$RUNTIME" = codex ] && echo ", then pick $MODEL${EFFORT:+ / $EFFORT} in the Codex pickers")"; exit 0
fi
# One switch at a time per brain: the lock spans preflight, the settings/record
# transaction and the live send, so two invocations cannot interleave.
LOCK="$STATE_DIR/.model-switch.lock"; mkdir -p "$STATE_DIR" 2>/dev/null
if ! { exec 9>"$LOCK"; } 2>/dev/null; then echo "switch-model: could not open the switch lock ($LOCK) — nothing changed" >&2; exit 1; fi
"$PY" -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX)' || { echo "switch-model: could not take the switch lock — nothing changed" >&2; exit 1; }
# The pane lock every pane writer flocks is held here for the WHOLE transaction (preflight, /model,
# both pickers, acceptance), so no other writer's line can land mid-picker; the sender borrows fd 8.
PANELOCK="$(bash "$REPO/scripts/tmux-pane-lock.sh" "$SOCK" "$SESSION")" || { echo "switch-model: could not derive the pane lock — nothing changed" >&2; exit 7; }
if ! { exec 8>"$PANELOCK"; } 2>/dev/null; then echo "switch-model: could not open the pane lock ($PANELOCK) — nothing changed" >&2; exit 7; fi
"$PY" -c 'import fcntl; fcntl.flock(8, fcntl.LOCK_EX)' || { echo "switch-model: could not take the pane lock — nothing changed" >&2; exit 7; }
# Preflight the live pane before any write through the ONE sender
# (scripts/tmux-send-line.sh): its --dry-run inspects the prompt under the
# socket lock and refuses (5) on pending text, (7) on a failed inspection.
SENDER="$REPO/scripts/tmux-send-line.sh"
LIVE=""
if [ -x "$SENDER" ] || [ -f "$SENDER" ]; then
  bash "$SENDER" "$SESSION" "$LINE" --socket "$SOCK" --runtime "$RUNTIME" --refuse-if-pending --lock-fd 8 --dry-run > /dev/null 2> "$STATE_DIR/.send-preflight.err"; PRC=$?
  case $PRC in
    0) LIVE=1;;
    3) LIVE="";;
    5) echo "switch-model: refused — $(cat "$STATE_DIR/.send-preflight.err"); nothing changed" >&2; exit 5;;
    *) echo "switch-model: refused — $(cat "$STATE_DIR/.send-preflight.err"); nothing changed" >&2; exit 7;;
  esac
else
  echo "switch-model: shared sender missing ($SENDER) — nothing changed" >&2; exit 7
fi
OBS="$REPO/skills/model-switch/scripts/pane-observe.sh"; [ "$RUNTIME" = codex ] && OBS="$REPO/skills/model-switch/scripts/pane-observe-codex.sh"
if [ -z "$LIVE" ]; then echo "live: NOT sent — no tmux session '$SESSION' on $SOCK; nothing switched, nothing recorded" >&2; exit 3; fi
# Snapshot `previous` BEFORE the send: the CLI persists the new model on
# acceptance, so a read afterwards would return the model being switched to.
PREVLINE="$("$PY" - "$CFG" "$STATE_DIR" "$RUNTIME" <<'PYEOF'
import json, os, re, sys
cfg, state_dir, runtime = sys.argv[1], sys.argv[2], sys.argv[3]
if runtime == "codex":
    # The CLI rewrites the top-level model key on every /model pick (\x22 is the quote: bash 3.2 balances quotes inside this heredoc).
    try: m = re.search(r'^model\s*=\s*\x22([^\x22]*)\x22', open(cfg).read(), re.M); prev = m.group(1) if m else None
    except OSError: prev = None
    print(f"{prev or ''}\t{'config.toml' if prev else 'none'}"); sys.exit(0)
try: prev = json.load(open(cfg)).get("model")
except (OSError, ValueError): prev = None
src = "settings.json" if prev else "none"
if not prev:
    try: prev, src = json.load(open(os.path.join(state_dir, "model-switch.json"))).get("model"), "last-record"
    except (OSError, ValueError): prev, src = None, "none"
print(f"{prev or ''}\t{src}")
PYEOF
)"; PREV="${PREVLINE%%	*}"; PREV_SRC="${PREVLINE#*	}"
# Baseline the acceptance lines for THIS model already on screen, so a stale one cannot pass as new.
BASE="$(bash "$OBS" "$SESSION" --socket "$SOCK" --model "$MODEL" --count)"; BRC=$?
case "$BRC:$BASE" in 0:[0-9]*) ;; *) echo "switch-model: could not read the core pane before sending (rc=$BRC, '$BASE'); nothing sent, nothing recorded" >&2; exit 7;; esac
bash "$SENDER" "$SESSION" "$LINE" --socket "$SOCK" --runtime "$RUNTIME" --refuse-if-pending --lock-fd 8 > /dev/null || { echo "switch-model: send failed; nothing recorded" >&2; exit 7; }
CONFIRMED=false
if [ "$RUNTIME" = codex ]; then
  # The observer drives both pickers (model row, then reasoning level) and waits for the acceptance line.
  VERDICT="$(bash "$OBS" "$SESSION" --socket "$SOCK" --model "$MODEL" ${EFFORT:+--effort "$EFFORT"} --wait --baseline "$BASE" --timeout "$ACCEPT_TIMEOUT" 2> "$STATE_DIR/.observe.err")"
  case "$VERDICT" in
    ACCEPTED|"ACCEPTED "*) EFFORT_SEEN="${VERDICT#ACCEPTED}"; EFFORT_SEEN="${EFFORT_SEEN# }";;
    NOT-OFFERED) echo "switch-model: $(cat "$STATE_DIR/.observe.err") Nothing recorded" >&2; exit 9;;
    "EFFORT-MISMATCH"*) echo "switch-model: $(cat "$STATE_DIR/.observe.err") Nothing recorded" >&2; exit 10;;
    *) echo "switch-model: sent '/model' and picked $MODEL but saw no 'Model changed to $MODEL' within ${ACCEPT_TIMEOUT}s; nothing recorded" >&2; exit 8;;
  esac
else
VERDICT="$(bash "$OBS" "$SESSION" --socket "$SOCK" --model "$MODEL" --wait --baseline "$BASE" --timeout "$ACCEPT_TIMEOUT")"
case "$VERDICT" in
  ACCEPTED) ;;
  DIALOG)
    if [ -n "$CONFIRM" ]; then
      VERDICT="$(bash "$OBS" "$SESSION" --socket "$SOCK" --model "$MODEL" --wait --baseline "$BASE" --timeout "$ACCEPT_TIMEOUT" --answer-enter)"; CONFIRMED=true
      [ "$VERDICT" = ACCEPTED ] || { echo "switch-model: confirmed the dialog but no acceptance within ${ACCEPT_TIMEOUT}s; nothing recorded" >&2; exit 8; }
    else
      bash "$OBS" "$SESSION" --socket "$SOCK" --cancel > /dev/null
      echo "switch-model: the core asked to confirm the switch (warm conversation cache); not confirmed — pass --confirm on an owner instruction. Dialog cancelled, nothing recorded" >&2; exit 6
    fi;;
  *) echo "switch-model: sent '/model $MODEL' but saw no acceptance OF THAT MODEL within ${ACCEPT_TIMEOUT}s; nothing recorded" >&2; exit 8;;
esac
fi
# Record only now, with the previous model snapshotted before the send and the effort the CLI applied.
OUT="$("$PY" - "$CFG" "$MODEL" "$STATE_DIR" "$CONFIRMED" "$PREV" "$PREV_SRC" "$RUNTIME" "$EFFORT_SEEN" <<'PYEOF'
import json, os, sys, time, tempfile
cfg, model, state_dir, confirmed, prev, src, runtime, effort = sys.argv[1:9]
rec = {"model": model, "previous": prev or None, "previous_source": src, "accepted": True, "confirmed": confirmed == "true",
       "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
       "by": "skills/model-switch/scripts/switch-model.sh", "settings_read": cfg}
if runtime == "codex": rec.update({"runtime": "codex", "effort": effort or None})
try:
    os.makedirs(state_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".model-switch.staged.")
    with os.fdopen(fd, "w") as f: json.dump(rec, f, indent=2); f.write("\n")
    os.replace(tmp, os.path.join(state_dir, "model-switch.json"))
except OSError as e:
    print(f"RECORD-FAILED {e}"); sys.exit(1)
print(f"OK {prev if prev else '-'}")
PYEOF
)"; RC=$?
case "$OUT" in
  RECORD-FAILED*) echo "switch-model: switched, but could not write the record ($STATE_DIR): ${OUT#RECORD-FAILED }" >&2; exit 1;;
  OK*) ;;
  *) echo "switch-model: unexpected python outcome (rc=$RC): $OUT" >&2; exit 1;;
esac
echo "switched: model=$MODEL${EFFORT_SEEN:+ effort=$EFFORT_SEEN} (was ${OUT#OK }); accepted by the CLI$([ "$CONFIRMED" = true ] && echo ' after confirming its dialog'); record: $STATE_DIR/model-switch.json"
exit 0
