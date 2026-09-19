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
# The installer targets the core's launch dir when this is set; the core session exports it,
# so a suite inherited from that session would write every fixture into the LIVE settings.
unset SUTANDO_CLAUDE_WORKING_DIR

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
mkdir -p "$REPO/src" "$REPO/.claude"
cp "$INSTALLER" "$REPO/src/install-claude-hooks.sh"
# Stub the hook targets so an executed command can prove WHICH file it reached.
printf '#!/bin/bash\necho "HANDOFF-RAN"\n'      > "$REPO/src/session-handoff.sh"
printf '#!/bin/bash\necho "PENDING-RAN"\n'      > "$REPO/src/check-pending-tasks.sh"
# The archiver is exercised for real below, so copy it and its resolver rather
# than stubbing: a stub would assert the hook string, not that it archives.
cp "$HERE/../src/archive-transcript.sh" "$HERE/../src/hook_transcript_path.sh" "$REPO/src/"
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

# The archive hook is a bare `cp`, so it cannot create its own destination. The
# assertion that matters is not "the directory exists" but "the stored command
# executed by a shell actually archives a file" — the same standard as above.
ok "installer created the archive hook's destination directory" \
   "$([ -d "$HOME/Desktop/sutando-conversations" ] && echo 0 || echo 1)"

AR_CMD="$(cmds PreCompact | grep sutando-conversations || true)"
printf 'transcript\n' > "$ROOT/transcript.jsonl"
# Drive it the way Claude Code does — transcript_path on stdin JSON, no env var.
# Feeding $TRANSCRIPT_PATH instead passed only while the legacy `cp` co-existed.
printf '{"transcript_path": "%s"}' "$ROOT/transcript.jsonl" \
  | bash -c "$AR_CMD" >/dev/null 2>&1
ok "PreCompact archive stored command actually writes a transcript" \
   "$([ "$(ls "$HOME/Desktop/sutando-conversations" 2>/dev/null | wc -l | tr -d ' ')" = 1 ] && echo 0 || echo 1)"

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
   "$(cmds PreCompact | grep -q 'archive-transcript\.sh.*sutando-conversations' && echo 0 || echo 1)"
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

# --- 8. a checkout path containing an APOSTROPHE ----------------------------
# An apostrophe is legal in a path, and `shq` escapes it as '\'' — so any
# matching scheme that "normalizes" by deleting quote characters leaves a stray
# backslash and silently stops recognising its own output. The symptom is not a
# crash: the stale entry simply survives and BOTH hooks fire forever.
# Fixture path has a space AND an apostrophe on purpose.
AROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks apos.XXXXXX")"
AREPO="$AROOT/repo'quote with spaces"
mkdir -p "$AREPO/src" "$AREPO/.claude"
cp "$INSTALLER" "$AREPO/src/install-claude-hooks.sh"
printf '#!/bin/bash\necho "HANDOFF-RAN"\n' > "$AREPO/src/session-handoff.sh"
printf '#!/bin/bash\necho "PENDING-RAN"\n' > "$AREPO/src/check-pending-tasks.sh"
chmod +x "$AREPO/src/"*.sh
echo '{}' > "$AREPO/.claude/settings.json"
bash "$AREPO/src/install-claude-hooks.sh" >/dev/null 2>&1

export A_SETTINGS="$AREPO/.claude/settings.json" A_REPO="$AREPO"
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
mkdir -p "$BREPO/src" "$BREPO/.claude"
cp "$INSTALLER" "$BREPO/src/install-claude-hooks.sh"
printf '#!/bin/bash\necho "HANDOFF-RAN"\n' > "$BREPO/src/session-handoff.sh"
printf '#!/bin/bash\necho "PENDING-RAN"\n' > "$BREPO/src/check-pending-tasks.sh"
chmod +x "$BREPO/src/"*.sh
echo '{}' > "$BREPO/.claude/settings.json"
bash "$BREPO/src/install-claude-hooks.sh" >/dev/null 2>&1

export B_SETTINGS="$BREPO/.claude/settings.json" B_REPO="$BREPO"
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
mkdir -p "$CREPO/src" "$CREPO/.claude"
cp "$INSTALLER" "$CREPO/src/install-claude-hooks.sh"
printf '#!/bin/bash\necho "HANDOFF-RAN"\n' > "$CREPO/src/session-handoff.sh"
printf '#!/bin/bash\necho "PENDING-RAN"\n' > "$CREPO/src/check-pending-tasks.sh"
chmod +x "$CREPO/src/"*.sh
export C_SETTINGS="$CREPO/.claude/settings.json"
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
   "$(echo "$CCMDS" | grep -q 'archive-transcript\.sh.*sutando-conversations' && echo 0 || echo 1)"
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
  mkdir -p "$1/src" "$1/.claude"
  cp "$INSTALLER" "$1/src/install-claude-hooks.sh"
  printf '#!/bin/bash\n:\n' > "$1/src/session-handoff.sh"
  printf '#!/bin/bash\n:\n' > "$1/src/check-pending-tasks.sh"
  printf '#!/bin/bash\n:\n' > "$1/src/archive-transcript.sh"
  chmod +x "$1/src/"*.sh
  LEG_SETTINGS="$1/.claude/settings.json" LEG_CMD="$LEG_CMD" python3 - <<'PY'
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
                   for h in g['hooks'] if 'sutando-conversations' in h['command']))
"
}

LROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks legacy.XXXXXX")"
seed_legacy_repo "$LROOT/repo with spaces"
bash "$LROOT/repo with spaces/src/install-claude-hooks.sh" >/dev/null 2>&1
LCMDS="$(archive_cmds "$LROOT/repo with spaces/.claude/settings.json")"
ok "legacy bare-cp archiver is removed on upgrade" \
   "$(echo "$LCMDS" | grep -qF 'cp "$TRANSCRIPT_PATH"' && echo 1 || echo 0)"
ok "and replaced by the archive-transcript.sh form" \
   "$(echo "$LCMDS" | grep -q 'archive-transcript\.sh' && echo 0 || echo 1)"
ok "leaving exactly one archiver hook, not two" \
   "$([ "$(echo "$LCMDS" | grep -c 'sutando-conversations')" = 1 ] && echo 0 || echo 1)"
rm -rf "$LROOT"

OROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks legacy omit.XXXXXX")"
seed_legacy_repo "$OROOT/repo with spaces"
SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE=1 \
  bash "$OROOT/repo with spaces/src/install-claude-hooks.sh" >/dev/null 2>&1
OCMDS="$(archive_cmds "$OROOT/repo with spaces/.claude/settings.json")"
ok "under the omit flag the opt-in survives untouched" \
   "$(echo "$OCMDS" | grep -qF 'cp "$TRANSCRIPT_PATH"' && echo 0 || echo 1)"
ok "and no archiver is installed in its place" \
   "$(echo "$OCMDS" | grep -q 'archive-transcript\.sh' && echo 1 || echo 0)"
rm -rf "$OROOT"

# ---- upgrade path: a pre-existing runner-first skill hook must be MIGRATED ----
# The outage case: a re-run must replace the old `python3 <path>` entry, not add beside it.
UROOT="$(mktemp -d)"; UREPO="$UROOT/repo"
mkdir -p "$UREPO/.claude" "$UREPO/src" "$UREPO/skills/demo/hooks"
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
export U_SETTINGS="$UREPO/.claude/settings.json"
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
mkdir -p "$EREPO/.claude" "$EREPO/src" "$EREPO/skills/demo/hooks"
cp "$HERE/../src/install-claude-hooks.sh" "$EREPO/src/"
cp "$HERE/../src/skill_hooks.py" "$EREPO/src/"
printf '#!/bin/bash\n:\n' > "$EREPO/src/session-handoff.sh"
printf '#!/bin/bash\n:\n' > "$EREPO/src/check-pending-tasks.sh"
chmod +x "$EREPO/src/"*.sh
printf '{"name":"demo","hooks":[{"event":"PreToolUse","command":"./hooks/g.py"}]}\n' \
    > "$EREPO/skills/demo/manifest.json"
printf 'import sys; sys.exit(2)\n' > "$EREPO/skills/demo/hooks/g.py"
EPATH="$(python3 -c "import pathlib,sys;print(pathlib.Path(sys.argv[1]).resolve())" "$EREPO/skills/demo/hooks/g.py")"
export E_SETTINGS="$EREPO/.claude/settings.json"
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

# --- 7. the target is the directory the core LAUNCHES from --------------------
# start-cli.sh honours SUTANDO_CLAUDE_WORKING_DIR; Claude Code reads project settings
# there, so an installer that always writes the engine tree arms nothing on such a host.
CROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks cwd.XXXXXX")"
CREPO="$CROOT/engine repo"; CCWD="$CROOT/core cwd"
mkdir -p "$CREPO/src" "$CREPO/scripts" "$CREPO/.claude"
cp "$INSTALLER" "$CREPO/src/install-claude-hooks.sh"
cp "$HERE/../scripts/python-binary.sh" "$CREPO/scripts/python-binary.sh"
cp "$HERE/../scripts/core-working-dir.sh" "$CREPO/scripts/core-working-dir.sh"
printf '#!/bin/bash\necho "HANDOFF-RAN"\n' > "$CREPO/src/session-handoff.sh"
printf '#!/bin/bash\necho "PENDING-RAN"\n' > "$CREPO/src/check-pending-tasks.sh"
chmod +x "$CREPO/src/"*.sh
echo '{"hooks":{"SessionStart":[{"matcher":"","hooks":[{"type":"command","command":"echo app-owned"}]}]}}' > "$CREPO/.claude/settings.json"
ENGINE_BEFORE="$(cat "$CREPO/.claude/settings.json")"
C_OUT="$(SUTANDO_CLAUDE_WORKING_DIR="$CCWD" bash "$CREPO/src/install-claude-hooks.sh" 2>&1)"; C_RC=$?
ok "cwd override: installer exits 0" "$([ $C_RC = 0 ] && echo 0 || echo 1)"
ok "cwd override: settings.json is written in the launch dir" \
   "$([ -f "$CCWD/.claude/settings.json" ] && echo 0 || echo 1)"
ok "cwd override: the engine tree's settings are untouched" \
   "$([ "$(cat "$CREPO/.claude/settings.json")" = "$ENGINE_BEFORE" ] && echo 0 || echo 1)"
C_SE="$(jq -r '(.hooks // {})["SessionEnd"] // [] | map(.hooks // []) | flatten | map(.command) | .[]' "$CCWD/.claude/settings.json" | grep session-handoff || true)"
ok "cwd override: the stored command still runs the ENGINE's script" \
   "$([ "$(TRANSCRIPT_PATH=/dev/null bash -c "$C_SE" 2>&1)" = "HANDOFF-RAN" ] && echo 0 || echo 1)"
# Compared physically: macOS resolves /var to /private/var and the installer stores `pwd -P`.
ok "cwd override: the reported target names the launch dir" \
   "$(echo "$C_OUT" | grep -qF "→ $(cd "$CCWD" && pwd -P)/.claude/settings.json" && echo 0 || echo 1)"
rm -rf "$CROOT"

# --- 8. skill-hook discovery runs the LAUNCHER's interpreter, not a bare python3 ----
# A configured SUTANDO_PY must win over a broken PATH python3, and a broken discovery
# must be reported (non-zero, stderr) rather than silently dropping the skill hooks.
PROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks py.XXXXXX")"
PREPO="$PROOT/repo"; mkdir -p "$PREPO/src" "$PREPO/scripts" "$PREPO/.claude" "$PREPO/skills/demo/hooks" "$PROOT/bin"
cp "$INSTALLER" "$PREPO/src/install-claude-hooks.sh"
cp "$HERE/../scripts/python-binary.sh" "$PREPO/scripts/python-binary.sh"
cp "$HERE/../src/skill_hooks.py" "$PREPO/src/skill_hooks.py"
printf '#!/bin/bash\nexit 0\n' > "$PREPO/src/session-handoff.sh"; printf '#!/bin/bash\nexit 0\n' > "$PREPO/src/check-pending-tasks.sh"
printf 'print("HOOK_EXECUTED")\n' > "$PREPO/skills/demo/hooks/demo-hook.py"
echo '{"name":"demo","hooks":[{"event":"PreToolUse","command":"./hooks/demo-hook.py"}]}' > "$PREPO/skills/demo/manifest.json"
REAL_PY="$(command -v python3)"
# A PATH python3 that records every invocation and fails.
printf '#!/bin/bash\necho invoked >> "%s/stub.log"\nexit 79\n' "$PROOT" > "$PROOT/bin/python3"; chmod +x "$PROOT/bin/python3"
skill_hooks_in() { jq -r '(.hooks // {})["PreToolUse"] // [] | map(.hooks // []) | flatten | map(.command) | .[]' "$1" 2>/dev/null | grep -c "demo-hook.py"; }
echo '{}' > "$PREPO/.claude/settings.json"
P_OUT="$(PATH="$PROOT/bin:$PATH" SUTANDO_PY="$REAL_PY" bash "$PREPO/src/install-claude-hooks.sh" 2>&1)"; P_RC=$?
ok "configured SUTANDO_PY + broken PATH python3: installer exits 0" "$([ $P_RC = 0 ] && echo 0 || echo 1)"
ok "configured SUTANDO_PY + broken PATH python3: the skill hook IS registered" \
   "$([ "$(skill_hooks_in "$PREPO/.claude/settings.json")" = 1 ] && echo 0 || echo 1)"
ok "configured SUTANDO_PY + broken PATH python3: the PATH stub was never invoked" \
   "$([ ! -f "$PROOT/stub.log" ] && echo 0 || echo 1)"
# EXECUTION, not registration: fire the exact stored command in the same environment.
P_CMD="$(jq -r '(.hooks // {})["PreToolUse"] // [] | map(.hooks // []) | flatten | map(.command) | .[]' "$PREPO/.claude/settings.json" | grep demo-hook.py)"
P_RUN="$(PATH="$PROOT/bin:$PATH" SUTANDO_PY="$REAL_PY" bash -c "$P_CMD" 2>&1)"; P_RUN_RC=$?
ok "configured SUTANDO_PY + broken PATH python3: the registered hook EXECUTES (rc 0, HOOK_EXECUTED)" \
   "$([ $P_RUN_RC = 0 ] && [ "$P_RUN" = "HOOK_EXECUTED" ] && echo 0 || echo 1)"
[ "$P_RUN" = "HOOK_EXECUTED" ] || echo "     got rc=$P_RUN_RC: $P_RUN"
ok "configured SUTANDO_PY + broken PATH python3: executing it never touched the PATH stub" \
   "$([ ! -f "$PROOT/stub.log" ] && echo 0 || echo 1)"
# Migration: the shape the previous revision registered (exec bare python3) is swept, not kept beside.
python3 - "$PREPO/.claude/settings.json" "$PREPO" <<'PY'
import json, pathlib, shlex, sys
p, repo = sys.argv[1], sys.argv[2]
d = json.load(open(p)); q = shlex.quote(str(pathlib.Path(repo).resolve() / "skills/demo/hooks/demo-hook.py"))
d["hooks"]["PreToolUse"][0]["hooks"].append({"type": "command", "command": f"[ -f {q} ] || exit 0; exec python3 {q}"})
json.dump(d, open(p, "w"), indent=2)
PY
ok "old guarded-bare-python3 form present before re-run (fixture sanity)" \
   "$([ "$(skill_hooks_in "$PREPO/.claude/settings.json")" = 2 ] && echo 0 || echo 1)"
PATH="$PROOT/bin:$PATH" SUTANDO_PY="$REAL_PY" bash "$PREPO/src/install-claude-hooks.sh" >/dev/null 2>&1
ok "old guarded-bare-python3 form is SWEPT on re-run (exactly one skill hook)" \
   "$([ "$(skill_hooks_in "$PREPO/.claude/settings.json")" = 1 ] && echo 0 || echo 1)"
ok "the survivor is the one that resolves its interpreter through scripts/python-binary.sh" \
   "$(jq -r '.hooks.PreToolUse[].hooks[].command' "$PREPO/.claude/settings.json" | grep -q 'resolve_python' && echo 0 || echo 1)"
echo '{}' > "$PREPO/.claude/settings.json"
P_OUT2="$(env -u SUTANDO_PY PATH="$PROOT/bin:$PATH" bash "$PREPO/src/install-claude-hooks.sh" 2>&1)"; P_RC2=$?
ok "broken PATH python3 only: installer exits NON-zero" "$([ $P_RC2 != 0 ] && echo 0 || echo 1)"
ok "broken PATH python3 only: stderr names the discovery failure" \
   "$(echo "$P_OUT2" | grep -q "skill-hook discovery failed (rc=79" && echo 0 || echo 1)"
ok "broken PATH python3 only: the static hooks are still installed" \
   "$(jq -r '(.hooks // {})["Stop"] // [] | map(.hooks // []) | flatten | map(.command) | .[]' "$PREPO/.claude/settings.json" | grep -q check-pending-tasks && echo 0 || echo 1)"
ok "broken PATH python3 only: the skill hook is absent (reported, not silent)" \
   "$([ "$(skill_hooks_in "$PREPO/.claude/settings.json")" = 0 ] && echo 0 || echo 1)"
# Second rung: a bundled interpreter beside the engine (repo/../runtime/python/bin/python3) is
# found by discovery AND becomes the command's default runner, so no SUTANDO_PY and a broken PATH
# still registers and still EXECUTES the hook.
mkdir -p "$PROOT/runtime/python/bin"; ln -s "$REAL_PY" "$PROOT/runtime/python/bin/python3"
echo '{}' > "$PREPO/.claude/settings.json"; rm -f "$PROOT/stub.log"
B_OUT="$(env -u SUTANDO_PY PATH="$PROOT/bin:$PATH" bash "$PREPO/src/install-claude-hooks.sh" 2>&1)"; B_RC=$?
ok "bundled python, no SUTANDO_PY, broken PATH: installer exits 0 and registers the skill hook" \
   "$([ $B_RC = 0 ] && [ "$(skill_hooks_in "$PREPO/.claude/settings.json")" = 1 ] && echo 0 || echo 1)"
B_CMD="$(jq -r '.hooks.PreToolUse[].hooks[].command' "$PREPO/.claude/settings.json" | grep demo-hook.py)"
ok "bundled python: the stored command names no interpreter itself (the resolver picks it at event time)" \
   "$(echo "$B_CMD" | grep -q 'resolve_python' && ! echo "$B_CMD" | grep -qF "runtime/python/bin/python3" && echo 0 || echo 1)"
B_RUN="$(env -u SUTANDO_PY PATH="$PROOT/bin:$PATH" bash -c "$B_CMD" 2>&1)"; B_RUN_RC=$?
ok "bundled python: the registered hook EXECUTES with no SUTANDO_PY and a broken PATH" \
   "$([ $B_RUN_RC = 0 ] && [ "$B_RUN" = "HOOK_EXECUTED" ] && [ ! -f "$PROOT/stub.log" ] && echo 0 || echo 1)"
[ "$B_RUN" = "HOOK_EXECUTED" ] || echo "     got rc=$B_RUN_RC: $B_RUN"
# A STALE override (nonexistent SUTANDO_PY) must not be exec'd (rc 126): the resolver validates
# executability at event time and falls to the bundled interpreter.
S_RUN="$(SUTANDO_PY="$PROOT/no/such/python3" PATH="$PROOT/bin:$PATH" bash -c "$B_CMD" 2>&1)"; S_RUN_RC=$?
ok "stale SUTANDO_PY + bundled python: the hook EXECUTES via the bundled interpreter (not rc 126)" \
   "$([ $S_RUN_RC = 0 ] && [ "$S_RUN" = "HOOK_EXECUTED" ] && echo 0 || echo 1)"
[ "$S_RUN" = "HOOK_EXECUTED" ] || echo "     got rc=$S_RUN_RC: $S_RUN"
ok "the stored command resolves through scripts/python-binary.sh (one interpreter policy)" \
   "$(echo "$B_CMD" | grep -q "scripts/python-binary.sh" && echo "$B_CMD" | grep -q "resolve_python" && echo 0 || echo 1)"
rm -rf "$PROOT"

# --- 9. the override contract is ONE policy on both sides (installer + probe): absolute or ~/ only ----
TROOT="$(mktemp -d "${TMPDIR:-/tmp}/sutando hooks tilde.XXXXXX")"
TREPO="$TROOT/repo"; mkdir -p "$TREPO/src" "$TREPO/scripts" "$TREPO/.claude"; export HOME="$TROOT/home"; mkdir -p "$HOME"
cp "$INSTALLER" "$TREPO/src/install-claude-hooks.sh"
cp "$HERE/../scripts/core-working-dir.sh" "$TREPO/scripts/core-working-dir.sh"
printf '#!/bin/bash\nexit 0\n' > "$TREPO/src/session-handoff.sh"; printf '#!/bin/bash\nexit 0\n' > "$TREPO/src/check-pending-tasks.sh"
T_OUT="$(SUTANDO_CLAUDE_WORKING_DIR="~/core home" bash "$TREPO/src/install-claude-hooks.sh" 2>&1)"; T_RC=$?
ok "tilde: ~/… resolves under HOME" "$([ $T_RC = 0 ] && [ -f "$HOME/core home/.claude/settings.json" ] && echo 0 || echo 1)"
U_OUT="$(SUTANDO_CLAUDE_WORKING_DIR="~someoneelse/core" bash "$TREPO/src/install-claude-hooks.sh" 2>&1)"; U_RC=$?
ok "tilde: ~user/… is REFUSED (rc 1), not mangled into HOME + user" "$([ $U_RC = 1 ] && echo 0 || echo 1)"
ok "tilde: the refusal names the contract" "$(echo "$U_OUT" | grep -q "absolute path or start with ~/" && echo 0 || echo 1)"
ok "tilde: nothing was created for the refused form" "$([ ! -e "$HOME/someoneelse" ] && [ ! -e "$HOME/core" ] && [ ! -e "$HOME"someoneelse ] && echo 0 || echo 1)"
R_OUT="$(SUTANDO_CLAUDE_WORKING_DIR="relative/dir" bash "$TREPO/src/install-claude-hooks.sh" 2>&1)"; R_RC=$?
ok "tilde: a relative path is refused too" "$([ $R_RC = 1 ] && echo 0 || echo 1)"
rm -rf "$TROOT"

rm -rf "$ROOT"
echo "---"
if [ "$fail" -gt 0 ]; then
    echo "FAILED — $fail of $((pass+fail)) checks"; exit 1
fi
echo "PASS — install-claude-hooks ($pass checks)"
