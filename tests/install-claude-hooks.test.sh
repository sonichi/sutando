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

# Same shq() the installer uses (src/install-claude-hooks.sh) — a fixture that
# quotes a path its own way tests a shape the installer never actually emits.
shq() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

pass=0; fail=0
ok() {  # ok <name> <condition-rc>
    if [ "$2" = 0 ]; then echo "ok   $1"; pass=$((pass+1))
    else echo "FAIL $1"; fail=$((fail+1)); fi
}

command -v jq >/dev/null 2>&1 || { echo "SKIP — jq not installed"; exit 0; }

# --- build a fake clone whose path contains spaces ---------------------------
ROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks test.XXXXXX")"
# The installer creates the archive hook's destination under $HOME, so every
# invocation below must run against a throwaway home, not the developer's.
export HOME="$ROOT/home"; mkdir -p "$HOME"
REPO="$ROOT/repo with spaces"
mkdir -p "$REPO/src" "$REPO/.claude" "$REPO/workspace/.claude-sutando"
cp "$INSTALLER" "$REPO/src/install-claude-hooks.sh"
# Stub the hook targets so an executed command can prove WHICH file it reached.
printf '#!/bin/bash\necho "HANDOFF-RAN"\n'      > "$REPO/src/session-handoff.sh"
printf '#!/bin/bash\necho "PENDING-RAN"\n'      > "$REPO/src/check-pending-tasks.sh"
# The archiver is exercised for real below, so copy it and its resolver rather
# than stubbing: a stub would assert the hook string, not that it archives.
cp "$HERE/../src/archive-transcript.sh" "$HERE/../src/hook_transcript_path.sh" "$REPO/src/"
chmod +x "$REPO/src/"*.sh
# The installer writes to the CORE's config dir. With no scripts/ in the
# fixture, its resolver falls back to <repo>/workspace/.claude-sutando.
SETTINGS="$REPO/workspace/.claude-sutando/settings.json"
LEGACY_SETTINGS="$REPO/.claude/settings.json"
ARCHIVE_DIR="$REPO/workspace/logs/conversations"

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
cp "$SETTINGS" "$LEGACY_SETTINGS"   # pre-move install: same hooks at project level

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

# The archive hook is a bare `cp`, so it cannot create its own destination. The
# assertion that matters is not "the directory exists" but "the stored command
# executed by a shell actually archives a file" — the same standard as above.
ok "installer created the archive hook's destination directory" \
   "$([ -d "$ARCHIVE_DIR" ] && echo 0 || echo 1)"

AR_CMD="$(cmds PreCompact | grep logs/conversations || true)"
printf 'transcript\n' > "$ROOT/transcript.jsonl"
# Drive it the way Claude Code does — transcript_path on stdin JSON, no env var.
# Feeding $TRANSCRIPT_PATH instead passed only while the legacy `cp` co-existed.
printf '{"transcript_path": "%s"}' "$ROOT/transcript.jsonl" \
  | bash -c "$AR_CMD" >/dev/null 2>&1
ok "PreCompact archive stored command actually writes a transcript" \
   "$([ "$(ls "$ARCHIVE_DIR" 2>/dev/null | wc -l | tr -d ' ')" = 1 ] && echo 0 || echo 1)"

# --- 2. legacy Desktop repo hooks are migrated away -------------------------
ok "legacy Desktop session-handoff hook removed (PreCompact)" \
   "$(cmds PreCompact | grep -q 'Desktop/sutando/src/session-handoff.sh' && echo 1 || echo 0)"
ok "legacy Desktop session-handoff hook removed (SessionEnd)" \
   "$(cmds SessionEnd | grep -q 'Desktop/sutando/src/session-handoff.sh' && echo 1 || echo 0)"
ok "legacy Desktop check-pending-tasks hook removed (Stop)" \
   "$(cmds Stop | grep -q 'Desktop/sutando/src/check-pending-tasks.sh' && echo 1 || echo 0)"

# --- 3. things that must SURVIVE the sweep ----------------------------------
# The fixture seeds the legacy bare-`cp` archiver, which this PR migrates rather
# than sweeps — so name the surviving form, or the grep passes on either one.
ok "an archive hook on PreCompact, in the archive-transcript.sh form" \
   "$(cmds PreCompact | grep -q 'archive-transcript\.sh.*logs/conversations' && echo 0 || echo 1)"
ok "operator-added unrelated hook preserved" \
   "$(cmds Stop | grep -q 'operator-added-keepme' && echo 0 || echo 1)"

# --- 4. exactly one of each hook we own -------------------------------------
ok "exactly one SessionEnd handoff hook (no old+new double-fire)" \
   "$([ "$(cmds SessionEnd | grep -c session-handoff)" = 1 ] && echo 0 || echo 1)"
ok "exactly one Stop pending-tasks hook" \
   "$([ "$(cmds Stop | grep -c check-pending-tasks)" = 1 ] && echo 0 || echo 1)"

# --- 4b. the pre-move project-level copy is cleaned up -----------------------
# These hooks are core-only, but project-level settings fire for EVERY session
# with this cwd. Leaving the old copy behind would keep conscripting guests and
# double-register the core, so a re-run must sweep it — without touching hooks
# the operator added there.
legacy_cmds() {
    jq -r --arg e "$1" '(.hooks // {})[$e] // [] | map(.hooks // []) | flatten | map(.command) | .[]' \
       "$LEGACY_SETTINGS" 2>/dev/null
}
ok "project-level Stop hook removed from the pre-move settings" \
   "$(legacy_cmds Stop | grep -q 'check-pending-tasks' && echo 1 || echo 0)"
ok "project-level session-handoff hooks removed (PreCompact)" \
   "$(legacy_cmds PreCompact | grep -q 'session-handoff' && echo 1 || echo 0)"
ok "project-level session-handoff hooks removed (SessionEnd)" \
   "$(legacy_cmds SessionEnd | grep -q 'session-handoff' && echo 1 || echo 0)"
ok "project-level archiver removed from the pre-move settings" \
   "$(legacy_cmds PreCompact | grep -q 'sutando-conversations' && echo 1 || echo 0)"
ok "operator's own hook in the project settings survives the move" \
   "$(legacy_cmds Stop | grep -q 'operator-added-keepme' && echo 0 || echo 1)"

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
# Properly quoted (shq() has quoted every installer-written path for a long
# time now, on any clone) — an UNQUOTED path containing literal spaces is a
# different, narrower scenario (this clone's own pre-shq legacy form, covered
# by the "unquoted stale variant" checks above via its known $REPO_DIR), not
# a realistic shape for a different clone's installer output.
python3 - "$SETTINGS" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["hooks"]["SessionEnd"][0]["hooks"].append(
    {"type": "command",
     "command": "bash '/a different clone/src/session-handoff.sh' \"$TRANSCRIPT_PATH\""})
json.dump(d, open(p, "w"), indent=2)
PY
bash "$REPO/src/install-claude-hooks.sh" >/dev/null 2>&1
ok "another clone's installer-shaped entry is still swept" \
   "$(cmds SessionEnd | grep -q 'a different clone' && echo 1 || echo 0)"
ok "sweeping it did not take the operator hooks with it" \
   "$([ "$(cmds SessionEnd | grep -cE -- '--verbose|echo custom|env FOO=1')" = 3 ] && echo 0 || echo 1)"

# An operator's OWN hook using an unexpanded shell-variable path prefix must
# survive — its double-quoted argv[1] contains our marker as a plain substring
# even though nothing here wrote it. #4309 review round 6 (keweichen,
# 2026-09-16): candidate_is_owned() accepted any argv[1] containing the
# marker with no check that it is shaped like something WE could have
# written; repro `bash "$CUSTOM_ROOT/src/session-handoff.sh" "$TRANSCRIPT_PATH"`.
python3 - "$SETTINGS" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["hooks"]["SessionEnd"][0]["hooks"].append(
    {"type": "command",
     "command": 'bash "$CUSTOM_ROOT/src/session-handoff.sh" "$TRANSCRIPT_PATH"'})
json.dump(d, open(p, "w"), indent=2)
PY
bash "$REPO/src/install-claude-hooks.sh" >/dev/null 2>&1
ok "operator's own \$VAR-prefixed hook survives the sweep" \
   "$(cmds SessionEnd | grep -qF 'CUSTOM_ROOT' && echo 0 || echo 1)"
ok "and our own hook is still the one that actually executes" \
   "$([ "$(TRANSCRIPT_PATH=/dev/null bash -c "$(cmds SessionEnd | grep session-handoff | grep -vE -- '--verbose|echo custom|env FOO=1|CUSTOM_ROOT')" 2>&1)" = "HANDOFF-RAN" ] && echo 0 || echo 1)"

# --- 8. a checkout path containing an APOSTROPHE ----------------------------
# An apostrophe is legal in a path, and `shq` escapes it as '\'' — so any
# matching scheme that "normalizes" by deleting quote characters leaves a stray
# backslash and silently stops recognising its own output. The symptom is not a
# crash: the stale entry simply survives and BOTH hooks fire forever.
# Fixture path has a space AND an apostrophe on purpose.
AROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks apos.XXXXXX")"
AREPO="$AROOT/repo'quote with spaces"
mkdir -p "$AREPO/src" "$AREPO/.claude" "$AREPO/workspace/.claude-sutando"
cp "$INSTALLER" "$AREPO/src/install-claude-hooks.sh"
printf '#!/bin/bash\necho "HANDOFF-RAN"\n' > "$AREPO/src/session-handoff.sh"
printf '#!/bin/bash\necho "PENDING-RAN"\n' > "$AREPO/src/check-pending-tasks.sh"
chmod +x "$AREPO/src/"*.sh
echo '{}' > "$AREPO/.claude/settings.json"
bash "$AREPO/src/install-claude-hooks.sh" >/dev/null 2>&1

export A_SETTINGS="$AREPO/workspace/.claude-sutando/settings.json" A_REPO="$AREPO"
# Seed the prior UNQUOTED installer variant, exactly as an older revision wrote it.
python3 - <<'PY'
import json, os
p = os.environ['A_SETTINGS']
d = json.load(open(p))
d['hooks']['SessionEnd'][0]['hooks'].append(
    {'type': 'command',
     'command': 'bash ' + os.environ['A_REPO'] + '/src/session-handoff.sh "$TRANSCRIPT_PATH"'})
json.dump(d, open(p, 'w'), indent=2)
PY
acount() { python3 -c "
import json, os
d = json.load(open(os.environ['A_SETTINGS']))
print(sum(1 for g in d['hooks']['SessionEnd'] for h in g['hooks'] if 'session-handoff' in h['command']))
"; }
ok "apostrophe fixture: stale variant present before re-run (sanity)" \
   "$([ "$(acount)" = 2 ] && echo 0 || echo 1)"
bash "$AREPO/src/install-claude-hooks.sh" >/dev/null 2>&1
ok "apostrophe path: stale variant IS swept on re-run" \
   "$([ "$(acount)" = 1 ] && echo 0 || echo 1)"
A_SURVIVOR="$(python3 -c "
import json, os
d = json.load(open(os.environ['A_SETTINGS']))
print([h['command'] for g in d['hooks']['SessionEnd'] for h in g['hooks']
       if 'session-handoff' in h['command']][0])
")"
ok "apostrophe path: the surviving command actually executes" \
   "$([ "$(TRANSCRIPT_PATH=/dev/null bash -c "$A_SURVIVOR" 2>&1)" = "HANDOFF-RAN" ] && echo 0 || echo 1)"
rm -rf "$AROOT"

# --- 9. customization BEFORE the path (flags / wrappers) ---------------------
# The trailing anchor protects customization that comes AFTER the script path.
# It says nothing about customization BEFORE it: a bare wildcard between the
# command word and the marker swallows `-x`, so an operator's `bash -x <path>/…`
# was classified installer-owned and deleted (reproduced: before 2, after 1).
# The path region must START like a path, so a flag token fails immediately —
# while `$HOME/…`, which the legacy migration depends on, still qualifies.
BROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks flag.XXXXXX")"
BREPO="$BROOT/repo with spaces"
mkdir -p "$BREPO/src" "$BREPO/.claude" "$BREPO/workspace/.claude-sutando"
cp "$INSTALLER" "$BREPO/src/install-claude-hooks.sh"
printf '#!/bin/bash\necho "HANDOFF-RAN"\n' > "$BREPO/src/session-handoff.sh"
printf '#!/bin/bash\necho "PENDING-RAN"\n' > "$BREPO/src/check-pending-tasks.sh"
chmod +x "$BREPO/src/"*.sh
echo '{}' > "$BREPO/.claude/settings.json"
bash "$BREPO/src/install-claude-hooks.sh" >/dev/null 2>&1

export B_SETTINGS="$BREPO/workspace/.claude-sutando/settings.json" B_REPO="$BREPO"
python3 - <<'PY'
import json, os
p = os.environ['B_SETTINGS']; repo = os.environ['B_REPO']
d = json.load(open(p))
d['hooks']['SessionEnd'][0]['hooks'] += [
    # (a) operator ran the same script under a shell flag — must SURVIVE.
    {'type': 'command',
     'command': 'bash -x ' + repo + '/src/session-handoff.sh "$TRANSCRIPT_PATH"'},
    # (b) legacy installer entry — must still be SWEPT (it starts with `$`,
    #     so a naive "path must start with /" rule would wrongly spare it).
    {'type': 'command',
     'command': 'bash $HOME/Desktop/sutando/src/session-handoff.sh "$TRANSCRIPT_PATH"'},
]
json.dump(d, open(p, 'w'), indent=2)
PY
bcount() { python3 -c "
import json, os
d = json.load(open(os.environ['B_SETTINGS']))
print(sum(1 for g in d['hooks']['SessionEnd'] for h in g['hooks'] if 'session-handoff' in h['command']))
"; }
ok "flag fixture: 3 session-handoff hooks before re-run (sanity)" \
   "$([ "$(bcount)" = 3 ] && echo 0 || echo 1)"
bash "$BREPO/src/install-claude-hooks.sh" >/dev/null 2>&1
BSURV="$(python3 -c "
import json, os
d = json.load(open(os.environ['B_SETTINGS']))
print(chr(10).join(h['command'] for g in d['hooks']['SessionEnd'] for h in g['hooks']))
")"
ok "operator hook with a flag BEFORE the path survives (bash -x)" \
   "$(echo "$BSURV" | grep -q -- 'bash -x ' && echo 0 || echo 1)"
ok "legacy \$HOME entry is still swept (rule must not require a literal /)" \
   "$(echo "$BSURV" | grep -q 'Desktop/sutando/src/session-handoff' && echo 1 || echo 0)"
ok "exactly 2 session-handoff hooks remain (ours + the operator's)" \
   "$([ "$(bcount)" = 2 ] && echo 0 || echo 1)"
rm -rf "$BROOT"

# --- 10. a hook with NO repo path must never be swept ------------------------
# Phase 0 exists to migrate entries whose PATH went stale. The transcript-archive
# hook embeds no repo path at all ($HOME only), so nothing about it can go stale
# and sweeping it can only destroy someone else's command. With a wildcard shape
# the `.*` spanned the SOURCE ARGUMENT, so an operator archiving from a different
# variable matched "our shape" and was deleted and replaced on re-run.
CROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks archive.XXXXXX")"
CREPO="$CROOT/repo with spaces"
mkdir -p "$CREPO/src" "$CREPO/.claude" "$CREPO/workspace/.claude-sutando"
cp "$INSTALLER" "$CREPO/src/install-claude-hooks.sh"
printf '#!/bin/bash\necho "HANDOFF-RAN"\n' > "$CREPO/src/session-handoff.sh"
printf '#!/bin/bash\necho "PENDING-RAN"\n' > "$CREPO/src/check-pending-tasks.sh"
chmod +x "$CREPO/src/"*.sh
export C_SETTINGS="$CREPO/workspace/.claude-sutando/settings.json"
python3 - <<'PY'
import json, os
json.dump({"hooks": {"PreCompact": [{"hooks": [{"type": "command", "command":
    'cp "$CUSTOM_TRANSCRIPT_PATH" "$HOME/Desktop/sutando-conversations/$(date +%Y-%m-%dT%H-%M-%S).jsonl"'
}]}]}}, open(os.environ["C_SETTINGS"], "w"), indent=2)
PY
bash "$CREPO/src/install-claude-hooks.sh" >/dev/null 2>&1
CCMDS="$(python3 -c "
import json, os
d = json.load(open(os.environ['C_SETTINGS']))
print(chr(10).join(h['command'] for g in d['hooks'].get('PreCompact', []) for h in g['hooks']))
")"
ok "operator's custom transcript-archive command survives re-run" \
   "$(echo "$CCMDS" | grep -q 'CUSTOM_TRANSCRIPT_PATH' && echo 0 || echo 1)"
# Match OUR script, not just the destination: the operator's custom command above
# also names sutando-conversations, so a destination-only grep passes vacuously.
ok "our archive hook is still installed alongside it" \
   "$(echo "$CCMDS" | grep -q 'archive-transcript\.sh.*logs/conversations' && echo 0 || echo 1)"
ok "and the repo-path hook is still installed on the same event" \
   "$(echo "$CCMDS" | grep -q 'session-handoff' && echo 0 || echo 1)"
rm -rf "$CROOT"

# --- 11. the LEGACY archive command is migrated, but never under the omit flag -
# This PR changed the archiver from a bare `cp` to archive-transcript.sh. Phase 0
# cannot migrate the old form (no repo path, see 10) and phase 1 matches the exact
# command, so a host that had opted in would keep BOTH — the stale one failing on
# every compaction. Under the omit flag the same migration would turn an inert
# opt-in into live ~/Desktop egress, which an unattended re-run must not decide.
LEG_CMD='cp "$TRANSCRIPT_PATH" "$HOME/Desktop/sutando-conversations/$(date +%Y-%m-%dT%H-%M-%S).jsonl"'
seed_legacy_repo() {  # $1 = destination repo dir
  mkdir -p "$1/src" "$1/.claude" "$1/workspace/.claude-sutando"
  cp "$INSTALLER" "$1/src/install-claude-hooks.sh"
  printf '#!/bin/bash\n:\n' > "$1/src/session-handoff.sh"
  printf '#!/bin/bash\n:\n' > "$1/src/check-pending-tasks.sh"
  printf '#!/bin/bash\n:\n' > "$1/src/archive-transcript.sh"
  chmod +x "$1/src/"*.sh
  LEG_SETTINGS="$1/workspace/.claude-sutando/settings.json" LEG_CMD="$LEG_CMD" python3 - <<'PY'
import json, os
json.dump({"hooks": {"PreCompact": [{"hooks": [{"type": "command",
    "command": os.environ["LEG_CMD"]}]}]}},
    open(os.environ["LEG_SETTINGS"], "w"), indent=2)
PY
}
archive_cmds() {  # $1 = settings path -> one command per line, archiver-targeting only
  LEG_SETTINGS="$1" python3 -c "
import json, os
d = json.load(open(os.environ['LEG_SETTINGS']))
print(chr(10).join(h['command'] for g in d['hooks'].get('PreCompact', [])
                   for h in g['hooks'] if 'conversations' in h['command']))
"
}

LROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks legacy.XXXXXX")"
seed_legacy_repo "$LROOT/repo with spaces"
bash "$LROOT/repo with spaces/src/install-claude-hooks.sh" >/dev/null 2>&1
LCMDS="$(archive_cmds "$LROOT/repo with spaces/workspace/.claude-sutando/settings.json")"
ok "legacy bare-cp archiver is removed on upgrade" \
   "$(echo "$LCMDS" | grep -qF 'cp "$TRANSCRIPT_PATH"' && echo 1 || echo 0)"
ok "and replaced by the archive-transcript.sh form" \
   "$(echo "$LCMDS" | grep -q 'archive-transcript\.sh' && echo 0 || echo 1)"
ok "leaving exactly one archiver hook, not two" \
   "$([ "$(echo "$LCMDS" | grep -c 'conversations')" = 1 ] && echo 0 || echo 1)"
rm -rf "$LROOT"

OROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks legacy omit.XXXXXX")"
seed_legacy_repo "$OROOT/repo with spaces"
SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE=1 \
  bash "$OROOT/repo with spaces/src/install-claude-hooks.sh" >/dev/null 2>&1
OCMDS="$(archive_cmds "$OROOT/repo with spaces/workspace/.claude-sutando/settings.json")"
ok "under the omit flag the opt-in survives untouched" \
   "$(echo "$OCMDS" | grep -qF 'cp "$TRANSCRIPT_PATH"' && echo 0 || echo 1)"
ok "and no archiver is installed in its place" \
   "$(echo "$OCMDS" | grep -q 'archive-transcript\.sh' && echo 1 || echo 0)"
rm -rf "$OROOT"

# ---- upgrade path: a pre-existing runner-first skill hook must be MIGRATED ----
# The outage case: a re-run must replace the old `python3 <path>` entry, not add beside it.
UROOT="$(mktemp -d)"; UREPO="$UROOT/repo"
mkdir -p "$UREPO/.claude" "$UREPO/src" "$UREPO/skills/demo/hooks" "$UREPO/workspace/.claude-sutando"
cp "$HERE/../src/install-claude-hooks.sh" "$UREPO/src/"
cp "$HERE/../src/skill_hooks.py" "$UREPO/src/"
printf '#!/bin/bash\n:\n' > "$UREPO/src/session-handoff.sh"
printf '#!/bin/bash\n:\n' > "$UREPO/src/check-pending-tasks.sh"
chmod +x "$UREPO/src/"*.sh
printf '{"name":"demo","hooks":[{"event":"PreToolUse","command":"./hooks/g.py"}]}\n' \
    > "$UREPO/skills/demo/manifest.json"
printf 'import sys; sys.exit(2)\n' > "$UREPO/skills/demo/hooks/g.py"
# Resolve the fixture path: skill_hooks writes RESOLVED paths, and macOS mktemp's
# /var/... alias would seed a string no installer ever wrote (false migration failure).
GPATH="$(python3 -c "import pathlib,sys;print(pathlib.Path(sys.argv[1]).resolve())" "$UREPO/skills/demo/hooks/g.py")"
export U_SETTINGS="$UREPO/workspace/.claude-sutando/settings.json"
# Seed EXACTLY what a previous installer wrote, plus an operator variant that
# invokes the same script — the negative control the sweep must not eat.
U_OLD="python3 $GPATH" python3 - <<'PY'
import json, os
json.dump({"hooks": {"PreToolUse": [{"matcher": "", "hooks": [
    {"type": "command", "command": os.environ["U_OLD"]},
    {"type": "command", "command": "bash -x " + os.environ["U_OLD"].split(" ", 1)[1]},
]}]}}, open(os.environ["U_SETTINGS"], "w"), indent=2)
PY
bash "$UREPO/src/install-claude-hooks.sh" >/dev/null 2>&1
UCMDS="$(python3 -c "
import json, os
d = json.load(open(os.environ['U_SETTINGS']))
print(chr(10).join(h['command'] for g in d['hooks'].get('PreToolUse', []) for h in g['hooks']))
")"
ok "old runner-first skill hook is REMOVED on re-run (not left beside the new one)" \
   "$(echo "$UCMDS" | grep -qx "python3 $GPATH" && echo 1 || echo 0)"
ok "guarded skill hook is registered exactly once" \
   "$([ "$(echo "$UCMDS" | grep -c '^\[ -f .*g\.py ')" = 1 ] && echo 0 || echo 1)"
ok "operator's own variant on the same script survives (negative control)" \
   "$(echo "$UCMDS" | grep -q 'bash -x ' && echo 0 || echo 1)"
# The point of the whole change: with the script gone, nothing blocks.
rm -f "$GPATH"
UGUARD="$(echo "$UCMDS" | grep '^\[ -f .*g\.py ' | head -1)"
bash -c "$UGUARD" >/dev/null 2>&1
ok "with the script deleted the guarded hook exits 0 (tool not blocked)" "$?"
rm -rf "$UROOT"

# ---- same upgrade, on a repo path containing `exec ` and `|` ----
# `${CMD#*exec }` splits inside such a path; the `|` exercises the NUL field framing.
EROOT="$(mktemp -d)"; EREPO="$EROOT/exec repo|x/repo"
mkdir -p "$EREPO/.claude" "$EREPO/src" "$EREPO/skills/demo/hooks" "$EREPO/workspace/.claude-sutando"
cp "$HERE/../src/install-claude-hooks.sh" "$EREPO/src/"
cp "$HERE/../src/skill_hooks.py" "$EREPO/src/"
printf '#!/bin/bash\n:\n' > "$EREPO/src/session-handoff.sh"
printf '#!/bin/bash\n:\n' > "$EREPO/src/check-pending-tasks.sh"
chmod +x "$EREPO/src/"*.sh
printf '{"name":"demo","hooks":[{"event":"PreToolUse","command":"./hooks/g.py"}]}\n' \
    > "$EREPO/skills/demo/manifest.json"
printf 'import sys; sys.exit(2)\n' > "$EREPO/skills/demo/hooks/g.py"
EPATH="$(python3 -c "import pathlib,sys;print(pathlib.Path(sys.argv[1]).resolve())" "$EREPO/skills/demo/hooks/g.py")"
export E_SETTINGS="$EREPO/workspace/.claude-sutando/settings.json"
# shq quotes the path, so the seeded legacy entry must be quoted the same way an
# installer would have written it — otherwise the fixture is not what it claims.
E_OLD="python3 $(python3 -c "import shlex,sys;print(shlex.quote(sys.argv[1]))" "$EPATH")"
export E_OLD
python3 - <<'PY'
import json, os
json.dump({"hooks": {"PreToolUse": [{"matcher": "", "hooks": [
    {"type": "command", "command": os.environ["E_OLD"]},
]}]}}, open(os.environ["E_SETTINGS"], "w"), indent=2)
PY
bash "$EREPO/src/install-claude-hooks.sh" >/dev/null 2>&1
ECMDS="$(python3 -c "
import json, os
d = json.load(open(os.environ['E_SETTINGS']))
print(chr(10).join(h['command'] for g in d['hooks'].get('PreToolUse', []) for h in g['hooks']))
")"
ok "path containing 'exec ': legacy entry is REMOVED, not left blocking" \
   "$(echo "$ECMDS" | grep -qxF "$E_OLD" && echo 1 || echo 0)"
# No trailing space in the pattern: this path needs quoting, so shq emits `g.py'`
# where an ordinary path emits a bare `g.py `.
ok "path containing 'exec ': guarded hook registered exactly once" \
   "$([ "$(echo "$ECMDS" | grep -c "^\[ -f .*g\.py")" = 1 ] && echo 0 || echo 1)"
rm -rf "$EROOT"

# --- 8. Phase 3 (project-level migration) must not eat hooks it does not own -
# Same false-positive as section 7, at the OTHER settings file: Phase 3 used to
# remove any project-level command CONTAINING our marker, so an operator's own
# customized `session-handoff.sh` invocation — sitting right beside the
# canonical entry it migrates — was deleted along with it. Reproduced: before
# the fix, `--operator-flag` below is gone after one re-run.
CROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks legacy-owner.XXXXXX")"
CREPO="$CROOT/repo with spaces"
mkdir -p "$CREPO/src" "$CREPO/.claude" "$CREPO/workspace/.claude-sutando"
cp "$INSTALLER" "$CREPO/src/install-claude-hooks.sh"
printf '#!/bin/bash\necho "HANDOFF-RAN"\n' > "$CREPO/src/session-handoff.sh"
chmod +x "$CREPO/src/"*.sh
echo '{}' > "$CREPO/workspace/.claude-sutando/settings.json"   # nothing at the core dir yet

export C_LEGACY="$CREPO/.claude/settings.json" C_REPO="$CREPO"
python3 - <<'PY'
import json, os
p, repo = os.environ['C_LEGACY'], os.environ['C_REPO']
json.dump({"hooks": {"SessionEnd": [{"matcher": "", "hooks": [
    # (a) today's EXACT canonical shape, pre-move — must be SWEPT (Phase 3 has
    #     no "leave the current shape" exclusion; none of it belongs here).
    {"type": "command",
     "command": f'bash {repo}/src/session-handoff.sh "$TRANSCRIPT_PATH"'},
    # (b) operator-customized: same script, extra flag — must SURVIVE.
    {"type": "command",
     "command": f'bash {repo}/src/session-handoff.sh "$TRANSCRIPT_PATH" --operator-flag'},
]}]}}, open(p, "w"), indent=2)
PY
bash "$CREPO/src/install-claude-hooks.sh" >/dev/null 2>&1
CSURV="$(jq -r '(.hooks.SessionEnd // []) | map(.hooks // []) | flatten | map(.command) | .[]' "$C_LEGACY")"
ok "Phase 3: operator's customized project-level hook survives the migration" \
   "$(echo "$CSURV" | grep -q -- '--operator-flag' && echo 0 || echo 1)"
ok "Phase 3: today's canonical project-level shape is still swept" \
   "$(echo "$CSURV" | grep -qxF "bash $CREPO/src/session-handoff.sh \"\$TRANSCRIPT_PATH\"" && echo 1 || echo 0)"
ok "Phase 3: the operator's hook did not get duplicated" \
   "$([ "$(echo "$CSURV" | grep -c -- '--operator-flag')" = 1 ] && echo 0 || echo 1)"
rm -rf "$CROOT"

# --- 9. deprecated archive sweep must not eat an operator's own Desktop hook -
# #4309 review (keweichen/qingyun-wu, 2026-09-16): DEPRECATED_HOOKS swept the
# two pre-move archiver shapes via a bare `contains("Desktop/sutando-conversations/")`,
# which also matches ANY operator command that merely targets that directory —
# reproduced: before the fix, a custom wrapper below is gone after one run.
DROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks archive-owner.XXXXXX")"
DREPO="$DROOT/repo with spaces"
mkdir -p "$DREPO/src" "$DREPO/.claude" "$DREPO/workspace/.claude-sutando"
cp "$INSTALLER" "$DREPO/src/install-claude-hooks.sh"
cp "$HERE/../src/archive-transcript.sh" "$HERE/../src/hook_transcript_path.sh" "$DREPO/src/"
chmod +x "$DREPO/src/"*.sh
echo '{}' > "$DREPO/workspace/.claude-sutando/settings.json"

# Normalize through cd+pwd, same as the installer's own $REPO_DIR resolution —
# $TMPDIR can carry a trailing slash (macOS), and an un-normalized double slash
# in the fixture would not byte-match the installer's own anchored regex.
export D_LEGACY="$DREPO/.claude/settings.json" D_REPO="$(cd "$DREPO" && pwd)"
python3 - <<'PY'
import json, os
p, repo = os.environ['D_LEGACY'], os.environ['D_REPO']
json.dump({"hooks": {"PreCompact": [{"matcher": "", "hooks": [
    # (a) ancient bare-cp archiver, fully static — must be SWEPT.
    {"type": "command",
     "command": 'cp "$TRANSCRIPT_PATH" "$HOME/Desktop/sutando-conversations/$(date +%Y-%m-%dT%H-%M-%S).jsonl"'},
    # (b) intermediate archive-transcript.sh-with-Desktop-destination shape,
    #     this clone's exact historical form — must be SWEPT.
    {"type": "command",
     "command": f'bash \'{repo}/src/archive-transcript.sh\' "$HOME/Desktop/sutando-conversations/"'},
    # (c) operator's OWN differently-shaped command that merely mentions the
    #     same directory as a substring — must SURVIVE.
    {"type": "command",
     "command": 'bash "$HOME/my-custom-archiver.sh" --dest "$HOME/Desktop/sutando-conversations/" --verbose'},
]}]}}, open(p, "w"), indent=2)
PY
bash "$DREPO/src/install-claude-hooks.sh" >/dev/null 2>&1
DSURV="$(jq -r '(.hooks.PreCompact // []) | map(.hooks // []) | flatten | map(.command) | .[]' "$D_LEGACY")"
ok "archive sweep: operator's own Desktop-targeting wrapper survives" \
   "$(echo "$DSURV" | grep -q 'my-custom-archiver.sh' && echo 0 || echo 1)"
ok "archive sweep: the ancient bare-cp form is still removed" \
   "$(echo "$DSURV" | grep -q 'cp \\"\$TRANSCRIPT_PATH\\"' && echo 1 || echo 0)"
ok "archive sweep: the intermediate archive-transcript.sh+Desktop form is still removed" \
   "$(echo "$DSURV" | grep -q 'archive-transcript.sh.*Desktop/sutando-conversations' && echo 1 || echo 0)"
rm -rf "$DROOT"

# --- 10. an EXISTING but FAILING resolver must not be silently guessed past --
# #4309 review (keweichen/qingyun-wu, 2026-09-16): with a readable but exit-9
# sutando-config.sh, the installer previously "succeeded" against a guessed
# workspace/.claude-sutando path — on a configured clone that can write the
# wrong file and sweep project hooks with no replacement where the core reads.
EROOT2="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks resolver-fail.XXXXXX")"
EREPO2="$EROOT2/repo with spaces"
mkdir -p "$EREPO2/src" "$EREPO2/scripts" "$EREPO2/.claude" "$EREPO2/workspace/.claude-sutando"
cp "$INSTALLER" "$EREPO2/src/install-claude-hooks.sh"
printf '#!/bin/bash\nexit 9\n' > "$EREPO2/scripts/sutando-config.sh"
chmod +x "$EREPO2/scripts/sutando-config.sh"
OUT_FAIL="$(bash "$EREPO2/src/install-claude-hooks.sh" 2>&1)"; RC_FAIL=$?
ok "an existing-but-failing resolver makes the installer exit non-zero" \
   "$([ "$RC_FAIL" -ne 0 ] && echo 0 || echo 1)"
ok "it does not silently write the guessed fallback settings file" \
   "$([ ! -s "$EREPO2/workspace/.claude-sutando/settings.json" ] && echo 0 || echo 1)"
rm -rf "$EROOT2"

# --- 11. omit flag must not leave a stale archiver visible to GUEST sessions --
# #4309 review (keweichen/qingyun-wu, 2026-09-16): "the unattended auto-fix can
# migrate the other hooks while leaving this core-only hook visible to guest
# sessions." A legacy Desktop-shaped archiver sitting in PROJECT settings (not
# core settings) must be swept even under omit — it's guest-exposed, unlike the
# core-scoped case in section "the omit flag the opt-in survives untouched".
FROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks omit-project.XXXXXX")"
FREPO="$FROOT/repo with spaces"
mkdir -p "$FREPO/src" "$FREPO/.claude" "$FREPO/workspace/.claude-sutando"
cp "$INSTALLER" "$FREPO/src/install-claude-hooks.sh"
printf '#!/bin/bash\n:\n' > "$FREPO/src/session-handoff.sh"
printf '#!/bin/bash\n:\n' > "$FREPO/src/check-pending-tasks.sh"
printf '#!/bin/bash\n:\n' > "$FREPO/src/archive-transcript.sh"
chmod +x "$FREPO/src/"*.sh
echo '{}' > "$FREPO/workspace/.claude-sutando/settings.json"
python3 - "$FREPO/.claude/settings.json" <<'PY'
import json, sys
json.dump({"hooks": {"PreCompact": [{"hooks": [{"type": "command",
    "command": 'cp "$TRANSCRIPT_PATH" "$HOME/Desktop/sutando-conversations/$(date +%Y-%m-%dT%H-%M-%S).jsonl"'}]}]}},
    open(sys.argv[1], "w"), indent=2)
PY
SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE=1 \
  bash "$FREPO/src/install-claude-hooks.sh" >/dev/null 2>&1
FCMDS="$(jq -r '(.hooks.PreCompact // []) | map(.hooks // []) | flatten | map(.command) | .[]' "$FREPO/.claude/settings.json")"
ok "under omit, a legacy archiver at PROJECT level is still swept (guest-exposed)" \
   "$(echo "$FCMDS" | grep -qF 'cp "$TRANSCRIPT_PATH"' && echo 1 || echo 0)"
ok "and no successor is installed in its place at project level" \
   "$(echo "$FCMDS" | grep -q 'archive-transcript\.sh' && echo 1 || echo 0)"
rm -rf "$FROOT"

# --- 12. a WRAPPER before the marker must survive, not just a flag -----------
# #4309 review (keweichen/qingyun-wu, 2026-09-16): the SHAPE's middle wildcard
# used to be `.*` (unbounded) after the "starts like a path" check — it could
# cross a CLOSING quote + space into a SECOND shell argument, so an operator
# wrapper that merely PASSES our script as an argument
# (`bash '/op/wrap.sh' '<repo>/src/session-handoff.sh' ...`) matched the shape
# and got deleted. Reproduced live: before the fix, both survivor lines below
# read 0. Same class for the archive destination as a later argument.
GROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks wrapper-before-marker.XXXXXX")"
GREPO="$GROOT/repo with spaces"
mkdir -p "$GREPO/src" "$GREPO/.claude" "$GREPO/workspace/.claude-sutando"
cp "$INSTALLER" "$GREPO/src/install-claude-hooks.sh"
printf '#!/bin/bash\n:\n' > "$GREPO/src/session-handoff.sh"
printf '#!/bin/bash\n:\n' > "$GREPO/src/check-pending-tasks.sh"
cp "$HERE/../src/archive-transcript.sh" "$HERE/../src/hook_transcript_path.sh" "$GREPO/src/"
chmod +x "$GREPO/src/"*.sh

export G_LEGACY="$GREPO/.claude/settings.json" G_REPO="$GREPO"
python3 - <<'PY'
import json, os
p, repo = os.environ['G_LEGACY'], os.environ['G_REPO']
json.dump({"hooks": {
    "SessionEnd": [{"matcher": "", "hooks": [
        # canonical, pre-move — must be SWEPT (this is a Phase 3 migration fixture).
        {"type": "command",
         "command": f'bash \'{repo}/src/session-handoff.sh\' "$TRANSCRIPT_PATH"'},
        # operator's OWN wrapper, our script passed as its argument — must SURVIVE.
        {"type": "command",
         "command": f'bash \'/tmp/operator-wrapper.sh\' \'{repo}/src/session-handoff.sh\' "$TRANSCRIPT_PATH"'},
        # same, but the wrapper's OWN path is UNQUOTED (no spaces in it, so
        # it's syntactically valid unquoted) — must also SURVIVE. This is the
        # exact counterexample that broke the quote+space-only lookahead: no
        # quote precedes the argv-separating space at all (qingyun-wu,
        # 2026-09-16, reproduced live on bd2ddd51).
        {"type": "command",
         "command": f'bash /tmp/operator-wrapper-unquoted.sh \'{repo}/src/session-handoff.sh\' "$TRANSCRIPT_PATH"'},
        # BOTH wrapper and marker path unquoted, no space-adjacent quote at
        # all anywhere in the command — must also SURVIVE. This is the third
        # round on the same defect class: `bd2ddd51` closed the quoted-wrapper
        # crossing, `88edc83f` closed the unquoted-wrapper-into-quoted-marker
        # crossing, and qingyun-wu reproduced THIS shape live against
        # `88edc83f` (2026-09-16) — a plain space + slash separator, no quote
        # on either side of it to key off of.
        {"type": "command",
         "command": f'bash /tmp/operator-wrapper-bothunquoted.sh {repo}/src/session-handoff.sh "$TRANSCRIPT_PATH"'},
    ]}],
    "PreCompact": [{"matcher": "", "hooks": [
        # same shape, the archive destination as the wrapper's argument — SURVIVE.
        {"type": "command",
         "command": f'bash \'/tmp/archive-wrapper.sh\' \'{repo}/workspace/logs/conversations/\''},
        # unquoted-wrapper-path variant of the same, for the archive hook.
        {"type": "command",
         "command": f'bash /tmp/archive-wrapper-unquoted.sh \'{repo}/workspace/logs/conversations/\''},
        # both-unquoted variant of the same, for the archive hook.
        {"type": "command",
         "command": f'bash /tmp/archive-wrapper-bothunquoted.sh {repo}/workspace/logs/conversations/'},
    ]}],
}}, open(p, "w"), indent=2)
PY
bash "$GREPO/src/install-claude-hooks.sh" >/dev/null 2>&1
GSURV_SE="$(jq -r '(.hooks.SessionEnd // []) | map(.hooks // []) | flatten | map(.command) | .[]' "$G_LEGACY")"
GSURV_PC="$(jq -r '(.hooks.PreCompact // []) | map(.hooks // []) | flatten | map(.command) | .[]' "$G_LEGACY")"
ok "wrapper-before-marker: operator's session-handoff wrapper survives" \
   "$(echo "$GSURV_SE" | grep -qF 'operator-wrapper.sh' && echo 0 || echo 1)"
ok "wrapper-before-marker: UNQUOTED session-handoff wrapper path survives" \
   "$(echo "$GSURV_SE" | grep -qF 'operator-wrapper-unquoted.sh' && echo 0 || echo 1)"
ok "wrapper-before-marker: BOTH-UNQUOTED session-handoff wrapper path survives" \
   "$(echo "$GSURV_SE" | grep -qF 'operator-wrapper-bothunquoted.sh' && echo 0 || echo 1)"
ok "wrapper-before-marker: today's canonical session-handoff shape is still swept" \
   "$(echo "$GSURV_SE" | grep -qxF "bash '$GREPO/src/session-handoff.sh' \"\$TRANSCRIPT_PATH\"" && echo 1 || echo 0)"
ok "wrapper-before-marker: operator's archive-destination wrapper survives" \
   "$(echo "$GSURV_PC" | grep -qF 'archive-wrapper.sh' && echo 0 || echo 1)"
ok "wrapper-before-marker: UNQUOTED archive-destination wrapper path survives" \
   "$(echo "$GSURV_PC" | grep -qF 'archive-wrapper-unquoted.sh' && echo 0 || echo 1)"
ok "wrapper-before-marker: BOTH-UNQUOTED archive-destination wrapper path survives" \
   "$(echo "$GSURV_PC" | grep -qF 'archive-wrapper-bothunquoted.sh' && echo 0 || echo 1)"
rm -rf "$GROOT"

# --- 13. a COMPOUND command joined by a shell control operator, no space --
# `;` `&&` `||` `|` and friends separate shell commands exactly like a space
# separates argv words — but the round-12 tokenizer only knew about
# whitespace, so `bash /op/wrap.sh;<repo>/src/session-handoff.sh ...` (no
# space anywhere near the `;`) fused the wrapper's word onto the marker's
# word into ONE argv[1] that contains the marker, and got deleted
# (qingyun-wu 2026-09-16, reproduced live on 46132a6d).
#
# Deliberately a SPACE-FREE repo path here (unlike every other fixture in
# this file, which uses "repo with spaces" on purpose) — a repo path
# containing spaces ALSO forces the tokenizer into the bucket-B fallback for
# an unrelated reason (the wrapper's own unquoted word already splits on
# those spaces), which happens to reject the same candidate for the wrong
# reason and would silently pass this check even without the fix. Confirmed
# by mutation: the round-12 fixture (spaces-in-path) fixture did NOT
# reproduce this bug; only a clean, space-free path does.
KROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando-hooks-compound-op.XXXXXX")"
KREPO="$KROOT/repo-no-spaces"
mkdir -p "$KREPO/src" "$KREPO/.claude" "$KREPO/workspace/.claude-sutando"
cp "$INSTALLER" "$KREPO/src/install-claude-hooks.sh"
printf '#!/bin/bash\n:\n' > "$KREPO/src/session-handoff.sh"
printf '#!/bin/bash\n:\n' > "$KREPO/src/check-pending-tasks.sh"
cp "$HERE/../src/archive-transcript.sh" "$HERE/../src/hook_transcript_path.sh" "$KREPO/src/"
chmod +x "$KREPO/src/"*.sh

export K_LEGACY="$KREPO/.claude/settings.json" K_REPO="$KREPO"
python3 - <<'PY'
import json, os
p, repo = os.environ['K_LEGACY'], os.environ['K_REPO']
json.dump({"hooks": {
    "SessionEnd": [{"matcher": "", "hooks": [
        {"type": "command",
         "command": f'bash \'{repo}/src/session-handoff.sh\' "$TRANSCRIPT_PATH"'},
        # SEMICOLON-joined compound command — must SURVIVE.
        {"type": "command",
         "command": f'bash /tmp/operator-wrapper.sh;{repo}/src/session-handoff.sh "$TRANSCRIPT_PATH"'},
        # ANDAND-joined compound command — must SURVIVE.
        {"type": "command",
         "command": f'bash /tmp/operator-wrapper.sh&&{repo}/src/session-handoff.sh "$TRANSCRIPT_PATH"'},
        # the operator sits DIRECTLY after the command word, no wrapper word
        # at all — `bash` alone is one (no-op) command, and the marker's own
        # path is argv[0] of a SEPARATE second command, never an argument to
        # `bash`. Flattening argv into one array without segment boundaries
        # still read the marker as argv[1] of the first command (qingyun-wu
        # 2026-09-16, reproduced live on 16a1c6a8: this exact repro deleted
        # both variants below even though the round-4 fix already handled
        # the wrapper-plus-operator shape correctly). Must SURVIVE.
        {"type": "command",
         "command": f'bash ;{repo}/src/session-handoff.sh "$TRANSCRIPT_PATH"'},
        {"type": "command",
         "command": f'bash &&{repo}/src/session-handoff.sh "$TRANSCRIPT_PATH"'},
    ]}],
    "PreCompact": [{"matcher": "", "hooks": [
        {"type": "command",
         "command": f'bash \'/tmp/archive-wrapper.sh\' \'{repo}/workspace/logs/conversations/\''},
        # SEMICOLON-joined variant, for the archive hook.
        {"type": "command",
         "command": f'bash /tmp/archive-wrapper.sh;{repo}/workspace/logs/conversations/'},
        # operator directly after the command word, for the archive hook.
        {"type": "command",
         "command": f'bash ;{repo}/workspace/logs/conversations/'},
    ]}],
}}, open(p, "w"), indent=2)
PY
bash "$KREPO/src/install-claude-hooks.sh" >/dev/null 2>&1
KSURV_SE="$(jq -r '(.hooks.SessionEnd // []) | map(.hooks // []) | flatten | map(.command) | .[]' "$K_LEGACY")"
KSURV_PC="$(jq -r '(.hooks.PreCompact // []) | map(.hooks // []) | flatten | map(.command) | .[]' "$K_LEGACY")"
ok "compound-op (space-free repo): today's canonical shape is still swept" \
   "$(echo "$KSURV_SE" | grep -qxF "bash '$KREPO/src/session-handoff.sh' \"\$TRANSCRIPT_PATH\"" && echo 1 || echo 0)"
ok "compound-op: SEMICOLON-joined command survives" \
   "$(echo "$KSURV_SE" | grep -qF 'operator-wrapper.sh;' && echo 0 || echo 1)"
ok "compound-op: ANDAND-joined command survives" \
   "$(echo "$KSURV_SE" | grep -qF 'operator-wrapper.sh&&' && echo 0 || echo 1)"
ok "compound-op: bare SEMICOLON right after command word survives" \
   "$(echo "$KSURV_SE" | grep -qxF "bash ;$KREPO/src/session-handoff.sh \"\$TRANSCRIPT_PATH\"" && echo 0 || echo 1)"
ok "compound-op: bare ANDAND right after command word survives" \
   "$(echo "$KSURV_SE" | grep -qxF "bash &&$KREPO/src/session-handoff.sh \"\$TRANSCRIPT_PATH\"" && echo 0 || echo 1)"
ok "compound-op: SEMICOLON-joined archive-destination command survives" \
   "$(echo "$KSURV_PC" | grep -qF 'archive-wrapper.sh;' && echo 0 || echo 1)"
ok "compound-op: bare SEMICOLON archive-destination command survives" \
   "$(echo "$KSURV_PC" | grep -qxF "bash ;$KREPO/workspace/logs/conversations/" && echo 0 || echo 1)"
rm -rf "$KROOT"

# --- 14. RELOCATED CHECKOUT — the archiver hook a prior installer run wrote
# at the OLD checkout path must still be swept after the checkout moved.
# #4309 review round 6 (keweichen, 2026-09-16): ARCHIVE_LEGACY_SHAPES baked
# the CURRENT $REPO_DIR into its regex at construction time, so a relocated
# checkout's own pre-move hook (OLD path baked in) never matched a freshly
# built regex using the NEW path.
LROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks relocated-checkout.XXXXXX")"
LREPO="$LROOT/repo with spaces"
mkdir -p "$LREPO/src" "$LREPO/.claude" "$LREPO/workspace/.claude-sutando"
cp "$INSTALLER" "$LREPO/src/install-claude-hooks.sh"
cp "$HERE/../src/archive-transcript.sh" "$HERE/../src/hook_transcript_path.sh" "$LREPO/src/"
chmod +x "$LREPO/src/"*.sh
echo '{}' > "$LREPO/workspace/.claude-sutando/settings.json"

# The old path is a DIFFERENT, nonexistent location — simulating a checkout
# that was later moved to where $LREPO now lives. One old path is plain; the
# other carries a legal apostrophe (#4309 review round 7, keweichen), which
# shq() spells `'\''` mid-string — a shape the OLD regex's `'/[^']*` could
# never match, since it assumes zero apostrophes between the quotes.
# shq()-quote the apostrophe path the way the REAL installer would (Python's
# shlex.quote uses a different, also-valid escaping — '"'"' — that this
# fixture must NOT use, or it tests a shape shq() never actually produces).
export L_LEGACY="$LREPO/.claude/settings.json" \
       L_OLDREPO="$LROOT/an old checkout path that no longer exists" \
       L_OLDREPO_APOS_QUOTED="$(shq "$LROOT/an old'checkout path that no longer exists/src/archive-transcript.sh")" \
       L_OLDREPO_APOS="$LROOT/an old'checkout path that no longer exists"
python3 - <<'PY'
import json, os
p = os.environ['L_LEGACY']
old_repo, old_repo_apos_quoted = os.environ['L_OLDREPO'], os.environ['L_OLDREPO_APOS_QUOTED']
json.dump({"hooks": {"PreCompact": [{"matcher": "", "hooks": [
    # written by a PRIOR run of this installer at the OLD checkout path,
    # before the checkout moved to where it lives now — must be SWEPT.
    {"type": "command",
     "command": f'bash \'{old_repo}/src/archive-transcript.sh\' "$HOME/Desktop/sutando-conversations/"'},
    # same, but the OLD path itself contains an apostrophe, shq()-quoted the
    # way the real installer spells it — must ALSO be SWEPT.
    {"type": "command",
     "command": f'bash {old_repo_apos_quoted} "$HOME/Desktop/sutando-conversations/"'},
    # operator's own differently-shaped command mentioning the same
    # directory as a substring — must SURVIVE.
    {"type": "command",
     "command": 'bash "$HOME/my-custom-archiver.sh" --dest "$HOME/Desktop/sutando-conversations/" --verbose'},
]}]}}, open(p, "w"), indent=2)
PY
bash "$LREPO/src/install-claude-hooks.sh" >/dev/null 2>&1
LSURV="$(jq -r '(.hooks.PreCompact // []) | map(.hooks // []) | flatten | map(.command) | .[]' "$L_LEGACY")"
ok "relocated checkout: pre-move archiver hook (OLD path baked in) is swept" \
   "$(echo "$LSURV" | grep -qF "$L_OLDREPO/src/archive-transcript.sh" && echo 1 || echo 0)"
# Search for the shq()-QUOTED value, not the raw path: shq() rewrites the
# apostrophe itself as `'\''`, so the raw (unescaped) path is never a literal
# substring of the stored, quoted command — only its quoted form is.
ok "relocated checkout: pre-move archiver hook (OLD path WITH APOSTROPHE) is swept" \
   "$(echo "$LSURV" | grep -qF "$L_OLDREPO_APOS_QUOTED" && echo 1 || echo 0)"
ok "relocated checkout: operator's own Desktop-targeting wrapper still survives" \
   "$(echo "$LSURV" | grep -q 'my-custom-archiver.sh' && echo 0 || echo 1)"
rm -rf "$LROOT"

# --- 15. RELOCATED CHECKOUT — a skill-declared hook (src/skill_hooks.py) left
# behind at project scope must also be swept after the checkout moved.
# #4309 review round 7 (keweichen, 2026-09-16): its `[ -f Q ] || exit 0; exec
# RUNNER Q` guard command carries an unquoted `||`/`;`, which candidate_is_owned()
# rejects outright for every OTHER shape — so this one, alone, never matched.
SROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks skill-relocated.XXXXXX")"
SREPO="$SROOT/repo with spaces"
mkdir -p "$SREPO/src" "$SREPO/.claude" "$SREPO/workspace/.claude-sutando" \
         "$SREPO/skills/testhook"
cp "$INSTALLER" "$SREPO/src/install-claude-hooks.sh"
cp "$HERE/../src/skill_hooks.py" "$SREPO/src/"
echo '{}' > "$SREPO/workspace/.claude-sutando/settings.json"
cat > "$SREPO/skills/testhook/manifest.json" <<'JSON'
{"hooks": [{"event": "PreToolUse", "command": "hook.sh"}]}
JSON
printf '#!/bin/bash\ntrue\n' > "$SREPO/skills/testhook/hook.sh"
chmod +x "$SREPO/skills/testhook/hook.sh"

# The OLD path is a different, nonexistent location — the hook file only
# exists at the NEW ($SREPO) path, exactly like a checkout that moved.
export S_LEGACY="$SREPO/.claude/settings.json" \
       S_OLDREPO="$SROOT/an old checkout path that no longer exists"
python3 - <<'PY'
import json, os, shlex
p, old_repo = os.environ['S_LEGACY'], os.environ['S_OLDREPO']
old_hook = f"{old_repo}/skills/testhook/hook.sh"
q = shlex.quote(old_hook)
json.dump({"hooks": {"PreToolUse": [{"matcher": "", "hooks": [
    # written by a PRIOR run at the OLD checkout path — must be SWEPT.
    {"type": "command", "command": f"[ -f {q} ] || exit 0; exec bash {q}"},
    # operator's own command that merely mentions the same filename — must SURVIVE.
    {"type": "command", "command": "bash /Users/dev/my-own-hook.sh --target hook.sh"},
]}]}}, open(p, "w"), indent=2)
PY
bash "$SREPO/src/install-claude-hooks.sh" >/dev/null 2>&1
SSURV="$(jq -r '(.hooks.PreToolUse // []) | map(.hooks // []) | flatten | map(.command) | .[]' "$S_LEGACY")"
ok "relocated checkout: pre-move SKILL hook (guard shape, OLD path baked in) is swept" \
   "$(echo "$SSURV" | grep -qF "old checkout path that no longer exists" && echo 1 || echo 0)"
ok "relocated checkout: operator's own hook.sh-mentioning command still survives" \
   "$(echo "$SSURV" | grep -qF 'my-own-hook.sh' && echo 0 || echo 1)"
rm -rf "$SROOT"

rm -rf "$ROOT"
echo "---"
if [ "$fail" -gt 0 ]; then
    echo "FAILED — $fail of $((pass+fail)) checks"; exit 1
fi
echo "PASS — install-claude-hooks ($pass checks)"
