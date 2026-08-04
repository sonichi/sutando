#!/usr/bin/env bash
# Regression: the SHIPPED default carrier set must back up each host's anchor at
# the PER-HOST path `hosts/<label>/current-track.md`, must NOT carry it at the
# shared flat path `state/current-track.md`, and must NOT thereby carry its
# transient `state/` siblings.
#
# This file used to assert the opposite direction — that the shipped default
# lists `state/current-track.md`. That was right when it was written and is now
# the defect. The flat path is not host-qualified while the file is per-host
# state (its own first line names the host), so one host's anchor is delivered
# onto another host at the identical local path. #2567/#2568 host-qualified it;
# this is the carrier-set half of that migration.
#
# The PROTECTION is unchanged and is why this is a rewrite, not a deletion: the
# anchor `context-reconstruct` reads first every pass must be backed up, and a
# suite that goes green when that stops being true is not a test. Only the path
# it is backed up AT has moved. `hosts/*/` was already in the shipped default,
# so the anchor is carried before and after — measured on the live vault at the
# time of this change: `hosts/Chis-Mac-mini/current-track.md` and
# `hosts/Chis-MacBook-Pro/current-track.md` both TRACKED, `state/current-track.md`
# tracked on 0 of 6 refs. The flat entry was carrying nothing.
#
# So this drives the SHIPPED config through the SHIPPED composer and asserts
# both directions:
#   positive — `hosts/*/` is in the default list, the generated rules un-ignore
#              `hosts/` (ancestor) + the per-host anchor, and git tracks it;
#   negative — the flat `state/current-track.md` is NOT in the list, the rules
#              do NOT un-ignore it or `state/**`, and transient siblings
#              (core-status.json, voice-state.json, *.pid) stay untracked.
# The negative half matters as much: re-adding the flat path resumes the
# cross-host delivery, and widening to `state/` would carry per-run churn and
# secrets-adjacent files into the vault.

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
check "shipped sutando.config.json carries the per-host anchor dir (hosts/*/)" \
    python3 -c "
import json, pathlib, sys
inc = json.loads(pathlib.Path('$REPO/sutando.config.json').read_text())['vault']['sync']['include']
sys.exit(0 if 'hosts/*/' in inc else 1)
"

check "...and does NOT carry the anchor at the shared flat state/current-track.md" \
    python3 -c "
import json, pathlib, sys
inc = json.loads(pathlib.Path('$REPO/sutando.config.json').read_text())['vault']['sync']['include']
sys.exit(1 if 'state/current-track.md' in inc else 0)
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
# sutando-config.sh sources this helper (#2599)
cp "$REPO/scripts/python-binary.sh" "$FIXTURE_REPO/scripts/"
cp "$REPO/src/sutando_config.py" "$FIXTURE_REPO/src/"
cp "$REPO/sutando.config.json" "$FIXTURE_REPO/sutando.config.json"   # <-- the point
touch "$FIXTURE_REPO/CLAUDE.md"
mkdir -p "$FIXTURE_REPO/skills"
git init -q "$FIXTURE_REPO"
git init -q --bare "$FIXTURE_VAULT"

# Point the workspace at the fixture and populate the discriminating files.
mkdir -p "$FIXTURE_WS/state" "$FIXTURE_WS/notes" "$FIXTURE_WS/hosts/carrier-host"
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

check "generated rules un-ignore the hosts/ ancestor" \
    grep -qFx '!hosts/' "$RULES"
check "generated rules un-ignore the per-host subtree (glob, not per-label)" \
    grep -qFx '!hosts/*/' "$RULES"
check "...including its contents, which is what carries the anchor file" \
    grep -qFx '!hosts/*/**' "$RULES"
refute "generated rules do NOT un-ignore the flat state/current-track.md" \
    grep -qFx '!state/current-track.md' "$RULES"
refute "generated rules do NOT contain !state/** (would carry transient churn)" \
    grep -qFx '!state/**' "$RULES"

# ---------------------------------------------------------------------------
# 3. The behaviour the rules are for: what git would actually track.
#    This is the half that survives a future refactor of the rule syntax.
# ---------------------------------------------------------------------------
git -C "$FIXTURE_WS" add -A >/dev/null 2>&1 || true

check "git tracks the PER-HOST anchor (the protection this file exists for)" \
    git -C "$FIXTURE_WS" ls-files --error-unmatch hosts/carrier-host/current-track.md
refute "git does NOT track the flat state/current-track.md" \
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
