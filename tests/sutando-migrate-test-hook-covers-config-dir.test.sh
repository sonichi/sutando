#!/bin/bash
# SUTANDO_MIGRATE_DEST must redirect EVERY destination, not just DEST.
#
# The env var is declared in sutando-migrate.sh as a TEST hook for E2E fixtures.
# It overrides DEST — but the hook bridge and the channel bridge each resolve
# claude-sutando-config-dir independently via sutando-config.sh, so under a
# test-redirected migration they still wrote the OPERATOR's real config dir.
#
# Measured on a live host before the fix, alternating a 3s idle window against a
# 2s run of tests/sutando-migrate-argparse.test.py, three rounds:
#
#   round 1  IDLE(3s)       mtime same        round 1  TEST  MTIME CHANGED
#   round 2  IDLE(3s)       mtime same        round 2  TEST  MTIME CHANGED
#   round 3  IDLE(3s)       mtime same        round 3  TEST  MTIME CHANGED
#
# Content was IDENTICAL every time, because the hook install is idempotent, so a
# content-hash comparison reads "unchanged" and clears falsely. That is the
# measurement note the rounds above exist to record — mtime was the only thing
# that discriminated WHEN OBSERVING THE LIVE FILE.
#
# This file no longer observes it. The mtime guard was removed at b189b1ad
# (@john-the-dev): it could only fire AFTER a write, so it was a post-mortem
# rather than a guard, and obtaining it put live operator state inside the blast
# radius of the regression under test. On a detached worktree it silently
# SKIPPED, so it could not fire where it ran and could only do harm where it
# would.
#
# What the file asserts NOW, both directions, entirely side-effect-free:
#   * with the test hook set    -> both bridges announce the skip and do not run
#   * with NO test hook         -> neither bridge is skipped for that reason
# Mutation confirms the pair is sufficient: making the gate unconditional fails
# 2 named assertions, making it never fire fails 3, 6 of 6 running in both.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
FAILURES=0

check() {  # check <name> <condition-exit> <detail>
    if [ "$2" -eq 0 ]; then echo "  ok   $1"; else
        echo "  FAIL $1 ${3:-}"; FAILURES=$((FAILURES + 1))
    fi
}

TMP="$(mktemp -d -t migrate-hook-bridge-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/dest" "$TMP/src/a" "$TMP/src/b" "$TMP/src/c" "$TMP/home"

# FULL isolation of every source the script discovers, not just DEST.
# @qingyun-wu on #2624: the first version created $TMP/src and never wired it in,
# so `--commit` ran against the caller's REAL source discovery and REAL $HOME —
# sutando-migrate.sh:279 defaults B_PATH to the legacy install-home workspace
# under $HOME, and the post-commit path at :1650 invokes
# `sutando-shell-setup.sh --import` against the real Claude config.
# (Naming that default path literally here trips lint-sutando-home-path.sh, and
# widening its ALLOWED list to quiet a comment would disarm the guard for a file
# that owns no resolution. Cite the line instead.)
# A test proving the migration does not touch real config must not itself scan,
# copy, or adopt real config. Correct, and exactly the defect class this PR fixes.
#
# `--no-claude-import` opts out of the unrelated import. The two bridge opt-outs
# (--no-hook-bridge / --no-channel-bridge) are deliberately NOT passed: they are
# the code paths under test, and disabling them would make every assertion vacuous.

# NO live-config probe here. Earlier versions resolved claude-sutando-config-dir
# and stat'd the operator's settings.json around the run, as belt-and-braces
# evidence that the real config was untouched. @john-the-dev's blocker on
# e302e717: that assertion can only fire AFTER the write, so it is a post-mortem
# rather than a guard, and obtaining it puts live operator state inside the blast
# radius of the very regression under test. On his detached worktree the file was
# absent and the assertion silently skipped, which is the other half of the
# problem — a probe that cannot fire where it runs and can only do harm where it
# would.
#
# Nothing is lost. Both directions are already discriminated without touching
# live state: the skip-message assertions prove the guard fires under the hook,
# and the positive control below proves it does NOT fire without it. Verified by
# mutation — making the gate unconditional fails two NAMED assertions, neither of
# which was the mtime one.
#
# The stronger version @john-the-dev offered as an alternative — run the
# migration from a throwaway checkout whose Sutando config points at fixture
# state — was tried and does not work by symlinking a repo skeleton:
# `resolve_claude_sutando_config_dir()` derives from the repo root that
# `sutando-config.sh` computes as `cd "$(dirname "$0")/.." && pwd`, which
# resolved back to the real checkout. A real copy of src/ + scripts/ would do it;
# that is a larger change than this PR's single concern.

echo "sutando-migrate: test hook covers the config dir, not just DEST"

# --commit, NOT --dry-run: the bridges live in commit_main(), so a dry run
# never reaches them. My first version used --dry-run and produced IDENTICAL
# output with and without the fix — a reporter that cannot discriminate.
OUT="$(HOME="$TMP/home" \
       SUTANDO_MIGRATE_DEST="$TMP/dest" \
       SUTANDO_MIGRATE_SRC_A="$TMP/src/a" \
       SUTANDO_MIGRATE_SRC_B="$TMP/src/b" \
       SUTANDO_MIGRATE_SRC_C="$TMP/src/c" \
       bash "$REPO/scripts/sutando-migrate.sh" --commit --no-confirm --no-claude-import 2>&1)"

# 1. The guard announces itself. Silence would be indistinguishable from the
#    bridge having run and found nothing to do.
grep -q "hook bridge: skipped (SUTANDO_MIGRATE_DEST set" <<<"$OUT"
check "hook bridge announces the test-redirect skip" $? "output did not contain the skip line"

grep -q "channel bridge: skipped (SUTANDO_MIGRATE_DEST set" <<<"$OUT"
check "channel bridge announces the test-redirect skip" $? "output did not contain the skip line"

# 2. The bridges must NOT report doing work.
! grep -q "bridging hooks via sutando-config-hooks.sh" <<<"$OUT"
check "hook bridge did not run" $? "the bridge announced an install under a redirected DEST"

! grep -q "bridging channels (Sutando bridge access lists" <<<"$OUT"
check "channel bridge did not run" $? "the bridge announced a copy under a redirected DEST"


# --- POSITIVE CONTROL: WITHOUT the test hook the bridges must STILL RUN --------
# Self-audit after @john-the-dev's review of #2628: he disabled a gate entirely
# and the suite still passed. Same mutation here — dropping the
# `[ -n "${SUTANDO_MIGRATE_DEST:-}" ]` condition so both bridges skip
# UNCONDITIONALLY, even in a real migration — passed all five assertions above.
# A skip-gate whose test only ever exercises the skip cannot tell "correctly
# skipped" from "never runs at all", and an over-broad future edit would silently
# disable hook + channel bridging for real users while this file stayed green.
#
# Fully isolated: its own repo, workspace, HOME and SRC_{A,B,C}, so "no test hook"
# never means "the caller's real config".
PC="$(mktemp -d -t migrate-positive-control-XXXXXX)"
mkdir -p "$PC/repo/scripts" "$PC/repo/src" "$PC/ws/state" "$PC/home" "$PC/src/a" "$PC/src/b" "$PC/src/c"
cp "$REPO/scripts/sutando-migrate.sh" "$PC/repo/scripts/"
cp "$REPO/scripts/sutando-config.sh" "$PC/repo/scripts/"
cp "$REPO/src/sutando_config.py"     "$PC/repo/src/"
cp "$REPO/sutando.config.json"       "$PC/repo/"
touch "$PC/repo/CLAUDE.md"
printf '{"workspace": {"path": "%s"}}\n' "$PC/ws" > "$PC/repo/sutando.config.local.json"
PC_OUT="$(HOME="$PC/home" \
    SUTANDO_MIGRATE_SRC_A="$PC/src/a" \
    SUTANDO_MIGRATE_SRC_B="$PC/src/b" \
    SUTANDO_MIGRATE_SRC_C="$PC/src/c" \
    bash "$PC/repo/scripts/sutando-migrate.sh" --commit --no-confirm --no-claude-import 2>&1)"

! grep -q "hook bridge: skipped (SUTANDO_MIGRATE_DEST set" <<<"$PC_OUT"
check "no test hook -> the hook bridge is NOT skipped for that reason" $? \
    "the skip fired without SUTANDO_MIGRATE_DEST — the gate is unconditional"

! grep -q "channel bridge: skipped (SUTANDO_MIGRATE_DEST set" <<<"$PC_OUT"
check "no test hook -> the channel bridge is NOT skipped for that reason" $? \
    "the skip fired without SUTANDO_MIGRATE_DEST — the gate is unconditional"

rm -rf "$PC"

echo
if [ "$FAILURES" -ne 0 ]; then echo "FAILED ($FAILURES)"; exit 1; fi
echo "All test-hook coverage checks passed."
