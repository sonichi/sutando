#!/usr/bin/env bash
# list-sync-files.sh — inventory helper for cross-node-sync.
#
# Lists every file that rsync WOULD touch on the current node, with mtime +
# size. Output is pipe-delimited so diffing two nodes' outputs is trivial:
#
#   relpath|sha1(12)|bytes|mtime(iso)
#
# Output is sorted by relpath so `diff <(node-a.sh) <(node-b.sh)` surfaces
# missing files + content mismatches.
#
# Reads the same scope as setup-rsync-sync.sh:
#   - ~/.claude/projects/.../memory/
#   - <repo>/notes/
#
# Excludes the same per-node garbage (.DS_Store, swap files, etc.)
#
# Usage:
#   bash skills/cross-node-sync/scripts/list-sync-files.sh > /tmp/my-manifest.txt
#   # then on peer node, do the same, and `diff` the two files.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"

MEM_DIR="$HOME/.claude/projects/-Users-xueqingliu-Documents-sutando-sutando/memory"
NOTES_DIR="$REPO_ROOT/notes"

list_dir() {
    local root="$1"
    local prefix="$2"
    [ -d "$root" ] || return 0
    # find all regular files, exclude noise, print rel-path
    find "$root" -type f \
        ! -name '.DS_Store' \
        ! -name '*.swp' \
        ! -name '*.swo' \
        ! -path '*/.stversions/*' \
        ! -path '*/.stfolder/*' \
        2>/dev/null \
    | while read -r f; do
        rel="${prefix}/$(basename "$f")"
        # portable stat -f for BSD (macOS), fall back to -c for GNU
        if sz=$(stat -f %z "$f" 2>/dev/null); then
            mt=$(stat -f %Sm -t %Y-%m-%dT%H:%M:%SZ "$f" 2>/dev/null)
        else
            sz=$(stat -c %s "$f" 2>/dev/null)
            mt=$(stat -c %Y "$f" 2>/dev/null | xargs -I{} date -u -d @{} +%Y-%m-%dT%H:%M:%SZ)
        fi
        # 12-char sha1 prefix — enough to detect content divergence without
        # the full 40-char overhead. shasum is macOS-native.
        hash=$(shasum "$f" 2>/dev/null | awk '{print substr($1,1,12)}')
        printf "%s|%s|%s|%s\n" "$rel" "$hash" "$sz" "$mt"
    done
}

{
    list_dir "$MEM_DIR"    "memory"
    list_dir "$NOTES_DIR"  "notes"
} | sort
