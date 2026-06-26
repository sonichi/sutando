#!/usr/bin/env bash
# Test for the --init isolation fix: `_init_impl` must not let `git init` (or a
# skipped init) operate on a PARENT repo's worktree when the workspace is nested
# inside another git repo (e.g. a git submodule).
#
# Root cause (two compounding bugs):
#   1. generate_exclude ran `mkdir -p .git/info` even under --dry-run, leaving a
#      STUB `.git/` (lone `info/`, no HEAD). A later --init then saw `.git`
#      present, skipped `git init`, and git walked UP to the parent worktree.
#   2. _init_impl decided "already a repo" from a bare `-d .git` check, which a
#      stub passes — so it skipped init and every remote/commit/push hijacked
#      the parent (rewrote its origin, committed its whole tree, pushed it).
#
# Fix: dry-run never mkdirs; _init_impl decides by the resolved toplevel
# (`git rev-parse --show-toplevel` -ef "$WORKSPACE_DIR"), git-inits when not
# isolated, and dies (fail-safe) if the fresh repo still resolves to a parent.

set -u
# NOTE: deliberately not `set -e` — accumulate failures via `fail=1`.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
SYNC="$REPO/scripts/sync-workspace.sh"

fail=0

# ── Structural tests ────────────────────────────────────────────────────────

# Test 1: Fix A — the generate_exclude mkdir is guarded against --dry-run.
EXCL_BLOCK="$(awk '/^generate_exclude\(\) \{/,/^}$/' "$SYNC")"
echo "$EXCL_BLOCK" \
    | grep -Eq 'DRY_RUN" != "1" \] && \[ ! -d "\$WORKSPACE_DIR/\.git/info"' \
    || { echo "  FAIL: T1 — generate_exclude mkdir not guarded by DRY_RUN (dry-run would leave a .git stub)"; fail=1; }

# Test 2: Fix B — _init_impl decides isolation via --show-toplevel, not bare -d.
INIT_BLOCK="$(awk '/^_init_impl\(\) \{/,/^}$/' "$SYNC")"
echo "$INIT_BLOCK" | grep -q 'rev-parse --show-toplevel' \
    || { echo "  FAIL: T2 — _init_impl does not use 'rev-parse --show-toplevel' isolation check"; fail=1; }

# Test 3: Fix B — _init_impl compares the toplevel to WORKSPACE_DIR (-ef).
echo "$INIT_BLOCK" | grep -q -- '-ef "\$WORKSPACE_DIR"' \
    || { echo "  FAIL: T3 — _init_impl does not compare toplevel -ef WORKSPACE_DIR"; fail=1; }

# Test 4: Fix B — there is a fail-safe `die` if isolation could not be achieved.
echo "$INIT_BLOCK" | grep -q 'did not isolate as its own git repo' \
    || { echo "  FAIL: T4 — _init_impl missing the post-init isolation fail-safe (die)"; fail=1; }

# ── E2E: reproduce the real git climbing behavior the fix turns on ───────────
# We model the decision predicate exactly as _init_impl does and exercise it
# against the four states that matter, in a real parent-repo + nested-dir
# setup. (Running the full script needs the global lock + a workspace-path
# override the script doesn't expose, so — like the Bug #4 test — we mirror the
# predicate and prove the LOGIC; structural tests above prove the wiring.)

# Mirror of _init_impl's "already isolated?" decision.
_is_own_repo() {  # arg: dir
    local _d="$1" _top
    _top="$(git -C "$_d" rev-parse --show-toplevel 2>/dev/null || true)"
    [ -n "$_top" ] && [ "$_top" -ef "$_d" ]
}

TMP="$(mktemp -d -t sutando-sync-init-iso.XXXXXX)"
git -C "$TMP" init -q                     # the "parent" repo (stands in for a submodule)
git -C "$TMP" remote add origin https://example.invalid/parent.git
git -C "$TMP" -c user.email=t@t -c user.name=t commit -q --allow-empty -m base
PARENT_ORIGIN_BEFORE="$(git -C "$TMP" remote get-url origin)"
PARENT_HEAD_BEFORE="$(git -C "$TMP" rev-parse HEAD)"

# State A: fresh nested dir, no .git → must be treated as "not a repo" (init).
mkdir -p "$TMP/wsA"
_is_own_repo "$TMP/wsA" && { echo "  FAIL: T5 — fresh nested dir wrongly judged 'already a repo'"; fail=1; }

# State B: the STUB from the old dry-run bug (lone .git/info) → must NOT be
# judged a repo (the bare -d .git check did; the fix must not).
mkdir -p "$TMP/wsB/.git/info"
_is_own_repo "$TMP/wsB" && { echo "  FAIL: T6 — stub .git/info wrongly judged 'already a repo' (the original hijack)"; fail=1; }

# State C: after a real `git init`, the nested dir IS its own isolated repo.
git -C "$TMP/wsB" init -q
if ! _is_own_repo "$TMP/wsB"; then
    echo "  FAIL: T7 — real 'git init' in a nested dir did not isolate (predicate still climbs to parent)"; fail=1
fi
# ...and the resolved git-dir is the nested one, not the parent's. Compare with
# -ef (same inode) — macOS mktemp yields /var/... while git resolves the
# /private/var/... realpath; a string compare would spuriously differ.
gd="$(git -C "$TMP/wsB" rev-parse --absolute-git-dir 2>/dev/null)"
if ! { [ -n "$gd" ] && [ "$gd" -ef "$TMP/wsB/.git" ]; }; then
    echo "  FAIL: T8 — nested repo git-dir resolved to '$gd', expected '$TMP/wsB/.git'"; fail=1
fi

# State D: the parent repo was never touched by any of the above.
[ "$(git -C "$TMP" remote get-url origin)" = "$PARENT_ORIGIN_BEFORE" ] \
    || { echo "  FAIL: T9 — parent repo origin was modified (hijack not prevented)"; fail=1; }
[ "$(git -C "$TMP" rev-parse HEAD)" = "$PARENT_HEAD_BEFORE" ] \
    || { echo "  FAIL: T10 — parent repo HEAD changed (hijack not prevented)"; fail=1; }

rm -rf "$TMP"

# ── Report ──────────────────────────────────────────────────────────────────
if [ "$fail" = "0" ]; then
    echo "ALL TESTS PASS"
else
    echo "TESTS FAILED"
    exit 1
fi
