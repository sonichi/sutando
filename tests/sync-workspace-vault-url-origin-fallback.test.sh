#!/usr/bin/env bash
# The vault URL survives losing the per-clone config, and is never recovered
# from a remote that is not a vault this script has already pushed to.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
SYNC="$REPO/scripts/sync-workspace.sh"

fail=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s — %s\n' "$1" "${2:-}"; fail=1; }

export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-sync-ws-fallback-test}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-sync-ws-fallback-test@invalid}"
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"

TMP="$(mktemp -d -t sutando-vault-url-fallback.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

# This workspace's own identity. A remote is its vault only if it carries this.
WSID="ab12cd"

# A bare remote carrying exactly one branch. $1 = name, $2 = branch ref.
# A vault is identified by its `host/<host>/<wsId>` branches and nothing else.
mk_remote() {
    local bare="$TMP/$1.git" seed="$TMP/$1-seed"
    git init -q --bare "$bare"
    git init -q "$seed"
    : > "$seed/f"
    git -C "$seed" add f
    git -C "$seed" commit -qm seed
    git -C "$seed" push -q "$bare" "HEAD:refs/heads/$2"
    printf '%s' "$bare"
}

# One skeleton per case: a copy of the REAL script plus a stub config resolving
# that case's workspace. $1 = case dir, $2 = vault-url the stub reports.
mk_skel() {
    local skel="$TMP/$1" url="${2:-}"
    mkdir -p "$skel/scripts" "$skel/workspace/.sutando-vault"
    printf '%s\n' "$WSID" > "$skel/workspace/.sutando-vault/ws-id"
    cp "$SYNC" "$skel/scripts/sync-workspace.sh"
    cat > "$skel/scripts/sutando-config.sh" << STUB
#!/usr/bin/env bash
case "\${1:-}" in
    workspace) echo "\$(cd "\$(dirname "\$0")/.." && pwd)/workspace" ;;
    vault-url) printf '%s' '$url' ;;
    *) : ;;
esac
STUB
    chmod +x "$skel/scripts/sutando-config.sh"
    printf '%s' "$skel"
}

# Run a subcommand against a skeleton with a clean env; echo stderr+stdout.
run_sync() {
    local skel="$1"; shift
    env -u SUTANDO_MEMORY_REPO \
        SUTANDO_REPO_DIR="$skel" \
        SUTANDO_SYNC_LOCK_DIR="$skel/lock.d" \
        SYNC_WORKSPACE_LOG="$skel/sync.log" \
        bash "$skel/scripts/sync-workspace.sh" "$@" 2>&1
}

echo "vault URL recovery from the workspace repo's own origin:"

# The vault carries our wsId under a DIFFERENT host segment than the one this
# test runs on, so a pass here means the id matched, not the hostname.
VAULT_REMOTE="$(mk_remote vault "host/testhost/$WSID")"
PLAIN_REMOTE="$(mk_remote plain 'main')"
FOREIGN_REMOTE="$(mk_remote foreign 'host/someone-else/99ffee')"

# Must cross the slashes AND reject another workspace's branch; the last clause
# is the anti-vacuity anchor — FOREIGN does satisfy the broad `host/*` pattern.
if [ -n "$(git ls-remote --heads "$VAULT_REMOTE" "host/*/$WSID")" ] \
   && [ -z "$(git ls-remote --heads "$PLAIN_REMOTE" "host/*/$WSID")" ] \
   && [ -z "$(git ls-remote --heads "$FOREIGN_REMOTE" "host/*/$WSID")" ] \
   && [ -n "$(git ls-remote --heads "$FOREIGN_REMOTE" 'host/*')" ]; then
    ok "the host/*/<wsId> probe accepts only this workspace's own vault"
else
    bad "the host/*/<wsId> probe accepts only this workspace's own vault" \
        "vault=$(git ls-remote --heads "$VAULT_REMOTE" "host/*/$WSID") foreign-scoped=$(git ls-remote --heads "$FOREIGN_REMOTE" "host/*/$WSID") foreign-broad=$(git ls-remote --heads "$FOREIGN_REMOTE" 'host/*')"
fi

# --- 1: no config URL, workspace is a git repo whose origin is a vault --------
skel="$(mk_skel with-origin)"
git -C "$skel/workspace" init -q
git -C "$skel/workspace" remote add origin "$VAULT_REMOTE"
out="$(run_sync "$skel" --status)"
if echo "$out" | grep VAULT_URL | grep -qF "$VAULT_REMOTE"; then
    ok "a vault origin is adopted as the vault URL"
else
    bad "a vault origin is adopted as the vault URL" "status said: $(echo "$out" | grep VAULT_URL)"
fi
if echo "$out" | grep -q "recovered it from the workspace repo's own origin"; then
    ok "the recovery is announced, not silent"
else
    bad "the recovery is announced, not silent" "no notice in: $out"
fi

# --- 2: the same case on the DEFAULT (cron) path — no longer a silent skip ----
out="$(run_sync "$skel")"
if echo "$out" | grep -q 'cross-machine sync disabled'; then
    bad "default path no longer reports sync disabled" "got: $out"
else
    ok "default path no longer reports sync disabled"
fi

# --- 3: nothing configured and no origin -> the clean skip is preserved -------
skel="$(mk_skel no-origin)"
out="$(run_sync "$skel")"
rc_out="$(env -u SUTANDO_MEMORY_REPO SUTANDO_REPO_DIR="$skel" \
    SUTANDO_SYNC_LOCK_DIR="$skel/lock.d" SYNC_WORKSPACE_LOG="$skel/sync.log" \
    bash "$skel/scripts/sync-workspace.sh" >/dev/null 2>&1; echo $?)"
if echo "$out" | grep -q 'cross-machine sync disabled' && [ "$rc_out" = "0" ]; then
    ok "no URL and no origin still skips cleanly (rc 0)"
else
    bad "no URL and no origin still skips cleanly (rc 0)" "rc=$rc_out out: $out"
fi

# --- 4: a configured URL still wins over the workspace's origin ---------------
skel="$(mk_skel configured-wins https://example.invalid/configured.git)"
git -C "$skel/workspace" init -q
git -C "$skel/workspace" remote add origin "$VAULT_REMOTE"
out="$(run_sync "$skel" --status)"
if echo "$out" | grep -q 'VAULT_URL:.*https://example.invalid/configured.git'; then
    ok "configured vault.remote_url outranks the origin fallback"
else
    bad "configured vault.remote_url outranks the origin fallback" \
        "status said: $(echo "$out" | grep VAULT_URL)"
fi

# --- 5: workspace NESTED in a git repo must not adopt the PARENT's origin -----
# This is the default layout, so getting it wrong publishes private state.
skel="$(mk_skel nested)"
git -C "$skel" init -q
git -C "$skel" remote add origin "https://example.invalid/CODE-REPO.git"
out="$(run_sync "$skel" --status)"
if echo "$out" | grep -q 'CODE-REPO'; then
    bad "a workspace inside another repo does not adopt that repo's origin" \
        "adopted the enclosing repo's remote: $(echo "$out" | grep VAULT_URL)"
else
    ok "a workspace inside another repo does not adopt that repo's origin"
fi
out="$(run_sync "$skel")"
if echo "$out" | grep -q 'cross-machine sync disabled'; then
    ok "the nested case still reports sync disabled"
else
    bad "the nested case still reports sync disabled" "got: $out"
fi

# --- 6: own git root, but the origin is NOT a vault ---------------------------
# A restored backup or mis-aimed clone reaches here with a public remote.
skel="$(mk_skel wrong-origin)"
git -C "$skel/workspace" init -q
git -C "$skel/workspace" remote add origin "$PLAIN_REMOTE"
out="$(run_sync "$skel" --status)"
if echo "$out" | grep VAULT_URL | grep -qF "$PLAIN_REMOTE"; then
    bad "a non-vault origin at the workspace's own root is refused" \
        "adopted it anyway: $(echo "$out" | grep VAULT_URL)"
else
    ok "a non-vault origin at the workspace's own root is refused"
fi
if echo "$out" | grep -q "carries no host/\*/$WSID branch"; then
    ok "the refusal names why, so the operator can restore the real URL"
else
    bad "the refusal names why, so the operator can restore the real URL" "got: $out"
fi
out="$(run_sync "$skel")"
if echo "$out" | grep -q 'cross-machine sync disabled'; then
    ok "the wrong-origin case still reports sync disabled"
else
    bad "the wrong-origin case still reports sync disabled" "got: $out"
fi

# --- 6b: the origin IS a vault, but somebody else's --------------------------
# A `host/*` naming convention is not identity: any remote can advertise one.
skel="$(mk_skel foreign-origin)"
git -C "$skel/workspace" init -q
git -C "$skel/workspace" remote add origin "$FOREIGN_REMOTE"
out="$(run_sync "$skel" --status)"
if echo "$out" | grep VAULT_URL | grep -qF "$FOREIGN_REMOTE"; then
    bad "an origin carrying only ANOTHER workspace's host/* branch is refused" \
        "adopted it anyway: $(echo "$out" | grep VAULT_URL)"
else
    ok "an origin carrying only ANOTHER workspace's host/* branch is refused"
fi
out="$(run_sync "$skel")"
if echo "$out" | grep -q 'cross-machine sync disabled'; then
    ok "the foreign-vault case still reports sync disabled"
else
    bad "the foreign-vault case still reports sync disabled" "got: $out"
fi

# --- 6c: no ws-id -> no identity to confirm, so nothing is adopted -----------
skel="$(mk_skel no-wsid)"
rm -f "$skel/workspace/.sutando-vault/ws-id"
git -C "$skel/workspace" init -q
git -C "$skel/workspace" remote add origin "$VAULT_REMOTE"
out="$(run_sync "$skel" --status)"
if echo "$out" | grep VAULT_URL | grep -qF "$VAULT_REMOTE"; then
    bad "a workspace with no ws-id adopts nothing, even from a real vault" \
        "adopted: $(echo "$out" | grep VAULT_URL)"
else
    ok "a workspace with no ws-id adopts nothing, even from a real vault"
fi
if echo "$out" | grep -q 'no .sutando-vault/ws-id'; then
    ok "the no-ws-id refusal names the missing identity file"
else
    bad "the no-ws-id refusal names the missing identity file" "got: $out"
fi

# --- 6d: a wildcard-capable ws-id must not widen the identity probe ----------
# The id is interpolated into `host/*/<wsId>`, so a persisted `*` asks for
# host/*/* — which every remote carrying any host branch satisfies.
skel="$(mk_skel wildcard-wsid)"
printf '%s\n' '*' > "$skel/workspace/.sutando-vault/ws-id"
git -C "$skel/workspace" init -q
git -C "$skel/workspace" remote add origin "$FOREIGN_REMOTE"
out="$(run_sync "$skel" --status)"
if echo "$out" | grep VAULT_URL | grep -qF "$FOREIGN_REMOTE"; then
    bad "a wildcard ws-id does not match a foreign remote's host branch" \
        "adopted: $(echo "$out" | grep VAULT_URL)"
else
    ok "a wildcard ws-id does not match a foreign remote's host branch"
fi
if echo "$out" | grep -q 'ws-id is not a valid workspace id'; then
    ok "the refusal names a malformed id, not a missing one"
else
    bad "the refusal names a malformed id, not a missing one" "got: $out"
fi

# --- 7: an unreachable origin is reported as unreachable, not as not-a-vault --
skel="$(mk_skel unreachable-origin)"
git -C "$skel/workspace" init -q
git -C "$skel/workspace" remote add origin "$TMP/does-not-exist.git"
out="$(run_sync "$skel" --status)"
if echo "$out" | grep -q 'could not reach the workspace repo'; then
    ok "an unreachable origin is reported as unreachable"
else
    bad "an unreachable origin is reported as unreachable" "got: $out"
fi
if echo "$out" | grep VAULT_URL | grep -q 'does-not-exist'; then
    bad "an unreachable origin is not adopted" "adopted: $(echo "$out" | grep VAULT_URL)"
else
    ok "an unreachable origin is not adopted"
fi

# --- 8: a git WORKTREE layout (.git is a FILE) is not, by itself, a refusal ---
# The guard is about the remote's identity, not the workspace's on-disk layout.
skel="$(mk_skel worktree-layout)"
git init -q "$TMP/wt-main"
: > "$TMP/wt-main/f"
git -C "$TMP/wt-main" add f
git -C "$TMP/wt-main" commit -qm seed
rm -rf "$skel/workspace"
git -C "$TMP/wt-main" worktree add -q --detach "$skel/workspace"
mkdir -p "$skel/workspace/.sutando-vault"
printf '%s\n' "$WSID" > "$skel/workspace/.sutando-vault/ws-id"
git -C "$skel/workspace" remote add origin "$VAULT_REMOTE"
if [ -f "$skel/workspace/.git" ]; then
    ok "the worktree fixture really has a .git FILE, not a directory"

    out="$(run_sync "$skel" --status)"
    if echo "$out" | grep VAULT_URL | grep -qF "$VAULT_REMOTE"; then
        ok "a worktree workspace still adopts its own vault origin"
    else
        bad "a worktree workspace still adopts its own vault origin" \
            "status said: $(echo "$out" | grep VAULT_URL)"
    fi

    # Status and push disagree here by design; asserting only status would leave
    # that unpinned, and a later change could make push accept a worktree.
    if echo "$out" | grep -q 'linked git WORKTREE'; then
        ok "status names the worktree layout instead of denying the repo"
    else
        bad "status names the worktree layout instead of denying the repo" \
            "status said: $(echo "$out" | grep 'git status:')"
    fi
    push_out="$(run_sync "$skel" --push-only)"; push_rc=$?
    if [ "$push_rc" != "0" ]; then
        ok "push still refuses the worktree layout the status line names"
    else
        bad "push still refuses the worktree layout the status line names" \
            "push exited 0: $push_out"
    fi
else
    bad "the worktree fixture really has a .git FILE, not a directory" \
        "fixture did not reproduce the layout under test — dependent assertions skipped"
fi

# --- 8b: status carries the URL's provenance, not just its value -------------
# A recovered URL and a configured one printed identically is what made the
# earlier report unreadable: the operator could not tell which fact they held.
skel="$(mk_skel provenance-recovered)"
git -C "$skel/workspace" init -q
git -C "$skel/workspace" remote add origin "$VAULT_REMOTE"
out="$(run_sync "$skel" --status)"
if echo "$out" | grep VAULT_URL | grep -q 'source: workspace repo origin, identity-verified'; then
    ok "a recovered URL is labelled as recovered and identity-verified"
else
    bad "a recovered URL is labelled as recovered and identity-verified" \
        "status said: $(echo "$out" | grep VAULT_URL)"
fi

skel="$(mk_skel provenance-configured "$PLAIN_REMOTE")"
out="$(run_sync "$skel" --status)"
if echo "$out" | grep VAULT_URL | grep -q 'source: sutando.config'; then
    ok "a configured URL is labelled as configured"
else
    bad "a configured URL is labelled as configured" \
        "status said: $(echo "$out" | grep VAULT_URL)"
fi

# --- 8c: a declined candidate is named in status, with the reason ------------
# The refusal is a stderr line during resolution; --status is read long after.
# Printing a bare <unset> there hides the one fact that explains the state.
skel="$(mk_skel provenance-declined)"
git -C "$skel/workspace" init -q
git -C "$skel/workspace" remote add origin "$PLAIN_REMOTE"
out="$(run_sync "$skel" --status)"
if echo "$out" | grep -qF "candidate NOT adopted: $PLAIN_REMOTE"; then
    ok "status names the origin it declined to adopt"
else
    bad "status names the origin it declined to adopt" \
        "status said: $(echo "$out" | grep -A2 VAULT_URL)"
fi
if echo "$out" | grep -q "reason: carries no host/\*/$WSID branch"; then
    ok "status gives the reason the candidate was declined"
else
    bad "status gives the reason the candidate was declined" \
        "status said: $(echo "$out" | grep -A2 VAULT_URL)"
fi

# --- 9: end-to-end — a push-eligible workspace pointed at a public remote -----
# The refusal above is a message; this asserts the consequence. Hand-built, not
# --init'd, so nothing but the vault check stands between this and a push.
skel="$(mk_skel public-origin-no-leak)"
git init -q --bare "$TMP/public-code.git"
git -C "$TMP/seed-public" init -q 2>/dev/null || git init -q "$TMP/seed-public"
: > "$TMP/seed-public/README.md"
git -C "$TMP/seed-public" add -A
git -C "$TMP/seed-public" commit -qm "public code"
git -C "$TMP/seed-public" push -q "$TMP/public-code.git" HEAD:refs/heads/main
printf 'SECRET-CREDENTIAL-DO-NOT-PUBLISH\n' > "$skel/workspace/private-state.txt"
git -C "$skel/workspace" init -q
git -C "$skel/workspace" add -A
git -C "$skel/workspace" commit -qm "private state"
git -C "$skel/workspace" remote add origin "$TMP/public-code.git"
run_sync "$skel" > /dev/null 2>&1
if [ -z "$(git ls-remote --heads "$TMP/public-code.git" "host/*")" ]; then
    ok "no workspace branch is pushed to a non-vault origin"
else
    bad "no workspace branch is pushed to a non-vault origin" \
        "pushed: $(git ls-remote --heads "$TMP/public-code.git" 'host/*')"
fi
# Scoped deliberately: `grep --all` searches only ref-reachable content, so this
# proves nothing checkoutable carries the sentinel, not that no object arrived.
if git --git-dir="$TMP/public-code.git" grep -q 'SECRET-CREDENTIAL' --all 2>/dev/null; then
    bad "no workspace content is reachable from any ref on a non-vault origin" \
        "the secret is present in an object on the public remote"
else
    ok "no workspace content is reachable from any ref on a non-vault origin"
fi

if [ "$fail" = "0" ]; then
    echo "ALL TESTS PASS"
    exit 0
fi
echo "TESTS FAILED"
exit 1
