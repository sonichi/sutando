#!/usr/bin/env bash
# setup-rsync-sync.sh — rsync-over-ssh cross-node sync for Sutando.
#
# Pivoted from the Syncthing prototype (2026-04-17) after agreeing the sync
# scope is narrow enough that a daemon is overkill. Rsync is macOS-native,
# no new binary, no web UI, no continuous process — fires on proactive-loop
# cron ticks or on demand.
#
# What it does (when not in --dry-run):
#   1. Verify an SSH key pair exists and is authorized on the peer.
#   2. Ping the peer via `ssh PEER true` — fail fast if unreachable.
#   3. Rsync the two scoped folders in each direction with delete-after-
#      dry-run gate so the operator sees what would go before mutating.
#
# Scope (what rsyncs):
#   - ~/.claude/projects/-Users-xueqingliu-Documents-sutando-sutando/memory/
#     (cross-session bot memory — MEMORY.md index + feedback/project/ref
#     markdown files)
#   - <repo>/notes/             (user's second-brain notes)
#
# What does NOT sync (per-node, excluded via rsync --exclude):
#   - state/, tasks/, results/, logs/
#   - .env, .env.* (different secrets per node)
#   - core-status.json, build_log.md, contextual-chips.json
#   - data/voice-metrics.jsonl
#   - src/.discord-pending-replies.json, src/Sutando/SutandoApp
#   - ~/.claude/projects/ (other projects' session transcripts)
#   - ~/.claude/skills/ (installed per-node)
#
# Conflict handling:
#   rsync is one-way per invocation. To simulate two-way sync we run it
#   twice: Studio→Mini then Mini→Studio (no --delete on either leg, so
#   the union of files lands on both sides). Concurrent edits to the same
#   file get last-writer-wins. For the scope we chose (mostly append-only
#   memory + notes), conflicts are rare. If conflicts become an issue we
#   can add a `--backup-dir` to preserve losers.
#
# Usage:
#   bash skills/cross-node-sync/scripts/setup-rsync-sync.sh            # run sync
#   bash skills/cross-node-sync/scripts/setup-rsync-sync.sh --dry-run  # preview
#   bash skills/cross-node-sync/scripts/setup-rsync-sync.sh --setup    # keypair + auth setup guide
#   bash skills/cross-node-sync/scripts/setup-rsync-sync.sh --help

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$REPO_ROOT"

# --- Config ------------------------------------------------------------------
# Peer host: set via SUTANDO_SYNC_PEER env var (e.g. "susan@macbook.local")
# so the script is portable between Studio and Mini without code changes.
PEER="${SUTANDO_SYNC_PEER:-}"

MEM_LOCAL="$HOME/.claude/projects/-Users-xueqingliu-Documents-sutando-sutando/memory/"
NOTES_LOCAL="$REPO_ROOT/notes/"

# Peer-side paths — set via env because Claude Code's project dirs encode the
# full absolute repo path, which differs between hosts (e.g. /Users/xueqingliu
# on Studio vs /Users/xliu/Documents/xqq/... on MBP). Operator pokes these
# into .env once per peer pairing.
#   SUTANDO_PEER_MEM_DIR   — absolute peer path ending in "memory/"
#   SUTANDO_PEER_NOTES_DIR — absolute peer path ending in "notes/"
MEM_PEER="${SUTANDO_PEER_MEM_DIR:-}"
NOTES_PEER="${SUTANDO_PEER_NOTES_DIR:-}"

# Common rsync flags:
#   -a         archive (preserves modtime/perms — critical for conflict semantics)
#   -z         compress in transit (LAN so mostly wasted, but helps if we ever
#              hit a bad wifi link)
#   --update   skip files newer on receiver (reduces last-writer-wins surprises)
#   --exclude  per-node exclusions
RSYNC_FLAGS=(-az --update
    --exclude '.DS_Store' --exclude '*.swp' --exclude '*.swo'
    --exclude '.stversions' --exclude '.stfolder'
    --exclude 'MEMORY.md' --exclude 'INDEX.md')
# Index-manifest files (MEMORY.md, INDEX.md) cannot use mtime-wins — one side's
# newer-but-shorter version clobbers the other's longer listing. Both Studio
# and MBP hit this on 2026-04-17 (74 files on disk, only 19 linked). Workaround:
# exclude from rsync, regenerate locally from file frontmatter after each sync.
# v2 should add a pre-sync hook that merges manifests by union rather than mtime.

# --- Arg parsing -------------------------------------------------------------
DRY_RUN=0
MODE="sync"
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n) DRY_RUN=1 ;;
        --setup) MODE="setup" ;;
        -h|--help)
            sed -n '2,38p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "unknown arg: $arg — use --dry-run / --setup / --help" >&2; exit 2 ;;
    esac
done

say()  { echo "$@"; }
# In dry-run mode log the command so the operator sees exactly what would run
# (rsync still executes, because its own --dry-run flag gives real preview
# output). In live mode just execute. Failures are tolerated so a broken
# memory-sync doesn't skip the subsequent notes-sync — each rsync is
# independent and worth trying.
run() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "[DRY] would run: $*"
    fi
    "$@" || true
}

# --- --setup mode: print SSH keypair + authorize guide ------------------------
if [ "$MODE" = "setup" ]; then
    say "━━━ SSH key setup for rsync cross-node sync ━━━"
    say ""
    KEY="$HOME/.ssh/id_ed25519"
    if [ -f "$KEY.pub" ]; then
        say "✓ SSH key already exists at $KEY"
        say "  Public key (paste into peer's ~/.ssh/authorized_keys):"
        say ""
        cat "$KEY.pub" | sed 's/^/    /'
    else
        say "No ed25519 key found at $KEY."
        say "Generate with:"
        say "    ssh-keygen -t ed25519 -f $KEY -N ''"
        say "Then paste $KEY.pub into peer's ~/.ssh/authorized_keys"
    fi
    say ""
    say "Or use ssh-copy-id (easier):"
    say "    ssh-copy-id susan@<peer-hostname>.local"
    say ""
    say "Test with:"
    say '    ssh $SUTANDO_SYNC_PEER true  # expects exit 0, no prompt'
    exit 0
fi

# --- Sync mode ---------------------------------------------------------------
if [ -z "$PEER" ]; then
    say "ERROR: SUTANDO_SYNC_PEER not set. Example:" >&2
    say '    export SUTANDO_SYNC_PEER="susan@MacBook-Pro.local"' >&2
    say "Then: bash skills/cross-node-sync/scripts/setup-rsync-sync.sh" >&2
    exit 1
fi
if [ -z "$MEM_PEER" ] || [ -z "$NOTES_PEER" ]; then
    say "ERROR: peer paths not set. Example:" >&2
    say '    export SUTANDO_PEER_MEM_DIR=$HOME/.claude/projects/-Users-xliu-.../memory/' >&2
    say '    export SUTANDO_PEER_NOTES_DIR=$HOME/.../sutando/notes/' >&2
    say "(check peer with: ssh \$SUTANDO_SYNC_PEER 'ls -d \$HOME/.claude/projects/*/memory/')" >&2
    exit 1
fi

if [ "$DRY_RUN" = "1" ]; then
    say "━━━ DRY-RUN MODE — no files will be transferred ━━━"
    say ""
fi

# 1) Peer reachability
if [ "$DRY_RUN" = "0" ]; then
    say "Testing SSH to $PEER ..."
    if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "$PEER" true 2>/dev/null; then
        say "ERROR: cannot SSH to $PEER. Run: bash $0 --setup" >&2
        exit 1
    fi
    say "  ✓ SSH ok"
fi

# 2) Memory sync (both directions, --update so newer files on either side win)
say ""
say "Syncing memory/ ..."
DRYFLAG=()
[ "$DRY_RUN" = "1" ] && DRYFLAG=(--dry-run -v)
run rsync "${RSYNC_FLAGS[@]}" ${DRYFLAG[@]+"${DRYFLAG[@]}"} "$MEM_LOCAL" "$PEER:$MEM_PEER"
run rsync "${RSYNC_FLAGS[@]}" ${DRYFLAG[@]+"${DRYFLAG[@]}"} "$PEER:$MEM_PEER" "$MEM_LOCAL"

# 3) Notes sync (both directions)
say ""
say "Syncing notes/ ..."
run rsync "${RSYNC_FLAGS[@]}" ${DRYFLAG[@]+"${DRYFLAG[@]}"} "$NOTES_LOCAL" "$PEER:$NOTES_PEER"
run rsync "${RSYNC_FLAGS[@]}" ${DRYFLAG[@]+"${DRYFLAG[@]}"} "$PEER:$NOTES_PEER" "$NOTES_LOCAL"

say ""
if [ "$DRY_RUN" = "1" ]; then
    say "━━━ DRY-RUN complete ━━━"
else
    say "━━━ Sync complete ━━━"
fi
