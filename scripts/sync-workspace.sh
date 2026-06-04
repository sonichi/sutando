#!/bin/bash
# sync-workspace.sh — Bidirectional sync of the Sutando workspace to a private vault repo.
#
# Replaces scripts/sync-memory.sh. The fundamental architecture shift (per
# 2026-06-04 #design discussion):
#
#   OLD (sync-memory.sh): workspace is a regular dir; sync via rsync to a
#   separate vault clone at ~/.sutando/memory-sync/. Two file trees on disk,
#   bidirectional copy mechanics.
#
#   NEW (sync-workspace.sh): the workspace ITSELF is a git repo, with the
#   vault as its remote. Selective tracking via .gitignore exposes only the
#   carrier set (notes/, build_log/<hostname>.md, pending-questions.md, the
#   per-project memory mirror). Sync = vanilla git push/pull on the workspace.
#
# Branch-per-host topology: each host pushes only to its own branch
# `host/<hostname>`; pulls all peers via fetch + merge. Conflicts use 3-way
# merge first, `git checkout --ours` fallback on unresolvable conflicts.
#
# Memory translation layer: Claude Code derives its per-project memory dir
# from the local cwd path slug (e.g. `-Users-qingyunwu-Documents-github-sutando`)
# which differs per host. To merge memory across hosts we maintain a
# canonical_id (sha256-8 of vault remote URL) and a tracked
# projects.map.json mapping local_slug → canonical_id. Before push, copy
# local-slug/memory/ → canonical_id/memory/. After pull, copy back.
#
# User-configurable carrier set: vault.sync.{include, exclude} in
# sutando.config.{json,local.json}. Include adds to default; exclude
# subtracts (rsync semantics, exclude wins on conflict).
#
# Usage:
#   bash scripts/sync-workspace.sh                # default: pull + translate + push
#   bash scripts/sync-workspace.sh --pull-only    # pull peers + canonical → local-slug
#   bash scripts/sync-workspace.sh --push-only    # local-slug → canonical + push
#   bash scripts/sync-workspace.sh --init         # one-time init: git init + setup vault remote + map.json bootstrap
#   bash scripts/sync-workspace.sh --migrate-from-legacy  # move ~/.sutando/memory-sync/ → workspace-as-git-repo
#   bash scripts/sync-workspace.sh --status       # show sync status
#   bash scripts/sync-workspace.sh --help         # show this usage
#
# Env vars:
#   SUTANDO_VAULT             — git URL of the private vault repo (REQUIRED)
#                               Legacy alias: SUTANDO_MEMORY_REPO (honored one release)
#   SUTANDO_REPO_DIR          — path to sutando code checkout. Auto-detected from script path.
#   SUTANDO_VAULT_PROJECT_ID  — override canonical_id; default = sha256-8 of vault URL
#   NO_COLOR                  — suppress ANSI escapes in warnings (no-color.org)
#   SUTANDO_SYNC_MAX_DELETE   — mass-deletion tripwire threshold (default 50)
#   SUTANDO_FORCE_SYNC        — bypass mass-deletion tripwire (set =1)

set -euo pipefail

# --------------------------------------------------------------------------- #
# Section 1 — Bootstrap (paths, env, config)                                   #
# --------------------------------------------------------------------------- #

_self="${BASH_SOURCE[0]:-$0}"
if command -v realpath >/dev/null 2>&1; then _self="$(realpath "$_self")"; fi
SCRIPT_DIR="$(cd "$(dirname "$_self")" && pwd)"
unset _self
SCRIPT_PARENT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load .env from the sutando workspace early — non-interactive shells (cron,
# launchd) don't run user shell startup. Without this the script exits 0
# silently with "VAULT not set" on cron paths.
if [ -f "$SCRIPT_PARENT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$SCRIPT_PARENT/.env"
    set +a
fi

# Resolve REPO_DIR (the sutando code checkout). Auto-detect from script path
# when invoked as `<repo>/scripts/sync-workspace.sh`; honor SUTANDO_REPO_DIR
# override; fall back to SCRIPT_PARENT as last resort.
if [ -n "${SUTANDO_REPO_DIR:-}" ]; then
    REPO_DIR="$SUTANDO_REPO_DIR"
elif [ -f "$SCRIPT_PARENT/CLAUDE.md" ] && [ -d "$SCRIPT_PARENT/skills" ] && [ -d "$SCRIPT_PARENT/.git" ]; then
    REPO_DIR="$SCRIPT_PARENT"
else
    REPO_DIR="$SCRIPT_PARENT"
fi
if [ ! -d "$REPO_DIR" ]; then
    echo "sync-workspace: repo not found at $REPO_DIR; set SUTANDO_REPO_DIR or invoke from <repo>/scripts/." >&2
    exit 1
fi

# Resolve WORKSPACE_DIR via the canonical M0 helper. SCRIPT_PARENT-anchored
# lookup so we don't fall through to a stale SUTANDO_REPO_DIR pin (see
# feedback_stale_repo_dir_pin memory).
if [ -f "$SCRIPT_PARENT/scripts/sutando-config.sh" ]; then
    WORKSPACE_DIR="$(bash "$SCRIPT_PARENT/scripts/sutando-config.sh" workspace)"
else
    echo "sync-workspace: sutando-config.sh helper not found beside this script. Cannot resolve workspace." >&2
    exit 1
fi
if [ -z "$WORKSPACE_DIR" ] || [ ! -d "$WORKSPACE_DIR" ]; then
    echo "sync-workspace: resolved WORKSPACE_DIR ($WORKSPACE_DIR) is empty or missing." >&2
    exit 1
fi

# Vault URL: SUTANDO_VAULT canonical; SUTANDO_MEMORY_REPO honored as legacy alias.
VAULT_URL="${SUTANDO_VAULT:-${SUTANDO_MEMORY_REPO:-}}"
if [ -n "${SUTANDO_MEMORY_REPO:-}" ] && [ -z "${SUTANDO_VAULT:-}" ]; then
    echo "sync-workspace: SUTANDO_MEMORY_REPO is set; please rename to SUTANDO_VAULT (legacy alias honored this release)." >&2
fi
# Load from .env if still empty.
if [ -z "$VAULT_URL" ] && [ -f "$REPO_DIR/.env" ]; then
    VAULT_URL=$(grep -E '^SUTANDO_VAULT=' "$REPO_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
    if [ -z "$VAULT_URL" ]; then
        VAULT_URL=$(grep -E '^SUTANDO_MEMORY_REPO=' "$REPO_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
    fi
fi

# --------------------------------------------------------------------------- #
# Section 2 — Logging + UI                                                     #
# --------------------------------------------------------------------------- #

LOG="/tmp/sync-workspace.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

color_warn() {
    if [ -t 2 ] && [ -z "${NO_COLOR:-}" ]; then
        printf '\033[1;31m%s\033[0m\n' "$1" >&2
    else
        printf '%s\n' "$1" >&2
    fi
}

die() {
    color_warn "sync-workspace: $1"
    exit "${2:-1}"
}

# --------------------------------------------------------------------------- #
# Section 3 — Lock (atomic mkdir, POSIX, no flock dependency)                  #
# --------------------------------------------------------------------------- #

LOCK_DIR="/tmp/sync-workspace.lock.d"

acquire_lock() {
    # Stale lock cleanup: lock dir older than 10 min = assume crash, remove.
    if [ -d "$LOCK_DIR" ]; then
        if find "$LOCK_DIR" -maxdepth 0 -mmin +10 2>/dev/null | grep -q .; then
            log "Stale lock removed (older than 10 min)"
            rm -rf "$LOCK_DIR"
        fi
    fi
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        log "Another sync already in progress, exiting."
        echo "sync-workspace: another instance is running, skipping."
        exit 0
    fi
    trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM
}

# --------------------------------------------------------------------------- #
# Section 4 — Canonical ID + projects.map.json                                 #
# --------------------------------------------------------------------------- #

# Derive canonical_id from vault URL. Stable across hosts (same URL → same id);
# neutral on usernames + paths (no privacy leak from filesystem layout).
canonical_id() {
    if [ -n "${SUTANDO_VAULT_PROJECT_ID:-}" ]; then
        printf '%s' "$SUTANDO_VAULT_PROJECT_ID"
        return 0
    fi
    if [ -z "$VAULT_URL" ]; then
        die "canonical_id: SUTANDO_VAULT not set and no override (SUTANDO_VAULT_PROJECT_ID)."
    fi
    printf '%s' "$VAULT_URL" | shasum -a 256 | cut -c1-8
}

# Compute Claude Code's per-project local slug for this host's repo cwd.
# Claude Code derives this from the absolute path: `/Users/foo/sutando` →
# `-Users-foo-sutando` (slashes replaced with dashes).
local_slug() {
    printf '%s' "$REPO_DIR" | sed 's|/|-|g'
}

# Projects.map.json — per-host map of {<local_slug>: <canonical_id>}. Lives
# inside the workspace so it can be tracked + cross-host-debuggable.
PROJECTS_MAP_PATH() {
    printf '%s/.sutando-vault/projects.map.json' "$WORKSPACE_DIR"
}

# Ensure projects.map.json exists + this host's entry is present.
ensure_projects_map_entry() {
    local map_path
    map_path="$(PROJECTS_MAP_PATH)"
    mkdir -p "$(dirname "$map_path")"
    local slug canonical
    slug="$(local_slug)"
    canonical="$(canonical_id)"

    if [ ! -f "$map_path" ]; then
        printf '{\n  "%s": "%s"\n}\n' "$slug" "$canonical" > "$map_path"
        log "ensure_projects_map_entry: created $map_path with {$slug: $canonical}"
        return 0
    fi

    # Check if this host's slug is already mapped — if so, no-op.
    if python3 -c "
import json, sys
with open('$map_path') as f: m = json.load(f)
sys.exit(0 if m.get('$slug') == '$canonical' else 1)
" 2>/dev/null; then
        return 0
    fi

    # Add/update this host's entry.
    python3 -c "
import json
with open('$map_path') as f: m = json.load(f)
m['$slug'] = '$canonical'
with open('$map_path', 'w') as f: json.dump(m, f, indent=2, sort_keys=True)
"
    log "ensure_projects_map_entry: added {$slug: $canonical} to $map_path"
}

# --------------------------------------------------------------------------- #
# Section 5 — .gitignore template (Phase 2 fills generation logic)             #
# --------------------------------------------------------------------------- #

# Default carrier set (what TO sync) — appears as `!path` un-ignore rules.
# Per the 2026-06-04 #design crystallization: notes/, pending-questions.md,
# build_log/<hostname>.md (per-host split), the per-project memory mirror.
# Archive paths (tasks/archive/, results/archive/) are opt-in via config.
DEFAULT_CARRIER_INCLUDES=(
    "!notes/"
    "!notes/**"
    "!pending-questions.md"
    "!build_log/"
    "!build_log/**"
    "!.claude-sutando/projects/<CANONICAL_ID>/memory/"
    "!.claude-sutando/projects/<CANONICAL_ID>/memory/**"
    "!projects.map.json"
)

# Default exclusions (what NOT to sync, baseline). Per-host runtime state,
# credentials, caches.
DEFAULT_GITIGNORE_LINES=(
    "# Generated by sync-workspace.sh — do not edit by hand."
    "# Source of truth: scripts/sync-workspace.sh::DEFAULT_CARRIER_INCLUDES + DEFAULT_GITIGNORE_LINES"
    "# User customization: vault.sync.{include,exclude} in sutando.config.local.json"
    ""
    "# Baseline: ignore EVERYTHING by default; then un-ignore the carrier set below."
    "*"
    ""
    "# Per-host runtime state — never synced (also explicit-deny for safety)"
    "state/"
    "tasks/"
    "results/"
    "logs/"
    "data/"
    "*.heartbeat"
    "*.alive"
    "*.sentinel"
    "*.pid"
    ""
    "# Local credentials (never synced)"
    ".env*"
    ""
    "# Carrier set (un-ignore selectively)"
)

# Write .gitignore to $WORKSPACE_DIR. Whitelist mode: ignore everything by
# default, un-ignore the carrier set. Each carrier path also un-ignores its
# ancestor dirs (gitignore requirement: can't re-include a child if parent
# is excluded).
#
# Phase 2 baseline: hardcoded DEFAULT carrier set, no user config merge yet
# (config merge follow-up tracked for Phase 4 or sibling PR — keeps Phase 2
# scope focused on the core workspace-as-git-repo plumbing).
#
# Side effects: writes $WORKSPACE_DIR/.gitignore. Idempotent: same output
# every call for the same canonical_id. Does NOT preserve user edits in the
# file — operators should put per-host overrides in sutando.config.local.json
# (Phase 4) rather than hand-editing the generated .gitignore.
generate_gitignore() {
    local canonical gitignore_path
    canonical="$(canonical_id)"
    gitignore_path="$WORKSPACE_DIR/.gitignore"
    {
        echo "# Generated by sync-workspace.sh — do not edit by hand."
        echo "# Source: scripts/sync-workspace.sh::generate_gitignore"
        echo "# Carrier set (what gets synced to vault) is defined here + by"
        echo "# vault.sync.{include,exclude} in sutando.config.local.json (Phase 4)."
        echo ""
        echo "# Whitelist mode: ignore everything by default, un-ignore the carrier set."
        echo "*"
        echo ""
        echo "# Always-tracked metadata"
        echo "!.gitignore"
        echo "!projects.map.json"
        echo ""
        echo "# Default carrier set — top-level dirs + files"
        echo "!notes/"
        echo "!notes/**"
        echo "!pending-questions.md"
        echo "!build_log/"
        echo "!build_log/**"
        echo ""
        echo "# Per-project Claude Code memory (canonical-id mirror only;"
        echo "# host-specific slugs stay gitignored). The intermediate dirs"
        echo "# must each be un-ignored or git stops at the parent."
        echo "!.claude-sutando/"
        echo "!.claude-sutando/projects/"
        echo "!.claude-sutando/projects/${canonical}/"
        echo "!.claude-sutando/projects/${canonical}/memory/"
        echo "!.claude-sutando/projects/${canonical}/memory/**"
        echo ""
        echo "# vault metadata"
        echo "!.sutando-vault/"
        echo "!.sutando-vault/projects.map.json"
        echo ""
        echo "# Hard-deny credentials regardless of carrier set"
        echo ".env*"
        echo "*.heartbeat"
        echo "*.alive"
        echo "*.sentinel"
        echo "*.pid"
    } > "$gitignore_path"
    log "generate_gitignore: wrote $gitignore_path (canonical=$canonical)"
    return 0
}

# --------------------------------------------------------------------------- #
# Section 6 — Subcommand stubs (Phase 1 scaffold; Phase 2+ fills bodies)       #
# --------------------------------------------------------------------------- #

cmd_init() {
    acquire_lock
    _init_impl
}

_init_impl() {
    [ -z "$VAULT_URL" ] && die "init: SUTANDO_VAULT not set in env or .env"

    cd "$WORKSPACE_DIR" || die "init: cannot cd to $WORKSPACE_DIR"

    # 1. git init if not already a repo
    if [ ! -d "$WORKSPACE_DIR/.git" ]; then
        git init -q
        log "_init_impl: git init done in $WORKSPACE_DIR"
        echo "sync-workspace: git init done in $WORKSPACE_DIR" >&2
    else
        log "_init_impl: $WORKSPACE_DIR is already a git repo"
    fi

    # 2. Set vault remote (idempotent — replace if URL changed)
    if git remote get-url origin >/dev/null 2>&1; then
        local existing
        existing="$(git remote get-url origin)"
        if [ "$existing" != "$VAULT_URL" ]; then
            log "_init_impl: changing remote origin from $existing to $VAULT_URL"
            echo "sync-workspace: updating remote origin from $existing to $VAULT_URL" >&2
            git remote set-url origin "$VAULT_URL"
        fi
    else
        git remote add origin "$VAULT_URL"
        log "_init_impl: added remote origin $VAULT_URL"
        echo "sync-workspace: added remote origin $VAULT_URL" >&2
    fi

    # 3. Generate .gitignore (will overwrite — operators should config-override, not hand-edit)
    generate_gitignore

    # 4. Bootstrap projects.map.json + this host's entry
    ensure_projects_map_entry

    # 5. Create canonical_id memory dir (will be populated by translation later)
    local canonical
    canonical="$(canonical_id)"
    mkdir -p ".claude-sutando/projects/${canonical}/memory"

    # 6. Initial commit + push to host branch
    git add .gitignore .sutando-vault/projects.map.json ".claude-sutando/projects/${canonical}/" 2>/dev/null || true
    if git diff --cached --quiet; then
        log "_init_impl: nothing to commit on init (already-initialized re-run)"
    else
        git commit -q -m "Initial workspace-vault sync: bootstrap canonical=${canonical} host=$(hostname)"
        log "_init_impl: initial commit created"

        local host
        host="$(hostname | sed 's/\..*//')"
        if git push origin "HEAD:refs/heads/host/${host}" 2>&1 | tee -a "$LOG" >/dev/null; then
            log "_init_impl: pushed to origin host/${host}"
            echo "sync-workspace: initialized + pushed to host/${host}"
        else
            log "_init_impl: push failed (may need to set up tracking on first push)"
            echo "sync-workspace: initialized but push failed; check $LOG" >&2
            return 1
        fi
    fi
    return 0
}

# --- Translation layer (Phase 2) ---
# Memory bridge: bidirectional copy between Claude Code's auto-derived
# local-slug dir (gitignored, per-host) and the canonical-id mirror (tracked,
# shared across hosts via vault sync).
#
# Symmetric copy uses `cp -a path/. target/` so we copy CONTENTS (not the dir
# itself); preserves metadata; creates target if missing.

copy_localslug_to_canonical() {
    local canonical local_dir canonical_dir
    canonical="$(canonical_id)"
    local_dir="$WORKSPACE_DIR/.claude-sutando/projects/$(local_slug)/memory"
    canonical_dir="$WORKSPACE_DIR/.claude-sutando/projects/${canonical}/memory"

    if [ ! -d "$local_dir" ]; then
        log "copy_localslug_to_canonical: $local_dir doesn't exist (no Claude Code memory yet on this host); skipping"
        return 0
    fi
    mkdir -p "$canonical_dir"
    cp -a "$local_dir"/. "$canonical_dir"/ 2>/dev/null || true
    log "copy_localslug_to_canonical: $local_dir → $canonical_dir"
}

copy_canonical_to_localslug() {
    local canonical local_dir canonical_dir
    canonical="$(canonical_id)"
    local_dir="$WORKSPACE_DIR/.claude-sutando/projects/$(local_slug)/memory"
    canonical_dir="$WORKSPACE_DIR/.claude-sutando/projects/${canonical}/memory"

    if [ ! -d "$canonical_dir" ]; then
        log "copy_canonical_to_localslug: $canonical_dir doesn't exist yet (vault empty?); skipping"
        return 0
    fi
    mkdir -p "$local_dir"
    cp -a "$canonical_dir"/. "$local_dir"/ 2>/dev/null || true
    log "copy_canonical_to_localslug: $canonical_dir → $local_dir"
}

# --- Pull-side (Phase 2) ---
# Fetch all peer branches, merge into local host/<hostname> branch with 3-way
# auto-merge first. On unresolvable conflict, use-local fallback via
# `git checkout --ours`. Pull ordering: oldest peer push first (minimizes
# per-step merge diff under the use-local-on-conflict rule).
#
# After merge: copy canonical → local-slug so Claude Code on this host sees
# peers' memory writes.

cmd_pull_only() {
    acquire_lock
    _pull_only_impl
}

_pull_only_impl() {
    cd "$WORKSPACE_DIR" || die "pull-only: cannot cd to $WORKSPACE_DIR"
    [ -d ".git" ] || die "pull-only: $WORKSPACE_DIR is not a git repo; run --init first"

    log "_pull_only_impl: fetching all peer branches"
    git fetch --all --quiet 2>&1 | tee -a "$LOG" >/dev/null

    # Ensure we're on the host branch (idempotent)
    local host current_branch
    host="$(hostname | sed 's/\..*//')"
    current_branch="host/${host}"
    if [ "$(git symbolic-ref --short HEAD 2>/dev/null)" != "$current_branch" ]; then
        # Create from origin/<branch> if remote-tracking exists, else from current HEAD
        if git show-ref --quiet "refs/remotes/origin/${current_branch}"; then
            git checkout -B "$current_branch" "origin/${current_branch}" 2>&1 | tee -a "$LOG" >/dev/null
        else
            git checkout -B "$current_branch" 2>&1 | tee -a "$LOG" >/dev/null
        fi
    fi

    # Sort peer branches by last-push time, oldest first (per design)
    local peers
    peers=$(git for-each-ref --format='%(committerdate:unix) %(refname:short)' refs/remotes/origin/host/ 2>/dev/null \
                | sort -n | awk '{print $2}')

    local merged=0
    for peer in $peers; do
        # Skip self
        [ "$peer" = "origin/${current_branch}" ] && continue
        log "_pull_only_impl: merging $peer into $current_branch"
        if git merge --no-edit "$peer" 2>&1 | tee -a "$LOG" >/dev/null; then
            merged=$((merged + 1))
        else
            log "_pull_only_impl: conflict merging $peer; resolving via --ours (use-local fallback)"
            for f in $(git diff --name-only --diff-filter=U); do
                git checkout --ours -- "$f"
                git add "$f"
            done
            git -c core.editor=true commit --no-edit 2>/dev/null || true
            merged=$((merged + 1))
        fi
    done

    log "_pull_only_impl: merged $merged peer branch(es)"

    # Translate canonical → local-slug so Claude Code on this host sees peers' memory
    copy_canonical_to_localslug

    echo "sync-workspace: pull-only complete (merged $merged peer branches)"
    return 0
}

# --- Push-side (Phase 2) ---
# Copy local-slug → canonical so this host's memory writes propagate up.
# Stage everything, mass-deletion tripwire (preserved from sync-memory.sh),
# commit if anything changed, push to origin/host/<hostname> only.

cmd_push_only() {
    acquire_lock
    _push_only_impl
}

_push_only_impl() {
    cd "$WORKSPACE_DIR" || die "push-only: cannot cd to $WORKSPACE_DIR"
    [ -d ".git" ] || die "push-only: $WORKSPACE_DIR is not a git repo; run --init first"

    # Translate this host's writes UP to the canonical mirror
    copy_localslug_to_canonical
    ensure_projects_map_entry

    git add -A
    if git diff --cached --quiet; then
        log "_push_only_impl: nothing to commit"
        echo "sync-workspace: nothing to push (clean working tree)"
        return 0
    fi

    # Mass-deletion tripwire (carried over from sync-memory.sh)
    local deleted max_delete
    deleted=$(git diff --cached --name-only --diff-filter=D | wc -l | tr -d ' ')
    max_delete="${SUTANDO_SYNC_MAX_DELETE:-50}"
    if [ "$deleted" -gt "$max_delete" ] && [ "${SUTANDO_FORCE_SYNC:-0}" != "1" ]; then
        log "_push_only_impl: ABORT — would delete $deleted files (>$max_delete tripwire)"
        echo "sync-workspace: refusing push — would delete $deleted files (>SUTANDO_SYNC_MAX_DELETE=$max_delete). Set SUTANDO_FORCE_SYNC=1 to override." >&2
        git reset -q
        return 1
    fi

    git commit -q -m "Sync $(hostname) $(date +%Y-%m-%dT%H:%M)"

    local host
    host="$(hostname | sed 's/\..*//')"
    if git push origin "HEAD:refs/heads/host/${host}" 2>&1 | tee -a "$LOG" >/dev/null; then
        log "_push_only_impl: pushed to origin host/${host}"
        echo "sync-workspace: pushed to host/${host}"
        return 0
    else
        log "_push_only_impl: push failed"
        echo "sync-workspace: push failed; check $LOG" >&2
        return 1
    fi
}

# --- Default: bidirectional one tick ---
# Pull peers first (so own commits build on latest peer state), then push.

cmd_default_bidirectional() {
    acquire_lock
    _pull_only_impl || true   # pull failures shouldn't block push
    _push_only_impl
}

cmd_status() {
    log "cmd_status: stub — Phase 2 implements: show last sync, current branch, peer branches, projects.map.json contents"
    echo "WORKSPACE_DIR: $WORKSPACE_DIR"
    echo "REPO_DIR:      $REPO_DIR"
    echo "VAULT_URL:     ${VAULT_URL:-<unset>}"
    if [ -n "$VAULT_URL" ]; then
        echo "canonical_id:  $(canonical_id)"
    fi
    echo "local_slug:    $(local_slug)"
    local map_path
    map_path="$(PROJECTS_MAP_PATH)"
    if [ -f "$map_path" ]; then
        echo "projects.map.json: $map_path"
        cat "$map_path"
    else
        echo "projects.map.json: <not yet bootstrapped — run --init>"
    fi
    return 0
}

cmd_migrate_from_legacy() {
    acquire_lock
    _migrate_from_legacy_impl
}

# Phase 3: one-time migration from the legacy ~/.sutando/memory-sync/ git
# repo to the new workspace-as-git-repo model. Steps:
#
#   1. Detect legacy clone at $HOME/.sutando/memory-sync/ (or
#      $SUTANDO_MEMORY_SYNC_DIR if set)
#   2. Curated copy of legacy content into workspace's tracked paths:
#      - legacy/notes/ → workspace/notes/  (skip if workspace/notes is a
#        symlink into the legacy repo — workspace already points at it)
#      - legacy/memory/*.md → workspace/.claude-sutando/projects/<canonical>/memory/
#      - legacy/pending-questions.md → workspace/pending-questions.md
#      - legacy/build_log.md → workspace/build_log/<hostname>.md (split per
#        the per-host design)
#   3. Call _init_impl to git-init the workspace + push to vault
#   4. Print a "next steps" recipe for the operator (don't delete legacy yet;
#      they verify the migration landed cleanly first)
#
# Safe-by-default: never deletes the legacy dir; never overwrites existing
# workspace files (cp with -n). Operator deletes legacy manually after
# verifying.

_migrate_from_legacy_impl() {
    [ -z "$VAULT_URL" ] && die "migrate: SUTANDO_VAULT not set in env or .env"

    local legacy_dir
    legacy_dir="${SUTANDO_MEMORY_SYNC_DIR:-$HOME/.sutando/memory-sync}"

    if [ ! -d "$legacy_dir" ]; then
        die "migrate: legacy clone not found at $legacy_dir; nothing to migrate"
    fi
    if [ ! -d "$legacy_dir/.git" ]; then
        die "migrate: $legacy_dir exists but is not a git repo; expected a clone of the old memory-sync"
    fi

    log "_migrate_from_legacy_impl: starting migration from $legacy_dir → $WORKSPACE_DIR"
    echo "sync-workspace migrate: copying from $legacy_dir into $WORKSPACE_DIR" >&2

    local canonical
    canonical="$(canonical_id)"

    # Workspace-resident target dirs (ensure they exist)
    mkdir -p "$WORKSPACE_DIR/notes" \
             "$WORKSPACE_DIR/build_log" \
             "$WORKSPACE_DIR/.claude-sutando/projects/${canonical}/memory"

    # 1. notes/ — handle symlink case (workspace/notes is already a symlink
    # into legacy; nothing to copy, just unlink + create as a real dir for
    # the new model to take over).
    if [ -L "$WORKSPACE_DIR/notes" ]; then
        local symlink_target
        symlink_target="$(readlink "$WORKSPACE_DIR/notes")"
        log "_migrate_from_legacy_impl: workspace/notes is a symlink → $symlink_target; removing + copying content"
        rm "$WORKSPACE_DIR/notes"
        mkdir -p "$WORKSPACE_DIR/notes"
    fi
    if [ -d "$legacy_dir/notes" ]; then
        cp -an "$legacy_dir/notes"/. "$WORKSPACE_DIR/notes"/ 2>/dev/null || true
        log "_migrate_from_legacy_impl: copied $legacy_dir/notes/ → $WORKSPACE_DIR/notes/ (cp -n; existing files preserved)"
    fi

    # 2. memory/*.md → canonical-id memory dir
    if [ -d "$legacy_dir/memory" ]; then
        local copied=0
        for f in "$legacy_dir/memory"/*.md; do
            [ -f "$f" ] || continue
            cp -n "$f" "$WORKSPACE_DIR/.claude-sutando/projects/${canonical}/memory/" 2>/dev/null && copied=$((copied+1))
        done
        log "_migrate_from_legacy_impl: copied $copied memory file(s) → canonical=${canonical}"
    fi

    # 3. pending-questions.md (curated machine-<hostname>/pending-questions.md takes precedence
    # if present, else top-level)
    local host
    host="$(hostname | sed 's/\..*//')"
    local pq_src
    if [ -f "$legacy_dir/machine-${host}/pending-questions.md" ]; then
        pq_src="$legacy_dir/machine-${host}/pending-questions.md"
    elif [ -f "$legacy_dir/pending-questions.md" ]; then
        pq_src="$legacy_dir/pending-questions.md"
    else
        pq_src=""
    fi
    if [ -n "$pq_src" ]; then
        cp -n "$pq_src" "$WORKSPACE_DIR/pending-questions.md" 2>/dev/null \
            && log "_migrate_from_legacy_impl: copied $pq_src → workspace/pending-questions.md"
    fi

    # 4. build_log.md → build_log/<hostname>.md (per-host split per design)
    local bl_src
    if [ -f "$legacy_dir/machine-${host}/build_log.md" ]; then
        bl_src="$legacy_dir/machine-${host}/build_log.md"
    elif [ -f "$legacy_dir/build_log.md" ]; then
        bl_src="$legacy_dir/build_log.md"
    else
        bl_src=""
    fi
    if [ -n "$bl_src" ]; then
        cp -n "$bl_src" "$WORKSPACE_DIR/build_log/${host}.md" 2>/dev/null \
            && log "_migrate_from_legacy_impl: copied $bl_src → workspace/build_log/${host}.md"
    fi

    # 5. Run init impl to set up the workspace-as-git-repo + first push
    log "_migrate_from_legacy_impl: handing off to _init_impl for git init + first push"
    _init_impl

    # 6. Print next steps for the operator
    cat <<EOF >&2

sync-workspace migrate: complete.

Next steps (operator-supervised):
  1. Verify the new workspace has the expected content:
       ls $WORKSPACE_DIR/notes/ | head
       ls $WORKSPACE_DIR/.claude-sutando/projects/${canonical}/memory/ | head
  2. Confirm the first push landed in your $VAULT_URL repo (web UI).
  3. Run a normal sync to verify push + pull work end-to-end:
       bash scripts/sync-workspace.sh
  4. Once you're satisfied, you can delete the legacy clone:
       rm -rf $legacy_dir
     (Optional — keeping it around as a backup costs ~minor disk only.)
  5. Update your crons to invoke 'sync-workspace.sh' instead of 'sync-memory.sh'
     (PR-1 keeps sync-memory.sh untouched; PR-2 will add a backward-compat shim
     that auto-redirects).
EOF
    return 0
}

cmd_help() {
    sed -n 's/^# \?//;1,/^$/ {/^$/q;p;}' "$0" | head -50
    return 0
}

# --------------------------------------------------------------------------- #
# Section 7 — Subcommand dispatch                                              #
# --------------------------------------------------------------------------- #

cmd="${1:-default}"
case "$cmd" in
    --init|init)                       cmd_init ;;
    --pull-only|pull-only)             cmd_pull_only ;;
    --push-only|push-only)             cmd_push_only ;;
    --status|status)                   cmd_status ;;
    --migrate-from-legacy)             cmd_migrate_from_legacy ;;
    --help|-h|help)                    cmd_help ;;
    --default|default|'')              cmd_default_bidirectional ;;
    *)
        echo "sync-workspace: unknown subcommand '$cmd'. Try --help." >&2
        exit 2
        ;;
esac
