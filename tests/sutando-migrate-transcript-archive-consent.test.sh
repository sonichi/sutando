#!/bin/bash
# #4309 review round 15 (keweichen finding #4): the automatic migration
# bridge invoked install-claude-hooks.sh with no
# SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE, so a core with no prior opt-in
# gained the full-conversation-copy PreCompact hook merely by restarting
# through migration -- the opposite of health-check's own default-off
# --fix policy (src/health-check.py:9758-9825). Two directions, both real:
# absent prior opt-in must stay absent; a genuine prior opt-in must survive.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
FAILURES=0

check() {  # check <name> <condition-exit> <detail>
    if [ "$2" -eq 0 ]; then echo "  ok   $1"; else
        echo "  FAIL $1 ${3:-}"; FAILURES=$((FAILURES + 1))
    fi
}

new_fixture() {  # new_fixture <dir>
    local d="$1"
    mkdir -p "$d/repo/scripts" "$d/repo/src" "$d/ws/state" "$d/home" \
             "$d/src/a" "$d/src/b" "$d/src/c"
    cp "$REPO/scripts/sutando-migrate.sh"       "$d/repo/scripts/"
    cp "$REPO/scripts/sutando-config.sh"        "$d/repo/scripts/"
    cp "$REPO/scripts/sutando-config-hooks.sh"  "$d/repo/scripts/"
    cp "$REPO/scripts/python-binary.sh"         "$d/repo/scripts/"
    cp "$REPO/src/sutando_config.py"            "$d/repo/src/"
    cp "$REPO/src/install-claude-hooks.sh"      "$d/repo/src/"
    cp "$REPO/sutando.config.json"              "$d/repo/"
    touch "$d/repo/CLAUDE.md" \
          "$d/repo/src/archive-transcript.sh" "$d/repo/src/session-handoff.sh" \
          "$d/repo/src/check-pending-tasks.sh" "$d/repo/src/turn-start.sh"
    printf '{"workspace": {"path": "%s"}}\n' "$d/ws" > "$d/repo/sutando.config.local.json"
}

run_migrate() {  # run_migrate <dir>
    local d="$1"
    HOME="$d/home" \
    SUTANDO_MIGRATE_SRC_A="$d/src/a" \
    SUTANDO_MIGRATE_SRC_B="$d/src/b" \
    SUTANDO_MIGRATE_SRC_C="$d/src/c" \
    bash "$d/repo/scripts/sutando-migrate.sh" --commit --no-confirm --no-claude-import >/dev/null 2>&1
}

archiver_present() {  # archiver_present <settings-file>
    jq -e '[.hooks.PreCompact // [] | .[].hooks[] | select(.command | contains("archive-transcript.sh"))] | length >= 1' \
       "$1" >/dev/null 2>&1
}

# ── Negative control: no prior opt-in anywhere -> archiver stays absent.
PC1="$(mktemp -d -t migrate-archive-absent-XXXXXX)"
new_fixture "$PC1"
run_migrate "$PC1"
CCD1="$(HOME="$PC1/home" bash "$PC1/repo/scripts/sutando-config.sh" claude-sutando-config-dir 2>/dev/null)"
! archiver_present "$CCD1/settings.json"
check "no prior opt-in anywhere -> archiver hook absent after migration" $?
rm -rf "$PC1"

# ── Positive control: operator had ALREADY opted in (ran install-claude-hooks.sh
# plain, without the omit flag, before ever running migration) -> that consent
# must survive a migration, not be silently dropped back to default-off.
PC2="$(mktemp -d -t migrate-archive-present-XXXXXX)"
new_fixture "$PC2"
CCD2="$(HOME="$PC2/home" bash "$PC2/repo/scripts/sutando-config.sh" claude-sutando-config-dir 2>/dev/null)"
mkdir -p "$CCD2"
HOME="$PC2/home" bash "$PC2/repo/src/install-claude-hooks.sh" >/dev/null 2>&1
archiver_present "$CCD2/settings.json"
check "fixture sanity: plain install-claude-hooks.sh opts in the archiver" $?
run_migrate "$PC2"
archiver_present "$CCD2/settings.json"
check "THE POINT: a genuine prior opt-in survives migration" $? \
    "archiver hook was dropped -- migration reset an explicit prior consent"
rm -rf "$PC2"

# ── keweichen round 17, direction 1: a prior opt-in sitting ONLY at the
# pre-move project-level location ($REPO/.claude/settings.json, never the
# home-dir $_old_settings this file used to check) must survive migration.
PC3="$(mktemp -d -t migrate-archive-legacy-project-XXXXXX)"
new_fixture "$PC3"
mkdir -p "$PC3/repo/.claude"
WS3="$(HOME="$PC3/home" bash "$PC3/repo/scripts/sutando-config.sh" workspace 2>/dev/null)"
printf '{"hooks":{"PreCompact":[{"hooks":[{"type":"command","command":"bash %s %s"}]}]}}\n' \
    "'$PC3/repo/src/archive-transcript.sh'" "'$WS3/logs/conversations/'" \
    > "$PC3/repo/.claude/settings.json"
run_migrate "$PC3"
CCD3="$(HOME="$PC3/home" bash "$PC3/repo/scripts/sutando-config.sh" claude-sutando-config-dir 2>/dev/null)"
archiver_present "$CCD3/settings.json"
check "a legacy PROJECT-level opt-in ($REPO/.claude/settings.json) survives migration" $? \
    "the project-level consent was invisible to the old check -- migration reset it to default-off"
rm -rf "$PC3"

# ── keweichen round 17, direction 2: a FOREIGN decoy (same script basename
# and same "logs/conversations/" substring, wrong install's actual paths)
# planted at a location the check DOES read must NOT be read as consent --
# only THIS install's exact script+dest path counts, per the anchored fix.
# (Confirmed this decoy shape is read as consent on the PRE-fix code below.)
PC4="$(mktemp -d -t migrate-archive-foreign-decoy-XXXXXX)"
new_fixture "$PC4"
mkdir -p "$PC4/home/.claude"
printf '{"hooks":{"PreCompact":[{"hooks":[{"type":"command","command":"bash %s %s"}]}]}}\n' \
    "'/some/other/checkout/src/archive-transcript.sh'" "'/some/other/workspace/logs/conversations/decoy/'" \
    > "$PC4/home/.claude/settings.json"
run_migrate "$PC4"
CCD4="$(HOME="$PC4/home" bash "$PC4/repo/scripts/sutando-config.sh" claude-sutando-config-dir 2>/dev/null)"
! archiver_present "$CCD4/settings.json"
check "a foreign decoy (unrelated script/dest paths) is NOT read as consent" $? \
    "the loose old pattern let an unrelated install's hook enable full-transcript copying"
rm -rf "$PC4"

echo
if [ "$FAILURES" -ne 0 ]; then echo "FAILED ($FAILURES)"; exit 1; fi
echo "All transcript-archive consent checks passed."
