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
#   SUTANDO_REPO_DIR        — public sutando checkout. Auto-detected from the
#                             script's parent dir when invoked as
#                             `<repo>/scripts/sync-memory.sh` (zero-config for
#                             the common case, regardless of clone location).
#                             Falls back to ~/Desktop/sutando only when the
#                             auto-detect signature doesn't match (e.g. invoked
#                             from the memory-sync-dir copy of the script).
#   SUTANDO_WORKSPACE       — local workspace dir (per CLAUDE.md workspace
#                             contract). Default: ~/.sutando/workspace
#   SUTANDO_MEMORY_SYNC_DIR — local clone path. Default: ~/.sutando/memory-sync
#                             (was ~/.sutando-memory-sync before #762's
#                             companion PR; one-time auto-migration below)
#
# Run: bash scripts/sync-memory.sh

# If SUTANDO_MEMORY_SYNC_DIR is not set, try to auto-detect: if the script
# lives inside an existing memory-sync clone, use that. Otherwise default
# to ~/.sutando/memory-sync/. The auto-detect handles both the new convention
# (~/.sutando/memory-sync/) and the legacy convention (~/.sutando-memory-sync/)
# so a sync clone with this script copied in keeps working through the move.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
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

# One-time migration from legacy default (~/.sutando-memory-sync/) to the
# new convention (~/.sutando/memory-sync/). Triggers only when the env var
# is unset (user hasn't pinned a path), the legacy dir exists, AND the new
# default doesn't yet exist — so env-pinned installs and fresh installs
# both skip this. Idempotent: second run finds the new path populated and
# the migration becomes a no-op. Per owner directive (2026-05-16): default
# changes should ship with automatic migration, not just docs telling users
# to mv manually.
__OLD_DEFAULT="$HOME/.sutando-memory-sync"
__NEW_DEFAULT="$HOME/.sutando/memory-sync"
__MIGRATED=0
if [ -z "${SUTANDO_MEMORY_SYNC_DIR:-}" ] && [ -d "$__OLD_DEFAULT" ] && [ ! -e "$__NEW_DEFAULT" ]; then
    mkdir -p "$(dirname "$__NEW_DEFAULT")"
    if mv "$__OLD_DEFAULT" "$__NEW_DEFAULT" 2>/dev/null; then
        echo "sync-memory: migrated $__OLD_DEFAULT -> $__NEW_DEFAULT (one-time)" >&2
        __MIGRATED=1
    fi
fi

# --- Orphan symlink scan (post-migration) ---
# After moving __OLD_DEFAULT, any pre-existing symlinks pointing at the legacy
# path are now broken (target moved out from under them). Common cases:
#   - ~/.sutando/workspace/notes  → ~/.sutando-memory-sync/notes  (PR #831 rollout)
#   - ~/.claude/skills/personal-* → ~/.sutando-memory-sync/skills/personal-*
#   - any user-created convenience symlinks under ~/
# The migration can't enumerate external symlinks, so this scan WARNs the user
# post-fact with a one-line re-point recipe. Scoped to common dirs to keep cost
# bounded — full ~/ scan is too slow on large homes.
if [ "$__MIGRATED" = "1" ]; then
    __SCAN_DIRS=("$HOME/.sutando" "$HOME/.claude/skills" "$HOME/.config")
    __ORPHAN_COUNT=0
    for __dir in "${__SCAN_DIRS[@]}"; do
        [ -d "$__dir" ] || continue
        while IFS= read -r __orphan; do
            [ -z "$__orphan" ] && continue
            __ORPHAN_COUNT=$((__ORPHAN_COUNT + 1))
            __target=$(readlink "$__orphan")
            __new_target="${__target/$__OLD_DEFAULT/$__NEW_DEFAULT}"
            if [ "$__ORPHAN_COUNT" = "1" ]; then
                echo "sync-memory: WARN — found orphan symlinks pointing at old path. Re-point with:" >&2
            fi
            echo "  rm $__orphan && ln -s $__new_target $__orphan" >&2
        done < <(find "$__dir" -type l -lname "${__OLD_DEFAULT}*" 2>/dev/null)
    done
    if [ "$__ORPHAN_COUNT" -gt 0 ]; then
        echo "sync-memory: WARN — $__ORPHAN_COUNT orphan symlink(s) total. Run the rm+ln pairs above to fix." >&2
    fi
fi

if [ -n "$SUTANDO_MEMORY_SYNC_DIR" ]; then
    SYNC_DIR="$SUTANDO_MEMORY_SYNC_DIR"
elif [ "$(basename "$SCRIPT_PARENT")" = "memory-sync" ] \
     && [ "$(basename "$(dirname "$SCRIPT_PARENT")")" = ".sutando" ]; then
    # New convention: script is inside ~/.sutando/memory-sync/scripts/
    SYNC_DIR="$SCRIPT_PARENT"
elif [ "$(basename "$SCRIPT_PARENT")" = ".sutando-memory-sync" ]; then
    # Legacy convention kept for backward compat — auto-migration above
    # should have moved this case to the new path, but if a user has
    # SUTANDO_MEMORY_SYNC_DIR pointing somewhere else and the script lives
    # inside the old layout, still honor it.
    SYNC_DIR="$SCRIPT_PARENT"
else
    SYNC_DIR="$__NEW_DEFAULT"
fi
# Public-repo path resolution:
#   1. $SUTANDO_REPO_DIR (explicit override)
#   2. $SCRIPT_PARENT if it carries a sutando-checkout signature
#      (CLAUDE.md + skills/ + .git/) — zero-config for the common case
#      of `bash <repo>/scripts/sync-memory.sh` regardless of clone location.
#   3. ~/Desktop/sutando as last-resort default for the memory-sync-dir-copy
#      invocation (where SCRIPT_PARENT is the sync-dir, not the repo).
#
# Why no SUTANDO_WORKSPACE fallback: SUTANDO_WORKSPACE is reserved by CLAUDE.md
# for the per-user workspace dir (~/.sutando/workspace/); using it as a
# REPO_DIR alias would silently pick the wrong path on CLAUDE.md-compliant hosts.
if [ -n "${SUTANDO_REPO_DIR:-}" ]; then
    REPO_DIR="$SUTANDO_REPO_DIR"
elif [ -f "$SCRIPT_PARENT/CLAUDE.md" ] && [ -d "$SCRIPT_PARENT/skills" ] && [ -d "$SCRIPT_PARENT/.git" ]; then
    REPO_DIR="$SCRIPT_PARENT"
else
    REPO_DIR="$HOME/Desktop/sutando"
fi
if [ ! -d "$REPO_DIR" ]; then
    echo "sync-memory: public repo not found at $REPO_DIR; set SUTANDO_REPO_DIR or invoke the script from <repo>/scripts/." >&2
    exit 0
fi
# Claude's per-project memory dir is keyed on the LAUNCH-CWD path, not on
# SUTANDO_WORKSPACE. Use SCRIPT_PARENT (this script's parent.parent = repo
# root where the user launched Claude) — that's the canonical key on any
# sane install. Prior implementation used REPO_DIR (= SUTANDO_WORKSPACE),
# which silently picked a non-existent key on env-set hosts and then fell
# back via `find … | head -1` to whichever sibling memory dir landed first
# (alphabetical). Bug silently skipped real memory writes for 5+ weeks
# before being caught. See docs/workspace-contract.md.
MEMORY_DIR="$HOME/.claude/projects/$(echo "$SCRIPT_PARENT" | sed 's|/|-|g')/memory"
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

# --- Workspace symlink bootstrap (issue #769 fix) ---
# PR #769 migrated `notes/` from `<repo>/notes/` to `$SUTANDO_WORKSPACE/notes/`
# without preserving the pre-PR symlink to the private repo. Result: edits to
# workspace/notes/ no longer reach the private repo, breaking cross-machine
# sync silently. This block restores the symlink architecture:
#
#   $SUTANDO_WORKSPACE/notes  → $SYNC_DIR/notes  (symlink, idempotent)
#
# Behavior:
#   - already-correct symlink: no-op
#   - real dir at the target path: log WARN; manual reconcile required
#     (don't silently overwrite local data — operator does rsync + mv + ln -s)
#   - missing path: create the symlink
#
# The same convention is documented in this script's pre-2026-05-11 history
# ("notes/ bidirectional rsync removed: both nodes now symlink").
WS_DIR="${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}"
# Disambiguation guard: if a host has SUTANDO_WORKSPACE pointing at a public-repo
# checkout (legacy semantic), the symlink-bootstrap below would point at
# `<repo>/notes` not the workspace `notes/`. Detect via `.git` presence + skip.
if [ -d "$WS_DIR/.git" ]; then
    log "skipping workspace-symlink bootstrap: SUTANDO_WORKSPACE='$WS_DIR' looks like a public-repo checkout, not a workspace. Set SUTANDO_WORKSPACE=$HOME/.sutando/workspace per CLAUDE.md and re-run."
    WS_DIR=""
fi
if [ -n "$WS_DIR" ]; then
mkdir -p "$WS_DIR"
for pair in "notes:notes"; do
    src="$WS_DIR/${pair%%:*}"
    tgt="$SYNC_DIR/${pair##*:}"
    if [ -L "$src" ]; then
        actual=$(readlink "$src")
        if [ "$actual" != "$tgt" ]; then
            log "symlink mismatch: $src → $actual (expected $tgt)"
            echo "sync-memory: WARN — $src points at $actual not $tgt; investigate." >&2
        fi
    elif [ -d "$src" ]; then
        log "WARN: $src is a real dir not a symlink — manual reconcile needed"
        echo "sync-memory: WARN — $src is a real dir, not a symlink to $tgt." >&2
        echo "  Manual reconcile (preserves data; excludes build noise): " >&2
        echo "    rsync -au \\" >&2
        echo "      --exclude='**/node_modules/' --exclude='**/.cache/' \\" >&2
        echo "      --exclude='**/.remotion/'    --exclude='**/dist/' \\" >&2
        echo "      --exclude='*.mp4.bak'        --exclude='*-rerun*.mp4' \\" >&2
        echo "      --exclude='*-rerun*.mov'     --exclude='*-v[0-9][0-9]-[0-9]*.mp4' \\" >&2
        echo "      --exclude='*-v[0-9][0-9]-[0-9]*.mov' --exclude='*-v[0-9]-v[0-9]*.mp4' \\" >&2
        echo "      --exclude='*-v[0-9]-v[0-9]*.mov'     --exclude='*_v[0-9]*.mp4' \\" >&2
        echo "      --exclude='*_v[0-9]*.mov'    --exclude='ep[0-9]*-v[0-9]*.mp4' \\" >&2
        echo "      --exclude='sutando-wire-*-v[0-9]*.mp4' \\" >&2
        echo "      $src/ $tgt/ && rm -rf $src && ln -s $tgt $src" >&2
    elif [ ! -e "$src" ]; then
        mkdir -p "$(dirname "$src")"
        if ln -s "$tgt" "$src"; then
            log "symlink created: $src → $tgt"
            echo "sync-memory: symlinked $src → $tgt" >&2
        fi
    fi
done
fi  # WS_DIR symlink-bootstrap guard

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

# Channel access-control allowlists (per-host, painful to recreate: the Discord
# access.json holds the owner/team/other tierMap + allowFrom). Backed up so a
# rebuilt host can restore its channel onboarding. SECURITY: we glob ONLY
# `*/access.json` — never `.env`. The bot tokens in channels/<ch>/.env stay
# local (Keychain/vault) and must NOT reach the repo. Keep this glob exact.
CHANNELS_SRC="${CLAUDE_HOME:-$HOME/.claude}/channels"
if [ -d "$CHANNELS_SRC" ]; then
    for _acc in "$CHANNELS_SRC"/*/access.json; do
        [ -f "$_acc" ] || continue
        _ch="$(basename "$(dirname "$_acc")")"
        mkdir -p "$MACHINE_DIR/channels/$_ch"
        copy_if_newer "$_acc" "$MACHINE_DIR/channels/$_ch/access.json"
    done
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
    # Mass-deletion tripwire: refuse to push a commit that removes a large number
    # of files unless explicitly forced. A stale/divergent sync script or a bad
    # rsync can wipe the shared memory repo; this backstop catches the staged
    # deletions before they are pushed (incident 2026-05-30).
    DELETED=$(git diff --cached --name-only --diff-filter=D | wc -l | tr -d ' ')
    MAX_DELETE="${SUTANDO_SYNC_MAX_DELETE:-50}"
    if [ "$DELETED" -gt "$MAX_DELETE" ] && [ "${SUTANDO_FORCE_SYNC:-0}" != "1" ]; then
        log "ABORT: sync would delete $DELETED files (>$MAX_DELETE). Refusing to push. Set SUTANDO_FORCE_SYNC=1 to override."
        echo "Sync aborted: would delete $DELETED files (mass-deletion tripwire). Set SUTANDO_FORCE_SYNC=1 to override." >&2
        git reset -q
        exit 1
    fi
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
