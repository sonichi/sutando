#!/usr/bin/env bash
# Regression for #2391: a legacy host-scoped carrier include must not untrack
# and propagate deletion of a peer's hosts/<label>/ subtree.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_ROOT="$(mktemp -d -t sync-foreign-host-test.XXXXXX)"
trap 'rm -rf "$TEST_ROOT"' EXIT

pass=0
fail=0

check() {
    local description="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        echo "OK: $description"
        pass=$((pass + 1))
    else
        echo "FAIL: $description"
        fail=$((fail + 1))
    fi
}

setup_fixture() {
    local name="$1"
    FIXTURE_ROOT="$TEST_ROOT/$name"
    FIXTURE_REPO="$FIXTURE_ROOT/repo"
    FIXTURE_WS="$FIXTURE_ROOT/workspace"
    FIXTURE_VAULT="$FIXTURE_ROOT/vault.git"
    mkdir -p "$FIXTURE_REPO/scripts" "$FIXTURE_REPO/src"
    cp "$REPO/scripts/sync-workspace.sh" "$FIXTURE_REPO/scripts/"
    cp "$REPO/scripts/sutando-config.sh" "$FIXTURE_REPO/scripts/"
    # sutando-config.sh sources this helper (#2599)
    cp "$REPO/scripts/python-binary.sh" "$FIXTURE_REPO/scripts/"
    cp "$REPO/src/sutando_config.py" "$FIXTURE_REPO/src/"
    touch "$FIXTURE_REPO/CLAUDE.md"
    mkdir -p "$FIXTURE_REPO/skills"
    git init -q "$FIXTURE_REPO"
    git init -q --bare "$FIXTURE_VAULT"

    cat > "$FIXTURE_REPO/sutando.config.json" <<'JSON'
{
  "workspace": {"path": "${REPO_DIR}/workspace"},
  "vault": {
    "enabled": false,
    "sync": {
      "include": ["notes/", "hosts/*/"],
      "exclude": []
    }
  }
}
JSON
    cat > "$FIXTURE_REPO/sutando.config.local.json" <<'JSON'
{
  "vault": {
    "sync": {
      "include": ["notes/", "hosts/local-host/"],
      "exclude": []
    }
  }
}
JSON

    mkdir -p \
        "$FIXTURE_WS/notes" \
        "$FIXTURE_WS/hosts/local-host" \
        "$FIXTURE_WS/hosts/peer-host"
    echo "local" > "$FIXTURE_WS/hosts/local-host/state.json"
    echo "peer" > "$FIXTURE_WS/hosts/peer-host/state.json"
    echo "note" > "$FIXTURE_WS/notes/n.md"

    SYNC="$FIXTURE_REPO/scripts/sync-workspace.sh"
    SYNC_ENV=(
        SUTANDO_REPO_DIR="$FIXTURE_REPO"
        SUTANDO_WORKSPACE="$FIXTURE_WS"
        SUTANDO_TEST_MODE=1
        SUTANDO_HOST_OVERRIDE=local-host
        SUTANDO_WS_ID_OVERRIDE=guard1
        SUTANDO_SYNC_LOCK_DIR="$FIXTURE_ROOT/sync.lock"
        GIT_AUTHOR_NAME="Sync Guard Test"
        GIT_AUTHOR_EMAIL="sync-guard@example.com"
        GIT_COMMITTER_NAME="Sync Guard Test"
        GIT_COMMITTER_EMAIL="sync-guard@example.com"
    )
    env "${SYNC_ENV[@]}" bash "$SYNC" \
        --vault-url "$FIXTURE_VAULT" --force-gitignore --init \
        >/dev/null 2>&1

    # Materialize the exact generated pre-multi-host rule shape. With the
    # patched script this simulates an existing stale checkout; on the
    # unpatched script it is already the generated shape and is a no-op.
    sed \
        -e 's|^!hosts/\*/$|!hosts/local-host/|' \
        -e 's|^!hosts/\*/\*\*$|!hosts/local-host/**|' \
        "$FIXTURE_WS/.git/info/exclude" \
        > "$FIXTURE_WS/.git/info/exclude.legacy"
    mv "$FIXTURE_WS/.git/info/exclude.legacy" \
        "$FIXTURE_WS/.git/info/exclude"

    if ! git -C "$FIXTURE_WS" ls-files --error-unmatch \
        hosts/peer-host/state.json >/dev/null 2>&1; then
        (
            cd "$FIXTURE_WS"
            git add -f hosts/peer-host/state.json
            git -c user.name="Sync Guard Test" \
                -c user.email="sync-guard@example.com" \
                commit -q -m "seed peer host state"
            git push -q origin HEAD:refs/heads/host/local-host/guard1
        )
    fi
}

echo "Test 1: generated legacy host scope widens without deleting peer state"
setup_fixture "auto-migrate"
set +e
out="$(
    env "${SYNC_ENV[@]}" bash "$SYNC" \
        --vault-url "$FIXTURE_VAULT" --push-only 2>&1
)"
rc=$?
set -e
check "push-only succeeds after safe carrier migration" test "$rc" -eq 0
check "generated rules widen hosts/local-host/ to hosts/*/" \
    grep -qF '!hosts/*/' "$FIXTURE_WS/.git/info/exclude"
check "peer file remains tracked locally" \
    git -C "$FIXTURE_WS" ls-files --error-unmatch \
        hosts/peer-host/state.json
check "peer file remains in the remote host branch" \
    git --git-dir="$FIXTURE_VAULT" cat-file -e \
        refs/heads/host/local-host/guard1:hosts/peer-host/state.json

echo
echo "Test 2: operator host patterns and dot segments are never auto-widened"
for custom_scope in 'hosts/team?/' 'hosts/[ab]/' 'hosts/../'; do
    fixture_name="$(
        printf '%s' "$custom_scope" | tr -c '[:alnum:]' '-'
    )"
    setup_fixture "custom-$fixture_name"
    sed "s|hosts/local-host/|$custom_scope|" \
        "$FIXTURE_REPO/sutando.config.local.json" \
        > "$FIXTURE_REPO/sutando.config.local.json.custom"
    mv "$FIXTURE_REPO/sutando.config.local.json.custom" \
        "$FIXTURE_REPO/sutando.config.local.json"
    sed \
        -e "s|^!hosts/local-host/$|!$custom_scope|" \
        -e "s|^!hosts/local-host/\\*\\*$|!${custom_scope}**|" \
        "$FIXTURE_WS/.git/info/exclude" \
        > "$FIXTURE_WS/.git/info/exclude.custom"
    mv "$FIXTURE_WS/.git/info/exclude.custom" \
        "$FIXTURE_WS/.git/info/exclude"

    env "${SYNC_ENV[@]}" bash "$SYNC" \
        --vault-url "$FIXTURE_VAULT" --push-only \
        >/dev/null 2>&1 || true

    check "$custom_scope remains operator-authored" \
        grep -qFx "!$custom_scope" "$FIXTURE_WS/.git/info/exclude"
    if grep -qFx '!hosts/*/' "$FIXTURE_WS/.git/info/exclude"; then
        echo "FAIL: $custom_scope was silently widened"
        fail=$((fail + 1))
    else
        echo "OK: $custom_scope was not widened"
        pass=$((pass + 1))
    fi
done

echo
echo "Test 3: foreign-host deletion guard survives customized exclude rules"
setup_fixture "push-guard"
printf '%s\n' '# operator customization' \
    >> "$FIXTURE_WS/.git/info/exclude"
set +e
out="$(
    env "${SYNC_ENV[@]}" bash "$SYNC" \
        --vault-url "$FIXTURE_VAULT" --push-only 2>&1
)"
rc=$?
set -e
check "push is refused when custom rules stage a foreign-host deletion" \
    test "$rc" -ne 0
check "refusal names the foreign-host deletion guard" \
    grep -qF 'foreign host' <<<"$out"
check "guard restores peer file to the local index" \
    git -C "$FIXTURE_WS" ls-files --error-unmatch \
        hosts/peer-host/state.json
check "refused push leaves peer file in the remote host branch" \
    git --git-dir="$FIXTURE_VAULT" cat-file -e \
        refs/heads/host/local-host/guard1:hosts/peer-host/state.json

echo
echo "Test 4: foreign-host deletion guard catches a peer file move"
setup_fixture "move-guard"
git -C "$FIXTURE_WS" mv \
    hosts/peer-host/state.json hosts/local-host/moved-peer-state.json
set +e
out="$(
    env "${SYNC_ENV[@]}" bash "$SYNC" \
        --vault-url "$FIXTURE_VAULT" --push-only 2>&1
)"
rc=$?
set -e
check "push is refused when a rename removes foreign-host state" \
    test "$rc" -ne 0
check "rename refusal names the foreign-host deletion guard" \
    grep -qF 'foreign host' <<<"$out"
check "rename refusal restores the peer source path to the index" \
    git -C "$FIXTURE_WS" ls-files --error-unmatch \
        hosts/peer-host/state.json
check "refused rename leaves the peer file in the remote host branch" \
    git --git-dir="$FIXTURE_VAULT" cat-file -e \
        refs/heads/host/local-host/guard1:hosts/peer-host/state.json

echo
echo "Test 5: guard permits the owning host to delete its own state"
setup_fixture "own-host-delete"
rm "$FIXTURE_WS/hosts/local-host/state.json"
set +e
out="$(
    env "${SYNC_ENV[@]}" bash "$SYNC" \
        --vault-url "$FIXTURE_VAULT" --push-only 2>&1
)"
rc=$?
set -e
check "own-host deletion push succeeds" test "$rc" -eq 0
if git --git-dir="$FIXTURE_VAULT" cat-file -e \
    refs/heads/host/local-host/guard1:hosts/local-host/state.json \
    2>/dev/null; then
    echo "FAIL: own-host file remains in the remote"
    fail=$((fail + 1))
else
    echo "OK: own-host deletion reached the remote"
    pass=$((pass + 1))
fi
check "own-host deletion preserves peer state" \
    git --git-dir="$FIXTURE_VAULT" cat-file -e \
        refs/heads/host/local-host/guard1:hosts/peer-host/state.json

echo
echo "Total: $((pass + fail)) — pass: $pass, fail: $fail"
exit "$fail"
