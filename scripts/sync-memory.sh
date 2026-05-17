#!/bin/bash
# Sync Claude Code memory + Sutando notes between machines via a private
# git repo of your choosing.
#
# Setup (one-time per machine):
#   1. Create a private GitHub repo (any name, e.g. your-org/your-memory).
#   2. Add to .env in this sutando checkout:
#        SUTANDO_MEMORY_REPO=https://github.com/your-org/your-memory.git
#   3. Run once: bash scripts/sync-memory.sh
#      - First run auto-clones the repo to ~/.sutando-memory-sync/.
#   4. Add a cron entry calling this script every 10-30 min.
#
# Each machine in the fleet repeats the above. Commits are signed with the
# machine hostname so writes are attributable. Conflict model is
# rsync-mtime-wins; append-only files (build_log.md, MEMORY.md index) are
# safest. See docs/memory-sync.md for the architecture overview.
#
# Env vars (all optional except SUTANDO_MEMORY_REPO):
#   SUTANDO_MEMORY_REPO     — git URL of your private memory repo (REQUIRED)
#   SUTANDO_WORKSPACE       — public sutando checkout. Default: ~/Desktop/sutando
#   SUTANDO_MEMORY_SYNC_DIR — local clone path. Default: ~/.sutando-memory-sync
#
# Run: bash scripts/sync-memory.sh

# If SUTANDO_MEMORY_SYNC_DIR is not set, try to auto-detect: if the script
# lives inside an existing ~/.sutando-memory-sync/scripts/ checkout, use
# that. Otherwise default to ~/.sutando-memory-sync/. This lets the script
# work both when bundled in the public sutando repo AND when copied into
# the private sync clone.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_PARENT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load .env from the sutando workspace early — non-interactive shells (cron,
# launchd) don't run user shell startup, so SUTANDO_WORKSPACE / SUTANDO_MEMORY_REPO
# wouldn't otherwise be visible even when set in .env. Without this the script
# exits 0 silently with "workspace not found" or "MEMORY_REPO not set" (issue #714).
if [ -f "$SCRIPT_PARENT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$SCRIPT_PARENT/.env"
    set +a
fi

if [ -n "$SUTANDO_MEMORY_SYNC_DIR" ]; then
    SYNC_DIR="$SUTANDO_MEMORY_SYNC_DIR"
elif [ "$(basename "$SCRIPT_PARENT")" = ".sutando-memory-sync" ]; then
    SYNC_DIR="$SCRIPT_PARENT"
else
    SYNC_DIR="$HOME/.sutando-memory-sync"
fi
REPO_DIR="${SUTANDO_WORKSPACE:-$HOME/Desktop/sutando}"
if [ ! -d "$REPO_DIR" ]; then
    echo "sync-memory: workspace not found at $REPO_DIR; set SUTANDO_WORKSPACE or clone sutando to ~/Desktop/sutando." >&2
    exit 0
fi
MEMORY_DIR="$HOME/.claude/projects/$(echo "$REPO_DIR" | sed 's|/|-|g')/memory"
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

# --- Assert on main before doing any sync work (restored from PR #504, dropped by PR #511) ---
# If the sync repo has drifted to a feature/test branch (e.g. after a manual
# `git checkout`), pull-rebase + commit + push will operate on the wrong
# branch, silently stop propagating to origin/main, and leave both nodes
# quietly diverging. Hit live 2026-04-21 pass 874. Detect and self-heal.
CURRENT_BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "")
if [ "$CURRENT_BRANCH" != "main" ]; then
    log "sync repo on non-main branch '$CURRENT_BRANCH' — switching to main"
    echo "sync-memory: sync repo was on '$CURRENT_BRANCH', switching to main."
    if ! git checkout main 2>/dev/null; then
        log "Failed to checkout main in sync repo — manual intervention needed"
        echo "sync-memory: could not switch to main in $SYNC_DIR — aborting sync."
        exit 1
    fi
fi

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

# --- Merge by mtime + content: copy only if source is newer AND content differs ---
# Content check prevents the linter-touches-mtime clobber: the auto-memory linter
# bumps mtime without changing content, which under pure mtime-wins made
# peer's stale content propagate back through sync. Adding `cmp -s` as a guard
# turns a content-stable mtime bump into a no-op. Linter-idempotency fix.
copy_if_newer() {
    local src="$1" dst="$2"
    # If dst exists and content identical, nothing to do (content-stable mtime bump).
    if [ -e "$dst" ] && cmp -s "$src" "$dst"; then
        return 1
    fi
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
SYNC_EXCLUDES="$SYNC_DIR/sync-excludes.txt"
RSYNC_EXCLUDE_ARGS=()
if [ -f "$SYNC_EXCLUDES" ]; then
    RSYNC_EXCLUDE_ARGS+=(--exclude-from="$SYNC_EXCLUDES")
fi

# notes/ bidirectional rsync removed 2026-05-11: both nodes now symlink
# `<repo>/notes` → `~/.sutando-memory-sync/notes/`. Edits land directly in
# the private repo; git push/pull is the canonical cross-node mechanism.
# Pre-talk excludes still get machine-specific backup below (see SYNC_EXCLUDES block).

# presenter-mode.sentinel: cross-node mute for talk windows (restored from PR #503,
# dropped by PR #511). Opt-in single-file sync — rest of state/ stays per-node.
PRESENTER_SENTINEL="$REPO_DIR/state/presenter-mode.sentinel"
if [ -f "$PRESENTER_SENTINEL" ]; then
    mkdir -p state
    copy_if_newer "$PRESENTER_SENTINEL" "state/presenter-mode.sentinel" || true
fi

# --- Machine-specific push (one-way: this machine → machine-<hostname>/) ---
# Backs up personal / machine-local files that aren't appropriate for
# bidirectional sync (e.g. voice-context.txt is ICLR-tuned on MacBook; Mini
# has its own). Purely for disaster recovery: if this Mac dies, we can
# `git clone sutando-memory && cp -r machine-<hostname>/* ~/Desktop/sutando/`.
# Other machines' machine-<other>/ dirs are read-only from this machine's
# POV — NO pull-back in the sync → local section below.
HOST="$(hostname | sed 's/\..*//')"
MACHINE_DIR="machine-$HOST"
mkdir -p "$MACHINE_DIR/skills" "$MACHINE_DIR/data"

# Individual personal files from the public-repo. Most live at repo root;
# stand-avatar.png lives under assets/. Each entry is "src-relative-to-REPO".
# basename() of dst keeps the machine dir flat regardless of source path —
# matches the layout util_paths / personalPath() expects.
MACHINE_FILES=(
    voice-context.txt
    build_log.md
    PERSONAL_CLAUDE.md
    stand-identity.json
    assets/stand-avatar.png
    tab-aliases.json
)
for f in "${MACHINE_FILES[@]}"; do
    src="$REPO_DIR/$f"
    [ -f "$src" ] || continue
    copy_if_newer "$src" "$MACHINE_DIR/$(basename "$f")"
done

# Personal cron schedule (nested path)
if [ -f "$REPO_DIR/skills/schedule-crons/crons.json" ]; then
    copy_if_newer "$REPO_DIR/skills/schedule-crons/crons.json" \
        "$MACHINE_DIR/crons.json"
fi

# Personal skill dirs (gitignored `skills/personal-*/`) — one rsync PER skill
# dir to preserve per-skill subdirectories. A flat union (rsync with multiple
# sources to one dest) would clobber same-named files (manifest.json, README.md)
# across skills.
for skill_dir in "$REPO_DIR"/skills/personal-*/; do
    [ -d "$skill_dir" ] || continue
    skill_name="$(basename "$skill_dir")"
    mkdir -p "$MACHINE_DIR/skills/$skill_name"
    rsync -a --update --checksum \
        --exclude='node_modules' --exclude='.venv' --exclude='__pycache__' \
        --exclude='*.pyc' --exclude='.DS_Store' \
        "$skill_dir" "$MACHINE_DIR/skills/$skill_name/" 2>/dev/null || true
done

# Private operational data dir (excluding example files + known binaries)
if [ -d "$REPO_DIR/data" ]; then
    rsync -a --update --checksum \
        --exclude='*.example.json' --exclude='.DS_Store' \
        "$REPO_DIR/data/" "$MACHINE_DIR/data/" 2>/dev/null || true
fi

# Notes listed in sync-excludes.txt (shared-notes excluded pre-talk) still get
# backed up here in the machine-specific dir. Text-only filter: only .md / .html
# / .sh — media/ entry in sync-excludes is deliberately NOT pulled here
# (videos are large and already live under notes/media/ via LFS).
if [ -f "$SYNC_EXCLUDES" ] && [ -d "$NOTES_DIR" ]; then
    mkdir -p "$MACHINE_DIR/notes"
    grep -vE '^\s*(#|$)|^media/|^.*\.(png|jpg|jpeg|gif|mp4|mov|pdf|zip)$' "$SYNC_EXCLUDES" | \
        rsync -a --update --checksum --files-from=- \
            "$NOTES_DIR/" "$MACHINE_DIR/notes/" 2>/dev/null || true
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
# notes/ pull-direction rsync removed 2026-05-11 (see push-direction note above).
# Both nodes' `<repo>/notes` is now a symlink into this repo; no copy needed.

# Reverse: pull presenter-mode.sentinel from sync (other node flipped it on).
if [ -f "state/presenter-mode.sentinel" ]; then
    mkdir -p "$REPO_DIR/state"
    copy_if_newer "state/presenter-mode.sentinel" "$PRESENTER_SENTINEL" || true
fi

NOTES_COUNT=$(find notes -type f 2>/dev/null | wc -l | tr -d ' ')
MEMORY_COUNT=$(ls memory/*.md 2>/dev/null | wc -l | tr -d ' ')
log "Sync complete: $MEMORY_COUNT memory, $NOTES_COUNT notes"
echo "Sync complete. Memory: $MEMORY_COUNT files, Notes: $NOTES_COUNT files."
