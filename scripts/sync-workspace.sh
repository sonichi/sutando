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

# Phase 2 implements: merge DEFAULT_CARRIER_INCLUDES + DEFAULT_GITIGNORE_LINES
# with vault.sync.{include,exclude} from sutando.config.{json,local.json}.
# Substitute <CANONICAL_ID> placeholder. Write to <WORKSPACE>/.gitignore
# (or merge with existing — never clobber user edits).
generate_gitignore() {
    log "generate_gitignore: stub — Phase 2 implements config merge + placeholder substitution + write to WORKSPACE/.gitignore"
    return 0
}

# --------------------------------------------------------------------------- #
# Section 6 — Subcommand stubs (Phase 1 scaffold; Phase 2+ fills bodies)       #
# --------------------------------------------------------------------------- #

cmd_init() {
    acquire_lock
    [ -z "$VAULT_URL" ] && die "init: SUTANDO_VAULT not set in env or .env"
    log "cmd_init: stub — Phase 2 implements: git init workspace + remote add origin $VAULT_URL + .gitignore generation + ensure_projects_map_entry + first push"
    echo "sync-workspace --init: not yet implemented (Phase 2)" >&2
    return 0
}

cmd_pull_only() {
    acquire_lock
    log "cmd_pull_only: stub — Phase 2 implements: git fetch --all + merge peer branches with --strategy-option=ours fallback + copy canonical → local-slug"
    echo "sync-workspace --pull-only: not yet implemented (Phase 2)" >&2
    return 0
}

cmd_push_only() {
    acquire_lock
    log "cmd_push_only: stub — Phase 2 implements: copy local-slug → canonical + git add + commit + git push origin HEAD:host/<hostname>"
    echo "sync-workspace --push-only: not yet implemented (Phase 2)" >&2
    return 0
}

cmd_default_bidirectional() {
    acquire_lock
    log "cmd_default_bidirectional: stub — Phase 2 implements: pull → translate-down → translate-up → push (one tick)"
    echo "sync-workspace: not yet implemented (Phase 2). For now run scripts/sync-memory.sh." >&2
    return 0
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
    log "cmd_migrate_from_legacy: stub — Phase 3 implements: rsync ~/.sutando/memory-sync/{notes,memory,build_log.md,pending-questions.md,...} → workspace + git init + initial push"
    echo "sync-workspace --migrate-from-legacy: not yet implemented (Phase 3)" >&2
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
