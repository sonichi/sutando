#!/bin/bash
# PHASE 1 of the #2567 migration: every host copies its own anchor to the
# host-qualified path BEFORE any pull, so that a LATER change can retire the
# shared flat path without orphaning anyone.
#
# WHAT THIS TEST PROVES: the helper is correct and idempotent in isolation.
# WHAT IT DOES NOT PROVE — stated because a reviewer had to point it out:
# it calls the helper directly against one fixture, so the 'pulling' side
# always has the fix by construction. It therefore CANNOT exercise the
# mixed-version rolling upgrade (host A on new code, host B on old), which
# is exactly the sequence that loses data. That is why this PR no longer
# removes the flat carrier entry: phase 1 ships only the helper, so there is
# no deletion for an old host to pull. Removal waits for phase 2, gated on
# evidence that every host has migrated.
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
DRY_RUN=0
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

# --- DRY-RUN CONTRACT (#2568 review): an explicitly non-mutating command must
# not write workspace state. The helper runs BEFORE _pull_only_impl's dry-run
# early return by necessity, so the guard lives inside the helper.
T3=$(mktemp -d); WS3="$T3/ws"; mkdir -p "$WS3/state" "$WS3/hosts"
printf 'MY ANCHOR\n' > "$WS3/state/current-track.md"
WORKSPACE_DIR="$WS3"; DRY_RUN=1
_migrate_flat_anchor 2>/dev/null
ref "DRY_RUN=1 does NOT create the per-host anchor" \
    "[ -e '$WS3/hosts/test-host/current-track.md' ]"
chk "DRY_RUN=1 leaves the flat anchor untouched" "[ -f '$WS3/state/current-track.md' ]"
# and the positive control: same fixture, DRY_RUN off, MUST create it
DRY_RUN=0
_migrate_flat_anchor 2>/dev/null
chk "with DRY_RUN=0 the same fixture DOES migrate (control)" \
    "[ -f '$WS3/hosts/test-host/current-track.md' ]"
unset DRY_RUN

# --- THE FALSIFICATION: without the helper the anchor is lost to a peer deletion
T2=$(mktemp -d); WS2="$T2/ws"; mkdir -p "$WS2/state" "$WS2/hosts"
printf 'MY ANCHOR\n' > "$WS2/state/current-track.md"
WORKSPACE_DIR="$WS2"
# simulate the peer deletion WITHOUT running the helper
rm -f "$WS2/state/current-track.md"
ref "without the helper, no per-host copy exists after the deletion" \
    "[ -f '$WS2/hosts/test-host/current-track.md' ]"

echo; echo "Total: $((pass+fail)) — pass: $pass, fail: $fail"; [ "$fail" -eq 0 ]

# --- PHASE 2 REGRESSION (#2607, qingyun-wu review): --push-only must migrate too.
#     `_migrate_flat_anchor` was called only from `_pull_only_impl`, but the hazard
#     is `_enforce_carrier_set_pre` — which `--push-only` also runs. On a host whose
#     anchor exists ONLY at the flat path, push-only untracked and committed the
#     deletion and wrote no replacement: the sole carried copy, gone.
#     Drives the REAL script end to end rather than calling the helper, because a
#     direct-call test is what missed this the first time.
PO_ROOT="$(mktemp -d -t anchor-pushonly.XXXXXX)"
PO_REPO="$PO_ROOT/repo"; PO_WS="$PO_ROOT/ws"; PO_VAULT="$PO_ROOT/v.git"
mkdir -p "$PO_REPO/scripts" "$PO_REPO/src" "$PO_WS/state" "$PO_REPO/skills"
cp "$REPO/scripts/sync-workspace.sh" "$PO_REPO/scripts/"
cp "$REPO/scripts/sutando-config.sh" "$PO_REPO/scripts/"
# sutando-config.sh sources this helper (#2599)
cp "$REPO/scripts/python-binary.sh"  "$PO_REPO/scripts/"
cp "$REPO/src/sutando_config.py"     "$PO_REPO/src/"
cp "$REPO/sutando.config.json"       "$PO_REPO/"
touch "$PO_REPO/CLAUDE.md"
git init -q "$PO_REPO"; git init -q --bare "$PO_VAULT"
printf '{"workspace": {"path": "%s"}}\n' "$PO_WS" > "$PO_REPO/sutando.config.local.json"
# Pre-#2607 world: the anchor exists ONLY at the flat path, and no per-host copy.
printf 'the only anchor\n' > "$PO_WS/state/current-track.md"
# Set the PRIMARY var, not the legacy SUTANDO_HOST_OVERRIDE alias. Both are
# honored (util_paths.py:113), so the alias is not dead — but it is LOWER
# precedence, and this harness inherits the caller's environment. A shell that
# already exports SUTANDO_HOST_LABEL (a real core session does) silently
# outranks the alias we set here, so the fixture builds under the developer's
# own host label instead of `pushonly-host` and the assertions below then look
# like the FIX failing. Diagnosed by Sutando-Pro 2026-08-04, whose session
# exported SUTANDO_HOST_LABEL=Chis-MacBook-Pro. Setting the top-precedence name
# explicitly makes the fixture immune to whatever the caller exports.
PO_ENV=(SUTANDO_HOST_LABEL=pushonly-host SUTANDO_WS_ID_OVERRIDE=po1
        SUTANDO_SYNC_LOCK_DIR="$PO_ROOT/lock"
        GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@e GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@e)

# SETUP FAILURES MUST BE LOUD. These two commands build the fixture the three
# assertions below measure; when they were `>/dev/null 2>&1 || true` a broken
# setup was indistinguishable from a broken FIX, and the assertions reported
# "the anchor was not carried" — which reads as the change under test failing.
# That cost a reviewer an hour on 2026-08-04. Capture and surface instead.
_po_run() {  # $1 = label, rest = args to sync-workspace.sh
    local _label="$1"; shift
    local _out _rc
    _out=$(env "${PO_ENV[@]}" bash "$PO_REPO/scripts/sync-workspace.sh" "$@" 2>&1); _rc=$?
    if [ "$_rc" -ne 0 ]; then
        echo "FAIL: push-only fixture setup ($_label) exited $_rc — the assertions below measure a fixture that was never built:"
        printf '%s\n' "$_out" | sed 's/^/      /'
        fail=$((fail+1))
    fi
}
_po_run "--init" --vault-url "$PO_VAULT" --init
_po_run "--push-only" --push-only

chk "push-only wrote the per-host anchor on disk" \
    "[ -f '$PO_WS/hosts/pushonly-host/current-track.md' ]"
chk "push-only CARRIED the per-host anchor (this is the durability claim)" \
    "git -C '$PO_WS' ls-files --error-unmatch hosts/pushonly-host/current-track.md"
chk "the rescued copy has the original content, not an empty placeholder" \
    "grep -qFx 'the only anchor' '$PO_WS/hosts/pushonly-host/current-track.md'"
# Control: the flat path is genuinely retired by this PR, so the test above is
# proving a RESCUE and not merely that nothing changed.
ref "flat state/current-track.md is no longer carried" \
    "git -C '$PO_WS' ls-files --error-unmatch state/current-track.md"
rm -rf "$PO_ROOT"

echo
echo "Total: $((pass + fail)) — pass: $pass, fail: $fail"
[ "$fail" -eq 0 ]
