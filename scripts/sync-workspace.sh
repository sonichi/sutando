#!/bin/bash
# sync-workspace.sh — Bidirectional sync of the Sutando workspace to a private vault repo.
#
# Replaces scripts/sync-memory.sh. Architecture per 2026-06-04 #design + the
# 05:11Z simplification (owner: "remove the Memory translation layer. Keep
# do simple pull"):
#
#   OLD (sync-memory.sh): workspace is a regular dir; sync via rsync to a
#   separate vault clone at ~/.sutando/memory-sync/. Two file trees on disk,
#   bidirectional copy mechanics.
#
#   NEW (sync-workspace.sh): the workspace ITSELF is a git repo, with the
#   vault as its remote. Selective tracking via .gitignore exposes only the
#   carrier set. Sync = vanilla git push/pull on the workspace — no in-script
#   translation layer, no canonical-id mapping, no projects.map.json.
#
# Branch-per-host topology: each host pushes only to its own branch
# `host/<hostname>`; pulls all peers via fetch + merge. Conflicts use 3-way
# merge first, `git checkout --ours` fallback on unresolvable conflicts.
#
# Per-host Claude Code memory dirs (`.claude-sutando/projects/<local_slug>/`)
# are each tracked independently. Hosts see peers' subdirs after pull but
# memory is NOT auto-merged across slugs — peer memory is visible-not-merged.
# Operator/agent can browse peer subdirs manually if curious. This is the
# simplification (versus the earlier canonical-id translation-layer design).
#
# User-configurable carrier set: vault.sync.{include, exclude} in
# sutando.config.{json,local.json}. Include adds to default; exclude
# subtracts (rsync semantics, exclude wins on conflict). Currently
# defaults-only (config-merge tracked for follow-up).
#
# Usage:
#   bash scripts/sync-workspace.sh                # default: pull + push (one tick)
#   bash scripts/sync-workspace.sh --pull-only    # fetch + merge peers, no push
#   bash scripts/sync-workspace.sh --push-only    # commit + push to own host branch
#   bash scripts/sync-workspace.sh --init         # one-time init: git init + setup vault remote
#   bash scripts/sync-workspace.sh --migrate-from-legacy  # move ~/.sutando/memory-sync/ → workspace-as-git-repo
#   bash scripts/sync-workspace.sh --status       # show sync state
#   bash scripts/sync-workspace.sh --help         # show this usage
#
# Env vars:
#   SUTANDO_VAULT             — git URL of the private vault repo (REQUIRED)
#                               Legacy alias: SUTANDO_MEMORY_REPO (honored one release)
#   SUTANDO_REPO_DIR          — path to sutando code checkout. Auto-detected from script path.
#   NO_COLOR                  — suppress ANSI escapes in warnings (no-color.org)
#   SUTANDO_SYNC_MAX_DELETE   — mass-deletion tripwire threshold (default 50)
#   SUTANDO_FORCE_SYNC        — bypass mass-deletion tripwire (set =1)

set -euo pipefail

# --------------------------------------------------------------------------- #
# Section 0 — Global flags (parsed from args before subcommand dispatch)        #
# --------------------------------------------------------------------------- #

DRY_RUN=0            # --dry-run: skip mutating ops, print "would: ..." instead
FORCE_GITIGNORE=0    # --force-gitignore: overwrite existing .gitignore without warning

# Parse global flags out of $@ (leaves only the subcommand + its args).
_args=()
for _arg in "$@"; do
    case "$_arg" in
        --dry-run)         DRY_RUN=1 ;;
        --force-gitignore) FORCE_GITIGNORE=1 ;;
        *)                 _args+=("$_arg") ;;
    esac
done
# Reset $@ to non-flag args
set -- "${_args[@]:-}"
unset _args

# --------------------------------------------------------------------------- #
# Section 1 — Bootstrap (paths, env, config)                                   #
# --------------------------------------------------------------------------- #

_self="${BASH_SOURCE[0]:-$0}"
if command -v realpath >/dev/null 2>&1; then _self="$(realpath "$_self")"; fi
SCRIPT_DIR="$(cd "$(dirname "$_self")" && pwd)"
unset _self
SCRIPT_PARENT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load .env from the sutando workspace early — non-interactive shells (cron,
# launchd) don't run user shell startup.
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
# Section 4 — .gitignore generation                                             #
# --------------------------------------------------------------------------- #

# Compose .gitignore content to stdout (does not write). Used by both
# `generate_gitignore` (the writer) and the diff/warn logic to compare against
# an existing user-edited .gitignore. Whitelist mode: `*` ignores everything;
# selective un-ignore for the carrier set (ancestor dirs must each be
# un-ignored, gitignore can't re-include a child if parent is excluded).
_compose_gitignore_content() {
    echo "# Generated by sync-workspace.sh — do not edit by hand."
    echo "# Source: scripts/sync-workspace.sh::generate_gitignore"
    echo "# Carrier set is defined here + by vault.sync.{include,exclude} in"
    echo "# sutando.config.local.json (config merge tracked for follow-up)."
    echo ""
    echo "# Whitelist mode: ignore everything by default, un-ignore the carrier set."
    echo "*"
    echo ""
    echo "# Always-tracked metadata"
    echo "!.gitignore"
    echo ""
    echo "# Default carrier set — top-level dirs + files"
    echo "!notes/"
    echo "!notes/**"
    echo "!pending-questions.md"
    echo "!build_log/"
    echo "!build_log/**"
    echo ""
    echo "# Per-host Claude Code project dirs — track ONLY the memory/ subdir"
    echo "# within each slug. Transcripts, file_history, caches, etc. stay"
    echo "# ignored (can be large and per-host). Each host writes only to its"
    echo "# own slug; peer slugs' memory/ visible after pull but not auto-merged."
    echo "!.claude-sutando/"
    echo "!.claude-sutando/projects/"
    echo "!.claude-sutando/projects/*/"
    echo "!.claude-sutando/projects/*/memory/"
    echo "!.claude-sutando/projects/*/memory/**"
    echo ""
    echo "# Hard-deny credentials regardless of carrier set"
    echo ".env*"
    echo "*.heartbeat"
    echo "*.alive"
    echo "*.sentinel"
    echo "*.pid"
}

# Write .gitignore to $WORKSPACE_DIR. Whitelist mode: ignore everything by
# default, un-ignore the carrier set.
#
# Pro #1445 review fix #3: don't silently clobber an existing .gitignore.
# If the file exists AND differs from what we'd write, refuse to overwrite
# unless the operator passes `--force-gitignore`. Print a diff so they can
# see what would change. The risk this protects against: a custom .gitignore
# that explicitly blocks something the user DOES want synced would silently
# get reinstated by overwrite → data loss in the vault.
generate_gitignore() {
    local gitignore_path tmp_path
    gitignore_path="$WORKSPACE_DIR/.gitignore"
    tmp_path="$(mktemp -t sync-workspace-gitignore.XXXXXX)"
    _compose_gitignore_content > "$tmp_path"

    if [ -f "$gitignore_path" ]; then
        if diff -q "$gitignore_path" "$tmp_path" >/dev/null 2>&1; then
            # Identical — no-op
            rm -f "$tmp_path"
            log "generate_gitignore: existing $gitignore_path matches; no-op"
            return 0
        fi
        if [ "$FORCE_GITIGNORE" != "1" ]; then
            color_warn "sync-workspace: $gitignore_path EXISTS and DIFFERS from the generated content."
            color_warn "Refusing to overwrite (operator-authored content may block carrier-set paths)."
            echo "" >&2
            echo "Diff (existing → would-be-generated):" >&2
            diff -u "$gitignore_path" "$tmp_path" 2>&1 | head -40 >&2
            echo "" >&2
            echo "To overwrite anyway: pass --force-gitignore" >&2
            echo "(Or merge desired changes into the existing file by hand.)" >&2
            rm -f "$tmp_path"
            return 1
        fi
        log "generate_gitignore: overwriting existing $gitignore_path (--force-gitignore)"
    fi

    if [ "$DRY_RUN" = "1" ]; then
        echo "DRY-RUN: would write $gitignore_path ($(wc -l < "$tmp_path" | tr -d ' ') lines)" >&2
        rm -f "$tmp_path"
        return 0
    fi
    mv "$tmp_path" "$gitignore_path"
    log "generate_gitignore: wrote $gitignore_path"
    return 0
}

# --------------------------------------------------------------------------- #
# Section 5 — Subcommand bodies                                                #
# --------------------------------------------------------------------------- #

cmd_init() {
    acquire_lock
    _init_impl
}

_init_impl() {
    [ -z "$VAULT_URL" ] && die "init: SUTANDO_VAULT not set in env or .env"

    cd "$WORKSPACE_DIR" || die "init: cannot cd to $WORKSPACE_DIR"

    if [ "$DRY_RUN" = "1" ]; then
        echo "DRY-RUN: would init workspace as git repo at $WORKSPACE_DIR" >&2
        echo "DRY-RUN: would set git remote origin = $VAULT_URL" >&2
        echo "DRY-RUN: would (re)generate .gitignore" >&2
        echo "DRY-RUN: would stage + commit + push to refs/heads/host/$(hostname | sed 's/\..*//')" >&2
        # Still call generate_gitignore — its own dry-run logic will print the diff (no write)
        generate_gitignore || true
        return 0
    fi

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

    # 3. Generate .gitignore (refuses to overwrite an existing file without
    # --force-gitignore per Pro review #3; see generate_gitignore comment).
    generate_gitignore

    # 4. Initial commit + push to host branch
    git add -A 2>/dev/null || true
    if git diff --cached --quiet; then
        log "_init_impl: nothing to commit on init (already-initialized re-run, or empty workspace)"
    else
        git commit -q -m "Initial workspace-vault sync: bootstrap host=$(hostname)"
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

# Pull-side: fetch all peer branches, merge into local host/<hostname> branch
# with 3-way auto-merge first. On unresolvable conflict, use-local fallback
# via `git checkout --ours`. Pull ordering: oldest peer push first (minimizes
# per-step merge diff under the use-local-on-conflict rule).

cmd_pull_only() {
    acquire_lock
    _pull_only_impl
}

_pull_only_impl() {
    cd "$WORKSPACE_DIR" || die "pull-only: cannot cd to $WORKSPACE_DIR"
    [ -d ".git" ] || die "pull-only: $WORKSPACE_DIR is not a git repo; run --init first"

    if [ "$DRY_RUN" = "1" ]; then
        echo "DRY-RUN: would fetch + merge peer branches" >&2
        return 0
    fi

    log "_pull_only_impl: fetching all peer branches"
    git fetch --all --quiet 2>&1 | tee -a "$LOG" >/dev/null

    # Ensure we're on the host branch (idempotent)
    local host current_branch
    host="$(hostname | sed 's/\..*//')"
    current_branch="host/${host}"
    if [ "$(git symbolic-ref --short HEAD 2>/dev/null)" != "$current_branch" ]; then
        if git show-ref --quiet "refs/remotes/origin/${current_branch}"; then
            git checkout -B "$current_branch" "origin/${current_branch}" 2>&1 | tee -a "$LOG" >/dev/null
        else
            git checkout -B "$current_branch" 2>&1 | tee -a "$LOG" >/dev/null
        fi
    fi

    # Pro #1445 review fix #2: snapshot pre-pull state for the mass-deletion
    # tripwire on the pull side. The push-side tripwire only catches staged
    # deletions, but `git merge` can DELETE files in the working tree directly
    # if a peer's branch removed them. Save the pre-merge SHA + tracked-file
    # count so we can detect + roll back a mass-delete merge.
    local pre_pull_sha pre_pull_count
    pre_pull_sha="$(git rev-parse HEAD 2>/dev/null || echo "")"
    pre_pull_count="$(git ls-files | wc -l | tr -d ' ')"

    # Sort peer branches by last-push time, oldest first (per design)
    local peers
    peers=$(git for-each-ref --format='%(committerdate:unix) %(refname:short)' refs/remotes/origin/host/ 2>/dev/null \
                | sort -n | awk '{print $2}')

    local merged=0
    for peer in $peers; do
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

    # Pull-side mass-deletion tripwire — catches deletions that landed via
    # git merge rather than staged rm. Compare tracked-file count drop.
    local post_pull_count max_delete deleted_via_merge
    post_pull_count="$(git ls-files | wc -l | tr -d ' ')"
    max_delete="${SUTANDO_SYNC_MAX_DELETE:-50}"
    deleted_via_merge=$((pre_pull_count - post_pull_count))
    if [ "$deleted_via_merge" -gt "$max_delete" ] && [ "${SUTANDO_FORCE_SYNC:-0}" != "1" ]; then
        log "_pull_only_impl: ABORT — pull deleted $deleted_via_merge files (pre=$pre_pull_count post=$post_pull_count >tripwire $max_delete); resetting to $pre_pull_sha"
        if [ -n "$pre_pull_sha" ]; then
            git reset --hard "$pre_pull_sha" 2>&1 | tee -a "$LOG" >/dev/null
        fi
        echo "sync-workspace: REFUSING pull — peer branch(es) deleted $deleted_via_merge files (>SUTANDO_SYNC_MAX_DELETE=$max_delete). Reset to pre-pull state. Set SUTANDO_FORCE_SYNC=1 to override." >&2
        return 1
    fi

    echo "sync-workspace: pull-only complete (merged $merged peer branches)"
    return 0
}

# Push-side: stage all changes (gitignore filters to carrier set), mass-deletion
# tripwire, commit if anything changed, push to origin/host/<hostname>.

cmd_push_only() {
    acquire_lock
    _push_only_impl
}

_push_only_impl() {
    cd "$WORKSPACE_DIR" || die "push-only: cannot cd to $WORKSPACE_DIR"
    [ -d ".git" ] || die "push-only: $WORKSPACE_DIR is not a git repo; run --init first"

    if [ "$DRY_RUN" = "1" ]; then
        echo "DRY-RUN: would stage + commit + push to refs/heads/host/$(hostname | sed 's/\..*//')" >&2
        return 0
    fi

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

# Default: pull peers first (so own commits build on latest peer state), then push.

cmd_default_bidirectional() {
    acquire_lock
    _pull_only_impl || true   # pull failures shouldn't block push
    _push_only_impl
}

cmd_status() {
    echo "WORKSPACE_DIR: $WORKSPACE_DIR"
    echo "REPO_DIR:      $REPO_DIR"
    echo "VAULT_URL:     ${VAULT_URL:-<unset>}"
    if [ -d "$WORKSPACE_DIR/.git" ]; then
        cd "$WORKSPACE_DIR" || return 1
        local current_branch
        current_branch="$(git symbolic-ref --short HEAD 2>/dev/null || echo "<detached>")"
        echo "current branch: $current_branch"
        echo "remote branches:"
        git for-each-ref --format='  %(refname:short) (last push: %(committerdate:relative))' refs/remotes/origin/host/ 2>/dev/null | head -20
    else
        echo "git status: workspace is NOT a git repo (run --init)"
    fi
    return 0
}

cmd_migrate_from_legacy() {
    acquire_lock
    _migrate_from_legacy_impl
}

# One-time migration from the legacy ~/.sutando/memory-sync/ git repo to the
# new workspace-as-git-repo model. Steps:
#
#   1. Detect legacy clone at $HOME/.sutando/memory-sync/ (or
#      $SUTANDO_MEMORY_SYNC_DIR if set)
#   2. Curated copy of legacy content into workspace's tracked paths:
#      - legacy/notes/ → workspace/notes/
#      - legacy/memory/*.md → workspace/.claude-sutando/projects/<local_slug>/memory/
#        (uses this host's Claude Code-derived slug — `-<REPO_DIR-with-slashes-replaced>`)
#      - legacy/pending-questions.md → workspace/pending-questions.md
#      - legacy/build_log.md → workspace/build_log/<hostname>.md (per-host split)
#   3. Call _init_impl to git-init the workspace + push to vault
#   4. Print operator-supervised next-steps recipe
#
# Safe-by-default: never deletes the legacy dir; never overwrites existing
# workspace files (cp -n everywhere). Operator deletes legacy manually after
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

    log "_migrate_from_legacy_impl: starting migration from $legacy_dir → $WORKSPACE_DIR (DRY_RUN=$DRY_RUN)"
    if [ "$DRY_RUN" = "1" ]; then
        echo "DRY-RUN: migrate from $legacy_dir → $WORKSPACE_DIR" >&2
    else
        echo "sync-workspace migrate: copying from $legacy_dir into $WORKSPACE_DIR" >&2
    fi

    # Local slug derivation: matches Claude Code's auto-derived slug
    # (REPO_DIR with / replaced by -).
    local local_slug
    local_slug="$(printf '%s' "$REPO_DIR" | sed 's|/|-|g')"

    # Wrapper: run a command OR print "DRY-RUN: would ..." prefix. Per Pro
    # review fix #1 (--dry-run safety for the destructive migration path).
    _do() {
        if [ "$DRY_RUN" = "1" ]; then
            echo "DRY-RUN: would: $*" >&2
            return 0
        fi
        "$@"
    }

    _do mkdir -p "$WORKSPACE_DIR/notes" \
                "$WORKSPACE_DIR/build_log" \
                "$WORKSPACE_DIR/.claude-sutando/projects/${local_slug}/memory"

    # 1. notes/ — handle symlink case
    if [ -L "$WORKSPACE_DIR/notes" ]; then
        local symlink_target
        symlink_target="$(readlink "$WORKSPACE_DIR/notes")"
        log "_migrate_from_legacy_impl: workspace/notes is a symlink → $symlink_target; removing + copying content"
        _do rm "$WORKSPACE_DIR/notes"
        _do mkdir -p "$WORKSPACE_DIR/notes"
    fi
    if [ -d "$legacy_dir/notes" ]; then
        if [ "$DRY_RUN" = "1" ]; then
            local n_notes
            n_notes=$(find "$legacy_dir/notes" -type f 2>/dev/null | wc -l | tr -d ' ')
            echo "DRY-RUN: would: cp -an $legacy_dir/notes/. $WORKSPACE_DIR/notes/  (${n_notes} files)" >&2
        else
            cp -an "$legacy_dir/notes"/. "$WORKSPACE_DIR/notes"/ 2>/dev/null || true
            log "_migrate_from_legacy_impl: copied $legacy_dir/notes/ → $WORKSPACE_DIR/notes/ (cp -n)"
        fi
    fi

    # 2. memory/*.md → this host's local slug memory dir
    if [ -d "$legacy_dir/memory" ]; then
        local copied=0 would_copy=0
        for f in "$legacy_dir/memory"/*.md; do
            [ -f "$f" ] || continue
            if [ "$DRY_RUN" = "1" ]; then
                would_copy=$((would_copy+1))
            else
                cp -n "$f" "$WORKSPACE_DIR/.claude-sutando/projects/${local_slug}/memory/" 2>/dev/null && copied=$((copied+1))
            fi
        done
        if [ "$DRY_RUN" = "1" ]; then
            echo "DRY-RUN: would: copy ${would_copy} memory file(s) → local_slug=${local_slug}" >&2
        else
            log "_migrate_from_legacy_impl: copied $copied memory file(s) → local_slug=${local_slug}"
        fi
    fi

    # 3. pending-questions.md (prefer machine-<host>/pending-questions.md if present)
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
        _do cp -n "$pq_src" "$WORKSPACE_DIR/pending-questions.md"
        [ "$DRY_RUN" != "1" ] && log "_migrate_from_legacy_impl: copied $pq_src → workspace/pending-questions.md"
    fi

    # 4. build_log.md → build_log/<hostname>.md (per-host split)
    local bl_src
    if [ -f "$legacy_dir/machine-${host}/build_log.md" ]; then
        bl_src="$legacy_dir/machine-${host}/build_log.md"
    elif [ -f "$legacy_dir/build_log.md" ]; then
        bl_src="$legacy_dir/build_log.md"
    else
        bl_src=""
    fi
    if [ -n "$bl_src" ]; then
        _do cp -n "$bl_src" "$WORKSPACE_DIR/build_log/${host}.md"
        [ "$DRY_RUN" != "1" ] && log "_migrate_from_legacy_impl: copied $bl_src → workspace/build_log/${host}.md"
    fi

    # 5. Hand off to _init_impl for git init + first push (DRY_RUN propagates)
    log "_migrate_from_legacy_impl: handing off to _init_impl for git init + first push"
    _init_impl

    # 6. Operator-facing next steps
    cat <<EOF >&2

sync-workspace migrate: complete.

Next steps (operator-supervised):
  1. Verify the new workspace has the expected content:
       ls $WORKSPACE_DIR/notes/ | head
       ls $WORKSPACE_DIR/.claude-sutando/projects/${local_slug}/memory/ | head
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
# Section 6 — Subcommand dispatch                                              #
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
