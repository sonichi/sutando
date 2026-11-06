#!/usr/bin/env bash
# sync-workspace.sh must honour vault settings kept in <workspace>/sutando.config.local.json,
# the copy that survives an engine tree replaced on app update.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_ROOT="$(mktemp -d -t sync-ws-config-layer.XXXXXX)"
trap 'rm -rf "$TEST_ROOT"' EXIT

pass=0
fail=0
check() {
    local description="$1"; shift
    if "$@" >/dev/null 2>&1; then echo "OK: $description"; pass=$((pass + 1))
    else echo "FAIL: $description"; fail=$((fail + 1)); fi
}
refute() {
    local description="$1"; shift
    if "$@" >/dev/null 2>&1; then echo "FAIL: $description"; fail=$((fail + 1))
    else echo "OK: $description"; pass=$((pass + 1)); fi
}

# The desktop shape: no repo-local config, `workspace` a symlink into a durable dir.
FIXTURE_REPO="$TEST_ROOT/engine"
FIXTURE_WS="$TEST_ROOT/durable-workspace"
FIXTURE_VAULT="$TEST_ROOT/vault.git"
mkdir -p "$FIXTURE_REPO/scripts" "$FIXTURE_REPO/src" "$FIXTURE_REPO/skills"
cp "$REPO/scripts/sync-workspace.sh" "$REPO/scripts/sutando-config.sh" \
   "$REPO/scripts/python-binary.sh" "$FIXTURE_REPO/scripts/"
cp "$REPO/src/sutando_config.py" "$FIXTURE_REPO/src/"
cp "$REPO/sutando.config.json" "$FIXTURE_REPO/sutando.config.json"
touch "$FIXTURE_REPO/CLAUDE.md"
git init -q "$FIXTURE_REPO"
git init -q --bare "$FIXTURE_VAULT"

mkdir -p "$FIXTURE_WS/notes/private" "$FIXTURE_WS/notes/public"
ln -s "$FIXTURE_WS" "$FIXTURE_REPO/workspace"
printf 'secret\n' > "$FIXTURE_WS/notes/private/diary.md"
printf 'shared\n' > "$FIXTURE_WS/notes/public/a.md"
cat > "$FIXTURE_WS/sutando.config.local.json" <<JSON
{"vault": {"remote_url": "$FIXTURE_VAULT", "sync": {"exclude_extra": ["notes/private/"]}}}
JSON

CFG="$FIXTURE_REPO/scripts/sutando-config.sh"
check "sutando-config.sh vault-url reads the workspace layer" \
    test "$(bash "$CFG" vault-url)" = "$FIXTURE_VAULT"
check "sutando-config.sh vault-sync-exclude appends the workspace exclude_extra" \
    sh -c "bash '$CFG' vault-sync-exclude | grep -qFx 'notes/private/'"

# No --vault-url: the URL has to come from the workspace layer.
out="$(env SUTANDO_HOST_OVERRIDE=layer-host SUTANDO_WS_ID_OVERRIDE=abc123 \
    SUTANDO_SYNC_LOCK_DIR="$TEST_ROOT/sync.lock" \
    GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.com \
    GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.com \
    bash "$FIXTURE_REPO/scripts/sync-workspace.sh" --force-gitignore --init 2>&1 || true)"

check "sync-workspace initialised against the workspace-configured vault" \
    test "$(git -C "$FIXTURE_WS" remote get-url origin 2>/dev/null)" = "$FIXTURE_VAULT"
refute "...without reporting a missing vault URL" \
    grep -qE "no vault URL configured|SUTANDO_VAULT not set" <<<"$out"
check "the composed rules carve out the workspace-configured path" \
    grep -qE '^notes/private/' "$FIXTURE_WS/.git/info/exclude"

git -C "$FIXTURE_WS" add -A >/dev/null 2>&1 || true
refute "git does NOT track notes/private/diary.md" \
    git -C "$FIXTURE_WS" ls-files --error-unmatch notes/private/diary.md
check "git DOES track an ordinary note (carrier intact)" \
    git -C "$FIXTURE_WS" ls-files --error-unmatch notes/public/a.md

echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
