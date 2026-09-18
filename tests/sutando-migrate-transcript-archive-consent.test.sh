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

echo
if [ "$FAILURES" -ne 0 ]; then echo "FAILED ($FAILURES)"; exit 1; fi
echo "All transcript-archive consent checks passed."
