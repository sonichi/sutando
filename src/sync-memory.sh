#!/bin/bash
# Sync memory + notes between machines via private git repo
# Run: bash src/sync-memory.sh
# Add to cron for auto-sync: */10 * * * * bash ~/Desktop/sutando/src/sync-memory.sh
#
# Set SUTANDO_MEMORY_REPO in .env to your private repo URL, e.g.:
#   SUTANDO_MEMORY_REPO=git@github.com:youruser/sutando-memory.git
# If unset, sync is skipped (script exits cleanly).

SYNC_DIR="$HOME/.sutando-memory-sync"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MEMORY_DIR="$HOME/.claude/projects/-Users-$(whoami)-Desktop-sutando/memory"
NOTES_DIR="$REPO_DIR/notes"

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
    # Try to find it
    MEMORY_DIR=$(find "$HOME/.claude/projects" -name "memory" -type d 2>/dev/null | head -1)
fi

if [ ! -d "$SYNC_DIR" ]; then
    echo "Setting up sync repo from $SUTANDO_MEMORY_REPO..."
    git clone "$SUTANDO_MEMORY_REPO" "$SYNC_DIR"
fi

cd "$SYNC_DIR" || exit 1

# Pull latest from remote
git pull --rebase 2>/dev/null

# Copy local → sync dir
mkdir -p memory notes
if [ -d "$MEMORY_DIR" ]; then
    cp "$MEMORY_DIR"/*.md memory/ 2>/dev/null
fi
if [ -d "$NOTES_DIR" ]; then
    # Recursive sync — includes subdirs (meetings/, archive/) and non-md files (PNGs, etc.)
    rsync -a --delete --exclude='.gitkeep' "$NOTES_DIR/" notes/ 2>/dev/null
fi

# Check for changes
if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
    echo "No changes to sync."
else
    git add -A
    git commit -m "Sync $(hostname) $(date +%Y-%m-%dT%H:%M)" 2>/dev/null
    git push 2>/dev/null
    echo "Pushed changes from $(hostname)."
fi

# Copy sync dir → local (pull remote changes)
if [ -d "$MEMORY_DIR" ]; then
    cp memory/*.md "$MEMORY_DIR/" 2>/dev/null
fi
if [ -d "$NOTES_DIR" ]; then
    rsync -a --exclude='.gitkeep' notes/ "$NOTES_DIR/" 2>/dev/null
fi

echo "Sync complete. Memory: $(ls memory/*.md 2>/dev/null | wc -l | tr -d ' ') files, Notes: $(find notes -type f 2>/dev/null | wc -l | tr -d ' ') files."
