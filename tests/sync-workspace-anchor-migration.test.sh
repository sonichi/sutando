#!/bin/bash
# Regression (#2567): a peer's carrier-set deletion must not destroy an
# unmigrated host's anchor. The guarantee is LOCAL and pre-pull — a pushing
# host can only add its OWN hosts/<label>/ copy, never the puller's.
# Run: bash tests/sync-workspace-anchor-migration.test.sh
# Falsification harness for _migrate_flat_anchor (#2567): the peer-deletion path.
set -uo pipefail
REPO="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; pass=0; fail=0
chk(){ if eval "$2" >/dev/null 2>&1; then echo "OK: $1"; pass=$((pass+1)); else echo "FAIL: $1"; fail=$((fail+1)); fi; }
ref(){ if eval "$2" >/dev/null 2>&1; then echo "FAIL(refute): $1"; fail=$((fail+1)); else echo "OK(refute): $1"; pass=$((pass+1)); fi; }

T=$(mktemp -d); WS="$T/ws"; mkdir -p "$WS/state" "$WS/hosts"
printf 'MY ANCHOR — 1044 lines of irreplaceable context\n' > "$WS/state/current-track.md"

# Extract the helper and run it against a fixture, exactly as sync would.
WORKSPACE_DIR="$WS"
_host(){ echo "test-host"; }
log(){ :; }
eval "$(awk '/^_migrate_flat_anchor\(\) \{/,/^\}/' "$REPO/scripts/sync-workspace.sh")"

# --- the regression: flat exists, per-host absent -> must migrate
_migrate_flat_anchor 2>/dev/null
chk "migrates the flat anchor to the per-host path" "[ -f '$WS/hosts/test-host/current-track.md' ]"
chk "content preserved verbatim" "diff -q '$WS/state/current-track.md' '$WS/hosts/test-host/current-track.md'"

# --- idempotent: second run must not clobber an existing per-host copy
printf 'NEWER per-host content\n' > "$WS/hosts/test-host/current-track.md"
_migrate_flat_anchor 2>/dev/null
chk "second run does NOT overwrite an existing per-host anchor" \
    "grep -q 'NEWER per-host content' '$WS/hosts/test-host/current-track.md'"

# --- no flat file: must be a clean no-op, not an error
rm -f "$WS/state/current-track.md"
_migrate_flat_anchor 2>/dev/null; rc=$?
chk "no-op (exit 0) when the flat anchor is absent" "[ $rc -eq 0 ]"

# --- THE FALSIFICATION: without the helper the anchor is lost to a peer deletion
T2=$(mktemp -d); WS2="$T2/ws"; mkdir -p "$WS2/state" "$WS2/hosts"
printf 'MY ANCHOR\n' > "$WS2/state/current-track.md"
WORKSPACE_DIR="$WS2"
# simulate the peer deletion WITHOUT running the helper
rm -f "$WS2/state/current-track.md"
ref "without the helper, no per-host copy exists after the deletion" \
    "[ -f '$WS2/hosts/test-host/current-track.md' ]"

echo; echo "Total: $((pass+fail)) — pass: $pass, fail: $fail"; [ "$fail" -eq 0 ]
