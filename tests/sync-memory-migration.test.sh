#!/bin/bash
# Tests for scripts/sync-memory.sh one-time migration from
# ~/.sutando-memory-sync to ~/.sutando/memory-sync.
#
# Run: bash tests/sync-memory-migration.test.sh
# Exit: 0 on pass, 1 on fail.
#
# Approach: extract the migration block from sync-memory.sh, run it inside a
# tempdir-rooted fake HOME under three scenarios — legacy present, target
# already present (collision), env pinned — and assert the resulting tree.
# Keeps the migration testable without invoking the rest of the script (git
# clone, push, etc., which require a real remote).

set -u
SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/scripts/sync-memory.sh"

if [ ! -f "$SCRIPT" ]; then
    echo "FAIL: $SCRIPT not found"
    exit 1
fi

# Extract just the migration block from sync-memory.sh into a sourceable
# snippet. We grep between the start sentinel comment and the next blank
# line after the closing `fi`. Tightly coupled to the block's text but
# that's the point — a refactor that drops or renames the block trips this
# extraction and the test fails loudly.
MIGRATION_BLOCK="$(awk '
    /^__OLD_DEFAULT="/                  { capture = 1 }
    capture                              { print }
    capture && /^fi$/                    { capture = 0; exit }
' "$SCRIPT")"

if [ -z "$MIGRATION_BLOCK" ]; then
    echo "FAIL: could not extract migration block from $SCRIPT"
    echo "      (anchor likely changed — update the awk pattern above)"
    exit 1
fi

PASS=0
FAIL=0

run_case() {
    local name="$1"
    shift
    if ( "$@" ); then
        echo "PASS: $name"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $name"
        FAIL=$((FAIL + 1))
    fi
}

case_legacy_migrates() {
    local tmp; tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' RETURN
    mkdir -p "$tmp/.sutando-memory-sync/memory"
    echo "preserved content" > "$tmp/.sutando-memory-sync/memory/test.md"
    HOME="$tmp" unset SUTANDO_MEMORY_SYNC_DIR
    # Run the extracted block in a subshell with HOME overridden.
    HOME="$tmp" bash -c "$MIGRATION_BLOCK" >/dev/null 2>&1
    [ -d "$tmp/.sutando/memory-sync" ] || { echo "  new dir missing"; return 1; }
    [ ! -d "$tmp/.sutando-memory-sync" ] || { echo "  old dir still present (should have been mv'd)"; return 1; }
    grep -q "preserved content" "$tmp/.sutando/memory-sync/memory/test.md" || { echo "  content not preserved"; return 1; }
    return 0
}

case_env_pin_skips_migration() {
    local tmp; tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' RETURN
    mkdir -p "$tmp/.sutando-memory-sync/memory"
    echo "keep me" > "$tmp/.sutando-memory-sync/memory/test.md"
    HOME="$tmp" SUTANDO_MEMORY_SYNC_DIR="$tmp/elsewhere" \
        bash -c "$MIGRATION_BLOCK" >/dev/null 2>&1
    # Env pin → migration should NOT fire → legacy untouched.
    [ -d "$tmp/.sutando-memory-sync" ] || { echo "  legacy dir was moved despite env pin"; return 1; }
    [ ! -d "$tmp/.sutando/memory-sync" ] || { echo "  new dir created despite env pin"; return 1; }
    grep -q "keep me" "$tmp/.sutando-memory-sync/memory/test.md" || { echo "  legacy content lost"; return 1; }
    return 0
}

case_target_exists_skips_migration() {
    local tmp; tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' RETURN
    mkdir -p "$tmp/.sutando-memory-sync/memory"
    echo "legacy" > "$tmp/.sutando-memory-sync/memory/test.md"
    mkdir -p "$tmp/.sutando/memory-sync"
    echo "EXISTING" > "$tmp/.sutando/memory-sync/marker.txt"
    HOME="$tmp" unset SUTANDO_MEMORY_SYNC_DIR
    HOME="$tmp" bash -c "$MIGRATION_BLOCK" >/dev/null 2>&1
    # Target already exists → don't clobber → legacy stays put.
    [ -f "$tmp/.sutando/memory-sync/marker.txt" ] || { echo "  target marker lost"; return 1; }
    [ -d "$tmp/.sutando-memory-sync" ] || { echo "  legacy moved despite target existing"; return 1; }
    return 0
}

case_idempotent_second_run() {
    local tmp; tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' RETURN
    mkdir -p "$tmp/.sutando-memory-sync/memory"
    echo "v1" > "$tmp/.sutando-memory-sync/memory/test.md"
    HOME="$tmp" unset SUTANDO_MEMORY_SYNC_DIR
    HOME="$tmp" bash -c "$MIGRATION_BLOCK" >/dev/null 2>&1
    # Second run — should be a no-op.
    local out
    out="$(HOME="$tmp" bash -c "$MIGRATION_BLOCK" 2>&1)"
    [ -z "$out" ] || { echo "  second run emitted output: $out"; return 1; }
    [ -d "$tmp/.sutando/memory-sync" ] || { echo "  new dir lost on second run"; return 1; }
    return 0
}

case_no_legacy_no_op() {
    local tmp; tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' RETURN
    # Fresh install: neither old nor new exists.
    HOME="$tmp" unset SUTANDO_MEMORY_SYNC_DIR
    HOME="$tmp" bash -c "$MIGRATION_BLOCK" >/dev/null 2>&1
    [ ! -d "$tmp/.sutando-memory-sync" ] || { echo "  legacy created from thin air"; return 1; }
    [ ! -d "$tmp/.sutando/memory-sync" ] || { echo "  new dir created without trigger"; return 1; }
    return 0
}

case_script_dir_resolves_through_symlink() {
    # Regression guard for #1436: when scripts/ is a symlinked directory
    # (e.g. workspace/scripts → repo/scripts), $0 + pwd without -P resolves
    # to the symlink dir, breaking SCRIPT_PARENT. ${BASH_SOURCE[0]} + pwd -P
    # follows the physical path back to the real dir.
    local real_scripts; real_scripts="$(mktemp -d)"
    trap 'rm -rf "$real_scripts"' RETURN
    local link_parent; link_parent="$(mktemp -d)"
    trap 'rm -rf "$link_parent"' RETURN

    # Mini probe that uses the same ${BASH_SOURCE[0]} + pwd -P pattern.
    cat > "$real_scripts/probe.sh" << 'PROBE'
#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
echo "$SCRIPT_DIR"
PROBE
    chmod +x "$real_scripts/probe.sh"

    # Symlink the DIRECTORY (mirrors workspace/scripts → repo/scripts pattern).
    ln -s "$real_scripts" "$link_parent/scripts"

    # Invoke via the symlinked directory path.
    local got; got="$(bash "$link_parent/scripts/probe.sh")"
    # pwd -P on macOS may expand /var → /private/var; canonicalize both sides.
    local want; want="$(cd "$real_scripts" && pwd -P)"
    [ "$got" = "$want" ] || {
        echo "  SCRIPT_DIR='$got', want '$want'"
        echo "  (pwd -P missing — symlinked scripts/ dir not resolved to real path)"
        return 1
    }
}

run_case "legacy ~/.sutando-memory-sync migrates to ~/.sutando/memory-sync" case_legacy_migrates
run_case "SUTANDO_MEMORY_SYNC_DIR env pin skips migration"               case_env_pin_skips_migration
run_case "target already exists -> no clobber, no migration"             case_target_exists_skips_migration
run_case "idempotent: second run is silent no-op"                        case_idempotent_second_run
run_case "fresh install (no legacy) is silent no-op"                     case_no_legacy_no_op
run_case "SCRIPT_DIR resolves through symlink (issue #1436)"             case_script_dir_resolves_through_symlink

echo ""
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
