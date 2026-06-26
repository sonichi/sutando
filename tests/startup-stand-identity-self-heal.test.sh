#!/usr/bin/env bash
# Verifies src/startup.sh self-heal for pre-#1540 stand-identity.json misplacement.
#
# The self-heal block (added in PR #1542) moves
# <workspace>/state/stand-identity.json → <workspace>/stand-identity.json when
# state/ has the file AND root does not. Idempotent on subsequent runs and
# no-op when both/neither side has the file.
#
# Why a test: the self-heal is irreversible (mv, not cp) on the FIRST host
# that runs the patched startup. A typo in the guard condition that turned
# `! -e` into `-e` would clobber a freshly-configured stand-identity at root
# with the legacy state/ file. The guard order matters; pin it.
set -euo pipefail

# Extract the self-heal block from startup.sh (no full startup.sh execution —
# that boots the whole stack). Match by the unique marker phrase in the comment.
HEAL_BLOCK='if [ -f "$WORKSPACE/state/stand-identity.json" ] && [ ! -e "$WORKSPACE/stand-identity.json" ]; then
  mv "$WORKSPACE/state/stand-identity.json" "$WORKSPACE/stand-identity.json"
  echo "[startup] self-heal: moved stand-identity.json from state/ → workspace root (pre-#1540 migrate followup)" >&2
fi'

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Assertion: the block exists at the expected place in src/startup.sh — if it
# moved or got rewritten, the rest of this test is stale and the maintainer
# should re-author the extraction.
grep -q 'self-heal: moved stand-identity.json' "$REPO/src/startup.sh" \
  || { echo "FAIL: self-heal block not found in src/startup.sh — test is stale"; exit 1; }

run_heal() {
  local _ws="$1"
  WORKSPACE="$_ws" bash -c "$HEAL_BLOCK" 2>&1
}

# Case 1: legacy state/ has the file, root does NOT → mv fires
T1=$(mktemp -d)
mkdir -p "$T1/state"
echo '{"name":"Maddy"}' > "$T1/state/stand-identity.json"
out1=$(run_heal "$T1")
[ -f "$T1/stand-identity.json" ] || { echo "FAIL case 1: file not at root after heal"; exit 1; }
[ ! -e "$T1/state/stand-identity.json" ] || { echo "FAIL case 1: file still at state/ after heal"; exit 1; }
[[ "$out1" == *"self-heal: moved"* ]] || { echo "FAIL case 1: missing heal log"; exit 1; }
rm -rf "$T1"

# Case 2: root already has the file (configured fresh post-#1540) → no-op
T2=$(mktemp -d)
mkdir -p "$T2/state"
echo '{"name":"Fresh"}' > "$T2/stand-identity.json"
echo '{"name":"Legacy"}' > "$T2/state/stand-identity.json"
out2=$(run_heal "$T2")
fresh_contents=$(cat "$T2/stand-identity.json")
[ "$fresh_contents" = '{"name":"Fresh"}' ] || { echo "FAIL case 2: root file was clobbered"; exit 1; }
[ -f "$T2/state/stand-identity.json" ] || { echo "FAIL case 2: state/ file disappeared on no-op"; exit 1; }
[[ -z "$out2" ]] || { echo "FAIL case 2: heal fired on no-op path"; exit 1; }
rm -rf "$T2"

# Case 3: neither side has the file → no-op
T3=$(mktemp -d)
mkdir -p "$T3/state"
out3=$(run_heal "$T3")
[ ! -e "$T3/stand-identity.json" ] || { echo "FAIL case 3: file appeared from nowhere"; exit 1; }
[[ -z "$out3" ]] || { echo "FAIL case 3: heal fired on no-op path"; exit 1; }
rm -rf "$T3"

# Case 4: idempotent — run heal twice on case-1 fixture
T4=$(mktemp -d)
mkdir -p "$T4/state"
echo '{"name":"Maddy"}' > "$T4/state/stand-identity.json"
run_heal "$T4" >/dev/null
out4=$(run_heal "$T4")
[[ -z "$out4" ]] || { echo "FAIL case 4: second invocation fired (not idempotent)"; exit 1; }
[ -f "$T4/stand-identity.json" ] || { echo "FAIL case 4: file vanished on second run"; exit 1; }
rm -rf "$T4"

echo "OK — all 4 self-heal cases pass"
