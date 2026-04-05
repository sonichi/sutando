#!/bin/bash
# Sync memory + notes between machines via private git repo
# Run: bash src/sync-memory.sh
# Add to cron for auto-sync: */10 * * * * bash ~/Desktop/sutando/src/sync-memory.sh

SYNC_DIR="$HOME/.sutando-memory-sync"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MEMORY_DIR="$HOME/.claude/projects/-Users-$(whoami)-Desktop-sutando/memory"
NOTES_DIR="$REPO_DIR/notes"

# Auto-detect memory dir (may vary by machine)
if [ ! -d "$MEMORY_DIR" ]; then
    # Try to find it
    MEMORY_DIR=$(find "$HOME/.claude/projects" -name "memory" -type d 2>/dev/null | head -1)
fi

if [ ! -d "$SYNC_DIR" ]; then
    echo "Setting up sync repo..."
    git clone https://github.com/sonichi/sutando-memory.git "$SYNC_DIR"
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
    cp "$NOTES_DIR"/*.md notes/ 2>/dev/null
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
    cp notes/*.md "$NOTES_DIR/" 2>/dev/null
fi

echo "Sync complete. Memory: $(ls memory/*.md 2>/dev/null | wc -l | tr -d ' ') files, Notes: $(ls notes/*.md 2>/dev/null | wc -l | tr -d ' ') files."
