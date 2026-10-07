#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO/scripts/sync-workspace.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

WS="$TMP/workspace"
LEGACY="$TMP/legacy"
VAULT="$TMP/vault.git"
mkdir -p "$WS" "$LEGACY/notes"
git init -q "$LEGACY"
git init -q --bare "$VAULT"
printf 'text\n' > "$LEGACY/notes/note.md"

run_dry() {
    env \
        SUTANDO_REPO_DIR="$REPO" \
        SUTANDO_WORKSPACE="$WS" \
        SUTANDO_TEST_MODE=1 \
        SUTANDO_HOST_OVERRIDE=testhost \
        SUTANDO_WS_ID_OVERRIDE=a2285a \
        SUTANDO_MEMORY_SYNC_DIR="$LEGACY" \
        SUTANDO_SYNC_LOCK_DIR="$TMP/lock.d" \
        bash "$SCRIPT" --vault-url "$VAULT" --migrate-from-legacy --dry-run 2>&1
}

fail=0
out="$(run_dry)"
if grep -Fq '~2.65 GB' <<<"$out"; then
    echo "FAIL: dry-run still prints the hardcoded ~2.65 GB estimate"
    fail=$((fail + 1))
else
    echo "OK: hardcoded ~2.65 GB estimate is gone"
fi
if grep -Fq '1 text files; 0B generated/media left archived in legacy' <<<"$out"; then
    echo "OK: absent generated/media directories report 0B"
else
    echo "FAIL: absent generated/media directories did not report 0B"
    printf '%s\n' "$out" | grep 'rsync notes/' || true
    fail=$((fail + 1))
fi

mkdir -p "$LEGACY/notes/media" "$LEGACY/notes/generated"
dd if=/dev/zero of="$LEGACY/notes/media/blob.bin" bs=1024 count=1024 >/dev/null 2>&1
dd if=/dev/zero of="$LEGACY/notes/generated/blob.bin" bs=1024 count=1024 >/dev/null 2>&1
out="$(run_dry)"
if grep -Eq '[1-9][0-9]*(\.[0-9]+)? (KB|MB|GB) generated/media left archived in legacy' <<<"$out"; then
    echo "OK: present generated/media directories report a nonzero measured size"
else
    echo "FAIL: present generated/media directories did not report a nonzero measured size"
    printf '%s\n' "$out" | grep 'rsync notes/' || true
    fail=$((fail + 1))
fi

# Relocated media trees are commonly symlinked. The measurement must follow the
# symlink itself rather than report only the link inode as ~0 KB.
rm -rf "${LEGACY:?}/notes/media" "${LEGACY:?}/notes/generated"
MEDIA_TARGET="$TMP/media-target"
GENERATED_TARGET="$TMP/generated-target"
mkdir -p "$MEDIA_TARGET" "$GENERATED_TARGET"
dd if=/dev/zero of="$MEDIA_TARGET/blob.bin" bs=1024 count=1024 >/dev/null 2>&1
dd if=/dev/zero of="$GENERATED_TARGET/blob.bin" bs=1024 count=1024 >/dev/null 2>&1
ln -s "$MEDIA_TARGET" "$LEGACY/notes/media"
ln -s "$GENERATED_TARGET" "$LEGACY/notes/generated"
out="$(run_dry)"
if grep -Eq '[1-9][0-9]*(\.[0-9]+)? (KB|MB|GB) generated/media left archived in legacy' <<<"$out"; then
    echo "OK: symlinked generated/media directories measure their targets"
else
    echo "FAIL: symlinked generated/media directories reported zero instead of target size"
    printf '%s\n' "$out" | grep 'rsync notes/' || true
    fail=$((fail + 1))
fi

if [ "$fail" -ne 0 ]; then
    echo "sync-workspace-dry-run-media-size: $fail failure(s)"
    exit 1
fi
echo "sync-workspace-dry-run-media-size: all passed"
