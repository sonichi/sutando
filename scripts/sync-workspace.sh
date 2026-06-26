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
# `host/<hostname>/<wsId>`; pulls all peers via fetch + merge. Conflicts use 3-way
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
# Vault URL resolution (PR-2 — issue #1445 followup):
#   1. --vault-url <url> CLI flag (tests, one-shot overrides; canonical for explicit)
#   2. sutando.config.local.json → vault.remote_url (per-clone canonical)
#   3. sutando.config.json → vault.remote_url (tracked default)
#   4. .env SUTANDO_MEMORY_REPO (deprecated legacy alias; warn-and-honor for one release)
#
# Note: SUTANDO_VAULT env var (introduced in PR-1 = #1445) is REMOVED in PR-2.
# Brand new, no users to deprecate; CLI flag + config-file is the canonical surface.
#
# Other env vars:
#   SUTANDO_REPO_DIR          — path to sutando code checkout. Auto-detected from script path.
#   NO_COLOR                  — suppress ANSI escapes in warnings (no-color.org)
#   SUTANDO_SYNC_MAX_DELETE   — mass-deletion absolute tripwire (default 50)
#   SUTANDO_SYNC_MAX_DELETE_PCT — mass-deletion percentage tripwire (default 50;
#                                  catches small-workspace catastrophic deletes)
#   SUTANDO_FORCE_SYNC        — bypass mass-deletion tripwire (set =1)

set -euo pipefail

# --------------------------------------------------------------------------- #
# Section 0 — Global flags (parsed from args before subcommand dispatch)        #
# --------------------------------------------------------------------------- #

DRY_RUN=0            # --dry-run: skip mutating ops, print "would: ..." instead
FORCE_GITIGNORE=0    # --force-gitignore: overwrite existing .gitignore without warning
VAULT_URL_FLAG=""    # --vault-url <url>: explicit vault URL override (PR-2)

# Parse global flags out of $@ (leaves only the subcommand + its args). Two-arg
# flags (`--vault-url <url>`) supported via _consume_next state; equals-form
# (`--vault-url=<url>`) supported via prefix match.
_args=()
_consume_next=""
for _arg in "$@"; do
    if [ -n "$_consume_next" ]; then
        case "$_consume_next" in
            vault-url) VAULT_URL_FLAG="$_arg" ;;
        esac
        _consume_next=""
        continue
    fi
    case "$_arg" in
        --dry-run)         DRY_RUN=1 ;;
        --force-gitignore) FORCE_GITIGNORE=1 ;;
        --vault-url)       _consume_next="vault-url" ;;
        --vault-url=*)     VAULT_URL_FLAG="${_arg#--vault-url=}" ;;
        *)                 _args+=("$_arg") ;;
    esac
done
if [ -n "$_consume_next" ]; then
    echo "sync-workspace: --$_consume_next requires a value" >&2
    exit 2
fi
# Reset $@ to non-flag args
set -- "${_args[@]:-}"
unset _args _consume_next

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

# Resolve vault URL via the PR-2 priority chain. The `.env` file was already
# `set -a; . .env; set +a`-loaded above, so SUTANDO_MEMORY_REPO appears as an
# env var if set in .env — no need to re-grep the file (eliminates the
# var=$(grep | head | ...) set-e trap class entirely; see Mini #1445 v4 Medium).
VAULT_URL=""

# Priority 1: --vault-url CLI flag (explicit)
if [ -n "$VAULT_URL_FLAG" ]; then
    VAULT_URL="$VAULT_URL_FLAG"
fi

# Priority 2+3: sutando.config.{local,base}.json → vault.remote_url
# (loader merges local + base + applies ${REPO_DIR} substitution)
if [ -z "$VAULT_URL" ] && [ -f "$SCRIPT_PARENT/scripts/sutando-config.sh" ]; then
    VAULT_URL="$(bash "$SCRIPT_PARENT/scripts/sutando-config.sh" vault-url 2>/dev/null || true)"
fi

# Priority 4: legacy .env SUTANDO_MEMORY_REPO (warn-and-honor for one release)
if [ -z "$VAULT_URL" ] && [ -n "${SUTANDO_MEMORY_REPO:-}" ]; then
    VAULT_URL="$SUTANDO_MEMORY_REPO"
    echo "sync-workspace: SUTANDO_MEMORY_REPO is deprecated; move vault URL to sutando.config.local.json under vault.remote_url." >&2
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

# Host identity (used for `host/<host>` branch name + commit messages).
# SUTANDO_HOST_OVERRIDE is a TEST-ONLY shim — set per-invocation so the
# hermetic multi-host test can simulate two hosts from a single machine
# (sutando-workspace.test.sh Test 23, Codex P1.3 reproducer). Not for
# production use.
_host() {
    # Lockstep with `_host_label()` in src/util_paths.py. Precedence:
    #   1. $SUTANDO_HOST_LABEL (or legacy $SUTANDO_HOST_OVERRIDE)
    #   2. macOS `scutil --get LocalHostName` (stable Bonjour name)
    #   3. short `hostname`
    # scutil before hostname because a DHCP-assigned hostname can drift (e.g.
    # Comcast → Chis-MBP) and split per-host paths/branches from the stable
    # LocalHostName (Chis-MacBook-Pro). 2026-06-22 incident.
    local env="${SUTANDO_HOST_LABEL:-${SUTANDO_HOST_OVERRIDE:-}}"
    if [ -n "$env" ]; then
        printf '%s\n' "$env"
        return
    fi
    # Capture scutil ONCE and guard exit-0-but-empty output (parity with the
    # py side's non-empty `.strip()` check) — an empty LocalHostName must not
    # win over the hostname fallback.
    local lhn=""
    if command -v scutil >/dev/null 2>&1; then
        lhn="$(scutil --get LocalHostName 2>/dev/null)"
    fi
    if [ -n "$lhn" ]; then
        printf '%s\n' "$lhn"
    else
        hostname | sed 's/\..*//'
    fi
}

# Workspace identity (used for `host/<host>/<wsId>` branch name).
# Persisted at <workspace>/.sutando-vault/ws-id — 6-char lowercase hex,
# generated on first --init and reused thereafter. Decouples branch identity
# from hostname so the same host can run multiple workspaces (different
# checkouts) without their vault branches colliding. The wsId travels WITH the
# workspace, so moving the same workspace to a different host keeps pushing
# to the same wsId branch under the new hostname subdirectory — that reflects
# "workspace is the identity" rather than "host is the identity."
#
# SUTANDO_WS_ID_OVERRIDE is a TEST-ONLY shim (sibling of SUTANDO_HOST_OVERRIDE)
# so the hermetic multi-workspace test can pin two known wsIds. Not for prod.
_ws_id() {
    local ws_id_file="$WORKSPACE_DIR/.sutando-vault/ws-id"
    # File wins when present — guarantees stable identity across invocations
    # and across env-var noise.
    if [ -f "$ws_id_file" ]; then
        tr -d '[:space:]' < "$ws_id_file"
        printf '\n'
        return 0
    fi
    # No existing file: figure out what wsId to materialize.
    local new_id
    if [ -n "${SUTANDO_WS_ID_OVERRIDE:-}" ]; then
        # Test-only shim pins the wsId to a known value and PERSISTS it so
        # subsequent invocations (e.g. a follow-up --status without the env)
        # read the same value from disk. That matches the production
        # "generate-then-persist" contract — override just supplies the seed
        # rather than letting /dev/urandom pick.
        new_id="$SUTANDO_WS_ID_OVERRIDE"
    else
        # Generate fresh: 6 lowercase hex chars (24 bits = 16M permutations;
        # collision probability negligible for any plausible host's number
        # of workspaces).
        new_id="$(LC_ALL=C tr -dc 'a-f0-9' </dev/urandom 2>/dev/null | head -c 6)"
        if [ -z "$new_id" ]; then
            # Fallback when /dev/urandom is unavailable (some CI sandboxes).
            new_id="$(date +%s%N 2>/dev/null | LC_ALL=C tr -dc 'a-f0-9' | tail -c 6)"
            [ -z "$new_id" ] && new_id="$(printf '%06x' $$)"
        fi
    fi
    mkdir -p "$(dirname "$ws_id_file")"
    printf '%s\n' "$new_id" > "$ws_id_file"
    log "_ws_id: generated fresh wsId $new_id for workspace $WORKSPACE_DIR"
    printf '%s\n' "$new_id"
}

# Composite host-and-workspace branch name segment. Joined with `/` so the
# resulting refspec `host/<hostname>/<wsId>` forms git's natural ref-tree
# hierarchy — e.g. `git for-each-ref refs/remotes/origin/host/<hostname>/`
# enumerates all workspaces on that one host.
_host_ws_segment() {
    printf '%s/%s\n' "$(_host)" "$(_ws_id)"
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
# PR-3: emit gitignore lines for a single include path. Recursively un-ignores
# ancestor directories (gitignore can't include a child whose ancestor is
# excluded by `*`). Paths ending in `/` are treated as directories (emit
# ancestor `!prefix/` chain + final `!path/**`); paths without trailing `/`
# are files (ancestor chain + verbatim `!path`).
_emit_include_lines() {
    local path="$1"
    [ -z "$path" ] && return
    local is_dir=0
    if [[ "$path" == */ ]]; then
        is_dir=1
        path="${path%/}"
    fi
    # Ancestor chain: for dir-path = full chain; for file-path = parent chain only
    local chain_target="$path"
    [ "$is_dir" = "0" ] && [[ "$path" == */* ]] && chain_target="${path%/*}"
    # Emit ancestor un-ignores when there IS a directory chain
    if [ "$is_dir" = "1" ] || [[ "$path" == */* ]]; then
        local prefix="" part
        IFS='/' read -ra _parts <<<"$chain_target"
        for part in "${_parts[@]}"; do
            if [ -z "$prefix" ]; then prefix="$part"; else prefix="$prefix/$part"; fi
            echo "!${prefix}/"
        done
        unset IFS _parts
    fi
    # Final un-ignore
    if [ "$is_dir" = "1" ]; then
        echo "!${path}/**"
    else
        echo "!${path}"
    fi
}

# PR-3: emit gitignore line(s) for a single exclude path. Excludes carve out
# subpaths from an otherwise-included parent. For dirs emit `path/` + `path/**`;
# for files emit verbatim. Emitted AFTER includes so last-match wins.
_emit_exclude_lines() {
    local path="$1"
    [ -z "$path" ] && return
    if [[ "$path" == */ ]]; then
        local stripped="${path%/}"
        echo "${stripped}/"
        echo "${stripped}/**"
    else
        echo "${path}"
    fi
}

# Compose the sync rule set written to `<workspace>/.git/info/exclude`.
#
# Why `.git/info/exclude` and not `<workspace>/.gitignore`:
# The outer sutando repo ignores `workspace/*`, but a tracked-in-tree
# `workspace/.gitignore` with `!notes/` un-ignore rules was overriding
# that outer deny — gitignore's deeper-dir-wins precedence let inner
# un-ignores leak workspace content into the OUTER repo's `git status`
# (data-leak reproduced 2026-06-04: `workspace/.gitignore` and
# `workspace/notes/` showed as `??` in outer despite outer's `workspace/*`).
# `.git/info/exclude` lives INSIDE `.git/` which outer treats as opaque,
# so identical un-ignore rules here cannot cross the inner/outer boundary.
#
# Carrier set driven by vault.sync.{include,exclude} in
# sutando.config.{json,local.json} (PR-3). Edit those to customize.
_compose_exclude_content() {
    echo "# Generated by sync-workspace.sh — do not edit by hand."
    echo "# Source: scripts/sync-workspace.sh::_compose_exclude_content"
    echo "# Lives at <workspace>/.git/info/exclude — per-clone, not tracked."
    echo "# Outer sutando repo treats .git/ as opaque, so un-ignore (\`!\`)"
    echo "# rules below cannot leak across the inner/outer boundary."
    echo "# Carrier set driven by vault.sync.{include,exclude} in"
    echo "# sutando.config.{json,local.json}. Edit those to customize."
    echo ""
    echo "# Whitelist mode: ignore everything by default, un-ignore the carrier set."
    echo "*"

    # Includes from config (per-clone overrides in sutando.config.local.json)
    local include_list exclude_list path
    include_list="$(bash "$SCRIPT_PARENT/scripts/sutando-config.sh" vault-sync-include 2>/dev/null || true)"
    if [ -n "$include_list" ]; then
        echo ""
        echo "# Carrier set — from vault.sync.include"
        while IFS= read -r path; do
            [ -z "$path" ] && continue
            _emit_include_lines "$path"
        done <<<"$include_list"
    fi

    # Excludes — emitted after includes so gitignore last-match wins
    exclude_list="$(bash "$SCRIPT_PARENT/scripts/sutando-config.sh" vault-sync-exclude 2>/dev/null || true)"
    if [ -n "$exclude_list" ]; then
        echo ""
        echo "# Carve-outs — from vault.sync.exclude"
        while IFS= read -r path; do
            [ -z "$path" ] && continue
            _emit_exclude_lines "$path"
        done <<<"$exclude_list"
    fi

    echo ""
    echo "# Hard-deny credentials regardless of carrier set"
    echo ".env*"
    echo "*.heartbeat"
    echo "*.alive"
    echo "*.sentinel"
    echo "*.pid"
    # Secret material — name-pattern deny (M3). The deny list above caught
    # transient state + .env*; it did NOT cover SSH private keys or
    # cert/key material, which would be carried if they ever landed in a
    # synced path. These are gitignore-style globs composed into
    # .git/info/exclude. Public keys (*.pub) are intentionally NOT denied.
    echo "id_rsa"
    echo "id_dsa"
    echo "id_ecdsa"
    echo "id_ed25519"
    echo "*.pem"
    echo "*.key"
    echo "*.p12"
    echo "*.pfx"
    echo "*.ppk"
    echo "*.keystore"
    echo "*.jks"
}

# Write `<workspace>/.git/info/exclude` from the composed content. Also
# deletes a legacy `<workspace>/.gitignore` if one exists (migration from
# the pre-(6) layout that wrote rules to that tracked-in-tree path).
#
# Pro #1445 review fix #3: don't silently clobber an existing exclude file.
# If the file exists AND differs from what we'd write, refuse to overwrite
# unless the operator passes `--force-gitignore`. Print a diff so they can
# see what would change. The risk this protects against: an
# operator-edited exclude file that explicitly blocks something the user
# DOES want synced would silently get reinstated by overwrite → data loss
# in the vault.
generate_exclude() {
    local exclude_path tmp_path legacy_gitignore
    exclude_path="$WORKSPACE_DIR/.git/info/exclude"
    legacy_gitignore="$WORKSPACE_DIR/.gitignore"
    tmp_path="$(mktemp -t sync-workspace-exclude.XXXXXX)"
    _compose_exclude_content > "$tmp_path"

    # Migration: an in-tree .gitignore is the leak source — drop it. The
    # rules now live in .git/info/exclude (per-clone, opaque to outer).
    #
    # If it is TRACKED — an older host committed it to the vault, and its own
    # `!.gitignore` rule self-tracks it — a plain `rm -f` deletes only the
    # local copy: the file is re-materialized on the next peer pull/merge, so
    # the inner/outer leak (workspace content showing in the OUTER repo's
    # status; see the boundary note above) recurs forever. `git rm` instead, so
    # the untrack is committed and propagates through the vault history — the
    # file then disappears from every device on its next pull. Fall back to
    # `rm -f` when untracked (fresh local cruft, or pre-first-commit --init).
    if [ -f "$legacy_gitignore" ] && [ "$DRY_RUN" != "1" ]; then
        if git -C "$WORKSPACE_DIR" ls-files --error-unmatch .gitignore >/dev/null 2>&1; then
            git -C "$WORKSPACE_DIR" rm -q -f .gitignore
            log "generate_exclude: git-rm'd TRACKED in-tree $legacy_gitignore (untrack propagates via vault; rules live in .git/info/exclude)"
        else
            rm -f "$legacy_gitignore"
            log "generate_exclude: removed untracked in-tree $legacy_gitignore (rules moved to .git/info/exclude)"
        fi
    fi

    # NB: do NOT mkdir in --dry-run. Creating `.git/info` when no real repo
    # exists leaves a STUB `.git/` (a lone `info/`, no HEAD/objects). A later
    # `_init_impl` then sees `.git` present, skips `git init`, and git walks UP
    # to a parent repo's worktree (e.g. a submodule) — hijacking it. A dry-run
    # must never mutate state. (See the toplevel-isolation guard in _init_impl.)
    if [ "$DRY_RUN" != "1" ] && [ ! -d "$WORKSPACE_DIR/.git/info" ]; then
        mkdir -p "$WORKSPACE_DIR/.git/info"
    fi

    if [ -f "$exclude_path" ]; then
        if diff -q "$exclude_path" "$tmp_path" >/dev/null 2>&1; then
            # Identical — no-op
            rm -f "$tmp_path"
            log "generate_exclude: existing $exclude_path matches; no-op"
            return 0
        fi
        # `.git/info/exclude` ships with a stock git-init comment header
        # only (no `*` rule). Treat that case as "first generation, not
        # operator-customized" and overwrite without prompting.
        if ! grep -qE '^[^#]' "$exclude_path" 2>/dev/null; then
            log "generate_exclude: existing $exclude_path is stock comments only; overwriting"
        elif [ "$FORCE_GITIGNORE" != "1" ]; then
            color_warn "sync-workspace: $exclude_path EXISTS and DIFFERS from the generated content."
            color_warn "Refusing to overwrite (operator-authored content may block carrier-set paths)."
            echo "" >&2
            echo "Diff (existing → would-be-generated):" >&2
            # NB: `diff` exits 1 when files differ + `head -40` may SIGPIPE on
            # long output → with `set -euo pipefail` the pipeline exits nonzero,
            # tripping set -e before tmp_path cleanup. Mini #1445 v3 Medium fix.
            diff -u "$exclude_path" "$tmp_path" 2>&1 | head -40 >&2 || true
            echo "" >&2
            echo "To overwrite anyway: pass --force-gitignore" >&2
            echo "(Or merge desired changes into the existing file by hand.)" >&2
            rm -f "$tmp_path"
            return 1
        else
            log "generate_exclude: overwriting existing $exclude_path (--force-gitignore)"
        fi
    fi

    if [ "$DRY_RUN" = "1" ]; then
        echo "DRY-RUN: would write $exclude_path ($(wc -l < "$tmp_path" | tr -d ' ') lines)" >&2
        rm -f "$tmp_path"
        return 0
    fi
    mv "$tmp_path" "$exclude_path"
    log "generate_exclude: wrote $exclude_path"
}

# Carrier-set enforcement, pre-stage half — heal the exclude rules and
# untrack anything that is tracked but now excluded. Runs on EVERY push
# tick, not just --init: the 2026-06-11 incident showed a workspace whose
# info/exclude was never written (a stale engine copy ran the hooks for
# weeks) — and because gitignore-class rules never untrack already-tracked
# files, channel-token .env files + 5,130 task/result files ratcheted into
# vault history with no path back. generate_exclude is a cheap no-op when
# current, and the untrack walk only pays when rules and index disagree.
_enforce_carrier_set_pre() {
    # Respect an operator-customized exclude file: generate_exclude returns 1
    # and prints the diff in that case; the tick continues against the
    # operator's rules rather than dying.
    generate_exclude || log "_enforce_carrier_set_pre: keeping operator-authored exclude file (see warning above)"
    local _untracked_n=0 _ex
    # `git check-ignore --stdin` exits 1 when nothing matches — that exit
    # dies inside the process substitution, which set -e does not observe;
    # the loop simply sees no input. NUL-delimited for metachar/space paths.
    # --no-index is LOAD-BEARING: without it check-ignore consults the index
    # and never reports tracked files as ignored — which is precisely the
    # population this walk exists to untrack (verified live 2026-06-11: the
    # 5,130-file walk found zero candidates until this flag).
    while IFS= read -r -d '' _ex; do
        git rm -q --cached -- "$_ex" 2>/dev/null || true
        _untracked_n=$((_untracked_n + 1))
    done < <(git ls-files -z | git check-ignore -z --stdin --no-index 2>/dev/null)
    if [ "$_untracked_n" -gt 0 ]; then
        log "_enforce_carrier_set_pre: untracked $_untracked_n newly-excluded file(s) from the vault index"
        echo "sync-workspace: carrier-set enforcement untracked $_untracked_n file(s) that exclude rules no longer cover (content stays on disk; untrack propagates via vault)" >&2
    fi
}

# Carrier-set enforcement, post-stage half — refuse credential-shaped files
# at the staging boundary even when exclude rules missed them (defense in
# depth; the exclude file is config, this is policy). File-level refusal,
# not run-level: dying here would wedge every future tick behind one bad
# path — silent staleness, the exact failure mode sync exists to prevent.
_refuse_staged_secrets() {
    local _secret_hits=0 _sf
    while IFS= read -r -d '' _sf; do
        case "$_sf" in
            # NB: deliberately NOT a bare `*token*.json` — that matched
            # design-tokens-starter.json (a UI template) on first live run.
            # Credential-shaped means: .env family, credentials*.json, and
            # files whose basename is exactly token.json / *_token.json /
            # *-token.json (cloud-auth.json style lives under state/auth/,
            # which is never tracked to begin with).
            .env|*/.env|.env.*|*/.env.*|*credentials*.json|token.json|*/token.json|*_token.json|*-token.json)
                # rm --cached works for both newly-added and tracked files;
                # reset is the fallback for an added-but-never-committed path.
                git rm -q --cached -- "$_sf" 2>/dev/null || git reset -q HEAD -- "$_sf" 2>/dev/null || true
                color_warn "sync-workspace: SECRET-GUARD refused '$_sf' (credential-shaped path) — kept on disk, never synced"
                _secret_hits=$((_secret_hits + 1))
                ;;
        esac
    done < <(git diff --cached --name-only --diff-filter=AM -z)
    # --diff-filter=AM is LOAD-BEARING: a staged DELETION of a secret is the
    # carrier-set untrack doing its job — on first live run the unfiltered
    # loop matched those D entries and its reset fallback RESTORED the .env
    # files to the index, silently undoing the heal (caught 2026-06-11).
    [ "$_secret_hits" -gt 0 ] && log "_refuse_staged_secrets: refused $_secret_hits credential-shaped file(s)"
    return 0
    return 0
}

# Snapshot the per-host config that LIVES at $CLAUDE_CONFIG_DIR into
# <workspace>/hosts/<host>/ so it's carried by the hosts/*/ vault glob and
# survives a rebuild. settings.json and channel access.json are owned by Claude
# Code / the bridges at $CLAUDE_CONFIG_DIR — they can't be relocated, so they're
# *backed up* here (the live readers keep reading $CLAUDE_CONFIG_DIR; hosts/<host>/
# is a pure backup → no read/write skew).
#
# NOT snapshotted: PERSONAL_CLAUDE.md / stand-identity.json / tab-aliases.json —
# those follow the RELOCATION model (migrator one-time move + personal_path /
# CLAUDE.md readers that prefer hosts/<host>/). Snapshotting them would make the
# reader prefer a stale snapshot over the live root file.
#
# Secret-safe: copies ONLY access.json, never the sibling .env (bot tokens).
# Non-fatal by construction: every step tolerates failure and the function
# returns 0, so a snapshot hiccup can never block the push.
_snapshot_per_host_config() {
    local _cfg
    _cfg="$(bash "$SCRIPT_PARENT/scripts/sutando-config.sh" claude-home-path)" || return 0
    local _host_dir="$WORKSPACE_DIR/hosts/$(_host)"
    mkdir -p "$_host_dir" 2>/dev/null || return 0

    if [ -f "$_cfg/settings.json" ]; then
        cp -p "$_cfg/settings.json" "$_host_dir/settings.json" 2>/dev/null || true
    fi

    # Channel access.json only (allowlists / TOFU / tier-maps). Never the
    # sibling .env — that's a hard-denied secret.
    local _ch _svc
    for _ch in "$_cfg"/channels/*/access.json; do
        [ -f "$_ch" ] || continue
        _svc="$(basename "$(dirname "$_ch")")"
        mkdir -p "$_host_dir/channels/$_svc" 2>/dev/null || continue
        cp -p "$_ch" "$_host_dir/channels/$_svc/access.json" 2>/dev/null || true
    done

    # build_log.md is per-host (F1 decision) but its loop-writer keeps it at the
    # workspace root and reads it from there — so it's snapshot-model like the
    # files above (live at root, backup here; nothing reads the hosts/ copy live,
    # so no read/write skew). Its root entry is dropped from vault.sync.include
    # in tandem with this (it was colliding across hosts as a bare carried path);
    # this snapshot is what carries it per-host instead.
    if [ -f "$WORKSPACE_DIR/build_log.md" ]; then
        cp -p "$WORKSPACE_DIR/build_log.md" "$_host_dir/build_log.md" 2>/dev/null || true
    fi
    return 0
}

# Guard against running push/pull on a workspace that has a `.git` directory
# but was never properly sync-initialized. Without this, a stray `.git` (from
# a half-completed prior init, a backup restore, or operator-`git init` for
# unrelated reasons) lets `_push_only_impl` run `git add -A` against a tree
# with NO whitelist, silently staging + committing + pushing the WHOLE
# workspace — credentials, media, vendor caches, everything. Lucy's Maddy
# v0.8 migration report (2026-06-06): plain `bash scripts/sync-workspace.sh`
# silent-committed an uninitialized state.
#
# Sentinels we accept as proof of a real init (either suffices):
#   1. `.git/info/exclude` contains the generator marker from
#      `_compose_exclude_content()` — proves we wrote the whitelist.
#   2. `.sutando-vault/ws-id` — proves _init_impl reached its ws-id step (PR #1459).
#
# If neither is present, refuse with a clear error pointing to --init.
# Override: `SUTANDO_SYNC_SKIP_INIT_GUARD=1` for an operator who knows what
# they're doing (e.g. resurrecting a pre-marker init from before this fix).
_assert_sync_initialized() {
    local _caller="${1:-sync}"
    [ "${SUTANDO_SYNC_SKIP_INIT_GUARD:-0}" = "1" ] && return 0

    local _exclude="$WORKSPACE_DIR/.git/info/exclude"
    local _wsid="$WORKSPACE_DIR/.sutando-vault/ws-id"
    if [ -f "$_exclude" ] && grep -q "Generated by sync-workspace.sh" "$_exclude" 2>/dev/null; then
        return 0
    fi
    if [ -f "$_wsid" ]; then
        return 0
    fi
    die "${_caller}: $WORKSPACE_DIR has .git but sync was never initialized (no whitelist marker in .git/info/exclude and no .sutando-vault/ws-id). Refusing to push — git add -A here would commit the WHOLE workspace tree with NO carrier-set filter. Run: bash scripts/sync-workspace.sh --init  (or set SUTANDO_SYNC_SKIP_INIT_GUARD=1 to bypass at your own risk)"
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
        echo "DRY-RUN: would (re)generate .git/info/exclude" >&2
        echo "DRY-RUN: would stage + commit + push to refs/heads/host/$(_host_ws_segment)" >&2
        # Still call generate_exclude — its own dry-run logic will print the diff (no write)
        generate_exclude || true
        return 0
    fi

    # 1. git init if not already a *valid, isolated* repo.
    #
    # A bare `-d .git` check is insufficient. A stub `.git/` (e.g. a lone
    # `.git/info/` left by a prior `--dry-run`, a half-finished init, or a
    # backup restore) passes `-d` but is NOT a real repo. git then walks UP to
    # the nearest parent repo's worktree — e.g. when the workspace lives inside
    # a git SUBMODULE — and every subsequent remote/add/commit/push silently
    # hijacks that parent (rewrites its origin, commits its whole tree, pushes
    # it to the vault). Decide by the resolved toplevel: this is "already a
    # repo" ONLY if git resolves THIS dir as its own toplevel.
    local _top
    _top="$(git -C "$WORKSPACE_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -n "$_top" ] && [ "$_top" -ef "$WORKSPACE_DIR" ]; then
        log "_init_impl: $WORKSPACE_DIR is already a git repo"
    else
        git init -q
        log "_init_impl: git init done in $WORKSPACE_DIR"
        echo "sync-workspace: git init done in $WORKSPACE_DIR" >&2
        # Fail-safe: confirm the fresh repo isolated (git did NOT climb out to
        # a parent worktree). If it still resolves elsewhere, refuse rather
        # than operate on — and corrupt — a parent repo.
        _top="$(git -C "$WORKSPACE_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
        if ! { [ -n "$_top" ] && [ "$_top" -ef "$WORKSPACE_DIR" ]; }; then
            die "init: $WORKSPACE_DIR did not isolate as its own git repo (git resolved toplevel: ${_top:-<none>}). Refusing — remote/commit/push would leak into a parent repo. If the workspace is nested inside another git repo (e.g. a submodule), run 'git -C \"$WORKSPACE_DIR\" init' manually, verify 'git -C \"$WORKSPACE_DIR\" rev-parse --absolute-git-dir' points at \$WORKSPACE_DIR/.git, then re-run --init."
        fi
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

    # 3. Generate .git/info/exclude (refuses to overwrite an existing
    # exclude file without --force-gitignore per Pro #1445 review fix #3;
    # see generate_exclude comment). Also removes a legacy in-tree
    # .gitignore on first run (the (4)→(6) leak-fix migration).
    generate_exclude

    # 4. Initial commit + push to host branch. The carrier-set whitelist
    # lives in .git/info/exclude (opaque to outer sutando repo), so plain
    # `git add -A` honors the un-ignore rules without crossing the
    # inner/outer boundary that the in-tree .gitignore previously breached
    # (2026-06-04 leak fix).
    git add -A 2>/dev/null || true
    _refuse_staged_secrets
    # First-init must push a host branch to the vault even on an empty
    # workspace (no carrier-set files yet). The pre-(6) layout had an
    # in-tree .gitignore that always staged a non-empty index; with rules
    # moved to .git/info/exclude there's nothing tracked-in-tree to anchor
    # the initial commit. Allow an empty commit only when HEAD doesn't
    # exist yet (first init); on a re-init with HEAD present, a clean
    # index stays a no-op so the script doesn't spam "Initial bootstrap"
    # commits on every invocation.
    local _do_commit=0
    if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
        _do_commit=1
        local _empty_flag="--allow-empty"
    elif ! git diff --cached --quiet; then
        _do_commit=1
        local _empty_flag=""
    else
        local _empty_flag=""
    fi
    if [ "$_do_commit" = "0" ]; then
        log "_init_impl: nothing to commit on init (already-initialized re-run, or empty workspace)"
    else
        # Commit message includes path=<workspace_path> so a peer host
        # browsing the vault can map `host/<host>/<wsId>` back to a local
        # folder via `git log host/<host>/<wsId>` without an extra metadata
        # file. The path is the absolute workspace directory on the host
        # that initialized this branch.
        # shellcheck disable=SC2086  # intentional word-split on $_empty_flag
        git commit -q $_empty_flag -m "Initial workspace-vault sync: bootstrap host=${SUTANDO_HOST_OVERRIDE:-$(hostname)} path=${WORKSPACE_DIR}"
        log "_init_impl: initial commit created"

        local host_ws_seg
        host_ws_seg="$(_host_ws_segment)"
        if git push origin "HEAD:refs/heads/host/${host_ws_seg}" 2>&1 | tee -a "$LOG" >/dev/null; then
            log "_init_impl: pushed to origin host/${host_ws_seg}"
            echo "sync-workspace: initialized + pushed to host/${host_ws_seg}"
        else
            log "_init_impl: push failed (may need to set up tracking on first push)"
            echo "sync-workspace: initialized but push failed; check $LOG" >&2
            return 1
        fi
    fi
    return 0
}

# wsId migration (#1459 follow-up): retire pre-wsId flat `host/<host>` branches.
# Before #1459 each host pushed to a flat branch `host/<host>`. Post-#1459 the
# branch is nested: `host/<host>/<wsId>`. A leftover flat branch — local OR on
# the vault — is a leaf ref that DIRECTORY/FILE-conflicts with the nested ref:
# git cannot create `refs/{heads,remotes/origin}/host/<host>/<wsId>` while a ref
# named `.../host/<host>` exists ("cannot lock ref ... exists"). Left unhandled
# this stranded the pull-side `checkout -B` (whose error used to be swallowed by
# `... 2>&1 | tee >/dev/null`), so the script ran on the WRONG branch and
# reported success while pushing nothing. This helper carries the flat branch's
# history into the wsId branch and removes the flat ref (local + remote +
# remote-tracking) so the wsId scheme can take over. Idempotent: a no-op once no
# flat ref remains. Run BEFORE the checkout/fetch-merge in _pull_only_impl.
_migrate_flat_branch() {
    local host flat_branch wsid_branch
    host="$(_host)"
    flat_branch="host/${host}"
    wsid_branch="host/$(_host_ws_segment)"
    # Defensive: only meaningful while flat != nested (always true post-wsId).
    [ "$flat_branch" = "$wsid_branch" ] && return 0

    # --- local flat branch ---
    if git show-ref --quiet "refs/heads/${flat_branch}"; then
        local flat_sha
        flat_sha="$(git rev-parse "refs/heads/${flat_branch}")"
        log "_migrate_flat_branch: local flat $flat_branch ($flat_sha) -> $wsid_branch"
        echo "sync-workspace: migrating local flat branch $flat_branch -> $wsid_branch (wsId migration)" >&2
        # Move HEAD off the flat branch so it can be deleted.
        if [ "$(git symbolic-ref --short HEAD 2>/dev/null)" = "$flat_branch" ]; then
            git checkout --detach --quiet >>"$LOG" 2>&1 \
                || die "wsId migration: failed to detach HEAD off $flat_branch"
        fi
        # Decide whether the wsId branch needs seeding BEFORE we delete the flat
        # branch (don't clobber an existing wsId branch — it already carries this
        # content or newer).
        local _need_seed=0
        git show-ref --quiet "refs/heads/${wsid_branch}" || _need_seed=1
        # Delete the flat branch FIRST: it is a leaf ref that D/F-conflicts with
        # the nested `host/<host>/<wsId>` ref, so the wsId branch cannot be
        # created while it exists. flat_sha (captured above) preserves its tip.
        git branch -D "$flat_branch" >>"$LOG" 2>&1 || true
        if [ "$_need_seed" -eq 1 ]; then
            git branch "$wsid_branch" "$flat_sha" >>"$LOG" 2>&1 \
                || die "wsId migration: failed to seed $wsid_branch from $flat_branch"
        fi
    fi

    # --- remote flat branch (and its stale remote-tracking ref) ---
    if git show-ref --quiet "refs/remotes/origin/${flat_branch}"; then
        local remote_flat_sha
        remote_flat_sha="$(git rev-parse "refs/remotes/origin/${flat_branch}")"
        # Only retire the remote flat branch once its content is preserved in our
        # wsId branch (ancestor check) — never drop unmerged history. If it isn't
        # contained yet, warn and leave it for the operator (rather than the
        # pre-fix behavior of silently colliding).
        if git show-ref --quiet "refs/heads/${wsid_branch}" \
           && git merge-base --is-ancestor "$remote_flat_sha" "refs/heads/${wsid_branch}"; then
            echo "sync-workspace: retiring vault flat branch $flat_branch (superseded by $wsid_branch)" >&2
            # Delete the remote flat branch BEFORE pushing the nested wsId branch:
            # git refuses to create refs/heads/host/<host>/<wsId> on the vault
            # while the leaf ref refs/heads/host/<host> exists — even inside an
            # `--atomic` push (the loose-ref backend D/F-checks before completing
            # the delete). Content is already preserved in our local wsId branch
            # (ancestor check above), so the brief window where the vault has
            # neither ref is safe: the push below re-establishes it immediately,
            # and on failure the content stays local for the next sync to re-push.
            git push origin --delete "$flat_branch" >>"$LOG" 2>&1 \
                || log "_migrate_flat_branch: remote delete of $flat_branch failed (already gone?)"
            # Drop the local remote-tracking ref so it neither D/F-conflicts with
            # the nested tracking ref on the next fetch nor gets merged as a bogus
            # "peer" in the loop below.
            git update-ref -d "refs/remotes/origin/${flat_branch}" >>"$LOG" 2>&1 || true
            # Push the wsId branch now so this host's content stays visible to
            # peers. We cannot defer to the bidirectional push step — it skips a
            # clean tree (nothing-to-commit gate), so on a no-change pass the
            # nested branch would never land.
            local _mp_rc=0
            git push origin "refs/heads/${wsid_branch}:refs/heads/${wsid_branch}" >>"$LOG" 2>&1 || _mp_rc=$?
            if [ "$_mp_rc" -eq 0 ]; then
                log "_migrate_flat_branch: retired remote flat $flat_branch, pushed $wsid_branch"
            else
                log "_migrate_flat_branch: push of $wsid_branch failed (exit $_mp_rc) after retiring flat; content is local, next sync re-pushes"
                echo "sync-workspace: WARNING — retired vault flat branch but $wsid_branch push failed; content safe locally, will retry next sync (see $LOG)" >&2
            fi
        else
            log "_migrate_flat_branch: NOT deleting remote flat origin/$flat_branch — content not yet in $wsid_branch"
            echo "sync-workspace: vault flat branch $flat_branch not retired — its history isn't in $wsid_branch yet; resolve manually" >&2
        fi
    fi
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

    _assert_sync_initialized "pull-only"

    if [ "$DRY_RUN" = "1" ]; then
        echo "DRY-RUN: would fetch + merge peer branches" >&2
        return 0
    fi

    log "_pull_only_impl: fetching all peer branches"
    # --prune: without it, a peer's branch rename (e.g. the #1459 flat →
    # nested wsId migration) leaves a stale local remote-tracking ref that
    # D/F-conflicts every subsequent fetch ("cannot lock ref") — wedging
    # this host permanently while the error is swallowed by the tee below.
    # Bit for 6 days on Qingyuns-MBP 2026-06-05..11.
    git fetch --all --prune --quiet 2>&1 | tee -a "$LOG" >/dev/null

    # Retire any pre-#1459 flat `host/<host>` branch before the checkout below,
    # which would otherwise D/F-conflict with the nested wsId ref.
    _migrate_flat_branch

    # Ensure we're on the host-and-workspace branch (idempotent). Post-wsId
    # the branch is `host/<hostname>/<wsId>` so two workspaces on the same
    # host land in distinct refs.
    local host_ws_seg current_branch
    host_ws_seg="$(_host_ws_segment)"
    current_branch="host/${host_ws_seg}"
    if [ "$(git symbolic-ref --short HEAD 2>/dev/null)" != "$current_branch" ]; then
        # NOTE: capture the real checkout exit status the set-e-safe way
        # (`|| rc=$?`, not `cmd; rc=$?` which set -e would short-circuit, nor a
        # `| tee` pipe whose $? reflects tee, not git). The pre-fix `| tee
        # >/dev/null` form SWALLOWED a failed checkout (e.g. a D/F conflict from
        # a stale flat branch), leaving HEAD on the wrong branch while the run
        # reported success and pushed nothing. Fail loudly instead.
        local _co_rc=0
        if git show-ref --quiet "refs/remotes/origin/${current_branch}"; then
            git checkout -B "$current_branch" "origin/${current_branch}" >>"$LOG" 2>&1 || _co_rc=$?
        else
            git checkout -B "$current_branch" >>"$LOG" 2>&1 || _co_rc=$?
        fi
        [ "$_co_rc" -eq 0 ] || die "pull-only: failed to switch to $current_branch (git checkout exit $_co_rc); see $LOG"
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
        # P1.3 fix (Codex review on #1454): when two hosts each ran `--init`
        # independently against the same vault, their initial commits have NO
        # common ancestor. `git merge` then errors with "refusing to merge
        # unrelated histories" — Codex repro'd against two fresh vault clones.
        # Detect the no-merge-base case and pass `--allow-unrelated-histories`
        # so the first cross-host merge roots both lineages. After that, the
        # shared root exists and the flag becomes a no-op on subsequent peers.
        local -a merge_args=(--no-edit)
        if ! git merge-base HEAD "$peer" >/dev/null 2>&1; then
            log "_pull_only_impl: $peer has unrelated history with HEAD; using --allow-unrelated-histories"
            echo "sync-workspace: $peer has unrelated history with HEAD; merging with --allow-unrelated-histories" >&2
            merge_args+=(--allow-unrelated-histories)
        fi
        log "_pull_only_impl: merging $peer into $current_branch"
        if git merge "${merge_args[@]}" "$peer" 2>&1 | tee -a "$LOG" >/dev/null; then
            merged=$((merged + 1))
        else
            log "_pull_only_impl: conflict merging $peer; resolving via --ours (use-local fallback)"
            # NUL-delimited walk: the previous `for f in $(git diff ...)` form
            # word-split paths with spaces (routine in notes/), so those
            # conflicts were never resolved — the merge stayed open while the
            # run still reported success, wedging every later sync behind
            # "You have not concluded your merge". Review #2 finding (2026-06-11).
            while IFS= read -r -d '' f; do
                # `--ours` fails on DD-conflicts (both sides deleted) — the file
                # isn't on our side either. Fall back to `git rm` so the merge
                # can complete cleanly. Surfaced by Mini #1445 v3 Test 12.
                if git checkout --ours -- "$f" 2>/dev/null; then
                    git add -- "$f"
                else
                    git rm -f -- "$f" 2>/dev/null || true
                fi
            done < <(git diff --name-only --diff-filter=U -z)
            git -c core.editor=true commit --no-edit 2>/dev/null || true
            # Verify the merge actually concluded — unmerged entries here mean
            # the resolution above missed something; abort rather than leave a
            # half-merge that poisons every subsequent pull while logs say OK.
            if ! git diff --quiet --diff-filter=U || [ -f ".git/MERGE_HEAD" ]; then
                log "_pull_only_impl: merge of $peer did NOT conclude; aborting it"
                color_warn "sync-workspace: conflict resolution for $peer failed to conclude — aborted that merge; will retry next tick"
                git merge --abort 2>/dev/null || true
                continue
            fi
            merged=$((merged + 1))
        fi
    done

    log "_pull_only_impl: merged $merged peer branch(es)"

    # Pull-side mass-deletion tripwire — catches deletions that landed via
    # git merge rather than staged rm. Mini #1445 v3 Medium fix: count ACTUAL
    # deletions in the merge diff, not (pre_count - post_count) net change —
    # otherwise a "delete 60 / add 60" merge bypasses with net=0. Also adds a
    # percentage threshold so catastrophic small-workspace cases (e.g. 20-of-30
    # deletions) still trip below the absolute 50-file default.
    local max_delete max_pct deleted_via_merge tripped tripped_reason
    max_delete="${SUTANDO_SYNC_MAX_DELETE:-50}"
    max_pct="${SUTANDO_SYNC_MAX_DELETE_PCT:-50}"
    if [ -n "$pre_pull_sha" ]; then
        # `-M` enables rename detection (default 50% similarity) so legitimate
        # file moves count as rename, not delete+add — they don't trip the
        # tripwire. Mini #1445 v4 Low.
        deleted_via_merge=$(git diff -M --name-only --diff-filter=D "$pre_pull_sha" HEAD 2>/dev/null | wc -l | tr -d ' ')
    else
        deleted_via_merge=0
    fi
    tripped=0
    tripped_reason=""
    if [ "$deleted_via_merge" -gt "$max_delete" ]; then
        tripped=1
        tripped_reason="deleted $deleted_via_merge files (>SUTANDO_SYNC_MAX_DELETE=$max_delete)"
    elif [ "$pre_pull_count" -gt 0 ] && [ "$deleted_via_merge" -gt 0 ]; then
        local pct=$(( deleted_via_merge * 100 / pre_pull_count ))
        if [ "$pct" -ge "$max_pct" ]; then
            tripped=1
            tripped_reason="deleted $deleted_via_merge of $pre_pull_count files (${pct}% >=SUTANDO_SYNC_MAX_DELETE_PCT=$max_pct%)"
        fi
    fi
    if [ "$tripped" = "1" ] && [ "${SUTANDO_FORCE_SYNC:-0}" != "1" ]; then
        log "_pull_only_impl: ABORT — pull $tripped_reason; resetting to $pre_pull_sha"
        if [ -n "$pre_pull_sha" ]; then
            git reset --hard "$pre_pull_sha" 2>&1 | tee -a "$LOG" >/dev/null
        fi
        echo "sync-workspace: REFUSING pull — peer $tripped_reason. Reset to pre-pull state. Set SUTANDO_FORCE_SYNC=1 to override." >&2
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

    _assert_sync_initialized "push-only"

    if [ "$DRY_RUN" = "1" ]; then
        echo "DRY-RUN: would stage + commit + push to refs/heads/host/$(_host_ws_segment)" >&2
        return 0
    fi

    # Whitelist enforcement lives in .git/info/exclude (see _init_impl
    # rationale + the 2026-06-04 leak fix that motivated moving it there).
    # Heal rules + untrack newly-excluded BEFORE staging; sweep staged
    # credential-shaped paths AFTER (see the two functions' rationale).
    # Back up per-host config ($CLAUDE_CONFIG_DIR settings.json + channel
    # access.json) into hosts/<host>/ before staging, so it's carried + survives
    # a rebuild. Non-fatal: never blocks the push.
    _snapshot_per_host_config || color_warn "sync-workspace: per-host config snapshot failed (non-fatal); push continues"
    _enforce_carrier_set_pre
    git add -A
    _refuse_staged_secrets
    if git diff --cached --quiet; then
        log "_push_only_impl: nothing to commit"
        # A clean tree does NOT mean "done": a prior push may have failed (auth
        # blip, network, the recovered-from case during first --init) leaving a
        # local commit that the remote never received. Without this, a transient
        # push failure leaves the host branch silently stale until the NEXT
        # content change happens to create a fresh commit.
        #
        # Check the remote AUTHORITATIVELY with ls-remote rather than the local
        # remote-tracking ref: a fetch without --prune leaves a stale
        # refs/remotes/origin/... ref after the remote branch is gone, which
        # would falsely read as "up to date" and skip the recovery push.
        local host_ws_seg local_sha remote_out ls_rc remote_sha
        host_ws_seg="$(_host_ws_segment)"
        local_sha="$(git rev-parse HEAD 2>/dev/null || echo "")"
        # set -e-safe rc capture: a plain `var=$(cmd)` assignment is NOT
        # exempt from errexit — offline, the failing ls-remote killed the
        # whole script HERE, before ls_rc was ever read, making the graceful
        # "let the next tick retry" branch below dead code. Same class as
        # the repo's documented feedback_var_assign_setminus_e catches.
        ls_rc=0
        remote_out="$(git ls-remote --heads origin "host/${host_ws_seg}" 2>/dev/null)" || ls_rc=$?
        remote_sha="$(printf '%s\n' "$remote_out" | awk 'NR==1{print $1}')"
        if [ -z "$local_sha" ]; then
            echo "sync-workspace: nothing to push (no local commit yet)"
            return 0
        fi
        if [ "$ls_rc" -ne 0 ]; then
            # Couldn't reach the remote to verify — don't thrash a push that
            # would also fail; report softly and let the next tick retry.
            echo "sync-workspace: nothing to push (clean tree; could not reach remote to verify)"
            return 0
        fi
        if [ "$remote_sha" = "$local_sha" ]; then
            echo "sync-workspace: nothing to push (clean working tree, remote up to date)"
            return 0
        fi
        # ls-remote succeeded but the host branch is missing or behind HEAD →
        # the local commit was never (fully) pushed. Push it now.
        if git push origin "HEAD:refs/heads/host/${host_ws_seg}" 2>&1 | tee -a "$LOG" >/dev/null; then
            log "_push_only_impl: pushed previously-unpushed commit(s) to host/${host_ws_seg}"
            echo "sync-workspace: pushed previously-unpushed commit(s) to host/${host_ws_seg}"
            return 0
        fi
        log "_push_only_impl: push of unpushed commit(s) failed"
        echo "sync-workspace: push failed (clean tree, unpushed commit); check $LOG" >&2
        return 1
    fi

    # Mass-deletion tripwire (carried over from sync-memory.sh)
    local deleted max_delete
    # `-M` for rename detection: legitimate moves (refactor) don't count as
    # deletions. Mirrors pull-side tripwire fix. Mini #1445 v4 Low.
    deleted=$(git diff -M --cached --name-only --diff-filter=D | wc -l | tr -d ' ')
    max_delete="${SUTANDO_SYNC_MAX_DELETE:-50}"
    if [ "$deleted" -gt "$max_delete" ] && [ "${SUTANDO_FORCE_SYNC:-0}" != "1" ]; then
        log "_push_only_impl: ABORT — would delete $deleted files (>$max_delete tripwire)"
        echo "sync-workspace: refusing push — would delete $deleted files (>SUTANDO_SYNC_MAX_DELETE=$max_delete). Set SUTANDO_FORCE_SYNC=1 to override." >&2
        git reset -q
        return 1
    fi

    # Same path= suffix as _init_impl — see comment there. Cross-host
    # wsId → folder discovery works from `git log host/<host>/<wsId>`.
    git commit -q -m "Sync ${SUTANDO_HOST_OVERRIDE:-$(hostname)} $(date +%Y-%m-%dT%H:%M) path=${WORKSPACE_DIR}"

    local host_ws_seg
    host_ws_seg="$(_host_ws_segment)"
    if git push origin "HEAD:refs/heads/host/${host_ws_seg}" 2>&1 | tee -a "$LOG" >/dev/null; then
        log "_push_only_impl: pushed to origin host/${host_ws_seg}"
        echo "sync-workspace: pushed to host/${host_ws_seg}"
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
    # Surface the wsId only if it exists — don't generate just for status.
    # Pair it with the local workspace path on the same line so the
    # wsId↔folder mapping is visually unambiguous for the operator.
    local ws_id_file="$WORKSPACE_DIR/.sutando-vault/ws-id"
    if [ -f "$ws_id_file" ]; then
        local _ws_id_val
        _ws_id_val="$(tr -d '[:space:]' < "$ws_id_file")"
        echo "WS_ID:         ${_ws_id_val}  ← this id identifies workspace ${WORKSPACE_DIR}"
    elif [ -d "$WORKSPACE_DIR/.git" ]; then
        # Legacy: workspace was --init'd before the wsId scheme landed. Push
        # path goes to host/<hostname> instead of host/<hostname>/<wsId>.
        echo "WS_ID:         <legacy — pre-wsId init; next --init or --push-only will create + migrate to new host/<host>/<wsId> branch>"
    fi
    if [ -d "$WORKSPACE_DIR/.git" ]; then
        cd "$WORKSPACE_DIR" || return 1
        local current_branch
        current_branch="$(git symbolic-ref --short HEAD 2>/dev/null || echo "<detached>")"
        echo "current branch: $current_branch"
        echo "remote branches:"
        git for-each-ref --format='  %(refname:short) (last push: %(committerdate:relative))' refs/remotes/origin/host/ 2>/dev/null | head -20 || true
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
#      - legacy/pending-questions.md → workspace/hosts/<hostname>/pending-questions.md
#      - legacy/build_log.md → workspace/hosts/<hostname>/build_log.md
#        (both per-host, hostname-qualified per the hosts/<hostname>/ convention
#        — owner decision 2026-06-20 "F1: per host"; matches what the
#        personal_path/personalPath readers probe first, #1718)
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

    # Per-host segment for hostname-qualified destinations (build_log,
    # pending-questions). Computed once; matches `_host()` + the reader probe.
    local host
    host="$(_host)"

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
                "$WORKSPACE_DIR/hosts/${host}" \
                "$WORKSPACE_DIR/.claude-sutando/projects/${local_slug}/memory"

    # Full import mapping (owner directive 2026-06-20 "include everything that
    # should be"). Shared content → shared paths; per-host content → hosts/<host>/;
    # stale hosts dropped (unique skills salvaged first); bulky regenerable media
    # excluded from git (stays archived in the legacy repo); skills are SHARED.
    # Stale/defunct machine-<host> dirs to DROP (their unique skills are
    # salvaged into shared skills/ BEFORE they're skipped below). Per-clone /
    # owner-specific — read from the gitignored clone CONFIG
    # (sutando.config.local.json → migrate.stale_hosts), NOT committed to this
    # (public) repo and NOT from .env (this is config, not a secret). Default
    # empty (drop nothing).
    local STALE_HOSTS
    STALE_HOSTS="$(bash "$SCRIPT_PARENT/scripts/sutando-config.sh" migrate-stale-hosts 2>/dev/null | tr '\n' ' ')"

    # ---- SHARED: notes/ (text only; EXCLUDE bulky regenerable media) ----
    if [ -L "$WORKSPACE_DIR/notes" ]; then
        log "_migrate_from_legacy_impl: workspace/notes is a symlink → $(readlink "$WORKSPACE_DIR/notes"); removing + copying content"
        _do rm "$WORKSPACE_DIR/notes"
        _do mkdir -p "$WORKSPACE_DIR/notes"
    fi
    if [ -d "$legacy_dir/notes" ]; then
        if [ "$DRY_RUN" = "1" ]; then
            local n_notes
            n_notes=$(find "$legacy_dir/notes" -type f -not -path "$legacy_dir/notes/generated/*" -not -path "$legacy_dir/notes/media/*" 2>/dev/null | wc -l | tr -d ' ')
            echo "DRY-RUN: would: rsync notes/ (EXCL generated/ + media/) → workspace/notes/  (${n_notes} text files; ~2.65 GB media left archived in legacy)" >&2
        else
            rsync -a --exclude='generated/' --exclude='media/' "$legacy_dir/notes"/ "$WORKSPACE_DIR/notes"/ 2>/dev/null || true
            log "_migrate_from_legacy_impl: rsynced notes/ (excl generated,media) → workspace/notes/"
        fi
    fi

    # ---- SHARED: memory/*.md → this host's local-slug core-memory dir ----
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

    # ---- SHARED: misc top-level dirs verbatim ----
    local shared_dir
    for shared_dir in papers talk-slides voice-contexts assets; do
        [ -d "$legacy_dir/$shared_dir" ] || continue
        if [ "$DRY_RUN" = "1" ]; then
            echo "DRY-RUN: would: cp -an $shared_dir/ → workspace/$shared_dir/  ($(find "$legacy_dir/$shared_dir" -type f 2>/dev/null | wc -l | tr -d ' ') files)" >&2
        else
            _do mkdir -p "$WORKSPACE_DIR/$shared_dir"
            cp -an "$legacy_dir/$shared_dir"/. "$WORKSPACE_DIR/$shared_dir"/ 2>/dev/null || true
            log "_migrate_from_legacy_impl: copied $shared_dir/ → workspace/$shared_dir/"
        fi
    done

    # ---- SHARED: skills/ (canonical) + salvage host-only skills ----
    # skills are SHARED (verified vs git: vault-root skills/ = canonical). Copy
    # it, then promote any skill that lives ONLY under machine-*/skills/ (a
    # host-only orphan, incl on stale hosts) into shared so nothing is lost.
    if [ -d "$legacy_dir/skills" ]; then
        if [ "$DRY_RUN" = "1" ]; then
            echo "DRY-RUN: would: cp -an skills/ → workspace/skills/  ($(ls -1 "$legacy_dir/skills" 2>/dev/null | wc -l | tr -d ' ') skills, shared canonical)" >&2
        else
            _do mkdir -p "$WORKSPACE_DIR/skills"
            cp -an "$legacy_dir/skills"/. "$WORKSPACE_DIR/skills"/ 2>/dev/null || true
        fi
        # Host-only orphan skills to NOT promote to shared (stale/superseded).
        # They stay retrievable in the legacy archive. Per-clone / owner-specific
        # — read from the gitignored clone CONFIG (sutando.config.local.json →
        # migrate.skip_skills), NOT committed to this (public) repo and NOT from
        # .env (config, not a secret). Default empty (salvage all host-only).
        local SALVAGE_SKIP
        SALVAGE_SKIP="$(bash "$SCRIPT_PARENT/scripts/sutando-config.sh" migrate-skip-skills 2>/dev/null | tr '\n' ' ')"
        local ms sk b
        for ms in "$legacy_dir"/machine-*/skills; do
            [ -d "$ms" ] || continue
            for sk in "$ms"/*; do
                [ -e "$sk" ] || continue
                b="$(basename "$sk")"
                [ -e "$legacy_dir/skills/$b" ] && continue   # already in shared
                case " $SALVAGE_SKIP " in
                    *" $b "*)
                        [ "$DRY_RUN" = "1" ] && echo "DRY-RUN: would: SKIP stale host-only skill '$b' (superseded; left in legacy archive)" >&2
                        continue ;;
                esac
                if [ "$DRY_RUN" = "1" ]; then
                    echo "DRY-RUN: would: SALVAGE host-only skill '$b' (from ${ms#"$legacy_dir"/}) → workspace/skills/$b" >&2
                else
                    cp -an "$sk" "$WORKSPACE_DIR/skills/" 2>/dev/null || true
                fi
            done
        done
    fi

    # ---- PER-HOST: this host's pending-questions + build_log → hosts/<host>/ ----
    local pq_src
    if [ -f "$legacy_dir/machine-${host}/pending-questions.md" ]; then
        pq_src="$legacy_dir/machine-${host}/pending-questions.md"
    elif [ -f "$legacy_dir/pending-questions.md" ]; then
        pq_src="$legacy_dir/pending-questions.md"
    else
        pq_src=""
    fi
    if [ -n "$pq_src" ]; then
        _do cp -n "$pq_src" "$WORKSPACE_DIR/hosts/${host}/pending-questions.md"
        [ "$DRY_RUN" != "1" ] && log "_migrate_from_legacy_impl: copied $pq_src → workspace/hosts/${host}/pending-questions.md"
    fi
    local bl_src
    if [ -f "$legacy_dir/machine-${host}/build_log.md" ]; then
        bl_src="$legacy_dir/machine-${host}/build_log.md"
    elif [ -f "$legacy_dir/build_log.md" ]; then
        bl_src="$legacy_dir/build_log.md"
    else
        bl_src=""
    fi
    if [ -n "$bl_src" ]; then
        _do cp -n "$bl_src" "$WORKSPACE_DIR/hosts/${host}/build_log.md"
        [ "$DRY_RUN" != "1" ] && log "_migrate_from_legacy_impl: copied $bl_src → workspace/hosts/${host}/build_log.md"
    fi

    # ---- PER-HOST: peer machines' full subtree (EXCEPT skills/) → hosts/<peer>/ ----
    # Each non-stale machine-<peer>/ (config: build_log, crons.json,
    # PERSONAL_CLAUDE, stand-identity, data/, notes/, tab-aliases, voice-context,
    # channels/access.json, settings.json) → hosts/<peer>/. skills/ excluded
    # (handled as shared above). This host (no machine-<host> in the repo) is
    # handled via the legacy-root pq/bl above; its access/settings come from the
    # live ~/.claude post-migration (not present in the legacy clone).
    local md mname peer
    for md in "$legacy_dir"/machine-*; do
        [ -d "$md" ] || continue
        mname="$(basename "$md")"
        case " $STALE_HOSTS " in
            *" $mname "*)
                [ "$DRY_RUN" = "1" ] && echo "DRY-RUN: would: DROP stale $mname (unique skills already salvaged to shared)" >&2
                continue ;;
        esac
        peer="${mname#machine-}"
        [ "$peer" = "$host" ] && continue   # this host handled above
        if [ "$DRY_RUN" = "1" ]; then
            echo "DRY-RUN: would: rsync $mname/ (EXCL skills/) → hosts/$peer/  ($(find "$md" -type f -not -path "$md/skills/*" 2>/dev/null | wc -l | tr -d ' ') files)" >&2
        else
            _do mkdir -p "$WORKSPACE_DIR/hosts/$peer"
            rsync -a --exclude='skills/' "$md"/ "$WORKSPACE_DIR/hosts/$peer"/ 2>/dev/null || true
            log "_migrate_from_legacy_impl: rsynced $mname/ (excl skills) → hosts/$peer/"
        fi
    done

    # 5. Hand off to _init_impl for git init + first push (DRY_RUN propagates)
    log "_migrate_from_legacy_impl: handing off to _init_impl for git init + first push"
    _init_impl

    # 6. Operator-facing next steps
    cat <<EOF >&2

sync-workspace migrate: complete.

Next steps (operator-supervised):
  1. Verify the new workspace has the expected content:
       ls $WORKSPACE_DIR/notes/ | head            # text only (generated/+media/ left in legacy)
       ls $WORKSPACE_DIR/.claude-sutando/projects/${local_slug}/memory/ | head
       ls $WORKSPACE_DIR/skills/                   # shared canonical + salvaged host-only skills
       ls $WORKSPACE_DIR/hosts/                    # per-host: this host + peers (machine-<peer>/ minus skills/)
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
    sed -n 's/^# \?//;1,/^$/ {/^$/q;p;}' "$0" | head -50 || true
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
