#!/bin/bash
# Sync memory + notes between machines via private git repo
# Run: bash src/sync-memory.sh
# Add to cron for auto-sync: */30 * * * * bash ~/Desktop/sutando/src/sync-memory.sh
#
# Set SUTANDO_MEMORY_REPO in .env to your private repo URL, e.g.:
#   SUTANDO_MEMORY_REPO=git@github.com:youruser/sutando-memory.git
# If unset, sync is skipped (script exits cleanly).

SYNC_DIR="$HOME/.sutando-memory-sync"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MEMORY_DIR="$HOME/.claude/projects/-Users-$(whoami)-Desktop-sutando/memory"
NOTES_DIR="$REPO_DIR/notes"
LOG="/tmp/sync-memory.log"
LOCK_DIR="/tmp/sync-memory.lock.d"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# --- Locking via atomic mkdir (POSIX, no flock dependency) ---
# Stale lock cleanup: if lock dir is older than 10 minutes, assume crashed and remove
if [ -d "$LOCK_DIR" ]; then
    if find "$LOCK_DIR" -maxdepth 0 -mmin +10 2>/dev/null | grep -q .; then
        log "Stale lock removed (older than 10 min)"
        rm -rf "$LOCK_DIR"
    fi
fi
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "Another sync already in progress, exiting."
    echo "sync-memory: another instance is running, skipping."
    exit 0
fi
trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM

# Load SUTANDO_MEMORY_REPO from .env if not in shell env
if [ -z "$SUTANDO_MEMORY_REPO" ] && [ -f "$REPO_DIR/.env" ]; then
    SUTANDO_MEMORY_REPO=$(grep -E '^SUTANDO_MEMORY_REPO=' "$REPO_DIR/.env" | cut -d= -f2- | tr -d '"' | tr -d "'")
fi

if [ -z "$SUTANDO_MEMORY_REPO" ]; then
    echo "sync-memory: SUTANDO_MEMORY_REPO not set in .env, skipping sync."
    exit 0
fi

# Auto-detect memory dir (may vary by machine)
if [ ! -d "$MEMORY_DIR" ]; then
    MEMORY_DIR=$(find "$HOME/.claude/projects" -name "memory" -type d 2>/dev/null | head -1)
fi

if [ ! -d "$SYNC_DIR" ]; then
    log "First-run clone from $SUTANDO_MEMORY_REPO"
    echo "Setting up sync repo from $SUTANDO_MEMORY_REPO..."
    git clone --depth=10 "$SUTANDO_MEMORY_REPO" "$SYNC_DIR" 2>&1 | tee -a "$LOG"
fi

cd "$SYNC_DIR" || { log "Failed to cd $SYNC_DIR"; exit 1; }

# --- Pull latest, detect conflicts ---
PULL_OUT=$(git pull --rebase 2>&1)
PULL_RC=$?
if [ $PULL_RC -ne 0 ]; then
    if echo "$PULL_OUT" | grep -q "CONFLICT\|conflict"; then
        log "REBASE CONFLICT — saving local versions and aborting rebase"
        # Save conflicting files for inspection
        CONFLICT_DIR="$REPO_DIR/notes/.conflicts-$(hostname)-$(date +%Y%m%d-%H%M%S)"
        mkdir -p "$CONFLICT_DIR"
        git diff --name-only --diff-filter=U > "$CONFLICT_DIR/conflicting-files.txt" 2>/dev/null
        git rebase --abort 2>/dev/null
        log "Conflict file list saved to $CONFLICT_DIR/conflicting-files.txt"
        echo "sync-memory: rebase conflict — see $CONFLICT_DIR for the file list. Resolve manually."
        exit 1
    fi
    log "Pull failed (non-conflict): $PULL_OUT"
fi

mkdir -p memory notes

# --- Merge by mtime: only copy a file if source is newer than dest ---
copy_if_newer() {
    local src="$1" dst="$2"
    if [ ! -e "$dst" ] || [ "$src" -nt "$dst" ]; then
        cp "$src" "$dst"
        return 0
    fi
    return 1
}

# Local → sync (push direction)
COPIED_TO_SYNC=0
if [ -d "$MEMORY_DIR" ]; then
    for f in "$MEMORY_DIR"/*.md; do
        [ -f "$f" ] || continue
        if copy_if_newer "$f" "memory/$(basename "$f")"; then
            COPIED_TO_SYNC=$((COPIED_TO_SYNC + 1))
        fi
    done
fi
if [ -d "$NOTES_DIR" ]; then
    # rsync with --update (only newer src), recursive, exclude .gitkeep
    rsync -a --update --exclude='.gitkeep' "$NOTES_DIR/" notes/ 2>/dev/null
fi

# --- Commit and push if anything changed ---
if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
    log "Nothing to push"
    echo "No changes to sync."
else
    git add -A
    git commit -m "Sync $(hostname) $(date +%Y-%m-%dT%H:%M)" 2>&1 | tee -a "$LOG" >/dev/null
    if git push 2>&1 | tee -a "$LOG" >/dev/null; then
        log "Pushed changes"
        echo "Pushed changes from $(hostname)."
    else
        log "Push failed"
    fi
fi

# Sync → local (pull direction): also mtime-based
if [ -d "$MEMORY_DIR" ]; then
    for f in memory/*.md; do
        [ -f "$f" ] || continue
        copy_if_newer "$f" "$MEMORY_DIR/$(basename "$f")"
    done
fi
if [ -d "$NOTES_DIR" ]; then
    rsync -a --update --exclude='.gitkeep' notes/ "$NOTES_DIR/" 2>/dev/null
fi

NOTES_COUNT=$(find notes -type f 2>/dev/null | wc -l | tr -d ' ')
MEMORY_COUNT=$(ls memory/*.md 2>/dev/null | wc -l | tr -d ' ')
log "Sync complete: $MEMORY_COUNT memory, $NOTES_COUNT notes"
echo "Sync complete. Memory: $MEMORY_COUNT files, Notes: $NOTES_COUNT files."
