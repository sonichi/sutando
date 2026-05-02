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
#   - <repo>/assets/            (owner personal runtime assets — e.g.
#     gitignored stand-avatar.png. `/assets` is entirely gitignored so
#     git never sees these, and rsync keeps the nodes converged.)
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

# Derive Claude Code's per-project memory dir from REPO_ROOT.
# Claude Code stores memory at ~/.claude/projects/<slug>/memory/ where <slug>
# is the absolute path with '/' replaced by '-'. This makes MEM_LOCAL portable
# across nodes whose project checkouts live at different paths (Studio:
# /Users/xueqingliu/Documents/sutando/sutando vs Maddy:
# /Users/xliu/Documents/xqq/.../sutando-agent-sonichi-test2/sutando).
# Override with SUTANDO_MEM_LOCAL_DIR if the convention changes.
MEM_LOCAL="${SUTANDO_MEM_LOCAL_DIR:-$HOME/.claude/projects/$(echo "$REPO_ROOT" | tr '/' '-')/memory/}"
NOTES_LOCAL="$REPO_ROOT/notes/"
DATA_LOCAL="$REPO_ROOT/data/"
ASSETS_LOCAL="$REPO_ROOT/assets/"

# Peer-side paths — default to the same literal paths as local so users only
# need to set SUTANDO_SYNC_PEER (per owner's 2026-04-17 simplification: "only
# sync peer is necessary, just use the same directory for both machines").
# If your peer's sutando repo or memory dir lives at a different path, override
# via SUTANDO_PEER_MEM_DIR / SUTANDO_PEER_NOTES_DIR / SUTANDO_PEER_DATA_DIR —
# otherwise leave unset.
MEM_PEER="${SUTANDO_PEER_MEM_DIR:-$MEM_LOCAL}"
NOTES_PEER="${SUTANDO_PEER_NOTES_DIR:-$NOTES_LOCAL}"
ASSETS_PEER="${SUTANDO_PEER_ASSETS_DIR:-$ASSETS_LOCAL}"
# Data dir peer path: derive from NOTES_PEER (repo/notes/ → repo/data/) if no
# explicit override. Covers call-metrics.jsonl, voice-metrics.jsonl,
# subtitle-metrics.jsonl, latency.json, scanned-calls.json, etc. Owner's
# 2026-04-17 direction: "data/* is shared, not just voice-metrics".
DATA_PEER="${SUTANDO_PEER_DATA_DIR:-${NOTES_PEER%/notes/}/data/}"

# Common rsync flags:
#   -a         archive (preserves modtime/perms — critical for conflict semantics)
#   -z         compress in transit (LAN so mostly wasted, but helps if we ever
#              hit a bad wifi link)
#   --update   skip files newer on receiver (reduces last-writer-wins surprises)
#   --exclude  per-node exclusions
RSYNC_FLAGS=(-az --update
    --exclude '.DS_Store' --exclude '*.swp' --exclude '*.swo'
    --exclude '.stversions' --exclude '.stfolder'
    --exclude 'MEMORY.md' --exclude 'INDEX.md'
    --exclude 'self_identity.md'
    --exclude 'core-status.json')
# core-status.json is per-node proactive-loop state that occasionally lands
# in the memory dir (voice-agent + proactive-loop both write to it and
# sometimes pick up wrong CWD). Excluding it here so a stale snapshot from
# one node doesn't clobber the other's live status. Seen during PR #429
# testing on 2026-04-17 — Studio had a 24-hour-old copy inside memory/.
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
# MEM_PEER / NOTES_PEER default to local paths now; no hard-error path needed.
# (Previously required SUTANDO_PEER_* env vars; removed per owner's 2026-04-17
# simplification. If your peer's dir layout differs, set them explicitly.)

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

# 3b) Assets sync (both directions) — converges owner personal runtime
# assets (e.g. stand-avatar.png) across nodes. /assets is entirely
# gitignored; rsync is the only transport.
say ""
say "Syncing assets/ ..."
run rsync "${RSYNC_FLAGS[@]}" ${DRYFLAG[@]+"${DRYFLAG[@]}"} "$ASSETS_LOCAL" "$PEER:$ASSETS_PEER"
run rsync "${RSYNC_FLAGS[@]}" ${DRYFLAG[@]+"${DRYFLAG[@]}"} "$PEER:$ASSETS_PEER" "$ASSETS_LOCAL"

# 4) Data dir sync — covers all data/* files (call-metrics.jsonl,
# voice-metrics.jsonl, subtitle-metrics.jsonl, latency.json,
# scanned-calls.json, latency-tracker.py, etc.). Each jsonl file needs
# union merge (mtime-wins would drop entries written between syncs on
# the other node); non-jsonl files fall back to rsync --update.
#
# Strategy:
#   1. rsync whole peer data/ dir into a staging subdir `.peer-staging/`
#   2. for each .jsonl in staging, run merge-voice-metrics.sh against
#      the same-named local file (generic JSON-line merge, not
#      voice-metrics-specific — dedup on sessionId+timestamp).
#   3. rsync non-.jsonl files from staging to local with --update.
#   4. rsync local data/ back to peer (non-merge files).
#   5. push locally-merged .jsonl files back to peer explicitly.
#   6. clean up .peer-staging/.
say ""
say "Syncing data/ ..."
if [ "$DRY_RUN" = "0" ]; then
    mkdir -p "$DATA_LOCAL"
    STAGING="$DATA_LOCAL.peer-staging/"
    rm -rf "$STAGING"
    mkdir -p "$STAGING"
    # Pull peer data/ into staging (tolerant of missing peer dir)
    run rsync -az --exclude 'radar-topics.example.json' --exclude '.peer-staging' \
        "$PEER:$DATA_PEER" "$STAGING"
    # Merge each staging .jsonl into local; non-jsonl files copied via --update
    for pf in "$STAGING"*.jsonl; do
        [ -f "$pf" ] || continue
        fn="$(basename "$pf")"
        bash "$REPO_ROOT/skills/cross-node-sync/scripts/merge-voice-metrics.sh" \
            "$DATA_LOCAL$fn" "$pf" || true
    done
    # Copy non-jsonl staging files into local with --update (mtime-wins)
    run rsync -az --update --exclude '*.jsonl' "$STAGING" "$DATA_LOCAL"
    rm -rf "$STAGING"
    # Push merged local data/ back to peer (no --delete so peer-only files survive)
    run rsync -az --update --exclude 'radar-topics.example.json' --exclude '.peer-staging' \
        "$DATA_LOCAL" "$PEER:$DATA_PEER"
else
    say "[DRY] would pull $PEER:$DATA_PEER into .peer-staging/, merge each .jsonl, rsync non-jsonl, push back"
fi

say ""
if [ "$DRY_RUN" = "1" ]; then
    say "━━━ DRY-RUN complete ━━━"
else
    say "━━━ Sync complete ━━━"
fi
