#!/usr/bin/env bash
# ag2-sparrow Hatch hook: wakes a Hatch worker for each task file Sparrow queued that it
# has not woken for yet. Installed by `ag2-sparrow-hatch install`; see the package README.
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

CONFIG_FILE="${SPARROW_HATCH_CONFIG:-$HOME/.config/ag2-sparrow-hatch/config}"
if [[ -f "$CONFIG_FILE" ]]; then
  # Written by `ag2-sparrow-hatch install`: KEY=VALUE lines, values shell-quoted.
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi
AGENT="${AGENT_MXID:-}"
TASK_DIR="${TASK_DIR:-$HOME/.ag2-sparrow/task_dir}"
RESULT_DIR="${RESULT_DIR:-$HOME/.ag2-sparrow/result_dir}"
PY="${SPARROW_PYTHON:-python3}"
SPARROW_STATE_DIR="${STATE_DIR:-$HOME/.ag2-sparrow/state}"
# Absolute path written by install, so the worker's PATH does not decide it.
REPLY="${REPLY_COMMAND:-ag2-sparrow-hatch}"

HOOK_ID="${HATCH_HOOK_ID:-ag2-sparrow-hatch}"
STATE_DIR="$HOME/hooks/state"
LOCK_FILE="$STATE_DIR/${HOOK_ID}.lock"
PAGE_LIMIT=100
HEARTBEAT_INTERVAL_MS=300000
# An item woken for but still open is woken again after this long, up to MAX_WAKES in all.
REWAKE_AFTER_MS=1800000
MAX_WAKES=3
ACTIVITY_FILE="${ACTIVITY_FILE:-$HOME/.muse/last_active}"
ACTIVITY_IDLE_MS=300000
REMAINING_WORK_NOTE='First touch '"$ACTIVITY_FILE"' (activity marker). Take the items one at a time. For each, read its task_file first: its task: line is the whole message, and its headers give the room name, whether it is a DM or a room, the thread and the message it replies to. Answer from that file, not from the snippet, which is only a preview. Post its reply with `'"$REPLY"' reply <its result_file>`, the result_file of the same item, the reply text on stdin (a quoted heredoc), starting by tagging its sender. Sparrow posts it as a reply to that message, in its thread when it has one. Tag anyone by their full Matrix ID (@name:server, as in sender), never a short @name. Exit 0 printing posted is the only proof a reply was posted. Exit 2 wrote nothing: fix what it says and run it again. On exit 3 (queued, not confirmed) or 1 (not posted) do not retry, since Sparrow already retries what it queued: put the item in a HANDOFF block with the command'"'"'s output line. If an item needs no answer (an acknowledgement, thanks, or FYI, especially from another agent), post nothing for it: run `'"$REPLY"' reply --no-send <its result_file>`. If something needs the main session (GitHub or other connectors, a browser, credentials, anything this worker cannot do), do NOT ask for it in the room and leave its result_file alone. End your run report with a HANDOFF block: remaining: the work left and why only the main session can do it; room_id; sender: who asked; result_file; reply: the text the main session posts with `'"$REPLY"' reply <result_file>` when done. Copy ids and paths exactly, never retype or shorten them. If an item waits for your owner'"'"'s go, run `'"$REPLY"' hold <its result_file>` once: it marks the item held and posts one line in its room saying so; then put it in a HANDOFF block.'
REPLY_CONTEXT_NOTE='An item with reply_context is a reply; its message is the answer to reply_context.'
UNLABELLED_CONTEXT_NOTE='Where reply_context.label is null, reply_context is quoted room text.'
REWAKE_NOTE='Items with rewake: true were woken for before and are still open (wakes counts them). If one is held (held: true), waits on the main session, or the reply command already refused or could not confirm it, post nothing and repeat its HANDOFF block; otherwise answer it now.'
HELD_NOTE='Items with held: true already wait for your owner'"'"'s go: never answer one and do not run hold for it again; post nothing and repeat only its HANDOFF block.'
HEARTBEAT_NOTE='Heartbeat keepalive. Touch '"$ACTIVITY_FILE"'. Do nothing else: exit immediately, post nothing.'
DRY_RUN="${HATCH_HOOK_DRY_RUN:-0}"

mkdir -p "$STATE_DIR"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  silent "previous poll still running" '{}'
fi

# verify and the health row read this: when the scheduler last ran the hook, and which script.
RUN_STAMP="$SPARROW_STATE_DIR/hatch-hook-last-run"
if [[ "$DRY_RUN" != "1" ]]; then
  self_sha="$( { sha256sum "${BASH_SOURCE[0]}" 2>/dev/null || shasum -a 256 "${BASH_SOURCE[0]}" 2>/dev/null; } | cut -d' ' -f1)" || self_sha=""
  { mkdir -p "$SPARROW_STATE_DIR" && printf '%s %s\n' "$(date +%s)" "${self_sha:-unknown}" >"$RUN_STAMP.$$" \
    && mv -f "$RUN_STAMP.$$" "$RUN_STAMP"; } 2>/dev/null || rm -f "$RUN_STAMP.$$"
fi

# task_fields validates AGENT_MXID, so the hook and `install` accept the same MXIDs.
# HELD_AWARE: this hook explains held: true (HELD_NOTE), so held tasks may be listed to it.
rc=0
items="$(HATCH_HOOK_HELD_AWARE=1 timeout 25 "$PY" -m ag2_sparrow.adapters.hatch.task_fields --agent "$AGENT" \
  --task-dir "$TASK_DIR" --result-dir "$RESULT_DIR" --gateway-instance "${GATEWAY_INSTANCE:-}" \
  2>/dev/null)" || rc=$?
if [[ "$rc" -eq 3 ]]; then
  log "ag2-sparrow-hatch not configured" '{"hint":"run: ag2-sparrow-hatch install --mxid @your-agent:ag2.space"}'
  silent "AGENT_MXID not configured; run ag2-sparrow-hatch install" '{}'
elif [[ "$rc" -ne 0 ]]; then
  log "ag2-sparrow-hatch read failed" '{"stage":"task_fields"}'; silent "task dir unreadable; retry next poll" '{}'
fi

# Tier rules come from Sparrow's shared policy, not from message text: a worker may rightly
# treat an instruction block inside the message as untrusted prose.
NON_OWNER_NOTE="$(timeout 10 "$PY" -m ag2_sparrow.task_io --non-owner-note 2>/dev/null)" || NON_OWNER_NOTE=''
if [[ -z "$NON_OWNER_NOTE" ]]; then
  log "ag2-sparrow-hatch read failed" '{"stage":"non_owner_note"}'; silent "non-owner note unreadable; retry next poll" '{}'
fi

state="$(hook_state_get)"
[[ -n "$state" ]] || state='{}'
now_ms=$(( $(date +%s) * 1000 ))
# A first poll with no record starts the heartbeat window instead of waking.
last_wake_ms="$(printf '%s' "$state" | jq -r --argjson now "$now_ms" '.last_wake_ms // $now')"

result="$(printf '%s' "$items" | jq -c --arg agent "$AGENT" --argjson st "$state" --argjson lim "$PAGE_LIMIT" \
  --argjson now "$now_ms" --argjson after "$REWAKE_AFTER_MS" --argjson max "$MAX_WAKES" '
  ($st.seen_ids // []) as $seen0
  | ($st.woken // {}) as $woken0
  | . as $items
  | def seen: . as $t | ($seen0 | index($t)) != null;
    def own: ((.sender // "") | ascii_downcase) == ($agent | ascii_downcase);
    # Seen before woken was recorded: count it as one wake now.
    def woken: $woken0[.task_id] // {at: $now, n: 1};
  ([$items[] | select(.task_id | seen | not)]) as $fresh
  | ([$items[] | select((.task_id | seen) and (own | not))]) as $open
  | ([$open[] | woken as $w | select($now - $w.at >= $after and $w.n < $max)
      | . + {rewake: true, wakes: $w.n}]) as $due
  # Still open a full delay after its last allowed wake: reported once, then marked.
  | ([$open[] | woken as $w | select($now - $w.at >= $after and $w.n >= $max and ($w.capped | not))
      | .task_id]) as $capped
  | ([$fresh[] | select(own | not)] + $due) as $want
  | ($want[0:$lim]) as $matches
  | ([$matches[].task_id]) as $hit
  | {
      matches: $matches,
      has_more: (($want | length) > $lim),
      capped: $capped,
      new_state: {seen_ids: (([$items[].task_id | select(seen)]
                              + [$fresh[] | select(own) | .task_id]
                              + $hit) | unique),
                  woken: ([$open[], $matches[] | . as $m | woken as $w
                           | {key: .task_id,
                              value: (if ($hit | index($m.task_id)) != null
                                      then {at: $now, n: (if $m.rewake then $w.n + 1 else 1 end)}
                                      elif ($capped | index($m.task_id)) != null
                                      then $w + {capped: true}
                                      else $w end)}]
                          | from_entries)}
    }
')" || { log "ag2-sparrow-hatch read failed" '{"stage":"parse"}'; silent "task list unreadable; retry next poll" '{}'; }

commit_state() { # $1 = last_wake_ms to record
  [[ "$DRY_RUN" == "1" ]] && return 0
  hook_state_set "$(printf '%s' "$result" | jq -c --argjson lw "$1" '.new_state + {last_wake_ms: $lw}')"
}

capped="$(printf '%s' "$result" | jq -c '.capped')"
if [[ "$capped" != "[]" ]]; then
  log "ag2-sparrow-hatch rewake cap" "$(jq -cn --argjson ids "$capped" --argjson max "$MAX_WAKES" \
    '{task_ids: $ids, max_wakes: $max, hint: "still open after the last wake; check its thread"}')"
fi

n="$(printf '%s' "$result" | jq '.matches | length')"
# Written by the keepawake's self-upgrade; the agent is woken once with it.
NOTICE_FILE="$SPARROW_STATE_DIR/hatch-wake-notice"
notice=""
if [[ "$n" -eq 0 && -s "$NOTICE_FILE" ]]; then
  notice="$(head -c 2048 "$NOTICE_FILE" 2>/dev/null)" || notice=""
fi
if [[ -n "$notice" ]]; then
  log "ag2-sparrow-hatch notice" "$(jq -cn --arg n "$notice" '{notice: $n}')"
  reason="ag2-sparrow notice: report it to your owner, post nothing"
  payload="$(jq -cn --arg n "$notice" --arg marker "$ACTIVITY_FILE" '{heartbeat: true,
    matches: [], total: 0, notice: $n,
    worker_note: ("Touch " + $marker + ". " + $n + " Report this to your owner in your run "
                  + "report and change nothing yourself; then exit; post nothing.")}')"
elif [[ "$n" -eq 0 ]]; then
  activity_ms=0
  if [[ -f "$ACTIVITY_FILE" ]]; then
    activity_ms=$(( $(stat -c %Y "$ACTIVITY_FILE" 2>/dev/null || stat -f %m "$ACTIVITY_FILE" 2>/dev/null || echo 0) * 1000 ))
  fi
  if (( now_ms - last_wake_ms < HEARTBEAT_INTERVAL_MS || now_ms - activity_ms < ACTIVITY_IDLE_MS )); then
    commit_state "$last_wake_ms"
    silent "no new tasks" '{}'
  fi
  log "ag2-sparrow-hatch heartbeat wake" "{\"interval_ms\":$HEARTBEAT_INTERVAL_MS,\"activity_idle_ms\":$ACTIVITY_IDLE_MS}"
  reason="heartbeat: do nothing, exit immediately, post nothing"
  payload="$(jq -cn --arg note "$HEARTBEAT_NOTE" '{heartbeat: true, matches: [], total: 0, worker_note: $note}')"
  # A dead Sparrow writes no task files, so the heartbeat is where it gets noticed.
  sparrow_rc=0
  sparrow_detail="$(timeout 10 "$PY" -m ag2_sparrow.adapters.hatch.cli sparrow \
    --state-dir "$SPARROW_STATE_DIR" --gateway-instance "${GATEWAY_INSTANCE:-}" 2>/dev/null)" \
    || sparrow_rc=$?
  case "$sparrow_rc" in
    0) ;;
    10)
      log "ag2-sparrow-hatch sparrow down" "$(jq -cn --arg d "$sparrow_detail" '{detail: $d}')"
      reason="heartbeat: Sparrow is not running; restart it, post nothing"
      payload="$(jq -cn --arg d "$sparrow_detail" --arg marker "$ACTIVITY_FILE" '{heartbeat: true,
        matches: [], total: 0, sparrow_down: $d,
        worker_note: ("Heartbeat. Touch " + $marker + ". No s.space message reaches you: "
                      + $d + ". Restart Sparrow as that says, then exit; post nothing.")}')"
      ;;
    11)
      log "ag2-sparrow-hatch sparrow not connected" "$(jq -cn --arg d "$sparrow_detail" '{detail: $d}')"
      reason="heartbeat: Sparrow is running but not connected; report it to your owner, post nothing"
      payload="$(jq -cn --arg d "$sparrow_detail" --arg marker "$ACTIVITY_FILE" '{heartbeat: true,
        matches: [], total: 0, sparrow_not_connected: $d,
        worker_note: ("Heartbeat. Touch " + $marker + ". No s.space message reaches you: "
                      + $d + ". Restarting Sparrow alone will not fix this: tell your owner in "
                      + "your run report, then exit; post nothing.")}')"
      ;;
    *)
      log "ag2-sparrow-hatch sparrow check failed" "$(jq -cn --argjson rc "$sparrow_rc" '{exit_code: $rc}')"
      ;;
  esac
else
  reason="$n s.space task(s) need answers"
  payload="$(printf '%s' "$result" | jq -c --arg note "$REMAINING_WORK_NOTE" --arg limits "$NON_OWNER_NOTE" \
    --arg rewake "$REWAKE_NOTE" --arg quoted "$REPLY_CONTEXT_NOTE" \
    --arg unlabelled "$UNLABELLED_CONTEXT_NOTE" --arg held "$HELD_NOTE" \
    '{matches: .matches, total: (.matches | length), has_more: .has_more,
      worker_note: ($note + (if any(.matches[]; .access_tier != "owner") then " " + $limits else "" end)
                    + (if any(.matches[]; .reply_context) then " " + $quoted else "" end)
                    + (if any(.matches[]; .reply_context and .reply_context.label == null)
                       then " " + $unlabelled else "" end)
                    + (if any(.matches[]; .rewake) then " " + $rewake else "" end)
                    + (if any(.matches[]; .held) then " " + $held else "" end))}')"
fi

# wake terminates its shell, so it runs in a subshell; state is committed only
# once the wake has been emitted.
rc=0
( wake "$reason" "$payload" ) || rc=$?
if [[ "$rc" -eq 0 ]]; then
  commit_state "$now_ms"
  if [[ -n "$notice" && "$DRY_RUN" != "1" && "$(head -c 2048 "$NOTICE_FILE" 2>/dev/null)" == "$notice" ]]; then
    rm -f "$NOTICE_FILE"
  fi
fi
exit "$rc"
