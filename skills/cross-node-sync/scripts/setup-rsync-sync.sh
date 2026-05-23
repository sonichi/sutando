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
#   - data/  (entire dir is per-node — conversation.sqlite cannot be safely
#     rsynced while writers are live (#1040); voice-metrics.jsonl +
#     call-metrics.jsonl are frozen archives with no new writes since #603;
#     latency.json / scanned-calls.json are per-node metrics that don't
#     need consolidation. Nothing remaining in data/ benefits from
#     cross-node sync.)
#   - .env, .env.* (different secrets per node)
#   - core-status.json, build_log.md, contextual-chips.json
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

# Load .env from the sutando workspace early — non-interactive shells (cron,
# launchd) don't run user shell startup, so SUTANDO_SYNC_PEER / SUTANDO_PEER_*
# wouldn't otherwise be visible even when set in .env (same root cause as #714).
if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$REPO_ROOT/.env"
    set +a
fi

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
# Notes live under $SUTANDO_WORKSPACE per the workspace contract (CLAUDE.md
# "Workspace contract"); fall back to $REPO_ROOT for pre-contract installs.
# (data/ is intentionally NOT defined here — the whole data/ dir is per-node;
# see header "What does NOT sync" for why.)
if [ -n "${SUTANDO_WORKSPACE:-}" ]; then
	NOTES_LOCAL="$SUTANDO_WORKSPACE/notes/"
else
	NOTES_LOCAL="$REPO_ROOT/notes/"
fi
ASSETS_LOCAL="$REPO_ROOT/assets/"

# Peer-side paths — default to the same literal paths as local so users only
# need to set SUTANDO_SYNC_PEER (per owner's 2026-04-17 simplification: "only
# sync peer is necessary, just use the same directory for both machines").
# If your peer's sutando repo or memory dir lives at a different path, override
# via SUTANDO_PEER_MEM_DIR / SUTANDO_PEER_NOTES_DIR — otherwise leave unset.
MEM_PEER="${SUTANDO_PEER_MEM_DIR:-$MEM_LOCAL}"
NOTES_PEER="${SUTANDO_PEER_NOTES_DIR:-$NOTES_LOCAL}"
ASSETS_PEER="${SUTANDO_PEER_ASSETS_DIR:-$ASSETS_LOCAL}"

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

# NOTE — `data/` is intentionally NOT synced (see header "What does NOT
# sync" for the rationale). conversation.sqlite cannot be safely rsynced
# while writers are live (#1040: `integrity_check` damage signature
# "2nd reference to page" / "rowid out of order"); the rest of data/ is
# either frozen jsonl archives (#603 dropped the writers) or per-node
# metrics. If cross-node consolidation of conversation.sqlite ever
# becomes necessary, the right shape is `sqlite3 .backup`-snapshot
# based sync (SQLite-safe consistent copy of a live db) plus row-level
# reconciliation — not naive rsync.

# Rebuild MEMORY.md from per-file YAML frontmatter. We exclude MEMORY.md from
# the rsync pass (mtime-wins truncates the index — see comment near MEM rsync
# above and issue #712), so each node regenerates its own index after every
# sync. Falls back to a friendly skip if the memory dir doesn't exist.
if [ "$DRY_RUN" != "1" ]; then
    if command -v python3 >/dev/null 2>&1; then
        say "Regenerating MEMORY.md from frontmatter..."
        python3 "$REPO_ROOT/skills/cross-node-sync/scripts/regenerate-memory-index.py" \
            2>&1 | sed 's/^/  /' || say "  (regen failed; MEMORY.md may be stale until next pass)"
    else
        say "python3 not on PATH — skipping MEMORY.md regen"
    fi
fi

if [ "$DRY_RUN" = "1" ]; then
    say "━━━ DRY-RUN complete ━━━"
else
    say "━━━ Sync complete ━━━"
fi
