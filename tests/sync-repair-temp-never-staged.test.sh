#!/usr/bin/env bash
# An interrupted signature repair leaves hosts/<h>/.build_log.snapshot-sha.repair.XXXXXX;
# the real `git add -A` path must not vault it, and the sweep must reclaim it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_ROOT="$(mktemp -d -t sync-repair-temp-test.XXXXXX)"
trap 'rm -rf "$TEST_ROOT"' EXIT

pass=0; fail=0
check()  { local d="$1"; shift; if "$@" >/dev/null 2>&1; then echo "OK: $d"; pass=$((pass+1)); else echo "FAIL: $d"; fail=$((fail+1)); fi; }
refute() { local d="$1"; shift; if "$@" >/dev/null 2>&1; then echo "FAIL: $d"; fail=$((fail+1)); else echo "OK: $d"; pass=$((pass+1)); fi; }

FIXTURE_ROOT="$TEST_ROOT/repair-temp"
FIXTURE_REPO="$FIXTURE_ROOT/repo"
FIXTURE_WS="$FIXTURE_ROOT/workspace"
FIXTURE_VAULT="$FIXTURE_ROOT/vault.git"
mkdir -p "$FIXTURE_REPO/scripts" "$FIXTURE_REPO/src"
for f in sync-workspace.sh sutando-config.sh python-binary.sh; do cp "$REPO/scripts/$f" "$FIXTURE_REPO/scripts/"; done
cp "$REPO/src/sutando_config.py" "$FIXTURE_REPO/src/"
cp "$REPO/sutando.config.json" "$FIXTURE_REPO/sutando.config.json"
touch "$FIXTURE_REPO/CLAUDE.md"; mkdir -p "$FIXTURE_REPO/skills"
git init -q "$FIXTURE_REPO"; git init -q --bare "$FIXTURE_VAULT"

HOST=repair-host
mkdir -p "$FIXTURE_WS/hosts/$HOST"
cat > "$FIXTURE_REPO/sutando.config.local.json" <<JSON
{"workspace": {"path": "$FIXTURE_WS"}}
JSON

SYNC="$FIXTURE_REPO/scripts/sync-workspace.sh"
SYNC_ENV=(
    SUTANDO_HOST_OVERRIDE=$HOST
    SUTANDO_WS_ID_OVERRIDE=repair1
    SUTANDO_SYNC_LOCK_DIR="$FIXTURE_ROOT/sync.lock"
    GIT_AUTHOR_NAME="Repair Temp Test"   GIT_AUTHOR_EMAIL="repair@example.com"
    GIT_COMMITTER_NAME="Repair Temp Test" GIT_COMMITTER_EMAIL="repair@example.com"
)
env "${SYNC_ENV[@]}" bash "$SYNC" --vault-url "$FIXTURE_VAULT" --force-gitignore --init >/dev/null 2>&1 || true

RULES="$FIXTURE_WS/.git/info/exclude"
REPAIR_DENY='hosts/*/.build_log.snapshot-sha.repair.??????'
SNAP_DENY='hosts/*/build_log.md.snap.??????'

check "the generated exclude carries the snap-temp deny (mechanism control)" grep -qFx "$SNAP_DENY" "$RULES"
check "the generated exclude carries the repair-temp deny"                   grep -qFx "$REPAIR_DENY" "$RULES"

# The exact interrupted state: created and written, process died before cleanup.
H="$FIXTURE_WS/hosts/$HOST"
printf 'ordinary log\n'   > "$H/build_log.md"
printf 'snap temp\n'      > "$H/build_log.md.snap.AB12CD"
printf 'deadbeefcafe\n'   > "$H/.build_log.snapshot-sha.repair.AB12CD"

git -C "$FIXTURE_WS" add -A >/dev/null 2>&1 || true
STAGED="$FIXTURE_ROOT/staged.txt"
git -C "$FIXTURE_WS" diff --cached --name-only > "$STAGED" 2>/dev/null || true

check  "POSITIVE CONTROL: an ordinary build_log.md still stages"        grep -qx "hosts/$HOST/build_log.md" "$STAGED"
refute "NEGATIVE CONTROL: the reserved snap temp is not staged"         grep -qx "hosts/$HOST/build_log.md.snap.AB12CD" "$STAGED"
refute "an interrupted repair temp is not staged by the real add -A"    grep -qx "hosts/$HOST/.build_log.snapshot-sha.repair.AB12CD" "$STAGED"

# The sweep reclaims it past the grace window; a fresh one is left alone.
touch -t 200001010000 "$H/.build_log.snapshot-sha.repair.AB12CD"
printf 'fresh\n' > "$H/.build_log.snapshot-sha.repair.EF34GH"
# The sweep lives in _snapshot_per_host_config, reached only via _push_only_impl.
env "${SYNC_ENV[@]}" bash "$SYNC" --vault-url "$FIXTURE_VAULT" --push-only >/dev/null 2>&1 || true
refute "the sweep reclaims an aged interrupted repair temp"  test -f "$H/.build_log.snapshot-sha.repair.AB12CD"
check  "the sweep leaves a temp inside the grace window"     test -f "$H/.build_log.snapshot-sha.repair.EF34GH"

# UPGRADE PATH: an existing host reaches the deny via _adoptable_builtin_denies(), which a
# fresh --init never exercises; it is the unreviewed "adopted" path, so pin it here.
UP="$TEST_ROOT/upgrade"; UP_WS="$UP/workspace"; UP_VAULT="$UP/vault.git"
mkdir -p "$UP_WS/hosts/$HOST"; git init -q --bare "$UP_VAULT"
cat > "$FIXTURE_REPO/sutando.config.local.json" <<JSON
{"workspace": {"path": "$UP_WS"}}
JSON
UP_ENV=("${SYNC_ENV[@]}"); UP_ENV[1]=SUTANDO_WS_ID_OVERRIDE=upgrade1
env "${UP_ENV[@]}" bash "$SYNC" --vault-url "$UP_VAULT" --force-gitignore --init >/dev/null 2>&1 || true

UP_RULES="$UP_WS/.git/info/exclude"
# Rewind to a PRE-patch generated file: drop the repair deny, keep an operator comment.
if [ -f "$UP_RULES" ]; then
    grep -vF "$REPAIR_DENY" "$UP_RULES" > "$UP_RULES.tmp" && mv "$UP_RULES.tmp" "$UP_RULES"
    printf '# operator note: keep me\n' >> "$UP_RULES"
fi
refute "PRE-STATE CONTROL: the rewound exclude lacks the repair deny" grep -qFx "$REPAIR_DENY" "$UP_RULES"

env "${UP_ENV[@]}" bash "$SYNC" --vault-url "$UP_VAULT" --push-only >/dev/null 2>&1 || true
check "an EXISTING host adopts the repair deny on refresh (upgrade path)" grep -qFx "$REPAIR_DENY" "$UP_RULES"
check "the operator's own comment survives the adoption"                  grep -qF 'operator note: keep me' "$UP_RULES"

# health-check mirrors the generator's deny set; a drift here re-opens the hole.
check "health-check's mirrored pattern set carries the repair-temp deny" \
    grep -qF "$REPAIR_DENY" "$REPO/src/health-check.py"

echo
echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
