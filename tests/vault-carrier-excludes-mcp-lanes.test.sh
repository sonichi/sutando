#!/usr/bin/env bash
# The MCP lane env files hold a bearer: the shipped carrier must never sync
# `state/ag2-mcp.d/`, even for an owner whose include list carries `state/`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_ROOT="$(mktemp -d -t vault-mcp-lanes-test.XXXXXX)"
trap 'rm -rf "$TEST_ROOT"' EXIT

pass=0
fail=0
check() {
    local description="$1"
    shift
    if "$@" >/dev/null 2>&1; then echo "OK: $description"; pass=$((pass + 1))
    else echo "FAIL: $description"; fail=$((fail + 1)); fi
}
refute() {
    local description="$1"
    shift
    if "$@" >/dev/null 2>&1; then echo "FAIL: $description"; fail=$((fail + 1))
    else echo "OK: $description"; pass=$((pass + 1)); fi
}

check "shipped sutando.config.json carves out state/ag2-mcp.d/" \
    python3 -c "
import json, pathlib, sys
exc = json.loads(pathlib.Path('$REPO/sutando.config.json').read_text())['vault']['sync']['exclude']
sys.exit(0 if 'state/ag2-mcp.d/' in exc else 1)
"

FIXTURE_REPO="$TEST_ROOT/repo"
FIXTURE_WS="$TEST_ROOT/workspace"
FIXTURE_VAULT="$TEST_ROOT/vault.git"
mkdir -p "$FIXTURE_REPO/scripts" "$FIXTURE_REPO/src" "$FIXTURE_REPO/skills"
cp "$REPO/scripts/sync-workspace.sh" "$REPO/scripts/sutando-config.sh" "$REPO/scripts/python-binary.sh" \
   "$FIXTURE_REPO/scripts/"
cp "$REPO/src/sutando_config.py" "$FIXTURE_REPO/src/"
cp "$REPO/sutando.config.json" "$FIXTURE_REPO/sutando.config.json"
touch "$FIXTURE_REPO/CLAUDE.md"
git init -q "$FIXTURE_REPO"
git init -q --bare "$FIXTURE_VAULT"

mkdir -p "$FIXTURE_WS/state/ag2-mcp.d" "$FIXTURE_WS/notes" "$FIXTURE_WS/hosts/lanes-host"
printf "K='https://relay.example/relay|s3cret'\n" > "$FIXTURE_WS/state/ag2-mcp.d/conn-x.env"
printf '{"version": 1}\n' > "$FIXTURE_WS/state/ag2-mcp.d/conn-x.json"
printf 'x\n' > "$FIXTURE_WS/state/ag2-mcp.d/.conn-x.env.ab12.tmp"
printf 'track\n' > "$FIXTURE_WS/state/current-track.md"
printf 'a note\n' > "$FIXTURE_WS/notes/a.md"

# An owner include that carries all of state/ (an include override replaces the list).
cat > "$FIXTURE_REPO/sutando.config.local.json" <<JSON
{"workspace": {"path": "$FIXTURE_WS"}, "vault": {"sync": {"include": ["notes/", "hosts/*/", "state/"]}}}
JSON

env SUTANDO_HOST_OVERRIDE=lanes-host SUTANDO_WS_ID_OVERRIDE=lanes1 \
    SUTANDO_SYNC_LOCK_DIR="$TEST_ROOT/sync.lock" \
    GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.com GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.com \
    bash "$FIXTURE_REPO/scripts/sync-workspace.sh" --vault-url "$FIXTURE_VAULT" --force-gitignore --init \
    >/dev/null 2>&1 || true

check "generated rules carve out state/ag2-mcp.d/" grep -qE '^state/ag2-mcp.d/' "$FIXTURE_WS/.git/info/exclude"

git -C "$FIXTURE_WS" add -A >/dev/null 2>&1 || true
refute "git does NOT track a lane env file" git -C "$FIXTURE_WS" ls-files --error-unmatch state/ag2-mcp.d/conn-x.env
refute "git does NOT track a lane descriptor" git -C "$FIXTURE_WS" ls-files --error-unmatch state/ag2-mcp.d/conn-x.json
refute "git does NOT track a crashed write's temp file" \
    git -C "$FIXTURE_WS" ls-files --error-unmatch state/ag2-mcp.d/.conn-x.env.ab12.tmp
check "git DOES track the rest of an included state/ (carrier not broken)" \
    git -C "$FIXTURE_WS" ls-files --error-unmatch state/current-track.md
check "git DOES track an ordinary note" git -C "$FIXTURE_WS" ls-files --error-unmatch notes/a.md

echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
