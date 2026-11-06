#!/usr/bin/env bash
# Regression: `sutando-config.sh runtime` must not spawn git on a non-checkout,
# and must still populate identity on EVERY valid checkout shape.
#
# The descriptor's five git calls are polled by the desktop app
# (ag2space-cinny-desktop src-tauri/src/core_terminal.rs). On macOS
# /usr/bin/git is the Xcode-CLT stub: SPAWNING it raises the modal install
# dialog before it can fail, so the old "spawn and catch a non-zero exit"
# approach prompted on every poll of a packaged install (whose engine is an
# rsync copy with no .git).
#
# Two properties, and they pull in opposite directions — which is why both are
# pinned here:
#   1. no .git marker  -> ZERO git spawns (#2478)
#   2. a linked worktree, whose .git is a FILE not a directory, is still a real
#      checkout and must keep commit/branch/describe/tree_sha (#2478 review).
#      An isdir() gate satisfied (1) by breaking (2).
#
# Run: bash tests/sutando-config-runtime-git.test.sh
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"; git -C "$REPO" worktree prune 2>/dev/null' EXIT

pass=0; fail=0
ok() {
    if [ "$2" = "yes" ]; then echo "ok   $1"; pass=$((pass + 1))
    else echo "FAIL $1${3:+ — $3}"; fail=$((fail + 1)); fi
}

# A `git` shim that RECORDS execs. The bug is the spawn itself, not the exit
# code, so counting execs is the only instrument that can see it.
mkdir -p "$TMP/shim"
# shellcheck disable=SC2016  # the $ must reach the shim script literally
printf '#!/bin/sh\necho "git $*" >> "$GITLOG"\nexec /usr/bin/git "$@"\n' > "$TMP/shim/git"
chmod +x "$TMP/shim/git"
export GITLOG="$TMP/spawns.log"

code_field() {  # $1 = dir, $2 = key
    (cd "$1" && bash scripts/sutando-config.sh runtime 2>/dev/null) \
        | python3 -c "import json,sys; print(json.load(sys.stdin).get('code',{}).get('$2') or '')" 2>/dev/null
}

# --- 1. non-checkout: zero spawns -------------------------------------------
rsync -a --exclude '.git' --exclude 'node_modules' --exclude 'workspace' \
      "$REPO/" "$TMP/nogit/" 2>/dev/null
: > "$GITLOG"
(cd "$TMP/nogit" && PATH="$TMP/shim:$PATH" bash scripts/sutando-config.sh runtime >/dev/null 2>&1)
n=$(grep -c "^git " "$GITLOG" 2>/dev/null; true)
n=${n:-0}
if [ "$n" -eq 0 ]; then ok "no .git marker -> zero git spawns" yes
else ok "no .git marker -> zero git spawns" no "$n spawns"; fi

# ...and the descriptor is still valid JSON with an empty code block.
c=$(code_field "$TMP/nogit" commit)
if [ -z "$c" ]; then ok "no .git marker -> empty commit, still valid JSON" yes
else ok "no .git marker -> empty commit, still valid JSON" no "got '$c'"; fi

# --- 2. packaged engine reads its build manifest without spawning git --------
mkdir -p "$TMP/bundle/engine/sutando"
rsync -a --exclude '.git' --exclude 'node_modules' --exclude 'workspace' \
      "$REPO/" "$TMP/bundle/engine/sutando/" 2>/dev/null
cat > "$TMP/bundle/engine/ENGINE_MANIFEST.json" <<'JSON'
{"sha":"1234567890abcdef1234567890abcdef12345678","branch":"release/test","dirty":true,"built_at":"2026-08-24T00:00:00Z","post_build_tree_digest":"sha256:abcdef"}
JSON
: > "$GITLOG"
bundle_json="$(cd "$TMP/bundle/engine/sutando" && PATH="$TMP/shim:$PATH" bash scripts/sutando-config.sh runtime 2>/dev/null)"
n=$(grep -c "^git " "$GITLOG" 2>/dev/null; true); n=${n:-0}
if [ "$n" -eq 0 ]; then ok "packaged manifest -> zero git spawns" yes
else ok "packaged manifest -> zero git spawns" no "$n spawns"; fi
bundle_check="$(printf '%s' "$bundle_json" | python3 -c '
import json,sys
c=json.load(sys.stdin)["code"]
want={
 "commit":"1234567", "revision":"1234567890abcdef1234567890abcdef12345678",
 "branch":"release/test", "describe":"1234567", "dirty":True,
 "source":"engine-manifest", "built_at":"2026-08-24T00:00:00Z",
 "tree_digest":"sha256:abcdef", "tree_sha":None,
}
print("yes" if all(c.get(k)==v for k,v in want.items()) else json.dumps(c,sort_keys=True))
')"
if [ "$bundle_check" = "yes" ]; then ok "packaged manifest -> exact revision + provenance populated" yes
else ok "packaged manifest -> exact revision + provenance populated" no "$bundle_check"; fi

# --- 3. linked worktree (.git is a FILE) keeps identity ----------------------
if git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
    if git -C "$REPO" worktree add -q --detach "$TMP/wt" HEAD 2>/dev/null; then
        # Exercise the WORKING-TREE script, not whatever HEAD happens to carry —
        # otherwise the test passes/fails on the last commit rather than on the
        # change under review.
        cp "$REPO/scripts/sutando-config.sh" "$TMP/wt/scripts/sutando-config.sh"
        # sutando-config.sh sources this helper (#2599)
        cp "$REPO/scripts/python-binary.sh" "$TMP/wt/scripts/python-binary.sh"
        if [ -f "$TMP/wt/.git" ]; then ok "fixture: linked worktree .git is a FILE" yes
        else ok "fixture: linked worktree .git is a FILE" no "not a file"; fi
        c=$(code_field "$TMP/wt" commit)
        if [ -n "$c" ]; then ok "linked worktree -> commit populated" yes
        else ok "linked worktree -> commit populated" no "empty (isdir regression)"; fi
        t=$(code_field "$TMP/wt" tree_sha)
        if [ -n "$t" ]; then ok "linked worktree -> tree_sha populated" yes
        else ok "linked worktree -> tree_sha populated" no "empty"; fi
        git -C "$REPO" worktree remove --force "$TMP/wt" 2>/dev/null
    else
        echo "skip linked-worktree case (worktree add failed)"
    fi
else
    echo "skip linked-worktree case (not a git checkout)"
fi

# --- 4. ordinary checkout unaffected ----------------------------------------
if git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
    c=$(code_field "$REPO" commit)
    if [ -n "$c" ]; then ok "ordinary checkout -> commit populated" yes
    else ok "ordinary checkout -> commit populated" no "empty"; fi
    src=$(code_field "$REPO" source)
    if [ "$src" = "git" ]; then ok "ordinary checkout -> git remains authoritative" yes
    else ok "ordinary checkout -> git remains authoritative" no "source '$src'"; fi
fi

echo
echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ] || exit 1
