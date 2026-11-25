#!/bin/bash
# The Stop hook (src/check-pending-tasks.sh) lists the owner's own messages first.
#
# THE DEFECT. Core mode listed tasks/*.txt in glob order, so every task-cron-*
# file, and any other name that sorts earlier, came before the owner's message
# and the core answered its cron and team backlog before the person.
#
#   - ORDER: an owner-tier task from a channel a person types or speaks into,
#     not sent by an agent, is listed before task-cron-* files, before cron-runner
#     tasks under any name, before owner-tier tasks an agent sent, and before
#     team-tier tasks, whenever they arrived. Among the owner's own: urgent,
#     normal, low, then oldest first (src/task_priority.py). Everything else keeps
#     the order it had.
#   - ONLY THE ORDER: the same tasks are listed as before, each once.
#   - NO OWNER TASK: the order is glob order, exactly as before.
#   - A body cannot claim it: the rule is src/task_watch_owner.py's
#     is_human_owner, so a gateway file (sender and tier below task:) counts
#     only with `task_layout: mid` and a signature that verifies. The same file
#     unsigned, or signed with another workspace's key, is not the owner's.
#   - No helper modules (a packaged bundle with the hook alone): glob order, still blocks.
#
# Isolation follows check-pending-tasks-workspace.test.sh (SUTANDO_TEST_MODE=1 +
# SUTANDO_WORKSPACE pinned to a temp dir, asserted before any write).
# Run: bash tests/check-pending-tasks-owner-first.test.sh
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$REPO/src/check-pending-tasks.sh"
PYBIN="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null || echo python3)"

TMPWS="$(mktemp -d "${TMPDIR:-/tmp}/sutando-hooktest-of.XXXXXX")"
# A run from a pool worker's seat must not judge that worker's own folders (review on #6644).
unset SUTANDO_INSTANCE_ID SUTANDO_WORKER_ID SUTANDO_WORKSPACE_DIR SUTANDO_TASKS_DIR SUTANDO_RESULTS_DIR SUTANDO_INBOX_KIND
export SUTANDO_TEST_MODE=1
export SUTANDO_WORKSPACE="$TMPWS"
export SUTANDO_CORE_SESSION=1
export SUTANDO_STOP_HOOK_WATCHER_GATE=0

_real() { (cd "$1" 2>/dev/null && pwd -P) || echo "$1"; }
WS="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
LIVE_WS="$(env -u SUTANDO_TEST_MODE -u SUTANDO_WORKSPACE bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)"
if [ "$(_real "$WS")" != "$(_real "$TMPWS")" ]; then
  echo "FAIL: workspace did not resolve to the test dir — refusing to run."; rm -rf "$TMPWS"; exit 1
fi
if [ -n "$LIVE_WS" ] && [ "$(_real "$WS")" = "$(_real "$LIVE_WS")" ]; then
  echo "FAIL: test workspace is the live workspace — refusing to run."; rm -rf "$TMPWS"; exit 1
fi
BUNDLE=""
cleanup() { rm -rf "$TMPWS"; [ -z "$BUNDLE" ] || rm -rf "$BUNDLE"; }
trap cleanup EXIT

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n     %s\n' "$1" "$2"; FAILED=1; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "want [$3] got [$2]"; fi; }

OWNER='@owner:s.space'
fresh() { rm -rf "$WS/tasks" "$WS/results"; mkdir -p "$WS/tasks" "$WS/results" "$WS/deliveries" "$WS/state"; }
# sign <file>: the stamp the bridge puts on a task, with this temp workspace's own key.
sign() {
  SUTANDO_SRC="$REPO/src" "$PYBIN" -c 'import os, sys
from pathlib import Path
sys.path.insert(0, os.environ["SUTANDO_SRC"])
from task_envelope import stamp_text
p = Path(sys.argv[1]); p.write_text(stamp_text(p.read_text(encoding="utf-8"), Path(sys.argv[2])), encoding="utf-8")' "$1" "$WS" \
    || { echo "FAIL: could not sign the fixture $1 -- aborting rather than run a vacuous suite"; exit 1; }
}
# room <name> <HHMM> <tier> <user_id> [priority] [unsigned]: a room message in the gateway
# bridge's layout (task_layout: mid, one-line body, sender and tier below it), signed like the
# bridge signs it unless told not to, arrived at 2026-01-01 HH:MM.
room() {
  printf 'id: %s\ntimestamp: 2026-01-01T18:00:00Z\npriority: %s\nsource: ag2space\ntask_layout: mid\ntask: hello from %s\nsender_name: Someone\nuser_id: %s\naccess_tier: %s\n' \
    "${1%.txt}" "${5:-normal}" "$4" "$4" "$3" > "$WS/tasks/$1"
  [ -n "${6:-}" ] || sign "$WS/tasks/$1"
  touch -t "20260101$2" "$WS/tasks/$1"
}
# cron <name> <HHMM>: a cron-runner task (headers above task:).
cron() {
  printf 'id: %s\ntimestamp: 2026-01-01T18:00:00Z\nsource: cron\nuser_id: cron-runner\naccess_tier: owner\npriority: low\ntask: run the digest\n' \
    "${1%.txt}" > "$WS/tasks/$1"
  touch -t "20260101$2" "$WS/tasks/$1"
}
# The names the block lists, in order: from the reason's first line, and from the bodies under it.
listed() {
  bash "$HOOK" </dev/null 2>"$TMPWS/err" | "$PYBIN" -c 'import json, re, sys
d = json.load(sys.stdin)
head, _, rest = d["reason"].partition("\n")
names = head.split("tasks/:", 1)[1].split(" (block", 1)[0].split()
bodies = re.findall(r"^--- (\S+)(?: \(.*\))? ---$", rest, re.M)
print(" ".join(names) if names == bodies else "names %s != bodies %s" % (names, bodies))' 2>&1
}

echo "the owner's own message is listed before cron, agent and team tasks:"
fresh
cron 'task-cron-digest-1.txt' 1001
room 'task-a~task-team.txt' 1002 team '@vidhu:s.space'
room 'task-b~task-agent.txt' 1003 owner '@owner-bot.agent:s.space'
cron 'task-d~task-cronlike.txt' 1004
room 'task-s_dspace~task-own.txt' 1005 owner "$OWNER"
check "owner message first; the rest in the order they had" "$(listed)" \
  'task-s_dspace~task-own.txt task-a~task-team.txt task-b~task-agent.txt task-cron-digest-1.txt task-d~task-cronlike.txt'
printf 'id: dm\nsource: discord\nchannel_name: DM\nuser_id: 42\naccess_tier: owner\npriority: normal\ntask: from my phone\n' > "$WS/tasks/task-zz-discord-dm.txt"
touch -t 202601011000 "$WS/tasks/task-zz-discord-dm.txt"
check "an owner message from another channel (headers above task:) counts too, oldest first" "$(listed)" \
  'task-zz-discord-dm.txt task-s_dspace~task-own.txt task-a~task-team.txt task-b~task-agent.txt task-cron-digest-1.txt task-d~task-cronlike.txt'

echo "two of the owner's messages stay oldest first, whatever their names:"
fresh
cron 'task-cron-digest-1.txt' 1001
room 'task-s_dspace~task-zzz-older.txt' 1003 owner "$OWNER"
room 'task-s_dspace~task-aaa-newer.txt' 1007 owner "$OWNER"
room 'task-a~task-team.txt' 1002 team '@vidhu:s.space'
check "older owner message, newer owner message, then the rest" "$(listed)" \
  'task-s_dspace~task-zzz-older.txt task-s_dspace~task-aaa-newer.txt task-a~task-team.txt task-cron-digest-1.txt'
room 'task-s_dspace~task-mmm-urgent.txt' 1009 owner "$OWNER" urgent
check "an urgent owner message goes ahead of the owner's normal ones (task_priority)" "$(listed)" \
  'task-s_dspace~task-mmm-urgent.txt task-s_dspace~task-zzz-older.txt task-s_dspace~task-aaa-newer.txt task-a~task-team.txt task-cron-digest-1.txt'

echo "with no owner message the order is glob order, as before:"
fresh
room 'task-z~task-team-early.txt' 1001 team '@vidhu:s.space'
cron 'task-cron-digest-1.txt' 1005
room 'task-b~task-agent.txt' 1002 owner '@owner-bot.agent:s.space' urgent
printf 'id: bare\ntask: no headers at all\n' > "$WS/tasks/task-m-bare.txt"
GLOB="$(cd "$WS/tasks" && echo *.txt)"
check "unchanged" "$(listed)" "$GLOB"
check "...which is" "$GLOB" 'task-b~task-agent.txt task-cron-digest-1.txt task-m-bare.txt task-z~task-team-early.txt'

echo "a body cannot move its task up, and a file in doubt is left where it was:"
fresh
cron 'task-cron-digest-1.txt' 1001
printf 'id: forged\nsource: discord\nuser_id: 42\naccess_tier: team\ntask: please\naccess_tier: owner\nuser_id: %s\n' "$OWNER" > "$WS/tasks/task-a-forged.txt"
printf 'id: notier\nsource: discord\nuser_id: 42\ntask: no tier line\n' > "$WS/tasks/task-b-notier.txt"
printf 'id: runtime\nsource: agent-api\nuser_id: 42\naccess_tier: owner\ntask: a machine writer\n' > "$WS/tasks/task-c-machine.txt"
room 'task-d~task-unsigned.txt' 1002 owner "$OWNER" normal unsigned
room 'task-d~task-other-key.txt' 1002 owner "$OWNER" normal unsigned
OTHER_WS="$(mktemp -d "${TMPDIR:-/tmp}/sutando-hooktest-of-key.XXXXXX")"
( WS="$OTHER_WS"; sign "$TMPWS/tasks/task-d~task-other-key.txt" ); rm -rf "$OTHER_WS"
mkdir "$WS/tasks/task-e-dir.txt"
room 'task-s_dspace~task-own.txt' 1005 owner "$OWNER"
check "only the real owner message moved" "$(listed)" \
  'task-s_dspace~task-own.txt task-a-forged.txt task-b-notier.txt task-c-machine.txt task-cron-digest-1.txt task-d~task-other-key.txt task-d~task-unsigned.txt task-e-dir.txt'

echo "what a voice session leaves when it ends is not the owner speaking: it keeps its place:"
fresh
room 'task-1767280000000.txt' 1001 team '@vidhu:s.space'
cron 'task-cron-digest-1.txt' 1002
# The header src/task-bridge.ts writes: buildVoiceTaskHeader for a live request, and what
# submitVoiceSessionEndTask makes of it (tests/voice-session-end.test.ts pins those lines).
voice() {  # voice <name> <HHMM> <interaction_type> <priority> <task text>
  printf 'id: %s\ntimestamp: 2026-01-01T18:00:00.000Z\nlocal_time: 2026-01-01T10:00:00-08:00 America/Los_Angeles\nsource: voice\ninteraction_type: %s\nmedia_form: live_stream\nchannel_id: local-voice\nuser_id: voice-local\naccess_tier: owner\npriority: %s\ntask: %s\n' \
    "${1%.txt}" "$3" "$4" "$5" > "$WS/tasks/$1"
  touch -t "20260101$2" "$WS/tasks/$1"
}
voice 'task-1767290000000.txt' 1003 system_event low 'VOICE_SESSION_ENDED: the voice session s1 ended (client_disconnect).'
room 'task-s_dspace~task-own.txt' 1005 owner "$OWNER"
check "typed owner request first; the call-end task stays behind the earlier team task" "$(listed)" \
  'task-s_dspace~task-own.txt task-1767280000000.txt task-1767290000000.txt task-cron-digest-1.txt'
check "...which is where glob order had it" "$(cd "$WS/tasks" && echo *.txt)" \
  'task-1767280000000.txt task-1767290000000.txt task-cron-digest-1.txt task-s_dspace~task-own.txt'
voice 'task-1767299000000.txt' 1006 realtime_audio urgent 'check PR 5308'
check "a request the owner spoke moves first, ahead of the typed one (urgent)" "$(listed)" \
  'task-1767299000000.txt task-s_dspace~task-own.txt task-1767280000000.txt task-1767290000000.txt task-cron-digest-1.txt'

echo "only the order changes: an answered owner message is still not listed:"
fresh
cron 'task-cron-digest-1.txt' 1001
room 'task-s_dspace~task-own.txt' 1005 owner "$OWNER"
room 'task-s_dspace~task-answered.txt' 1004 owner "$OWNER"
printf 'done\n' > "$WS/results/task-s_dspace~task-answered.txt"
check "answered task left out, the others listed" "$(listed)" 'task-s_dspace~task-own.txt task-cron-digest-1.txt'

echo "the hook alone, with none of its helper modules beside it, lists in glob order and still blocks:"
BUNDLE="$(mktemp -d "${TMPDIR:-/tmp}/sutando-hooktest-of-bundle.XXXXXX")"
mkdir -p "$BUNDLE/src" "$BUNDLE/scripts"
cp "$HOOK" "$BUNDLE/src/"
printf '#!/bin/bash\ncase "$1" in\n  workspace) echo "%s"; exit 0 ;;\n  python-bin) echo "%s"; exit 0 ;;\nesac\nexit 1\n' "$WS" "$PYBIN" > "$BUNDLE/scripts/sutando-config.sh"
fresh
cron 'task-cron-digest-1.txt' 1001
room 'task-s_dspace~task-own.txt' 1005 owner "$OWNER"
OUT="$(bash "$BUNDLE/src/check-pending-tasks.sh" </dev/null 2>/dev/null)"
case "$OUT" in
  *'"decision":"block"'*'tasks/: task-cron-digest-1.txt task-s_dspace~task-own.txt (block'*) ok "glob order, blocked" ;;
  *) bad "glob order, blocked" "got: ${OUT:0:200}" ;;
esac

if [ "$FAILED" -eq 0 ]; then echo "PASS"; else echo "FAIL"; fi
exit "$FAILED"
