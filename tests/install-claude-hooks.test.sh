#!/usr/bin/env bash
# Tests for src/install-claude-hooks.sh.
#
# The bug this guards: REPO_DIR is expanded at INSTALL time and its literal text
# is re-parsed by a shell at HOOK-RUN time. Unquoted, a clone under
# "Library/Application Support/..." splits on the space and every hook dies with
# `bash: /Users/you/Library: No such file or directory` — silently, because a
# hook's exit code is not surfaced. So the load-bearing assertion here is not
# "the JSON contains the path", it is "the stored command STRING, executed by a
# shell, actually runs the intended script". Every fixture path below contains a
# space on purpose.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$HERE/../src/install-claude-hooks.sh"

pass=0; fail=0
ok() {  # ok <name> <condition-rc>
    if [ "$2" = 0 ]; then echo "ok   $1"; pass=$((pass+1))
    else echo "FAIL $1"; fail=$((fail+1)); fi
}

command -v jq >/dev/null 2>&1 || { echo "SKIP — jq not installed"; exit 0; }

# --- build a fake clone whose path contains spaces ---------------------------
ROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks test.XXXXXX")"
REPO="$ROOT/repo with spaces"
mkdir -p "$REPO/src" "$REPO/.claude"
cp "$INSTALLER" "$REPO/src/install-claude-hooks.sh"
# Stub the hook targets so an executed command can prove WHICH file it reached.
printf '#!/bin/bash\necho "HANDOFF-RAN"\n'      > "$REPO/src/session-handoff.sh"
printf '#!/bin/bash\necho "PENDING-RAN"\n'      > "$REPO/src/check-pending-tasks.sh"
chmod +x "$REPO/src/"*.sh
SETTINGS="$REPO/.claude/settings.json"

# Seed the legacy state a real install would have: Desktop-hardcoded hooks plus
# the transcript-archive hook, which must SURVIVE (it legitimately points at
# ~/Desktop/sutando-conversations and is not a repo path).
cat > "$SETTINGS" <<'JSON'
{
  "hooks": {
    "PreCompact": [{"matcher": "", "hooks": [
      {"type": "command", "command": "cp \"$TRANSCRIPT_PATH\" \"$HOME/Desktop/sutando-conversations/$(date +%Y-%m-%dT%H-%M-%S).jsonl\""},
      {"type": "command", "command": "bash $HOME/Desktop/sutando/src/session-handoff.sh \"$TRANSCRIPT_PATH\""}
    ]}],
    "SessionEnd": [{"matcher": "", "hooks": [
      {"type": "command", "command": "bash $HOME/Desktop/sutando/src/session-handoff.sh \"$TRANSCRIPT_PATH\""}
    ]}],
    "Stop": [{"matcher": "", "hooks": [
      {"type": "command", "command": "bash $HOME/Desktop/sutando/src/check-pending-tasks.sh"},
      {"type": "command", "command": "echo operator-added-keepme"}
    ]}]
  }
}
JSON

OUT1="$(bash "$REPO/src/install-claude-hooks.sh" 2>&1)"; RC1=$?
ok "installer exits 0 on a path with spaces" "$([ $RC1 = 0 ] && echo 0 || echo 1)"

cmds() {  # cmds <event> -> one command per line
    jq -r --arg e "$1" '(.hooks // {})[$e] // [] | map(.hooks // []) | flatten | map(.command) | .[]' "$SETTINGS"
}

# --- 1. the stored command must EXECUTE the intended script ------------------
# This is the assertion that fails on an unquoted path. Run the exact string.
SE_CMD="$(cmds SessionEnd | grep session-handoff || true)"
RUN_OUT="$(TRANSCRIPT_PATH=/dev/null bash -c "$SE_CMD" 2>&1)"
ok "SessionEnd stored command executes the intended script" \
   "$([ "$RUN_OUT" = "HANDOFF-RAN" ] && echo 0 || echo 1)"
[ "$RUN_OUT" = "HANDOFF-RAN" ] || echo "     got: $RUN_OUT"

ST_CMD="$(cmds Stop | grep check-pending-tasks || true)"
RUN_OUT2="$(bash -c "$ST_CMD" 2>&1)"
ok "Stop stored command executes the intended script" \
   "$([ "$RUN_OUT2" = "PENDING-RAN" ] && echo 0 || echo 1)"
[ "$RUN_OUT2" = "PENDING-RAN" ] || echo "     got: $RUN_OUT2"

PC_CMD="$(cmds PreCompact | grep session-handoff || true)"
RUN_OUT3="$(TRANSCRIPT_PATH=/dev/null bash -c "$PC_CMD" 2>&1)"
ok "PreCompact handoff stored command executes the intended script" \
   "$([ "$RUN_OUT3" = "HANDOFF-RAN" ] && echo 0 || echo 1)"

# --- 2. legacy Desktop repo hooks are migrated away -------------------------
ok "legacy Desktop session-handoff hook removed (PreCompact)" \
   "$(cmds PreCompact | grep -q 'Desktop/sutando/src/session-handoff.sh' && echo 1 || echo 0)"
ok "legacy Desktop session-handoff hook removed (SessionEnd)" \
   "$(cmds SessionEnd | grep -q 'Desktop/sutando/src/session-handoff.sh' && echo 1 || echo 0)"
ok "legacy Desktop check-pending-tasks hook removed (Stop)" \
   "$(cmds Stop | grep -q 'Desktop/sutando/src/check-pending-tasks.sh' && echo 1 || echo 0)"

# --- 3. things that must SURVIVE the sweep ----------------------------------
ok "transcript-archive hook preserved (different marker)" \
   "$(cmds PreCompact | grep -q 'sutando-conversations/' && echo 0 || echo 1)"
ok "operator-added unrelated hook preserved" \
   "$(cmds Stop | grep -q 'operator-added-keepme' && echo 0 || echo 1)"

# --- 4. exactly one of each hook we own -------------------------------------
ok "exactly one SessionEnd handoff hook (no old+new double-fire)" \
   "$([ "$(cmds SessionEnd | grep -c session-handoff)" = 1 ] && echo 0 || echo 1)"
ok "exactly one Stop pending-tasks hook" \
   "$([ "$(cmds Stop | grep -c check-pending-tasks)" = 1 ] && echo 0 || echo 1)"

# --- 5. re-run is idempotent ------------------------------------------------
BEFORE="$(cat "$SETTINGS")"
OUT2="$(bash "$REPO/src/install-claude-hooks.sh" 2>&1)"
AFTER="$(cat "$SETTINGS")"
ok "second run adds nothing (added=0)" "$(echo "$OUT2" | grep -q 'added=0' && echo 0 || echo 1)"
ok "second run leaves settings.json byte-identical" \
   "$([ "$BEFORE" = "$AFTER" ] && echo 0 || echo 1)"

# --- 6. an UNQUOTED entry from an earlier revision is swept ------------------
# Phase 1 compares exact strings, so without the marker sweep this stale broken
# hook would survive alongside the fixed one and both would fire.
python3 - "$SETTINGS" "$REPO" <<'PY'
import json, sys
p, repo = sys.argv[1], sys.argv[2]
d = json.load(open(p))
d["hooks"]["SessionEnd"][0]["hooks"].append(
    {"type": "command", "command": f'bash {repo}/src/session-handoff.sh "$TRANSCRIPT_PATH"'})
json.dump(d, open(p, "w"), indent=2)
PY
ok "unquoted stale variant is present before re-run (fixture sanity)" \
   "$([ "$(cmds SessionEnd | grep -c session-handoff)" = 2 ] && echo 0 || echo 1)"
bash "$REPO/src/install-claude-hooks.sh" >/dev/null 2>&1
ok "unquoted stale variant swept on re-run" \
   "$([ "$(cmds SessionEnd | grep -c session-handoff)" = 1 ] && echo 0 || echo 1)"
ok "the surviving SessionEnd hook is the QUOTED one that runs" \
   "$([ "$(TRANSCRIPT_PATH=/dev/null bash -c "$(cmds SessionEnd | grep session-handoff)" 2>&1)" = "HANDOFF-RAN" ] && echo 0 || echo 1)"

# --- 7. the sweep must not eat hooks it does not own -------------------------
# Carrying our marker is NOT ownership: an operator hook that invokes the same
# script with an extra flag also contains it. An earlier revision of the sweep
# deleted exactly those — silently, since a removed hook leaves no trace. These
# are the negative controls: without them the sweep only ever demonstrates what
# it CAN delete, never what it must refuse to.
python3 - "$SETTINGS" "$REPO" <<'PY'
import json, sys
p, repo = sys.argv[1], sys.argv[2]
d = json.load(open(p))
d["hooks"]["SessionEnd"][0]["hooks"] += [
    # (a) same script, operator-customized with a trailing flag.
    {"type": "command",
     "command": f'bash {repo}/src/session-handoff.sh "$TRANSCRIPT_PATH" --verbose'},
    # (b) same marker, entirely different command shape.
    {"type": "command",
     "command": f'echo custom && bash {repo}/src/session-handoff.sh'},
    # (c) same script under a path that is not this clone, wrapped by the operator.
    {"type": "command",
     "command": 'env FOO=1 bash "/somewhere else/src/session-handoff.sh" "$TRANSCRIPT_PATH"'},
]
json.dump(d, open(p, "w"), indent=2)
PY
bash "$REPO/src/install-claude-hooks.sh" >/dev/null 2>&1
SURVIVORS="$(cmds SessionEnd)"
ok "operator hook with a trailing flag survives the sweep" \
   "$(echo "$SURVIVORS" | grep -q -- '--verbose' && echo 0 || echo 1)"
ok "operator hook with a different command shape survives the sweep" \
   "$(echo "$SURVIVORS" | grep -q 'echo custom' && echo 0 || echo 1)"
ok "operator-wrapped hook for another path survives the sweep" \
   "$(echo "$SURVIVORS" | grep -q 'env FOO=1' && echo 0 || echo 1)"
# Ours = the session-handoff commands that are not one of the three operator
# fixtures. Counted by subtraction rather than by matching $REPO literally: the
# installer normalizes its stored path (mktemp can yield a `//`), so a literal
# comparison against the fixture path fails for a reason that has nothing to do
# with the sweep.
ok "our own hook is still installed exactly once alongside them" \
   "$([ "$(( $(echo "$SURVIVORS" | grep -c session-handoff) - $(echo "$SURVIVORS" | grep -cE -- '--verbose|echo custom|env FOO=1') ))" = 1 ] && echo 0 || echo 1)"
ok "and ours is still the one that actually executes" \
   "$([ "$(TRANSCRIPT_PATH=/dev/null bash -c "$(echo "$SURVIVORS" | grep session-handoff | grep -vE -- '--verbose|echo custom|env FOO=1')" 2>&1)" = "HANDOFF-RAN" ] && echo 0 || echo 1)"

# A stale INSTALLER-SHAPED entry from a different clone must still be swept —
# the fix must not be "stop sweeping", it must be "sweep only our own shapes".
python3 - "$SETTINGS" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["hooks"]["SessionEnd"][0]["hooks"].append(
    {"type": "command",
     "command": 'bash /a different clone/src/session-handoff.sh "$TRANSCRIPT_PATH"'})
json.dump(d, open(p, "w"), indent=2)
PY
bash "$REPO/src/install-claude-hooks.sh" >/dev/null 2>&1
ok "another clone's installer-shaped entry is still swept" \
   "$(cmds SessionEnd | grep -q 'a different clone' && echo 1 || echo 0)"
ok "sweeping it did not take the operator hooks with it" \
   "$([ "$(cmds SessionEnd | grep -cE -- '--verbose|echo custom|env FOO=1')" = 3 ] && echo 0 || echo 1)"

rm -rf "$ROOT"
echo "---"
if [ "$fail" -gt 0 ]; then
    echo "FAILED — $fail of $((pass+fail)) checks"; exit 1
fi
echo "PASS — install-claude-hooks ($pass checks)"
