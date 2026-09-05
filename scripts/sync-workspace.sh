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
# sutando.config.{json,local.json}.
#
# `include` REPLACES the default list wholesale — it does NOT add to it.
# Config merging is `_deep_merge` (src/sutando_config.py), whose contract is
# "dicts merge; everything else (lists, scalars, None) is REPLACED by the
# override", and that behaviour is pinned by
# tests/sutando-config.test.py::test_local_replaces_arrays_wholesale.
#
# This matters more than an ordinary doc nit because the carrier set is a
# WHITELIST: _compose_exclude_content() emits `*` (ignore everything) and then
# un-ignores exactly the include list. So setting vault.sync.include to add one
# path silently DROPS every default path — notes/, hosts/*/ and the whole
# .claude-sutando/projects/*/memory/ corpus — out of the backup, while this
# script goes on printing "pushed to <branch>" on every run. To add an INCLUDE
# path you must restate the full carrier set.
#
# `vault.sync.exclude_extra` appends instead of replacing — use it rather than
# restating `exclude`. No `include_extra`: unioning a whitelist widens the vault.
#
# `exclude` subtracts, carving subpaths out of an included parent (emitted after
# the includes so gitignore's last-match-wins applies).
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
#   5. the workspace repo's own `origin` remote (recovery — see Priority 5 below)
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

# A bare `python3` can be the Xcode-CLT stub on a LaunchAgent PATH; resolve
# through the repo's cascade. Empty when nothing resolves — callers decide.
SYNC_PY="$(bash "$SCRIPT_PARENT/scripts/sutando-config.sh" python-bin 2>/dev/null || true)"
[ -n "$SYNC_PY" ] && [ -x "$SYNC_PY" ] || SYNC_PY=""

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
# Provenance for --status: resolution reports it on stderr as it runs, but
# --status is read long after those lines have scrolled away.
VAULT_URL_SOURCE=""
VAULT_URL_DECLINED=""
VAULT_URL_DECLINED_REASON=""

# Priority 1: --vault-url CLI flag (explicit)
if [ -n "$VAULT_URL_FLAG" ]; then
    VAULT_URL="$VAULT_URL_FLAG"
    VAULT_URL_SOURCE="--vault-url flag"
fi

# Priority 2+3: sutando.config.{local,base}.json → vault.remote_url
# (loader merges local + base + applies ${REPO_DIR} substitution)
if [ -z "$VAULT_URL" ] && [ -f "$SCRIPT_PARENT/scripts/sutando-config.sh" ]; then
    VAULT_URL="$(bash "$SCRIPT_PARENT/scripts/sutando-config.sh" vault-url 2>/dev/null || true)"
    [ -n "$VAULT_URL" ] && VAULT_URL_SOURCE="sutando.config"
fi

# Priority 4: legacy .env SUTANDO_MEMORY_REPO (warn-and-honor for one release)
if [ -z "$VAULT_URL" ] && [ -n "${SUTANDO_MEMORY_REPO:-}" ]; then
    VAULT_URL="$SUTANDO_MEMORY_REPO"
    VAULT_URL_SOURCE="SUTANDO_MEMORY_REPO (deprecated)"
    echo "sync-workspace: SUTANDO_MEMORY_REPO is deprecated; move vault URL to sutando.config.local.json under vault.remote_url." >&2
fi

# Priority 5: the workspace repo's own origin, adopted only when it already
# carries THIS workspace's own `host/*/<wsId>` branch.
if [ -z "$VAULT_URL" ] \
   && [ "$(git -C "$WORKSPACE_DIR" rev-parse --show-toplevel 2>/dev/null || true)" \
        = "$(cd "$WORKSPACE_DIR" && pwd -P)" ]; then
    _origin_url="$(git -C "$WORKSPACE_DIR" remote get-url origin 2>/dev/null || true)"
    # Read the id, never mint one: _ws_id() is defined below and persists a
    # fresh id, which would invent an identity no vault can be carrying.
    _wsid="$(tr -d '[:space:]' < "$WORKSPACE_DIR/.sutando-vault/ws-id" 2>/dev/null || true)"
    # The id goes into a ref glob, so it must match what _ws_id mints: a
    # persisted `*` asks for host/*/*, which any host branch anywhere answers.
    case "$_wsid" in
        [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) _wsid_ok=1 ;;
        *) _wsid_ok=0 ;;
    esac
    if [ -n "$_origin_url" ] && [ -z "$_wsid" ]; then
        VAULT_URL_DECLINED="$_origin_url"
        VAULT_URL_DECLINED_REASON="workspace has no .sutando-vault/ws-id to identify its vault branch"
        echo "sync-workspace: no vault URL configured, and this workspace has no .sutando-vault/ws-id to identify its vault branch; refusing to recover a URL from the workspace repo's origin ($_origin_url)." >&2
    elif [ -n "$_origin_url" ] && [ "$_wsid_ok" != "1" ]; then
        VAULT_URL_DECLINED="$_origin_url"
        VAULT_URL_DECLINED_REASON="workspace ws-id is not a valid workspace id (expected six lowercase hex characters), so it identifies no vault branch"
        echo "sync-workspace: this workspace's .sutando-vault/ws-id is not a valid workspace id (expected six lowercase hex characters); refusing to recover a URL from the workspace repo's origin ($_origin_url)." >&2
    elif [ -n "$_origin_url" ]; then
        # Unreachable is not the same answer as not-a-vault, and an operator
        # told the wrong one edits the wrong thing.
        _ls_rc=0
        _ls_out="$(GIT_TERMINAL_PROMPT=0 git ls-remote --heads "$_origin_url" "host/*/$_wsid" 2>/dev/null)" || _ls_rc=$?
        if [ "$_ls_rc" != "0" ]; then
            VAULT_URL_DECLINED="$_origin_url"
            VAULT_URL_DECLINED_REASON="unreachable this run, so it could not be confirmed either way"
            echo "sync-workspace: could not reach the workspace repo's origin ($_origin_url) to confirm it is a vault; not recovering a URL from it this run." >&2
        elif [ -n "$_ls_out" ]; then
            VAULT_URL="$_origin_url"
            VAULT_URL_SOURCE="workspace repo origin, identity-verified (carries host/*/$_wsid)"
            echo "sync-workspace: no vault URL configured; recovered it from the workspace repo's own origin ($VAULT_URL). Restore vault.remote_url in sutando.config.local.json to silence this." >&2
        else
            VAULT_URL_DECLINED="$_origin_url"
            VAULT_URL_DECLINED_REASON="carries no host/*/$_wsid branch, so this workspace has never pushed to it"
            echo "sync-workspace: the workspace repo's origin ($_origin_url) carries no host/*/$_wsid branch, so it is not a vault this workspace has pushed to; refusing to recover a vault URL from it." >&2
        fi
        unset _ls_rc _ls_out
    fi
    unset _origin_url _wsid _wsid_ok
fi

# --------------------------------------------------------------------------- #
# Section 2 — Logging + UI                                                     #
# --------------------------------------------------------------------------- #

# Per-user log path: on multi-user machines a shared /tmp/sync-workspace.log is
# owned by whichever account wrote it first; the sticky bit blocks every other
# account from appending or replacing it. $TMPDIR is per-user on macOS.
LOG="${SYNC_WORKSPACE_LOG:-${TMPDIR:-/tmp}/sync-workspace.log}"


log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# log() alone writes only to $LOG and the caller returns success, so a refusal
# that stops backups indefinitely looks identical to a clean sync.
warn_operator() { log "$1"; color_warn "$1"; }

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
    # Trim first: `[ -n "  " ]` is true, so a blank-but-set override became the
    # host label and produced a whitespace-named branch/dir. Lockstep with
    # _host_label()'s strip() — trim the ENDS only, so an (unusual but legal)
    # label containing a space is preserved rather than silently compacted.
    env="${env#"${env%%[![:space:]]*}"}"
    env="${env%"${env##*[![:space:]]}"}"
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

LOCK_DIR="${SUTANDO_SYNC_LOCK_DIR:-/tmp/sync-workspace.lock.d}"

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

# Return success only for a literal host label that is safe to compare as a
# path segment. In particular, reject gitignore glob metacharacters and dot
# segments: vault.sync.include intentionally accepts patterns, and those are
# operator customizations rather than legacy per-host labels.
_is_literal_host_label() {
    local label="$1"
    [ "$label" != "." ] \
        && [ "$label" != ".." ] \
        && [[ "$label" =~ ^[[:alnum:]_.-]+$ ]]
}

# A pre-multi-host local config may still scope the carrier to exactly this
# machine's `hosts/<label>/` directory. That shape is unsafe now that peer
# subtrees form one durable aggregate: after a pull, carrier enforcement
# interprets every peer path as newly excluded and propagates its deletion.
# Widen only the literal validated current host label; gitignore patterns and
# nested paths retain their explicit operator-authored meaning.
_normalize_include_path() {
    local path="$1" own_host
    own_host="$(_host)"
    if _is_literal_host_label "$own_host" \
        && [ "$path" = "hosts/$own_host/" ]; then
        printf '%s\n' "hosts/*/"
        return 0
    fi
    printf '%s\n' "$path"
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
            path="$(_normalize_include_path "$path")"
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
    # Cleanup runs only when control RETURNS from the replace, so a mid-stage kill
    # leaves one under a carried hosts/*/ path that `git add -A` would vault.
    echo "hosts/*/build_log.md.snap.??????"
    echo "hosts/*/.build_log.snapshot-sha.repair.??????"
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

# Print `existing` with a legacy per-host carrier scope rewritten to the shared
# `hosts/*/` form. Unchanged when the host label is not a literal path segment.
_widen_legacy_host_scope() {
    local existing="$1" own_host
    own_host="$(_host)"
    if ! _is_literal_host_label "$own_host"; then
        cat "$existing"
        return 0
    fi
    awk -v host="$own_host" '
        $0 == "!hosts/" host "/" {
            print "!hosts/*/"
            next
        }
        $0 == "!hosts/" host "/**" {
            print "!hosts/*/**"
            next
        }
        { print }
    ' "$existing"
}

# Return success only when an existing generated rule set differs from the
# desired one solely because one or more legacy `!hosts/<label>/` entries need
# widening to `!hosts/*/`. This narrow comparison preserves the existing
# operator-edit protection while allowing the #2391 safety migration to heal
# automatically on the next sync tick.
_is_safe_legacy_host_scope_widening() {
    local existing="$1" desired="$2" own_host
    own_host="$(_host)"
    _is_literal_host_label "$own_host" || return 1
    cmp -s <(_widen_legacy_host_scope "$existing") "$desired"
}

# Comments and blanks are inert in gitignore, so header drift between generated
# versions must not decide whether a refresh is safe.
_exclude_rules_only() {
    grep -vE '^[[:space:]]*(#|$)' "$1" | sort
}

# Operator-authored COMMENTS are content too: the rule comparison cannot see them,
# so a refresh that drops one looks safe while discarding the operator's note.
_exclude_comments_only() {
    grep -E '^[[:space:]]*#' "$1" | sort
}

# Built-in deny rules this script owns and may migrate into an existing generated
# file. Deliberately explicit: anything listed is adopted without operator review.
_adoptable_builtin_denies() {
    printf '%s\n' 'hosts/*/build_log.md.snap.??????' 'hosts/*/.build_log.snapshot-sha.repair.??????'
}

# A generated exclude differing ONLY by shipped carve-outs is safe to refresh.
# Carry dropped comments forward: the rules refresh, the operator keeps theirs.
_preserve_dropped_comments() {
    # The marker is OURS: counting it as a dropped operator comment would nest each
    # tick's marker under a fresh one and grow the file forever.
    local marker='# --- preserved from the previous exclude (sync-workspace) ---'
    local existing="$1" desired="$2" dropped
    dropped="$(comm -23 <(_exclude_comments_only "$existing") <(_exclude_comments_only "$desired") \
        | grep -vxF -- "$marker" || true)"
    [ -n "$dropped" ] || return 0
    {
        printf '\n%s\n' "$marker"
        printf '%s\n' "$dropped"
    } >> "$desired"
}

_is_safe_carveout_addition() {
    local existing="$1" desired="$2" shipped shipped_rules line path widened rc
    # Compare against the HOST-WIDENED existing content: the two safe migrations are
    # independent, so a file needing both was refused by each recognizer alone.
    widened="$(mktemp -t sync-workspace-widened.XXXXXX)" || return 1
    _widen_legacy_host_scope "$existing" > "$widened"
    existing="$widened"
    shipped="$(bash "$SCRIPT_PARENT/scripts/sutando-config.sh" vault-sync-exclude 2>/dev/null || true)"
    # Compare against what the composer EMITS, not the raw config value: a
    # directory yields both `p/` and `p/**`, and a real older file lacks all of them.
    shipped_rules=""
    while IFS= read -r path; do
        [ -n "$path" ] || continue
        shipped_rules+="$(_emit_exclude_lines "$path")"$'\n'
    done <<<"$shipped"
    # The script-owned deny merges unconditionally: it only ever excludes MORE,
    # and `vault.sync.exclude: []` is a supported value that must not gate it.
    shipped="$shipped_rules"$'\n'"$(_adoptable_builtin_denies)"
    [ -n "$(printf '%s' "$shipped" | grep -vE '^[[:space:]]*$')" ] || { rm -f "$widened"; return 1; }
    rc=0
    # Refuse if the refresh would DROP any RULE the existing file carries.
    # Comments are not a refusal reason — _preserve_dropped_comments carries them.
    if [ -n "$(comm -23 <(_exclude_rules_only "$existing") <(_exclude_rules_only "$desired"))" ]; then
        rc=1
    else
        # Every added rule must be a shipped carve-out, never an operator's line.
        while IFS= read -r line; do
            [ -n "$line" ] || continue
            grep -qxF -- "$line" <<< "$shipped" || { rc=1; break; }
        done < <(comm -13 <(_exclude_rules_only "$existing") <(_exclude_rules_only "$desired"))
    fi
    rm -f "$widened"
    return "$rc"
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
        elif _is_safe_legacy_host_scope_widening "$exclude_path" "$tmp_path"; then
            _preserve_dropped_comments "$exclude_path" "$tmp_path"
            log "generate_exclude: safely widened legacy hosts/<label>/ carrier rules to hosts/*/"
            color_warn "sync-workspace: widened legacy hosts/<label>/ carrier rules to hosts/*/ so peer host state remains durable"
        elif _is_safe_carveout_addition "$exclude_path" "$tmp_path"; then
            _preserve_dropped_comments "$exclude_path" "$tmp_path"
            log "generate_exclude: refreshed a previously-generated exclude with shipped carve-outs only"
            color_warn "sync-workspace: added shipped carve-out(s) to the existing exclude file; no operator rule was removed"
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

# A host owns its own hosts/<label>/ subtree. This deletion-focused guard
# refuses any staged removal below a foreign label before commit/push,
# including the source side of a rename. It catches both stale carrier rules
# and future writers that accidentally treat absence as permission to delete a
# peer's durable state. In-place foreign-file modifications are outside this
# guard's #2391 deletion scope. The existing explicit force switch remains the
# operator escape hatch for intentional recovery.
_refuse_foreign_host_deletions() {
    [ "${SUTANDO_FORCE_SYNC:-0}" = "1" ] && return 0

    local own_host foreign_hits=0 path relative path_host first_path=""
    own_host="$(_host)"
    while IFS= read -r -d '' path; do
        case "$path" in
            hosts/*/*)
                relative="${path#hosts/}"
                path_host="${relative%%/*}"
                if [ -n "$path_host" ] && [ "$path_host" != "$own_host" ]; then
                    foreign_hits=$((foreign_hits + 1))
                    [ -z "$first_path" ] && first_path="$path"
                fi
                ;;
        esac
    done < <(git diff --cached --no-renames --name-only --diff-filter=D -z)

    if [ "$foreign_hits" -eq 0 ]; then
        return 0
    fi

    log "_refuse_foreign_host_deletions: ABORT — would delete $foreign_hits foreign host file(s); first=$first_path own_host=$own_host"
    echo "sync-workspace: refusing push — would delete $foreign_hits foreign host file(s) (first: $first_path). Only '$own_host' may write its hosts/<label>/ subtree. Restore/pull the peer state, or set SUTANDO_FORCE_SYNC=1 for an intentional recovery." >&2
    git reset -q
    return 1
}

# Snapshot the per-host config from the canonical Claude config dir into
# <workspace>/hosts/<host>/ so it's carried by the hosts/*/ vault glob and
# survives a rebuild. Startup exports this same path as CLAUDE_CONFIG_DIR for
# Claude Code / the bridges. Resolve it from config here because launchd/cron
# does not reliably inherit that export; falling back to ~/.claude would copy
# stale pre-migration access state over the live backup.
#
# NOT snapshotted: PERSONAL_CLAUDE.md / stand-identity.json / tab-aliases.json —
# those follow the RELOCATION model (migrator one-time move + personal_path /
# CLAUDE.md readers that prefer hosts/<host>/). Snapshotting them would make the
# reader prefer a stale snapshot over the live root file.
#
# Secret-safe: copies ONLY access.json, never the sibling .env (bot tokens).
# Config copies are best-effort (return 0). The build_log snapshot is not: a
# per-host copy left PARTIAL by a failed write returns 3 and the caller withholds
# that tick's push rather than vault a truncated log.
_snapshot_per_host_config() {
    local _cfg
    _cfg="$(bash "$SCRIPT_PARENT/scripts/sutando-config.sh" claude-sutando-config-dir)" || return 0
    local _host_dir="$WORKSPACE_DIR/hosts/$(_host)"
    mkdir -p "$_host_dir" 2>/dev/null || return 0

    # An interrupted stage cannot clean up after itself: host-scoped, past the grace window,
    # and never while a live intent names it (the scheduler tick outlives the grace; recovery decides).
    [ -f "$_host_dir/.build_log.snapshot-sha.next" ] ||
    find "$_host_dir" -maxdepth 1 -type f -name 'build_log.md.snap.??????' \
        -mmin +"${SYNC_SNAP_TMP_GRACE_MIN:-10}" -delete 2>/dev/null || true
    find "$_host_dir" -maxdepth 1 -type f -name '.build_log.snapshot-sha.repair.??????' \
        -mmin +"${SYNC_SNAP_TMP_GRACE_MIN:-10}" -delete 2>/dev/null || true

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

    # Ownership is provenance, never mtime — the re-hash before the swap cannot see a
    # writer holding an open fd. Prints the sha only for a lone 64-hex token.
    _usable_sig_record() {
        local _hex
        _hex="$(od -An -v -tx1 -- "$1" 2>/dev/null | tr -d ' \n')"
        printf '%s' "$_hex" | LC_ALL=C grep -qE '^(3[0-9]|6[1-6]){64}(0a)*$' || return 0
        tr -d '\n' < "$1" 2>/dev/null
    }
    # With no record, writer direction comes only from the append-only relationship:
    # root-live (dest is a prefix of root), host-live (root is a prefix of dest), diverged.
    _append_only_direction() {
        local _r="$1" _d="$2" _rn _dn
        _rn="$(wc -c < "$_r" 2>/dev/null | tr -d ' ')"
        _dn="$(wc -c < "$_d" 2>/dev/null | tr -d ' ')"
        if [ -z "$_rn" ] || [ -z "$_dn" ]; then
            echo diverged
        elif [ "$_dn" -eq 0 ]; then
            echo root-live           # an empty copy holds nothing that root could lose
        elif [ "$_rn" -eq 0 ]; then
            echo host-live
        elif [ "$_dn" -le "$_rn" ] && head -c "$_dn" "$_r" | cmp -s - "$_d"; then
            echo root-live
        elif head -c "$_rn" "$_d" | cmp -s - "$_r"; then
            echo host-live
        else
            echo diverged
        fi
    }
    # Replace the destination's BYTES, never its inode (an open O_APPEND descriptor keeps landing).
    # Exit 2 = refused under the lock; exit 3 = dst left a PREFIX of staged (--rollforward finishes it).
    _replace_in_place() {
        local _py="${SYNC_PY:-}"
        if [ -z "$_py" ] || [ ! -f "$_py" ] || [ ! -x "$_py" ]; then
            _py="$(bash "$SCRIPT_PARENT/scripts/sutando-config.sh" python-bin 2>/dev/null || true)"
        fi
        [ -n "$_py" ] && [ -f "$_py" ] && [ -x "$_py" ] || return 1
        "$_py" - "$@" <<'PY' 2>/dev/null
import fcntl, hashlib, os, sys
mode, src, dst = sys.argv[1:4]
expected = sys.argv[4] if len(sys.argv) > 4 else None
with open(src, "rb") as f:
    data = f.read()
fd = os.open(dst, os.O_RDWR | os.O_CREAT, 0o644)
try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    with open(fd, "rb", closefd=False) as f:
        cur = f.read()
    if mode == "--replace":
        # An empty `expected` means the caller saw NO destination: only a fresh, empty file qualifies.
        if hashlib.sha256(cur).hexdigest() != expected and not (expected == "" and not cur):
            sys.exit(2)
        view = memoryview(data)
    else:
        if cur == data:
            sys.exit(0)
        # A short write leaves a prefix; anything else is not ours to finish. Only the
        # missing suffix is written, so a second failure still leaves a longer prefix.
        if not data.startswith(cur):
            sys.exit(2)
        view = memoryview(data)[len(cur):]
    partial = mode != "--replace"
    try:
        if mode == "--replace":
            os.ftruncate(fd, 0)
            partial = True
            os.lseek(fd, 0, os.SEEK_SET)
        else:
            os.lseek(fd, len(cur), os.SEEK_SET)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except OSError:
        sys.exit(3 if partial else 1)
finally:
    os.close(fd)
PY
    }
    # fsync a path AND its directory: a rename is only durable once the parent
    # directory entry is on disk.
    _fsync_path_and_dir() {
        # Resolve lazily too: this function is loaded standalone by its test, so
        # it cannot assume the script-level SYNC_PY exists.
        local _py="${SYNC_PY:-}"
        if [ -z "$_py" ] || [ ! -f "$_py" ] || [ ! -x "$_py" ]; then
            _py="$(bash "$SCRIPT_PARENT/scripts/sutando-config.sh" python-bin 2>/dev/null || true)"
        fi
        # No verified interpreter -> no durability guarantee. Fail, never pretend.
        [ -n "$_py" ] && [ -f "$_py" ] && [ -x "$_py" ] || return 1
        "$_py" - "$1" <<'PY' 2>/dev/null || return 1
import os, sys
p = sys.argv[1]
fd = os.open(p, os.O_RDONLY)
try:
    os.fsync(fd)
finally:
    os.close(fd)
d = os.open(os.path.dirname(p) or ".", os.O_RDONLY)
try:
    os.fsync(d)
finally:
    os.close(d)
PY
    }

    # The staged copy whose bytes the intent names, if one survived; empty otherwise.
    _staged_copy_matching() {
        local _f
        for _f in "$1".snap.??????; do
            [ -f "$_f" ] || continue
            if [ "$(shasum -a 256 "$_f" 2>/dev/null | cut -d' ' -f1)" = "$2" ]; then
                printf '%s' "$_f"
                return 0
            fi
        done
        return 1
    }

    # An interrupted publish leaves an INTENT beside the signature; recovery trusts the
    # destination's bytes, not the intent. Malformed, stale and ambiguous fail closed.
    _recover_snapshot_publish() {
        local _d="$1" _s="$2" _i="$3" _want _have
        [ -f "$_i" ] || return 0
        _want="$(_usable_sig_record "$_i")"
        if [ -z "$_want" ]; then
            rm -f "$_i" 2>/dev/null || true
            log "snapshot: discarded a malformed publish intent (no usable sha); provenance unchanged"
            return 0
        fi
        [ -f "$_d" ] && _have="$(shasum -a 256 "$_d" 2>/dev/null | cut -d' ' -f1)"
        if [ "$_have" != "$_want" ]; then
            # A durable staged copy that matches the intent turns a partial destination
            # (short write, kill mid-write) into a roll-forward instead of a discard.
            local _staged _rrc=0
            _staged="$(_staged_copy_matching "$_d" "$_want")" || _staged=""
            if [ -n "$_staged" ]; then
                _replace_in_place --rollforward "$_staged" "$_d" || _rrc=$?
                if [ "$_rrc" -eq 0 ]; then
                    rm -f "$_staged" 2>/dev/null || true
                    if ! _fsync_path_and_dir "$_d"; then
                        log "snapshot: rolled-forward destination not confirmed durable; intent left for the next tick"
                        return 0
                    fi
                    _have="$_want"
                    log "snapshot: rolled hosts/$(_host)/build_log.md forward from its staged copy"
                elif [ "$_rrc" -eq 2 ]; then
                    rm -f "$_i" "$_staged" 2>/dev/null || true
                    warn_operator "snapshot: hosts/$(_host)/build_log.md matches neither its publish intent nor a prefix of the staged copy — a writer changed it mid-publish; intent and staged copy discarded, per-host copy left as found (root build_log.md still holds the intended content)"
                    return 0
                else
                    log "snapshot: roll-forward of hosts/$(_host)/build_log.md failed; staged copy and intent kept for the next tick"
                    return 3
                fi
            fi
        fi
        if [ -n "$_have" ] && [ "$_have" = "$_want" ]; then
            # The move landed; only the promote was lost. Finish it.
            mv -f "$_i" "$_s" 2>/dev/null || {
                log "snapshot: could not promote a verified intent; leaving it for the next tick"
                return 0
            }
            # The rename CONSUMED the only recovery record, so a non-durable signature must
            # re-create the intent — otherwise the completion line below would be a lie.
            if ! _fsync_path_and_dir "$_s"; then
                if printf '%s\n' "$_want" > "$_i" 2>/dev/null && _fsync_path_and_dir "$_i"; then
                    log "snapshot: promoted signature not confirmed durable; intent re-created for the next tick"
                else
                    warn_operator "snapshot: promoted signature for hosts/$(_host)/build_log.md is NOT confirmed durable and the recovery intent could not be re-created; provenance may be lost on a crash"
                fi
                return 0
            fi
            log "snapshot: completed an interrupted publish from its intent record"
        elif _dest_is_partial_of_root "$_d" "$_s"; then
            # Partial and no source to roll forward from: pushing it would vault a truncated
            # log, so the intent stays and this tick keeps withholding.
            warn_operator "snapshot: hosts/$(_host)/build_log.md is PARTIAL and its staged copy is gone; intent kept, this sync will not push it (root build_log.md still holds the whole content)"
            return 3
        else
            # The move never landed (or landed as something else): the intent
            # describes content that is not there, so it grants nothing.
            rm -f "$_i" 2>/dev/null || true
            log "snapshot: discarded a stale publish intent (destination does not match it)"
        fi
    }
    # Partial = a strict prefix of root that the signature does not vouch for (an old
    # complete copy is also a prefix, but its sha is the recorded one).
    _dest_is_partial_of_root() {
        local _d="$1" _s="$2" _r="$WORKSPACE_DIR/build_log.md" _dn _rn _dsha _rec
        [ -f "$_d" ] && [ -f "$_r" ] || return 1
        _dn="$(wc -c < "$_d" 2>/dev/null | tr -d ' ')"; _rn="$(wc -c < "$_r" 2>/dev/null | tr -d ' ')"
        [ -n "$_dn" ] && [ -n "$_rn" ] && [ "$_dn" -gt 0 ] && [ "$_dn" -lt "$_rn" ] || return 1
        head -c "$_dn" "$_r" 2>/dev/null | cmp -s - "$_d" || return 1
        _dsha="$(shasum -a 256 "$_d" 2>/dev/null | cut -d' ' -f1)"
        _rec=""; [ -f "$_s" ] && _rec="$(_usable_sig_record "$_s")"
        [ "$_dsha" != "$_rec" ]
    }

    # Record a sha as the destination's provenance through the SAME durable contract as a
    # publish (temp, fsync, rename, fsync); an in-place write risks a partial record.
    _stamp_snapshot_sig() {
        local _s="$1" _sha="$2" _rtmp
        _rtmp="$(mktemp "${_s}.repair.XXXXXX" 2>/dev/null)" || _rtmp=""
        if [ -z "$_rtmp" ]; then
            log "snapshot: could not stage a signature repair for hosts/$(_host)/build_log.md; provenance left unchanged"
        elif ! printf '%s\n' "$_sha" > "$_rtmp" 2>/dev/null ||
            ! _fsync_path_and_dir "$_rtmp"; then
            rm -f "$_rtmp" 2>/dev/null || true
            log "snapshot: signature repair not confirmed durable before promotion; provenance left unchanged"
        elif ! mv -f "$_rtmp" "$_s" 2>/dev/null; then
            rm -f "$_rtmp" 2>/dev/null || true
            log "snapshot: could not promote a signature repair for hosts/$(_host)/build_log.md; provenance left unchanged"
        elif ! _fsync_path_and_dir "$_s"; then
            # Nothing to recover TO — the record describes bytes already in place. Report
            # unconfirmed and stop; the next tick re-checks it.
            log "snapshot: repaired signature for hosts/$(_host)/build_log.md is not confirmed durable; it will be re-checked next tick"
        else
            return 0
        fi
        return 1
    }

    if [ -f "$WORKSPACE_DIR/build_log.md" ]; then
        local _src="$WORKSPACE_DIR/build_log.md"
        local _dst="$_host_dir/build_log.md" _sig="$_host_dir/.build_log.snapshot-sha"
        local _int="$_host_dir/.build_log.snapshot-sha.next"
        local _partial=0
        _recover_snapshot_publish "$_dst" "$_sig" "$_int" || _partial=$?
        # A destination still partial after recovery must not be pushed as if it were whole.
        [ "$_partial" -eq 3 ] && return 3
        local _cur="" _rec=""
        [ -f "$_dst" ] && _cur="$(shasum -a 256 "$_dst" 2>/dev/null | cut -d' ' -f1)"
        # Validate raw bytes BEFORE any $(): substitution strips NULs, so a
        # NUL-damaged record would collapse to 64 clean hex and gain authority.
        [ -f "$_sig" ] && _rec="$(_usable_sig_record "$_sig")"
        local _dir=""
        if [ -f "$_dst" ] && [ -n "$_cur" ] && [ -z "$_rec" ] && ! cmp -s "$_src" "$_dst" 2>/dev/null; then
            # A missing or damaged record grants nothing by itself: only a copy that root
            # strictly extends is adopted (nothing in it can be lost); every other shape refuses.
            _dir="$(_append_only_direction "$_src" "$_dst")"
            if [ "$_dir" = root-live ] && _stamp_snapshot_sig "$_sig" "$_cur"; then
                _rec="$_cur"
                log "snapshot: adopted hosts/$(_host)/build_log.md — no usable provenance record and root strictly extends it; stamped its current sha and refreshing from root"
            fi
        fi
        if [ -f "$_dst" ] && cmp -s "$_src" "$_dst" 2>/dev/null; then
            # Equal content is safe to re-own; nothing to propagate.
            if [ "$_cur" != "$_rec" ]; then
                _stamp_snapshot_sig "$_sig" "$_cur" || true
            fi
        elif [ ! -f "$_dst" ] || { [ -n "$_rec" ] && [ "$_cur" = "$_rec" ]; }; then
            # Ours or absent -> stage beside the dest, swap only if the dest still
            # matches. The recorded sha comes from the temp, never a post-swap read.
            local _tmp
            _tmp="$(mktemp "${_dst}.snap.XXXXXX" 2>/dev/null)" || _tmp=""
            if [ -n "$_tmp" ] && cp -p "$_src" "$_tmp" 2>/dev/null; then
                local _new _now=""
                _new="$(shasum -a 256 "$_tmp" 2>/dev/null | cut -d' ' -f1)"
                [ -f "$_dst" ] && _now="$(shasum -a 256 "$_dst" 2>/dev/null | cut -d' ' -f1)"
                if [ "$_now" = "$_cur" ]; then
                    # The staged copy is the roll-forward source and the INTENT names it; both must be
                    # durable BEFORE the destination is truncated. Every step fails CLOSED; none may be skipped.
                    local _rrc=0
                    if ! _fsync_path_and_dir "$_tmp"; then
                        rm -f "$_tmp" 2>/dev/null || true
                        warn_operator "snapshot refused: could not make the staged copy of hosts/$(_host)/build_log.md durable; per-host copy and provenance left unchanged"
                    elif ! printf '%s\n' "$_new" > "$_int" 2>/dev/null ||
                        ! _fsync_path_and_dir "$_int"; then
                        rm -f "$_tmp" "$_int" 2>/dev/null || true
                        warn_operator "snapshot refused: could not durably record the publish intent for hosts/$(_host)/build_log.md; per-host copy and provenance left unchanged"
                    elif _replace_in_place --replace "$_tmp" "$_dst" "$_cur" || {
                            _rrc=$?
                            # Partial destination: finish from the durable copy now, not next tick.
                            [ "$_rrc" -eq 3 ] && _replace_in_place --rollforward "$_tmp" "$_dst" && {
                                _rrc=0
                                log "snapshot: in-place write of hosts/$(_host)/build_log.md stopped short; rolled forward from the staged copy in the same tick"
                            }
                        }; then
                        rm -f "$_tmp" 2>/dev/null || true
                        # The destination must be durable BEFORE the signature claims it; if it is not,
                        # leave the intent for the next tick to verify or discard.
                        if ! _fsync_path_and_dir "$_dst"; then
                            log "snapshot: destination not confirmed durable; intent left for recovery, signature not promoted"
                        elif ! mv -f "$_int" "$_sig" 2>/dev/null; then
                            # Promotion is atomic-rename ONLY — an in-place write
                            # here would reintroduce the partial signature.
                            log "snapshot: could not promote the publish intent; intent left for recovery, signature unchanged"
                        elif ! _fsync_path_and_dir "$_sig"; then
                            # Same asymmetry as the recovery path: the promote rename consumed the intent, so
                            # a non-durable signature must leave a fresh one behind.
                            if printf '%s\n' "$_new" > "$_int" 2>/dev/null && _fsync_path_and_dir "$_int"; then
                                log "snapshot: signature promoted but not confirmed durable; intent re-created for the next tick"
                            else
                                warn_operator "snapshot: signature for hosts/$(_host)/build_log.md is NOT confirmed durable and the recovery intent could not be re-created; provenance may be lost on a crash"
                            fi
                        fi
                    elif [ "$_rrc" -eq 3 ]; then
                        # The staged copy is vault-excluded and the intent names it: both stay so the
                        # next tick's recovery rolls forward. Returning 3 keeps this tick from pushing.
                        _partial=3
                        warn_operator "snapshot: hosts/$(_host)/build_log.md is PARTIAL — the in-place write failed after the truncate and the roll-forward also failed; staged copy and intent kept for the next tick, this sync will not push it"
                    else
                        rm -f "$_tmp" "$_int" 2>/dev/null || true
                        if [ "$_rrc" -eq 2 ]; then
                            warn_operator "snapshot refused: hosts/$(_host)/build_log.md changed between check and replace; a live writer is active — not clobbering"
                        else
                            log "snapshot: in-place replace of hosts/$(_host)/build_log.md failed; temp removed, per-host copy and provenance left unchanged"
                        fi
                    fi
                else
                    rm -f "$_tmp" 2>/dev/null || true
                    warn_operator "snapshot refused: hosts/$(_host)/build_log.md changed between check and replace; a live writer is active — not clobbering"
                fi
            else
                [ -n "$_tmp" ] && rm -f "$_tmp" 2>/dev/null || true
            fi
        elif ! cmp -s "$_src" "$_dst" 2>/dev/null; then
            if [ "$_dir" = host-live ]; then
                warn_operator "snapshot refused: hosts/$(_host)/build_log.md extends root and has NO USABLE provenance record — root is a stale relic beside a live per-host log; not clobbering. pick ONE writer and archive the other"
            elif [ "$_dir" = diverged ]; then
                warn_operator "snapshot refused: hosts/$(_host)/build_log.md and root have DIVERGED with NO USABLE provenance record — writer direction cannot be established; not clobbering. pick ONE writer and archive the other"
            elif [ -z "$_rec" ]; then
                # Only reachable when the adoption stamp above was not confirmed durable.
                warn_operator "snapshot: hosts/$(_host)/build_log.md has NO USABLE provenance record and could not be adopted this tick (its new record was not confirmed durable) — per-host copy left unchanged, retried next sync; this is NOT evidence of an independent writer"
            else
                warn_operator "snapshot refused: hosts/$(_host)/build_log.md has an independent writer (content differs from the recorded snapshot); root and per-host both claim build_log — pick ONE writer and archive the other"
            fi
        fi
        return "$_partial"
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

# Resolve every unmerged path by keeping OUR side — after preserving THEIRS.
#
# `--ours` is right for host-local state and lossy for anything both hosts
# append to. On 2026-07-31 it silently dropped two MEMORY.md index lines
# (merge 64dec1b2) and a WIRE episode-index entry (merge 258c349b); the second
# stayed missing for ~2 days. Neither surfaced: the log named the PEER but
# never which files lost their incoming version, and `git log -- FILE` cannot
# show a change destroyed IN a merge, because history simplification hides
# merge commits — so the normal way of looking finds nothing.
#
# Stage 3 is "theirs". The copy goes under state/, which the sync excludes
# (0 tracked files there), so a backup can never itself become a conflicting
# tracked file. Best-effort throughout: a failure to preserve must never block
# the merge, and a DD conflict (both sides deleted) has no stage 3 to save.
#
# Extracted from the caller so the behaviour can be tested against a REAL
# conflicted index rather than a re-implementation of this loop — a test that
# rebuilds the loop it checks passes just as happily against the unfixed script.
_resolve_conflicts_keep_ours() {
    local peer="${1:-peer}" backup_root="${2:-}" f
    # Default location is INSIDE the git dir, not under state/. `state/` looked
    # safe because it currently has 0 tracked files — but that is an observation
    # of one config, not an invariant: `vault.sync.include` is user-configurable
    # and _compose_exclude_content emits includes before excludes (last match
    # wins), so a supported `state/` include un-ignores `state/**` and the next
    # push would stage these backups. Anything under the git dir is never
    # tracked by construction, whatever the carrier set says. `rev-parse
    # --git-dir` resolves correctly for a worktree too, where .git is a file.
    # (john-the-dev, #2476 review blocker 2.)
    if [ -z "$backup_root" ]; then
        local _gitdir; _gitdir="$(git rev-parse --git-dir 2>/dev/null || echo .git)"
        backup_root="$_gitdir/sutando-sync-conflicts/$(date -u +%Y%m%dT%H%M%SZ)-$(printf '%s' "$peer" | tr '/' '_')"
    fi
    # Counted, not assumed: the summary below must be a FUNCTION of what
    # actually got written. v1 logged only on success and then claimed "each
    # discarded incoming file is preserved" whenever the directory merely
    # existed — so a failed mkdir/write discarded the peer's version silently
    # while telling the operator it was recoverable (blocker 1, reproduced by
    # the reviewer by making backup/dir a regular file).
    local saved=0 failed=0 failed_list=""
    while IFS= read -r -d '' f; do
        if git show ":3:$f" >/dev/null 2>&1; then
            if mkdir -p "$backup_root/$(dirname "$f")" 2>/dev/null &&
               git show ":3:$f" > "$backup_root/$f" 2>/dev/null; then
                saved=$((saved + 1))
                log "_resolve_conflicts_keep_ours: discarded incoming $f -> $backup_root/$f"
            else
                failed=$((failed + 1))
                failed_list="${failed_list:+$failed_list, }$f"
                # Loud per-file: this one is genuinely unrecoverable.
                log "_resolve_conflicts_keep_ours: FAILED to preserve $f — incoming version is LOST"
                color_warn "sync-workspace: could NOT preserve the incoming version of $f (backup write failed under $backup_root) — that version is discarded unrecoverably"
            fi
        else
            log "_resolve_conflicts_keep_ours: discarded incoming $f (no stage-3 blob to preserve)"
        fi
        # `--ours` fails on DD-conflicts (both sides deleted) — the file isn't
        # on our side either. Fall back to `git rm` so the merge can complete
        # cleanly. Surfaced by Mini #1445 v3 Test 12. Preservation stays
        # best-effort: a backup failure must never block the merge.
        if git checkout --ours -- "$f" 2>/dev/null; then
            git add -- "$f"
        else
            git rm -f -- "$f" 2>/dev/null || true
        fi
    done < <(git diff --name-only --diff-filter=U -z)
    if [ "$failed" -gt 0 ]; then
        color_warn "sync-workspace: kept the local version on conflict with $peer; $saved incoming file(s) preserved under $backup_root, $failed NOT saved ($failed_list)"
    elif [ "$saved" -gt 0 ]; then
        color_warn "sync-workspace: kept the local version on conflict with $peer; all $saved discarded incoming file(s) preserved under $backup_root"
    fi
}

# Pre-pull anchor migration (#2567). `state/current-track.md` is per-host state
# that used to be carried at a shared flat path; that path is removed from the
# carrier set in this change. On any vault where the file is still TRACKED,
# `_enforce_carrier_set_pre` will untrack it and COMMIT that deletion — and a
# peer pulls the deletion before its own enforcement ever runs (the pull half of
# `cmd_default_bidirectional` precedes the push half). Without this helper that
# peer loses its anchor.
#
# Same-commit migration on the PUSHING host does not fix it: that host can only
# add ITS OWN `hosts/<label>/current-track.md`, which is not the puller's anchor.
# The guarantee therefore has to be local and to run BEFORE the fetch/merge —
# each host rescues its own copy. Same placement and contract as
# `_migrate_flat_branch` above.
#
# Called from BOTH entry points, and the reason is worth stating because the
# pull-side rationale above does not imply it. The hazard is not the merge; it
# is `_enforce_carrier_set_pre`, which untracks newly-excluded files and lets
# the caller commit that deletion. `--push-only` runs that enforcement without
# ever passing through `_pull_only_impl`, so a pull-only call site leaves the
# explicit push mode able to delete the sole carried copy of an anchor it never
# replaced. Idempotent, so calling it twice in the default bidirectional path
# costs one `[ -e ]`. Idempotent: a no-op once the per-host file
# exists, so it costs one `[ -e ]` per tick thereafter.
_migrate_flat_anchor() {
    local _flat _dest
    _flat="$WORKSPACE_DIR/state/current-track.md"
    _dest="$WORKSPACE_DIR/hosts/$(_host)/current-track.md"
    [ -f "$_flat" ] || return 0
    [ -e "$_dest" ] && return 0
    # DRY_RUN is checked HERE, not only at the call site: this helper runs before
    # _pull_only_impl's dry-run early return (it must, to beat an incoming
    # deletion), so the guard has to live with the mutation it protects. A future
    # caller cannot reintroduce the violation by placing the call differently.
    if [ "$DRY_RUN" = "1" ]; then
        echo "DRY-RUN: would migrate state/current-track.md -> hosts/$(_host)/current-track.md (#2567)" >&2
        return 0
    fi
    mkdir -p "$(dirname "$_dest")" || return 0
    cp "$_flat" "$_dest" || return 0
    log "_migrate_flat_anchor: copied state/current-track.md -> hosts/$(_host)/current-track.md before pull (#2567)"
    echo "sync-workspace: migrated the per-host anchor to hosts/$(_host)/current-track.md (was at the shared flat path; #2567)" >&2
}

_pull_only_impl() {
    cd "$WORKSPACE_DIR" || die "pull-only: cannot cd to $WORKSPACE_DIR"
    [ -d ".git" ] || die "pull-only: $WORKSPACE_DIR is not a git repo; run --init first"

    _assert_sync_initialized "pull-only"

    # Rescue this host's anchor BEFORE any peer deletion can merge in (#2567).
    _migrate_flat_anchor

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
            _resolve_conflicts_keep_ours "$peer"
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
    # a rebuild. Config failures are non-fatal; a PARTIAL build_log snapshot (rc 3) is
    # the one case that withholds the push — see the branch below.
    local _snap_rc=0
    _snapshot_per_host_config || _snap_rc=$?
    if [ "$_snap_rc" -eq 3 ]; then
        # A partial per-host build_log must not be vaulted as if whole; the next tick rolls it forward.
        color_warn "sync-workspace: per-host build_log snapshot is PARTIAL after a failed write; not pushing this tick"
        return 1
    elif [ "$_snap_rc" -ne 0 ]; then
        color_warn "sync-workspace: per-host config snapshot failed (non-fatal); push continues"
    fi
    # Rescue this host's anchor BEFORE carrier enforcement can untrack it
    # (#2567/#2607). `--push-only` never reaches `_pull_only_impl`, so without
    # this call the enforcement below regenerates the exclude, untracks the
    # now-uncarried flat `state/current-track.md`, and COMMITS that deletion —
    # on a host whose anchor exists only at the flat path that removes the sole
    # carried copy and writes no replacement. Pull-side placement alone is not
    # enough: the hazard is carrier enforcement, and both entry points run it.
    _migrate_flat_anchor
    _enforce_carrier_set_pre
    git add -A
    _refuse_staged_secrets
    if ! _refuse_foreign_host_deletions; then
        return 1
    fi
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
    local deleted staged_d untracked_by_policy max_delete _p
    # `-M` for rename detection: legitimate moves (refactor) don't count as
    # deletions. Mirrors pull-side tripwire fix. Mini #1445 v4 Low.
    staged_d=$(git diff -M --cached --name-only --diff-filter=D | wc -l | tr -d ' ')
    # A policy untrack leaves the file on disk; a real deletion does not. Both
    # stage a D under an excluded path, so disk presence is the discriminator.
    untracked_by_policy=0
    while IFS= read -r -d '' _p; do
        if [ -e "$_p" ] || [ -L "$_p" ]; then
            untracked_by_policy=$(( untracked_by_policy + 1 ))
        fi
    done < <(git diff -M --cached --name-only --diff-filter=D -z \
        | git check-ignore -z --stdin --no-index 2>/dev/null || true)
    deleted=$(( staged_d - untracked_by_policy ))
    [ "$deleted" -ge 0 ] || deleted=0
    if [ "$untracked_by_policy" -gt 0 ]; then
        log "_push_only_impl: tripwire counts $deleted real deletion(s); $untracked_by_policy staged D(s) are policy untracks still on disk"
    fi
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
    # Cross-machine sync is opt-in — enabled only when a vault URL is configured
    # (--vault-url, sutando.config vault.remote_url, or the legacy
    # SUTANDO_MEMORY_REPO). When none is set, sync is INTENTIONALLY disabled, and
    # this automated (cron) path must skip cleanly rather than fall into
    # _pull_only_impl → `die "…is not a git repo; run --init first"`, which paints
    # a red error and a non-zero exit on every tick (every 30 min for a typical
    # cron). A disabled feature erroring loudly is noise, not a signal. The
    # explicit subcommands (--init / --pull-only / --push-only) keep their loud
    # "run --init first" feedback because the operator asked for them directly.
    if [ -z "$VAULT_URL" ]; then
        log "cmd_default_bidirectional: no vault URL configured — cross-machine sync disabled; skipping."
        echo "sync-workspace: cross-machine sync disabled (no vault URL configured) — skipping." >&2
        return 0
    fi
    acquire_lock
    _pull_only_impl || true   # pull failures shouldn't block push
    # `|| _rc=$?`, NOT a bare call then `$?`: under `set -e` a non-zero
    # `_push_only_impl` would exit before the reporter below ever runs.
    local _rc=0
    _push_only_impl || _rc=$?
    # Report rather than auto-merge: a union merge loses no line but resurrects
    # an in-place retraction beneath its own correction, where it reads as current.
    _report_unmerged_conflicts || true   # fail-open: never change sync's outcome
    return "$_rc"
}

# Print preserved-but-unmerged peer content. Deliberately does NOT gate on the
# reporter's exit status: a broken diagnostic must not fail a good sync.
_report_unmerged_conflicts() {
    local script="$REPO_DIR/scripts/sync-conflicts-report.py"
    [ -f "$script" ] || return 0
    [ -n "${SYNC_PY:-}" ] || return 0
    local out
    out="$("$SYNC_PY" "$script" "$WORKSPACE_DIR" 2>&1)" || true
    # Only speak up when there is something to merge back; the clean case is
    # silent so a 30-minute cron does not grow a nag nobody reads.
    case "$out" in
        *"no unmerged peer content"*) log "_report_unmerged_conflicts: clean" ;;
        "") : ;;
        *) log "_report_unmerged_conflicts: $out"; printf '%s\n' "$out" ;;
    esac
    return 0
}

cmd_status() {
    echo "WORKSPACE_DIR: $WORKSPACE_DIR"
    echo "REPO_DIR:      $REPO_DIR"
    # A recovered URL and a configured one print identically without the source,
    # and a declined candidate reads as an <unset> naming nothing to go fix.
    if [ -n "$VAULT_URL" ]; then
        echo "VAULT_URL:     $VAULT_URL${VAULT_URL_SOURCE:+  (source: $VAULT_URL_SOURCE)}"
    else
        echo "VAULT_URL:     <unset>"
        if [ -n "$VAULT_URL_DECLINED" ]; then
            echo "               candidate NOT adopted: $VAULT_URL_DECLINED"
            echo "               reason: $VAULT_URL_DECLINED_REASON"
        fi
    fi
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
    elif [ -f "$WORKSPACE_DIR/.git" ]; then
        # A linked worktree has a .git FILE. Reporting it as "not a git repo"
        # contradicts the VAULT_URL line printed just above it.
        echo "git status: workspace is a linked git WORKTREE (.git is a file); --push-only refuses this layout"
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

    # Claude Code dashes EVERY non-alphanumeric char, not just `/`; a path with a
    # space or dot would otherwise resolve to a slug it never creates.
    local local_slug
    local_slug="$(printf '%s' "$REPO_DIR" | tr -c 'A-Za-z0-9' '-')"

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
