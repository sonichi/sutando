#!/usr/bin/env bash
# Regression: the SHIPPED default carrier set must carry state/current-track.md,
# and must NOT thereby carry its transient state/ siblings.
#
# Why this file exists, and why the two suites cited on the original PR were not
# enough. `sync-workspace-foreign-host-guard.test.sh` builds its own fixture
# `sutando.config.json`, and `sutando-config.test.py` exercises merge semantics
# with synthetic dicts. Neither one reads the committed default, so deleting the
# `state/current-track.md` entry from sutando.config.json leaves both suites
# green while restoring the exact defect: the anchor that context-reconstruct
# reads first every pass silently stops being backed up, and sync keeps
# reporting success. A test that cannot fail when the fix is removed is not yet
# a test.
#
# So this drives the SHIPPED config through the SHIPPED composer and asserts
# both directions:
#   positive — the entry is in the default list, and the generated rules
#              un-ignore `state/` (ancestor) + `state/current-track.md` (file);
#   negative — the rules do NOT contain `!state/**`, and transient siblings
#              (core-status.json, voice-state.json, *.pid) stay untracked.
# The negative half matters as much: widening this to `state/` would carry
# per-run churn and secrets-adjacent files into the vault.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_ROOT="$(mktemp -d -t vault-carrier-default-test.XXXXXX)"
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
refute() {
    local description="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        echo "FAIL: $description"
        fail=$((fail + 1))
    else
        echo "OK: $description"
        pass=$((pass + 1))
    fi
}

# ---------------------------------------------------------------------------
# 1. The shipped default list itself. Read sutando.config.json directly — this
#    is the assertion whose absence let the entry be removed silently.
# ---------------------------------------------------------------------------
refute "shipped sutando.config.json does NOT list the FLAT state/current-track.md (#2567)" \
    python3 -c "
import json, pathlib, sys
inc = json.loads(pathlib.Path('$REPO/sutando.config.json').read_text())['vault']['sync']['include']
sys.exit(0 if 'state/current-track.md' in inc else 1)
"

check "shipped sutando.config.json still carries hosts/*/ (the anchor's new home)" \
    python3 -c "
import json, pathlib, sys
inc = json.loads(pathlib.Path('$REPO/sutando.config.json').read_text())['vault']['sync']['include']
sys.exit(0 if 'hosts/*/' in inc else 1)
"

check "...and does NOT blanket-include state/ (which would carry transient churn)" \
    python3 -c "
import json, pathlib, sys
inc = json.loads(pathlib.Path('$REPO/sutando.config.json').read_text())['vault']['sync']['include']
bad = [p for p in inc if p.rstrip('/*') == 'state' or p in ('state/', 'state/**')]
sys.exit(0 if not bad else 1)
"

# ---------------------------------------------------------------------------
# 2. Drive the SHIPPED config through the SHIPPED composer and assert the
#    generated rules. Fixture mirrors sync-workspace-foreign-host-guard's setup
#    EXCEPT that sutando.config.json is copied, not written.
# ---------------------------------------------------------------------------
FIXTURE_ROOT="$TEST_ROOT/default-carrier"
FIXTURE_REPO="$FIXTURE_ROOT/repo"
FIXTURE_WS="$FIXTURE_ROOT/workspace"
FIXTURE_VAULT="$FIXTURE_ROOT/vault.git"
mkdir -p "$FIXTURE_REPO/scripts" "$FIXTURE_REPO/src"
cp "$REPO/scripts/sync-workspace.sh" "$FIXTURE_REPO/scripts/"
cp "$REPO/scripts/sutando-config.sh" "$FIXTURE_REPO/scripts/"
cp "$REPO/src/sutando_config.py" "$FIXTURE_REPO/src/"
cp "$REPO/sutando.config.json" "$FIXTURE_REPO/sutando.config.json"   # <-- the point
touch "$FIXTURE_REPO/CLAUDE.md"
mkdir -p "$FIXTURE_REPO/skills"
git init -q "$FIXTURE_REPO"
git init -q --bare "$FIXTURE_VAULT"

# Point the workspace at the fixture and populate the discriminating files.
mkdir -p "$FIXTURE_WS/state" "$FIXTURE_WS/notes"
mkdir -p "$FIXTURE_WS/hosts/carrier-host"
printf 'pinned track\n'  > "$FIXTURE_WS/hosts/carrier-host/current-track.md"
printf 'stale flat\n'    > "$FIXTURE_WS/state/current-track.md"
printf '{"status":"idle"}\n' > "$FIXTURE_WS/state/core-status.json"
printf '{"v":1}\n'       > "$FIXTURE_WS/state/voice-state.json"
printf '12345\n'         > "$FIXTURE_WS/state/watch-tasks-stream.pid"
printf 'a note\n'        > "$FIXTURE_WS/notes/a.md"

cat > "$FIXTURE_REPO/sutando.config.local.json" <<JSON
{"workspace": {"path": "$FIXTURE_WS"}}
JSON

SYNC="$FIXTURE_REPO/scripts/sync-workspace.sh"
SYNC_ENV=(
    SUTANDO_HOST_OVERRIDE=carrier-host
    SUTANDO_WS_ID_OVERRIDE=carrier1
    SUTANDO_SYNC_LOCK_DIR="$FIXTURE_ROOT/sync.lock"
    GIT_AUTHOR_NAME="Carrier Default Test"
    GIT_AUTHOR_EMAIL="carrier-default@example.com"
    GIT_COMMITTER_NAME="Carrier Default Test"
    GIT_COMMITTER_EMAIL="carrier-default@example.com"
)
env "${SYNC_ENV[@]}" bash "$SYNC" \
    --vault-url "$FIXTURE_VAULT" --force-gitignore --init \
    >/dev/null 2>&1 || true

RULES="$FIXTURE_WS/.git/info/exclude"

refute "generated rules do NOT un-ignore the FLAT state/current-track.md (#2567)" \
    grep -qFx '!state/current-track.md' "$RULES"
refute "generated rules do NOT contain !state/** (would carry transient churn)" \
    grep -qFx '!state/**' "$RULES"

# ---------------------------------------------------------------------------
# 3. The behaviour the rules are for: what git would actually track.
#    This is the half that survives a future refactor of the rule syntax.
# ---------------------------------------------------------------------------
git -C "$FIXTURE_WS" add -A >/dev/null 2>&1 || true

check "git tracks the PER-HOST anchor hosts/<label>/current-track.md" \
    git -C "$FIXTURE_WS" ls-files --error-unmatch hosts/carrier-host/current-track.md
refute "git does NOT track the flat state/current-track.md (the shared path that collided)" \
    git -C "$FIXTURE_WS" ls-files --error-unmatch state/current-track.md
check "git tracks notes/ (default carrier set still intact)" \
    git -C "$FIXTURE_WS" ls-files --error-unmatch notes/a.md
refute "git does NOT track state/core-status.json" \
    git -C "$FIXTURE_WS" ls-files --error-unmatch state/core-status.json
refute "git does NOT track state/voice-state.json" \
    git -C "$FIXTURE_WS" ls-files --error-unmatch state/voice-state.json
refute "git does NOT track state/watch-tasks-stream.pid" \
    git -C "$FIXTURE_WS" ls-files --error-unmatch state/watch-tasks-stream.pid

echo
echo "Total: $((pass + fail)) — pass: $pass, fail: $fail"
[ "$fail" -eq 0 ]
