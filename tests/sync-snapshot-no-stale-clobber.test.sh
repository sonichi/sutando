#!/usr/bin/env bash
# The build_log snapshot must never clobber an independently-written per-host
# copy — ownership is decided by recorded provenance, never by mtime.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fails=0
check() { if eval "$2"; then echo "  ok: $1"; else echo "  FAIL: $1"; fails=$((fails+1)); fi; }

SB="$(mktemp -d)"
trap 'rm -rf "$SB"' EXIT
export WORKSPACE_DIR="$SB/ws"
mkdir -p "$WORKSPACE_DIR/hosts/testhost"
# Stub SCRIPT_PARENT so the function's config-dir resolution succeeds.
export SCRIPT_PARENT="$SB/parent"
mkdir -p "$SCRIPT_PARENT/scripts" "$SB/cfg"
printf '#!/bin/sh\necho "%s"\n' "$SB/cfg" > "$SCRIPT_PARENT/scripts/sutando-config.sh"
chmod +x "$SCRIPT_PARENT/scripts/sutando-config.sh"

# Load ONLY the function under test, plus a _host stub.
_host() { echo testhost; }
eval "$(sed -n '/^_snapshot_per_host_config() {/,/^}$/p' "$REPO/scripts/sync-workspace.sh")"

log() { echo "log: $*"; }

echo "1. reviewer's control: per-host written independently, stale root TOUCHED LATER"
echo "stale relic" > "$WORKSPACE_DIR/build_log.md"
echo "per-host live entry" > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
sleep 1
touch "$WORKSPACE_DIR/build_log.md"   # root now strictly NEWER than the live per-host copy
_snapshot_per_host_config
check "independent per-host writer survives a later-touched root" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "per-host live entry" ]'
check "the refusal is loud, not silent" \
      '_snapshot_per_host_config | grep -q "independent writer"'

echo "2. reverse ordering: per-host newer than root — still refused, still loud"
touch "$WORKSPACE_DIR/hosts/testhost/build_log.md"
_snapshot_per_host_config
check "mtime ordering is irrelevant to the refusal" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "per-host live entry" ]'

echo "3. seed: absent per-host copy is created and its provenance recorded"
rm "$WORKSPACE_DIR/hosts/testhost/build_log.md" "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha" 2>/dev/null
_snapshot_per_host_config
check "first snapshot seeds the per-host copy" \
      '[ -f "$WORKSPACE_DIR/hosts/testhost/build_log.md" ]'
check "provenance sha recorded beside it" \
      '[ -s "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha" ]'

echo "4. root-live host: untouched snapshot refreshes when root changes"
echo "fresh root entries" > "$WORKSPACE_DIR/build_log.md"
_snapshot_per_host_config
check "pure snapshot (matching recorded sha) is refreshed from root" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "fresh root entries" ]'

echo "5. independent edit AFTER seeding is never overwritten"
echo "per-host went live" > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
echo "root moved on too" > "$WORKSPACE_DIR/build_log.md"
_snapshot_per_host_config
check "post-seed independent writer wins regardless of root changes" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "per-host went live" ]'

echo "5b. upgrade bootstrap: pre-provenance byte-identical snapshot is adopted"
rm -f "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
echo "same bytes both sides" > "$WORKSPACE_DIR/build_log.md"
cp "$WORKSPACE_DIR/build_log.md" "$WORKSPACE_DIR/hosts/testhost/build_log.md"
_snapshot_per_host_config
check "provenance recorded for the inherited equal snapshot" \
      '[ -s "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha" ]'
echo "root moved after upgrade" > "$WORKSPACE_DIR/build_log.md"
_snapshot_per_host_config
check "root-live refresh works after the upgrade bootstrap" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "root moved after upgrade" ]'

echo "5c. pre-provenance DIVERGED copy stays refused (no adoption on difference)"
rm -f "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
echo "independent content" > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
_snapshot_per_host_config
check "diverged unrecorded copy is preserved, not adopted or overwritten" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "independent content" ]'
echo "keep it that way" >> "$WORKSPACE_DIR/build_log.md"
echo "per-host went live" > "$WORKSPACE_DIR/hosts/testhost/build_log.md"

echo "6. absent root is a no-op"
rm "$WORKSPACE_DIR/build_log.md"
_snapshot_per_host_config
check "no root file leaves the per-host log untouched" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "per-host went live" ]'

echo "7. TOCTOU: a per-host write landing AFTER the ownership check is not clobbered"
# Seed a clean, owned snapshot (dst == root, sha recorded), then advance root.
printf 'root baseline\n' > "$WORKSPACE_DIR/build_log.md"
rm -f "$WORKSPACE_DIR/hosts/testhost/build_log.md" \
      "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
_snapshot_per_host_config
printf 'root advanced\n' > "$WORKSPACE_DIR/build_log.md"
# Inject a concurrent per-host append EXACTLY ONCE, right after the ownership
# hash of the destination — the check->replace window the reviewer exercised.
# Wrapping shasum lets the append land after _cur is computed but before the
# copy, identically for the fixed and the pre-fix function.
rm -f "$SB/.injected"
shasum() {
    local _o; _o="$(command shasum "$@")"
    if [ ! -e "$SB/.injected" ] && printf '%s' "$*" \
            | grep -q "hosts/testhost/build_log.md$"; then
        printf 'per-host live append\n' >> "$WORKSPACE_DIR/hosts/testhost/build_log.md"
        : > "$SB/.injected"
    fi
    printf '%s\n' "$_o"
}
_out7="$(_snapshot_per_host_config 2>&1)"
unset -f shasum
check "a per-host write in the check->replace window survives" \
      'grep -q "per-host live append" "$WORKSPACE_DIR/hosts/testhost/build_log.md"'
check "the racing replace is refused loudly" \
      'printf "%s" "$_out7" | grep -q "changed between check and replace"'

echo "8. self-heal: equal content with a stale provenance sha repairs itself"
printf 'converged\n' > "$WORKSPACE_DIR/build_log.md"
printf 'converged\n' > "$WORKSPACE_DIR/hosts/testhost/build_log.md"
printf 'deadbeefstale\n' > "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha"
_snapshot_per_host_config
check "equal content adopts the true sha over a stale one" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/.build_log.snapshot-sha")" = "$(shasum -a 256 "$WORKSPACE_DIR/hosts/testhost/build_log.md" | cut -d" " -f1)" ]'
printf 'root moved after self-heal\n' > "$WORKSPACE_DIR/build_log.md"
_snapshot_per_host_config
check "root-live refresh works once provenance self-healed" \
      '[ "$(cat "$WORKSPACE_DIR/hosts/testhost/build_log.md")" = "root moved after self-heal" ]'

[ "$fails" -eq 0 ] && { echo "ALL PASS"; exit 0; }
echo "$fails FAILURE(S)"; exit 1
