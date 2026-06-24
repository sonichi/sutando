#!/usr/bin/env bash
# Test that generate_exclude UNTRACKS a tracked in-tree `workspace/.gitignore`
# (via `git rm`) rather than only `rm -f`-ing it.
#
# Why: the whitelist `.gitignore` carries `!notes/` un-ignore rules. When the
# workspace lives inside another git repo (e.g. a submodule), the outer repo's
# `workspace/*` deny is overridden by those deeper-dir un-ignores, leaking
# workspace content into the OUTER repo's `git status` (data-leak reproduced
# 2026-06-04 — see the boundary note in generate_exclude). The fix moved rules
# to `.git/info/exclude` and deletes the in-tree `.gitignore`. But an older
# host committed `workspace/.gitignore` to the vault (its `!.gitignore` rule
# self-tracks it), so a plain `rm -f` only deletes the local copy — the file
# returns on the next peer pull and the leak recurs. `git rm` makes the
# untrack a committed change that propagates through the vault.

set -u
# NOTE: deliberately not `set -e` — accumulate failures via `fail=1`.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
SYNC="$REPO/scripts/sync-workspace.sh"

fail=0

# ── Structural ──────────────────────────────────────────────────────────────
EXCL_BLOCK="$(awk '/^generate_exclude\(\) \{/,/^}$/' "$SYNC")"

# Test 1: gates the tracked-vs-untracked decision on ls-files --error-unmatch.
echo "$EXCL_BLOCK" | grep -q 'ls-files --error-unmatch .gitignore' \
    || { echo "  FAIL: T1 — generate_exclude does not probe tracked state via 'ls-files --error-unmatch .gitignore'"; fail=1; }

# Test 2: untracks via `git rm` (not only rm -f) for the tracked case.
echo "$EXCL_BLOCK" | grep -Eq 'git -C "\$WORKSPACE_DIR" rm .* \.gitignore' \
    || { echo "  FAIL: T2 — generate_exclude does not 'git rm' the tracked in-tree .gitignore"; fail=1; }

# Test 3: still has the rm -f fallback for the untracked case.
echo "$EXCL_BLOCK" | grep -q 'rm -f "\$legacy_gitignore"' \
    || { echo "  FAIL: T3 — generate_exclude lost the rm -f fallback for the untracked case"; fail=1; }

# Test 4: the whole block is still skipped under --dry-run (no mutation).
echo "$EXCL_BLOCK" | grep -q 'DRY_RUN" != "1" \] *; then\|DRY_RUN" != "1" \]' \
    || { echo "  FAIL: T4 — the .gitignore-removal block is no longer dry-run guarded"; fail=1; }

# ── E2E: mirror the deletion logic and prove tracked → staged deletion ───────
# (Sourcing the script triggers the dispatcher + global lock; like the sibling
# tests, we mirror the decision and exercise it on real git repos.)
_drop_legacy_gitignore() {  # arg: workspace dir
    local WORKSPACE_DIR="$1"
    local legacy_gitignore="$WORKSPACE_DIR/.gitignore"
    [ -f "$legacy_gitignore" ] || return 0
    if git -C "$WORKSPACE_DIR" ls-files --error-unmatch .gitignore >/dev/null 2>&1; then
        git -C "$WORKSPACE_DIR" rm -q -f .gitignore
    else
        rm -f "$legacy_gitignore"
    fi
}

TMP="$(mktemp -d -t sutando-sync-gi-untrack.XXXXXX)"

# Case A: TRACKED .gitignore → must be staged-deleted (git rm), not just gone.
mkdir -p "$TMP/tracked"
git -C "$TMP/tracked" init -q
printf '*\n!.gitignore\n!notes/\n' > "$TMP/tracked/.gitignore"
git -C "$TMP/tracked" add .gitignore
git -C "$TMP/tracked" -c user.email=t@t -c user.name=t commit -q -m base
_drop_legacy_gitignore "$TMP/tracked"
# Disk: gone.
[ -e "$TMP/tracked/.gitignore" ] && { echo "  FAIL: T5 — tracked .gitignore still on disk after drop"; fail=1; }
# Index: staged as a deletion (so a commit propagates the untrack to the vault).
if ! git -C "$TMP/tracked" diff --cached --name-status | grep -q '^D[[:space:]]\+.gitignore$'; then
    echo "  FAIL: T6 — tracked .gitignore was not staged as a deletion (a plain rm -f would leave it tracked in HEAD)"; fail=1
fi
# And it is no longer tracked.
git -C "$TMP/tracked" ls-files --error-unmatch .gitignore >/dev/null 2>&1 \
    && { echo "  FAIL: T7 — .gitignore still tracked after drop"; fail=1; }

# Case B: UNTRACKED .gitignore → plain rm -f, nothing staged.
mkdir -p "$TMP/untracked"
git -C "$TMP/untracked" init -q
printf '*\n!notes/\n' > "$TMP/untracked/.gitignore"   # present but never added
_drop_legacy_gitignore "$TMP/untracked"
[ -e "$TMP/untracked/.gitignore" ] && { echo "  FAIL: T8 — untracked .gitignore still on disk after drop"; fail=1; }
if [ -n "$(git -C "$TMP/untracked" diff --cached --name-only)" ]; then
    echo "  FAIL: T9 — untracked case staged something (should be a plain rm -f)"; fail=1
fi

rm -rf "$TMP"

# ── Report ──────────────────────────────────────────────────────────────────
if [ "$fail" = "0" ]; then
    echo "ALL TESTS PASS"
else
    echo "TESTS FAILED"
    exit 1
fi
