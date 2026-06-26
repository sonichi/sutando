#!/usr/bin/env bash
# sutando-migrate.sh — M1 Part 2 recovery / migration CLI
#
# Migrates per-user workspace state from legacy locations to the canonical M0
# in-repo workspace (<repo>/workspace/ by default; honors sutando.config.local.json
# and $SUTANDO_WORKSPACE legacy env override).
#
# Sources auto-detected (any subset may be present):
#   A  <repo>/                                — pre-M0 repo-root writes (notes/, state/,
#                                                results/, tasks/, logs/, data/, build_log.md,
#                                                conversation.log, pending-questions.md,
#                                                session-state.md, context-drop.txt)
#   B  ~/.sutando/workspace/                  — pre-M0 default fallback
#   C  $SUTANDO_WORKSPACE  (env / .env)       — custom override (commonly the
#                                                managed-sync workspace dir)
#
# Per-class action (file-class → strategy):
#   structural-copy      notes/, data/, logs/, results/archive/, results/calls/,
#                        config/, slack-inbox/, telegram-inbox/, tasks/archive/
#   collision/keep-both  notes/*.md, data/* user files
#   newest-mtime-wins    state/*.json snapshots, pending-questions.md,
#                        session-state.md
#   append-merge OR      build_log.md, conversation.log, context-drop.txt
#     sidecar (default)
#   re-home              loose root *.json (cloud-auth, device, contextual-chips,
#                        voice-state, core-status) → <dest>/state/
#   skip-ephemeral       *.alive, tasks/task-*.txt + results/task-*.txt <60s old,
#                        migration-backup-*.tar*, .gitkeep, .DS_Store
#   skip-vcs             .git/  (handled by separate sync-rehome script if
#                        sync is in use; otherwise orphaned in source)
#
# Modes:
#   scan      (default) — read-only audit; print per-source per-class action; exits 0
#   commit    — actually do it (rsync -a + per-class strategy; backup first)
#   verify    — post-migration: source-empty + dest-complete + hash match
#   rollback  — restore <dest> from a prior migration-backup tarball
#
# Safety guarantees (per `feedback_design_quality`):
#   - Idempotent at every phase; re-runnable after partial failure
#   - Atomic per-file: rsync → sha256 verify → mark source-migrated → only then delete source (if --delete)
#   - Backup <dest> to <dest>/state/migration-backup-<ts>.tar.gz before any commit-mode write
#   - Refuses if realpath(dest) == realpath(any source)
#   - Refuses to follow source symlinks (the #1149 footgun)
#   - In-flight protection: tasks/*.txt + results/*.txt newer than $INFLIGHT_GUARD_SEC are skipped+warned
#   - Loud failure on partial state (set -euo pipefail); no silent fallbacks
#
# Honors `feedback_workspace_m1_no_auto_commit`: this script mutates workspace
# DATA but never runs `git commit` against any source/dest repo. The
# sync-workspace.sh re-route is a separate concern (see sutando-plus's
# sutando-migrate-sync.sh).
#
# Usage:
#   bash scripts/sutando-migrate.sh                       # scan (dry-run)
#   bash scripts/sutando-migrate.sh --json                # scan, JSON output
#   bash scripts/sutando-migrate.sh --source A,B          # restrict sources scanned
#   bash scripts/sutando-migrate.sh commit                # do it (writes to dest)
#   bash scripts/sutando-migrate.sh --commit              # alias for commit
#   bash scripts/sutando-migrate.sh commit --merge-append # Strategy B (concat with divider)
#                                                          # instead of sidecar (Strategy C default)
#   bash scripts/sutando-migrate.sh verify                # post-migration check
#   bash scripts/sutando-migrate.sh rollback <backup-id>  # restore from tarball
#   bash scripts/sutando-migrate.sh --help

set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

INFLIGHT_GUARD_SEC=60

# Canonical workspace surface — the ONLY paths the script reads from a source.
# Anything outside this surface in a source is ignored (a source can be a
# whole repo checkout (Source A) — we MUST NOT recursively walk sutando's
# code, node_modules, .git, etc.).
WORKSPACE_SURFACE_DIRS=(
    "notes"
    "state"
    "results"
    "tasks"
    "logs"
    "data"
    "config"
    "slack-inbox"
    "telegram-inbox"
    # Per Mini #7 #design 2026-06-02: defensive surface coverage for dirs observed
    # in real workspaces but not yet in M0 contract. Class rules use
    # collision-keep-both/structural to preserve content; CLAUDE.md should
    # eventually contract-define these or evict them.
    "agents"
    "docs"
    "email-drafts"
    "agent-inbox"
)

# Per `feedback_per_source_surface_lists` 2026-06-02: dirs in Mini's #7
# defensive set that EXIST at the sutando repo root as CODE (not workspace
# data). Source A walks must SKIP these — otherwise migration would treat
# `<repo>/docs/`, `<repo>/agents/`, etc. as workspace surface, migrate them
# into `<workspace>/`, and `--delete-source` would delete the repo's own
# documentation. Caught after owner's repo-root cleanup wiped `<repo>/docs/`
# (recovered via git checkout HEAD -- docs/).
SOURCE_A_EXCLUDE=(
    "agents"
    "docs"
    "email-drafts"
    "agent-inbox"
)
WORKSPACE_SURFACE_FILES=(
    "build_log.md"
    "conversation.log"
    "pending-questions.md"
    "session-state.md"
    "context-drop.txt"
    "cloud-auth.json"
    "device.json"
    "contextual-chips.json"
    "voice-state.json"
    "core-status.json"
    # Per Lucy #design 2026-06-02: contract-defined personal-override file
    # ("if PERSONAL_CLAUDE.md exists in workspace root, read+follow it").
    # Previously missing from the surface → would skip-unknown → user's
    # personal rules silently lost on migration. Now class-handled.
    "PERSONAL_CLAUDE.md"
    # Per Lucy #design 2026-06-02: additional per-host status JSONs that
    # were unruled. Adding as surface so they don't skip-unknown.
    "quota-state.json"
    "dynamic-content.json"
    "stand-identity.json"
)

# In-flight ephemeral patterns (skipped with warning if matched + age < guard)
EPHEMERAL_PATTERNS=(
    "*.alive"
    ".DS_Store"
    ".gitkeep"
    "migration-backup-*.tar*"
)

# Quarantine-walk excludes (when WALK_FULL_TREE is set for a source).
# Anything matching these patterns is skipped entirely — not migrated, not
# quarantined. Per Lucy #design 2026-06-02 + owner direction: capture user's
# custom workspace content (experiments/, obsidian-vault/, etc.) without
# accidentally hoovering up VCS/runtime/build artifacts.
QUARANTINE_EXCLUDES=(
    ".git"
    "node_modules"
    ".cache"
    ".venv"
    "venv"
    "__pycache__"
    "dist"
    "build"
    ".next"
    ".nuxt"
    ".DS_Store"
)

# Per-class file classification rules. Order matters — first match wins.
declare -a CLASS_RULES
CLASS_RULES=(
    # form: <relpath-glob>|<class>
    # ORDER MATTERS — first match wins. Per Mini's #design review 2026-06-02:
    # - root loose-file globs (no leading directory) only match root files;
    #   `state/voice-state.json` does NOT match the bare `voice-state.json` rule
    # - rule ordering puts root rules before sub-dir rules
    # - `state/auth/*` MUST precede `state/*.json` so per-host auth files don't
    #   get incorrectly `newest-mtime`-resolved (Mini #2)
    "build_log.md|append"
    "conversation.log|rehome-narrative-log"
    "context-drop.txt|append"
    "pending-questions.md|append"
    # Per Lucy #design 2026-06-02 — contract-defined personal override.
    # Singleton-canonical: newest-mtime is the right strategy (one user,
    # one PERSONAL_CLAUDE; cross-host divergence means whichever the user
    # edited most recently wins).
    "PERSONAL_CLAUDE.md|newest-mtime"
    # Per Lucy #design 2026-06-02 — per-host state files at root (pre-M0
    # layout) shouldn't migrate as newest-wins (would drop a host's data
    # if multi-host scan). Re-home to state/ via rehome-state class +
    # commit-time collision-keep-both for per-host preservation.
    "quota-state.json|rehome-state"
    "dynamic-content.json|rehome-state"
    # stand-identity.json is a personalPath (per-machine) file, NOT a state file.
    # Its reader personal_path()/personalPath() resolves
    # $SUTANDO_MEMORY_DIR/machine-<host>/<file> then falls back to
    # <workspace>/<file> (root) — it NEVER looks in state/. Classifying it
    # rehome-state (→ state/) homed it at a path the reader can't see, so
    # voice/discord-voice lost the Stand name and fell back to "Sutando" (#1540).
    # Treat it like the sibling personal-override root file PERSONAL_CLAUDE.md
    # above: keep at workspace root via newest-mtime so personalPath resolves it.
    "stand-identity.json|newest-mtime"
    "pending-questions-resolved-archive-*.md|rehome-dated-snapshot"
    "session-state.md|newest-mtime"
    "cloud-auth.json|rehome-state"
    "device.json|rehome-state"
    "contextual-chips.json|rehome-state"
    "voice-state.json|rehome-state"
    "core-status.json|rehome-state"
    "tasks/archive/*|structural"
    # Per Mini #design 5:27 UTC: legacy task-archive subdirs from older bridge
    # versions. Mini's workspace has 29 processed + 559 done; owner's B has 34
    # processed, C has 147 processed. Real historical task content — must NOT
    # hit the `tasks/*|skip-unknown` catchall below.
    "tasks/processed/*|structural"
    "tasks/done/*|structural"
    "tasks/task-*.txt|inflight-guard"  # CANCEL_INSTRUCTION still starts with `task-` per CLAUDE.md
    "tasks/*|skip-unknown"
    "results/archive/*|structural"
    "results/calls/*|structural"
    # Mini #design 5:27 UTC pre-merge checklist: defensive coverage for
    # symmetric legacy subdirs not seen in this scan but plausible on other
    # hosts. Cost = 0, prevents future scan gap.
    "results/processed/*|structural"
    "results/done/*|structural"
    "results/task-*.txt|inflight-guard"
    "results/*|skip-unknown"
    "state/cores/*.alive|skip-ephemeral"
    "state/auth/*|structural"  # Mini #2: per-host identity, NOT newest-wins
    # Per Lucy #design 2026-06-02 follow-up empirical: per-host status JSONs
    # at state/<name>.json would hit state/*.json|newest-mtime and drop a
    # losing host's data on multi-host scan. Same hazard as state/auth — carve
    # out to structural to preserve per-host copies.
    "state/core-status.json|structural"
    "state/quota-state.json|structural"
    "state/dynamic-content.json|structural"
    "state/voice-state.json|structural"
    "state/contextual-chips.json|structural"
    "state/*.json|newest-mtime"
    "state/*|structural"
    "notes/*|collision-keep-both"  # Mini #4: accretes cruft over N migrations;
                                    # commit-side identical-drop mitigates 87/87 in real data
    "logs/*|structural"
    "data/*|collision-keep-both"  # Mini #5: was structural (ambiguous); keep-both safer
    "config/*|collision-keep-both"  # Mini #3: scope undefined in contract; safer default
    "slack-inbox/*|structural"
    "telegram-inbox/*|structural"
    # Mini #7: surface adds for C-observed dirs not in M0-contract; safe default
    "agents/*|structural"
    "docs/*|structural"
    "email-drafts/*|structural"
    "agent-inbox/*|structural"
    # Catchall — per Lucy #design 2026-06-02 + owner direction: workspace
    # sources B+C may have user-custom dirs/files (experiments/, obsidian-vault/,
    # personal-src/, repro-*.ts, etc.) outside the canonical surface. Anything
    # that doesn't match an explicit rule above falls here and gets quarantined
    # to <dest>/legacy/<src-tag>/quarantine/<relpath>. Net: no user content is
    # silently lost. Only walked when WALK_FULL_TREE per-source flag is set
    # (B + C); Source A (repo root) stays surface-restricted to avoid
    # quarantining sutando code.
    "*|quarantine-unknown"
)

# ──────────────────────────────────────────────────────────────────────────────
# Resolve script root + helper location (handles cross-checkout via BASH_SOURCE)
# ──────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HELPER="$REPO_DIR/scripts/sutando-config.sh"

if [ ! -x "$HELPER" ] && [ ! -f "$HELPER" ]; then
    echo "sutando-migrate: cannot find $HELPER (expected next to this script)" >&2
    exit 2
fi
# Dest resolution deferred to after arg parsing so --respect-env can take effect.

# ──────────────────────────────────────────────────────────────────────────────
# Source detection
# ──────────────────────────────────────────────────────────────────────────────

# Source A — repo root, non-canonical legacy
# (TEST hook: SUTANDO_MIGRATE_SRC_A overrides for E2E fixtures)
A_PATH="${SUTANDO_MIGRATE_SRC_A:-$REPO_DIR}"
A_REAL="$(cd "$A_PATH" 2>/dev/null && pwd -P || true)"

# Source B — pre-M0 default fallback
# (TEST hook: SUTANDO_MIGRATE_SRC_B overrides for E2E fixtures)
B_PATH="${SUTANDO_MIGRATE_SRC_B:-$HOME/.sutando/workspace}"

# Source C — env override (env or .env)
# (TEST hook: SUTANDO_MIGRATE_SRC_C overrides for E2E fixtures)
detect_C() {
    local c=""
    if [ -n "${SUTANDO_MIGRATE_SRC_C:-}" ]; then
        c="$SUTANDO_MIGRATE_SRC_C"
    elif [ -n "${SUTANDO_WORKSPACE:-}" ]; then
        c="$SUTANDO_WORKSPACE"
    elif [ -f "$REPO_DIR/.env" ]; then
        c="$(grep -E '^SUTANDO_WORKSPACE=' "$REPO_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- | sed 's/^"//;s/"$//;s/^//;s/$//')"
    fi
    # Expand ~
    c="${c/#\~/$HOME}"
    echo "$c"
}
C_PATH="$(detect_C)"

# Validate + canonicalize sources; drop empty / non-existent / == dest
validate_source() {
    local tag="$1"
    local path="$2"
    local real=""
    [ -z "$path" ] && return 1
    [ ! -d "$path" ] && return 1
    real="$(cd "$path" 2>/dev/null && pwd -P || true)"
    [ -z "$real" ] && return 1
    if [ "$real" = "$DEST_REAL" ]; then
        echo "sutando-migrate: source $tag ($real) == dest; skipping" >&2
        return 1
    fi
    echo "$real"
    return 0
}

# ──────────────────────────────────────────────────────────────────────────────
# Per-file classification
# ──────────────────────────────────────────────────────────────────────────────

classify() {
    # $1 = source-relative path (e.g. "notes/foo.md")
    local rel="$1"
    local rule
    for rule in "${CLASS_RULES[@]}"; do
        local glob="${rule%%|*}"
        local class="${rule##*|}"
        # shellcheck disable=SC2254
        case "$rel" in
            $glob) echo "$class"; return 0 ;;
        esac
    done
    # No rule matched — caller decides (skip-unknown or structural)
    echo "unknown"
}

# Check inflight age — returns 0 if file is older than guard
age_safe() {
    local file="$1"
    local now mtime age
    now="$(date +%s)"
    mtime="$(stat -f %m "$file" 2>/dev/null || stat -c %Y "$file" 2>/dev/null || echo 0)"
    age=$((now - mtime))
    [ "$age" -ge "$INFLIGHT_GUARD_SEC" ]
}

# ──────────────────────────────────────────────────────────────────────────────
# Scan: walk a source, report per-class buckets
# ──────────────────────────────────────────────────────────────────────────────

declare -a SOURCES_REAL SOURCES_TAGS
declare -a REPORT_LINES

# Cross-source index: TSV file collecting (relpath, tag, class, mtime, size) per
# file across ALL sources. Post-processed to surface relpaths that appear in
# >1 source (cross-source collisions per-source scan misses — e.g. build_log.md
# in A AND C bound for the same dest path).
XSRC_INDEX="$(mktemp -t sutando-migrate-xsrc.XXXXXX)"
trap 'rm -f "$XSRC_INDEX"' EXIT INT TERM
# Also include dest's existing files (tag "DEST") so we surface dest-collisions
# uniformly with cross-source collisions.

record_xsrc() {
    # $1=tag, $2=relpath, $3=class, $4=abs-path
    local tag="$1" rel="$2" cls="$3" file="$4"
    local mt sz
    mt="$(stat -f %m "$file" 2>/dev/null || stat -c %Y "$file" 2>/dev/null || echo 0)"
    sz="$(stat -f %z "$file" 2>/dev/null || stat -c %s "$file" 2>/dev/null || echo 0)"
    printf '%s\t%s\t%s\t%s\t%s\n' "$rel" "$tag" "$cls" "$mt" "$sz" >> "$XSRC_INDEX"
}

scan_source() {
    local tag="$1"
    local src="$2"
    # NOTE on portability: --base is not portable; we use absolute paths only.
    # Walk every non-ignored regular file under src.
    local file rel cls dest_path collision_kind="" size mtime_iso
    local n_structural=0 n_append=0 n_newest=0 n_rehome=0 n_skip=0 n_inflight=0 n_collision=0 n_unknown=0 n_quarantine=0
    local bytes_total=0

    REPORT_LINES+=("")
    REPORT_LINES+=("--- Source $tag — $src ---")

    # .git detection (commit-history layer; informational only for scan)
    if [ -d "$src/.git" ]; then
        REPORT_LINES+=("  .git: present (commit history; needs separate sync-rehome handling for vault sync)")
    fi

    # Migration sentinels — partial-migration history at this source
    local sentinels
    sentinels="$(find "$src" -maxdepth 1 -type f -name ".*-migrated*" 2>/dev/null | sort)"
    if [ -n "$sentinels" ]; then
        REPORT_LINES+=("  prior partial migration sentinels:")
        while IFS= read -r s; do
            local sm
            sm="$(stat -f '%Sm' -t '%Y-%m-%d' "$s" 2>/dev/null || stat -c '%y' "$s" 2>/dev/null | cut -d' ' -f1)"
            REPORT_LINES+=("    $(basename "$s")  ($sm)")
        done <<<"$sentinels"
    fi

    # Build the explicit walk list. Surface-restricted for Source A (its root
    # is the SUTANDO REPO checkout, not a workspace — walking the whole tree
    # would pull in src/, node_modules/, .git, etc; 15,044-file false positive
    # caught in scan v1). Full-tree for sources B + C (they ARE workspace
    # roots; user-custom content like experiments/ obsidian-vault/ should be
    # quarantined per Lucy #design + owner direction 2026-06-02).
    local -a walk_paths=()
    local sd sf _ex_a
    # Per feedback_per_source_surface_lists 2026-06-02: detect whether THIS
    # source path IS a sutando repo checkout (by content, not by tag) — if
    # so, skip dirs that exist as repo code rather than workspace data
    # (docs/, agents/, etc.). Detection: presence of `src/sutando_config.py`
    # (or the same-shape sutando-config.sh) at source root. Owner clarified
    # 2026-06-02 07:29: "We should only EXCLUDE them when they are in the
    # sutando repo root." A custom workspace path that happens to live
    # inside a sutando checkout should ALSO get the exclude.
    local IS_SUTANDO_REPO=0
    if [ -f "$src/src/sutando_config.py" ] || [ -f "$src/scripts/sutando-config.sh" ]; then
        IS_SUTANDO_REPO=1
    fi
    for sd in "${WORKSPACE_SURFACE_DIRS[@]}"; do
        if [ "$IS_SUTANDO_REPO" = "1" ]; then
            local _skip_a=0
            for _ex_a in "${SOURCE_A_EXCLUDE[@]}"; do
                [ "$sd" = "$_ex_a" ] && _skip_a=1 && break
            done
            [ "$_skip_a" = "1" ] && continue
        fi
        [ -d "$src/$sd" ] && walk_paths+=("$src/$sd")
    done
    for sf in "${WORKSPACE_SURFACE_FILES[@]}"; do
        [ -f "$src/$sf" ] && walk_paths+=("$src/$sf")
    done
    # Quarantine walk: for B + C, also include any TOP-LEVEL entries not
    # already in the surface (e.g. experiments/, personal-src/, qr-codes/,
    # obsidian-vault/, loose .ts files). Skip standard noise.
    case "$tag" in
        B|C)
            local entry name
            for entry in "$src"/* "$src"/.[!.]*; do
                [ -e "$entry" ] || continue
                name="$(basename "$entry")"
                # Skip if already in surface (dirs or files).
                local already=0
                for sd in "${WORKSPACE_SURFACE_DIRS[@]}"; do [ "$name" = "$sd" ] && already=1 && break; done
                [ "$already" = "1" ] && continue
                for sf in "${WORKSPACE_SURFACE_FILES[@]}"; do [ "$name" = "$sf" ] && already=1 && break; done
                [ "$already" = "1" ] && continue
                # Skip standard excludes.
                local skip=0
                for ex in "${QUARANTINE_EXCLUDES[@]}"; do [ "$name" = "$ex" ] && skip=1 && break; done
                [ "$skip" = "1" ] && continue
                # Also skip hidden files that are migration sentinels themselves.
                case "$name" in
                    .migrated-from-*|.legacy-migrated-*|.notes-migrated|.build_log-migrated|.conversation-log-migrated|.status-migrated|.legacy-notice-printed|.last-pq-notify|.env|.gitignore)
                        continue ;;
                esac
                walk_paths+=("$entry")
            done
            ;;
    esac
    [ ${#walk_paths[@]} -eq 0 ] && {
        REPORT_LINES+=("  (no workspace surface present at this source — nothing to migrate)")
        return 0
    }

    # Walk only the surface; skip .git contents (handled separately) and pre-
    # existing migration backups.
    while IFS= read -r -d '' file; do
        rel="${file#"$src"/}"
        case "$rel" in
            .git/*) continue ;;
            state/migration-backup-*.tar*) n_skip=$((n_skip+1)); continue ;;
            .DS_Store|*/.DS_Store) n_skip=$((n_skip+1)); continue ;;
            .gitkeep|*/.gitkeep) n_skip=$((n_skip+1)); continue ;;
        esac

        cls="$(classify "$rel")"
        size="$(stat -f %z "$file" 2>/dev/null || stat -c %s "$file" 2>/dev/null || echo 0)"
        bytes_total=$((bytes_total + size))

        # Index for cross-source collision detection (only classes that
        # produce writes; skip-ephemeral / skip-unknown / inflight don't
        # collide because they aren't migrated). re-home target is
        # state/<basename>, not the original relpath, so we index under the
        # re-homed path so re-home collisions surface correctly.
        case "$cls" in
            structural|append|newest-mtime|collision-keep-both)
                record_xsrc "$tag" "$rel" "$cls" "$file"
                ;;
            rehome-state)
                # Per Mini #design 2026-06-02: cloud-auth/device → state/auth/, others → state/
                local _b="$(basename "$rel")"
                case "$_b" in
                    cloud-auth.json|device.json)
                        record_xsrc "$tag" "state/auth/$_b" "$cls" "$file"
                        ;;
                    *)
                        record_xsrc "$tag" "state/$_b" "$cls" "$file"
                        ;;
                esac
                ;;
            rehome-dated-snapshot)
                # Per Mini #design earlier: dated snapshot → notes/archive/<base>
                record_xsrc "$tag" "notes/archive/$(basename "$rel")" "$cls" "$file"
                ;;
            rehome-narrative-log)
                # Per Mini #design earlier: root conversation.log → logs/workspace-narrative.log
                record_xsrc "$tag" "logs/workspace-narrative.log" "$cls" "$file"
                ;;
        esac

        case "$cls" in
            structural)
                # Collision check
                dest_path="$DEST/$rel"
                if [ -e "$dest_path" ]; then
                    n_collision=$((n_collision+1))
                else
                    n_structural=$((n_structural+1))
                fi
                ;;
            append)
                # Always treated as collision once dest also has it; otherwise straight copy
                dest_path="$DEST/$rel"
                if [ -e "$dest_path" ] && [ -s "$dest_path" ]; then
                    n_append=$((n_append+1))
                else
                    n_structural=$((n_structural+1))
                fi
                ;;
            newest-mtime)
                n_newest=$((n_newest+1))
                ;;
            rehome-state)
                # Target is <dest>/state/<basename>
                n_rehome=$((n_rehome+1))
                ;;
            rehome-narrative-log|rehome-dated-snapshot)
                # Same bucket as rehome-state for display purposes (loose root file → canonical sub-path)
                n_rehome=$((n_rehome+1))
                ;;
            quarantine-unknown)
                # Catchall for user content under non-canonical relpaths (Source B/C only).
                # Counted separately so users see the quarantine footprint in scan output.
                n_quarantine=$((n_quarantine+1))
                ;;
            collision-keep-both)
                dest_path="$DEST/$rel"
                if [ -e "$dest_path" ]; then
                    n_collision=$((n_collision+1))
                else
                    n_structural=$((n_structural+1))
                fi
                ;;
            inflight-guard)
                if age_safe "$file"; then
                    # Old in-flight artifact — treat as archive
                    n_structural=$((n_structural+1))
                else
                    n_inflight=$((n_inflight+1))
                fi
                ;;
            skip-ephemeral|skip-unknown)
                n_skip=$((n_skip+1))
                ;;
            unknown)
                n_unknown=$((n_unknown+1))
                ;;
        esac
    done < <(find "${walk_paths[@]}" -type f -print0 2>/dev/null)

    REPORT_LINES+=("  files by action:")
    REPORT_LINES+=("    structural (copy-or-keep-both new):  $n_structural")
    REPORT_LINES+=("    collision    (same path, diff content):$n_collision")
    REPORT_LINES+=("    append-merge (build_log/conv.log):    $n_append")
    REPORT_LINES+=("    newest-mtime (snapshots):             $n_newest")
    REPORT_LINES+=("    re-home      (loose JSON → state/):   $n_rehome")
    REPORT_LINES+=("    quarantine   (non-canonical → legacy/<src>/quarantine/): $n_quarantine")
    REPORT_LINES+=("    in-flight-skip (<${INFLIGHT_GUARD_SEC}s old):       $n_inflight")
    REPORT_LINES+=("    skip-ephemeral / .DS_Store / .gitkeep:$n_skip")
    REPORT_LINES+=("    unknown (no rule matched, will skip): $n_unknown")
    REPORT_LINES+=("    total bytes:                          $(numfmt --to=iec "$bytes_total" 2>/dev/null || echo "$bytes_total")")
}

# ──────────────────────────────────────────────────────────────────────────────
# Arg parsing
# ──────────────────────────────────────────────────────────────────────────────

MODE="scan"
JSON=0
SOURCE_FILTER=""
MERGE_APPEND=0
DELETE_SOURCE=0
FORCE=0
ROLLBACK_ID=""
NO_CONFIRM=0
NO_CLAUDE_IMPORT=0
NO_HOOK_BRIDGE=0
NO_CHANNEL_BRIDGE=0

EXPLAIN_PATH=""
while [ $# -gt 0 ]; do
    case "$1" in
        scan|commit|verify|rollback) MODE="$1"; shift ;;
        --scan) MODE="scan"; shift ;;
        --commit) MODE="commit"; shift ;;
        --verify) MODE="verify"; shift ;;
        --rollback) MODE="rollback"; shift ;;
        explain)
            MODE="explain"
            shift
            # Next non-flag arg is the path to explain (positional).
            if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
                EXPLAIN_PATH="$1"
                shift
            fi
            ;;
        --dry-run) MODE="scan"; shift ;;  # alias for scan, per Mini #design 2026-06-02
        --json) JSON=1; shift ;;
        --source) SOURCE_FILTER="$2"; shift 2 ;;
        --merge-append) MERGE_APPEND=1; shift ;;
        --delete-source) DELETE_SOURCE=1; shift ;;
        --force) FORCE=1; shift ;;
        --respect-env) RESPECT_ENV=1; shift ;;
        --backup-id) ROLLBACK_ID="$2"; shift 2 ;;
        --no-confirm|--yes|-y) NO_CONFIRM=1; shift ;;  # skip pre-flight prompt (for CI / scripted runs)
        --no-claude-import) NO_CLAUDE_IMPORT=1; shift ;;  # skip auto-invocation of sutando-shell-setup.sh --import after commit (advanced; see Lucy's Maddy report 2026-06-06)
        --no-hook-bridge) NO_HOOK_BRIDGE=1; shift ;;  # skip auto-invocation of sutando-config-hooks.sh (Option D from #design 2026-06-07; see scripts/sutando-config-hooks.sh header)
        --no-channel-bridge) NO_CHANNEL_BRIDGE=1; shift ;;  # skip auto-copy of $SOURCE_CLAUDE_CONFIG_DIR/channels/ → $CLAUDE_CONFIG_DIR/channels/ (Option A+ from #design 2026-06-07)
        --help|-h)
            sed -n '1,80p' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        *)
            echo "sutando-migrate: unknown arg: $1" >&2
            echo "Try --help" >&2
            exit 2
            ;;
    esac
done

# ──────────────────────────────────────────────────────────────────────────────
# Dest resolution (deferred until after arg parsing so --respect-env applies)
# ──────────────────────────────────────────────────────────────────────────────

# Migration semantics (option 2 in M1 Part 2 design): the in-repo workspace is
# the destination regardless of whatever $SUTANDO_WORKSPACE the user has set
# today — migration MOVES data INTO the new default. Use `--respect-env` to
# honor env (e.g. for users whose dest is intentionally an env-overridden path).
# TEST hook: SUTANDO_MIGRATE_DEST overrides for E2E fixtures (symmetric with
# SUTANDO_MIGRATE_SRC_{A,B,C}). Takes precedence over both the resolver and
# --respect-env so test fixtures can pin DEST without cloning the whole repo.
if [ -n "${SUTANDO_MIGRATE_DEST:-}" ]; then
    DEST="$SUTANDO_MIGRATE_DEST"
elif [ "${RESPECT_ENV:-0}" = "1" ]; then
    DEST="$(bash "$HELPER" workspace 2>/dev/null)"
else
    DEST="$(env -u SUTANDO_WORKSPACE bash "$HELPER" workspace 2>/dev/null)"
fi
[ -z "$DEST" ] && {
    echo "sutando-migrate: workspace resolver returned no path. Run from a sutando checkout." >&2
    exit 2
}
mkdir -p "$DEST"
DEST_REAL="$(cd "$DEST" 2>/dev/null && pwd -P || true)"
[ -z "$DEST_REAL" ] && {
    echo "sutando-migrate: cannot resolve dest realpath ($DEST)" >&2
    exit 2
}

# ──────────────────────────────────────────────────────────────────────────────
# Main: scan (commit/verify/rollback stubs for now — phase 2B-2E)
# ──────────────────────────────────────────────────────────────────────────────

# In --json mode, all banner chatter goes to stderr so stdout is parseable JSON.
banner() { if [ "$JSON" = "1" ]; then echo "$@" >&2; else echo "$@"; fi; }

banner "sutando-migrate: mode=$MODE  dest=$DEST_REAL"
[ "${RESPECT_ENV:-0}" = "0" ] && [ -n "${SUTANDO_WORKSPACE:-}" ] && \
    banner "sutando-migrate: NOTE — \$SUTANDO_WORKSPACE=$SUTANDO_WORKSPACE in env; ignored for dest computation (use --respect-env to honor)"
banner ""

# Discover sources
A_REAL_OK=""; B_REAL_OK=""; C_REAL_OK=""
A_REAL_OK="$(validate_source A "$A_PATH" || true)"
B_REAL_OK="$(validate_source B "$B_PATH" || true)"
C_REAL_OK="$(validate_source C "$C_PATH" || true)"

banner "sources detected:"
[ -n "$A_REAL_OK" ] && banner "  A (repo root):                 $A_REAL_OK"
[ -n "$B_REAL_OK" ] && banner "  B (~/.sutando/workspace/):     $B_REAL_OK"
[ -n "$C_REAL_OK" ] && banner "  C (SUTANDO_WORKSPACE env):     $C_REAL_OK"
[ -z "$A_REAL_OK$B_REAL_OK$C_REAL_OK" ] && {
    # Rollback and verify don't need sources — they operate on dest alone.
    case "$MODE" in
        rollback|verify) ;;
        *)
            if [ "$JSON" = "1" ]; then
                echo '{"dest":"'"$DEST_REAL"'","sources":{},"totals":{"unique_relpaths":0,"collisions":0,"identical_content":0,"genuine_conflicts":0,"by_class":{}},"notable_collisions":[]}'
            else
                echo "  (none — nothing to migrate; dest=$DEST_REAL is the only locus)"
            fi
            exit 0
            ;;
    esac
}
banner ""

# Optional source filter
include_src() {
    local tag="$1"
    [ -z "$SOURCE_FILTER" ] && return 0
    [[ ",$SOURCE_FILTER," == *",$tag,"* ]]
}

index_dest_for_collisions() {
    # Walk dest's workspace surface, recording any existing files as tag DEST
    # so dest-collisions surface uniformly with cross-source collisions in the
    # post-processing report. Existing dest files do not get migrated; they're
    # the prior state the report is helping owner reason about.
    local sd sf
    local -a dest_walk=()
    for sd in "${WORKSPACE_SURFACE_DIRS[@]}"; do
        [ -d "$DEST_REAL/$sd" ] && dest_walk+=("$DEST_REAL/$sd")
    done
    for sf in "${WORKSPACE_SURFACE_FILES[@]}"; do
        [ -f "$DEST_REAL/$sf" ] && dest_walk+=("$DEST_REAL/$sf")
    done
    [ ${#dest_walk[@]} -eq 0 ] && return
    while IFS= read -r -d '' file; do
        local rel="${file#"$DEST_REAL"/}"
        case "$rel" in
            .git/*|*/.git/*) continue ;;
            state/migration-backup-*.tar*) continue ;;
            .DS_Store|*/.DS_Store|.gitkeep|*/.gitkeep) continue ;;
        esac
        local cls
        cls="$(classify "$rel")"
        case "$cls" in
            structural|append|newest-mtime|collision-keep-both|rehome-state)
                record_xsrc "DEST" "$rel" "existing" "$file"
                ;;
        esac
    done < <(find "${dest_walk[@]}" -type f -print0 2>/dev/null)
}

report_cross_source() {
    # Post-process XSRC_INDEX (TSV: relpath\ttag\tclass\tmtime\tsize) and print
    # any relpath that appears in 2+ rows (cross-source / dest collision).
    # Emits a compact per-class summary + a per-class detail list (capped).
    local total_xs total_xs_files
    total_xs_files="$(awk -F'\t' '{print $1}' "$XSRC_INDEX" | sort -u | wc -l | tr -d ' ')"
    total_xs="$(awk -F'\t' '
        {n[$1]++}
        END {c=0; for (k in n) if (n[k]>1) c++; print c}
    ' "$XSRC_INDEX")"

    REPORT_LINES+=("")
    REPORT_LINES+=("--- Cross-source collision report ---")
    REPORT_LINES+=("  total unique relpaths across sources+dest: $total_xs_files")
    REPORT_LINES+=("  relpaths present in >1 location:           $total_xs")

    if [ "$total_xs" -eq 0 ]; then
        REPORT_LINES+=("  (no cross-source / dest collisions — commit is safe per-source)")
        return
    fi

    # Identical-content collisions: cross-source rows where ALL entries share
    # the same mtime AND size for the relpath. High-confidence "same file
    # mirrored through sync" — commit can pick one source as canonical and
    # skip the rest (no real conflict). Common case: memory-sync mirroring
    # notes/ between B and C.
    local total_identical
    # Concatenated-key idiom (BSD awk has no array-of-array); detect identical
    # across all entries of a relpath by counting distinct (mtime,size) pairs.
    total_identical="$(awk -F'\t' '
        {
            n[$1]++
            key = $1 SUBSEP $4 "|" $5
            if (!(key in seen)) { seen[key]=1; pairs[$1]++ }
        }
        END {
            ident=0
            for (k in n) if (n[k]>1 && pairs[k]==1) ident++
            print ident
        }
    ' "$XSRC_INDEX" 2>/dev/null || echo 0)"

    REPORT_LINES+=("  of which identical-content (same mtime + size):  $total_identical (commit will pick one canonical + skip rest)")
    REPORT_LINES+=("  genuine cross-source conflicts (need strategy):  $((total_xs - total_identical))")

    # Per-class breakdown of cross-source collisions
    REPORT_LINES+=("  by class:")
    while read -r cnt c; do
        REPORT_LINES+=("    $cnt × $c")
    done < <(awk -F'\t' '
        {n[$1]++; cls[$1]=$3}
        END {for (k in n) if (n[k]>1) print cls[k]}
    ' "$XSRC_INDEX" | sort | uniq -c | sort -rn | head -10)

    # Top notable collisions (append-class first, then size)
    REPORT_LINES+=("  notable collisions (cap 20; full list with --json):")
    while IFS='|' read -r c rel locs; do
        REPORT_LINES+=("    [$c] $rel  → $locs")
    done < <(awk -F'\t' '
        {locs[$1]=locs[$1] "," $2 "(mt=" $4 ",sz=" $5 ")"; cls[$1]=$3; n[$1]++}
        END {for (k in n) if (n[k]>1) printf "%s|%s|%s\n", cls[k], k, substr(locs[k],2)}
    ' "$XSRC_INDEX" | sort -t'|' -k1,1 | head -20)
}

# ──────────────────────────────────────────────────────────────────────────────
# Commit / verify / rollback
# ──────────────────────────────────────────────────────────────────────────────

# Stable backup id for this commit invocation (also serves as sentinel suffix)
BACKUP_ID=""

# Default media exclusions for the pre-migration backup. Addresses Lucy's
# Maddy v0.8 report (2026-06-06 Bug #3): a 34GB workspace with 32GB of
# `notes/asset-library` mp4 video caused `tar -czf` to grind for 30+ min
# trying to compress already-compressed media. Two-pronged fix:
#
# 1. Default-exclude common media extensions + the `notes/asset-library/`
#    convention. Configurable via `SUTANDO_MIGRATE_BACKUP_EXCLUDE` (extra
#    space-separated patterns, layered ON TOP of these defaults).
#
# 2. Skip gzip (`tar -cf` instead of `-czf`) when total backup payload
#    exceeds `SUTANDO_MIGRATE_BACKUP_GZIP_THRESHOLD_MB` (default 5000 =
#    5GB). gzip on incompressible media is pure CPU waste; uncompressed
#    tar is ~100x faster on a 30GB+ media-heavy workspace.
#
# Users can opt out of all of this via SUTANDO_MIGRATE_NO_EXCLUDE=1
# (matches the existing `--no-confirm`/`--no-claude-import` opt-out style).
_BACKUP_DEFAULT_EXCLUDES=(
    "notes/asset-library"
    "*.mp4" "*.mov" "*.mkv" "*.avi" "*.webm"
    "*.zip" "*.tar" "*.tar.gz" "*.tgz" "*.gz"
    "*.iso" "*.dmg"
    # node_modules + .git: cheap defense for nested dirs under surface paths.
    # Maddy had ~497MB node_modules under notes/asset-library (remotion deps).
    # If a surface dir like `notes/some-project/` ever contains either, tar
    # would catch it. 100% regenerable (npm install) / preserved in source repo.
    "node_modules"
    ".git"
)

backup_dest() {
    # Per-second ts collision possible when two commits happen in same second
    # (test idempotency re-run + initial commit are the typical case). Append
    # PID + random so each backup_dest call gets a unique BACKUP_ID. Without
    # this, the second commit's tar would overwrite the first AND include it.
    BACKUP_ID="$(date -u +%Y%m%dT%H%M%SZ)-p$$r$RANDOM"
    local _ext="tar.gz"
    local _tar_compress_flag="z"

    # Per-Bug #3 pre-flight: estimate total surface bytes. If above the
    # gzip threshold, drop gzip — compressing incompressible media is
    # pure CPU waste (Maddy: tar -czf on 32GB mp4 froze 30+ min).
    local _gzip_threshold_mb="${SUTANDO_MIGRATE_BACKUP_GZIP_THRESHOLD_MB:-5000}"
    local -a surface=()
    local sd sf
    for sd in "${WORKSPACE_SURFACE_DIRS[@]}"; do
        [ -e "$DEST_REAL/$sd" ] && surface+=("$sd")
    done
    for sf in "${WORKSPACE_SURFACE_FILES[@]}"; do
        [ -e "$DEST_REAL/$sf" ] && surface+=("$sf")
    done

    # Quick surface size estimate via du. macOS BSD du differs from GNU,
    # but `du -sk` works on both (KB blocks). Sum the surface entries.
    local _surface_kb=0
    if [ ${#surface[@]} -gt 0 ]; then
        local _entry _kb
        for _entry in "${surface[@]}"; do
            _kb="$(du -sk "$DEST_REAL/$_entry" 2>/dev/null | awk '{print $1+0}')"
            _surface_kb=$((_surface_kb + _kb))
        done
    fi
    local _surface_mb=$((_surface_kb / 1024))

    # Decide compression based on size + the threshold.
    if [ "$_surface_mb" -gt "$_gzip_threshold_mb" ]; then
        _ext="tar"
        _tar_compress_flag=""
        echo "sutando-migrate: backup payload ~${_surface_mb}MB > ${_gzip_threshold_mb}MB threshold; skipping gzip (uncompressed tar — pre-migration backup of media-heavy workspace)" >&2
    fi

    local backup_path="$DEST_REAL/state/migration-backup-$BACKUP_ID.$_ext"
    mkdir -p "$DEST_REAL/state"

    # Build the exclude args. Layer:
    #   1. Default media excludes (above) — applied unless SUTANDO_MIGRATE_NO_EXCLUDE=1
    #   2. User-provided extra patterns via SUTANDO_MIGRATE_BACKUP_EXCLUDE
    local -a _tar_excludes=()
    if [ "${SUTANDO_MIGRATE_NO_EXCLUDE:-0}" != "1" ]; then
        local _excl
        for _excl in "${_BACKUP_DEFAULT_EXCLUDES[@]}"; do
            _tar_excludes+=(--exclude="$_excl")
        done
    fi
    if [ -n "${SUTANDO_MIGRATE_BACKUP_EXCLUDE:-}" ]; then
        for _excl in ${SUTANDO_MIGRATE_BACKUP_EXCLUDE}; do
            _tar_excludes+=(--exclude="$_excl")
        done
    fi

    if [ ${#surface[@]} -gt 0 ]; then
        [ "${SUTANDO_MIGRATE_DEBUG:-0}" = "1" ] && echo "[debug] backup_dest surface: ${surface[*]} (excludes: ${_tar_excludes[*]})" >&2
        # Tar to a system temp location (OUTSIDE dest/state/) to avoid the
        # self-reference where tarring state/ also includes the partial
        # backup tarball being written. Move atomically into state/ at end.
        # Mini's intermittent test-failure trail led to this — exclude didn't
        # work reliably across BSD/GNU tar.
        local tmp_backup
        tmp_backup="$(mktemp -t sutando-mig-backup.XXXXXX).$_ext"
        ( cd "$DEST_REAL" && tar "-c${_tar_compress_flag}f" "$tmp_backup" "${_tar_excludes[@]+"${_tar_excludes[@]}"}" "${surface[@]}" 2>/dev/null )
        # Now delete the migration-backup-*.tar.* entries from the tar so
        # restoring doesn't repopulate them. Easier: just mv to final spot.
        mv "$tmp_backup" "$backup_path"
        echo "sutando-migrate: backup → $backup_path"
    else
        # Empty dest — write an empty marker so rollback still has a known id
        : > "$backup_path"
        echo "sutando-migrate: dest empty; placeholder backup → $backup_path"
    fi
}

# Per-source migration sentinel. Idempotency token: if present + not --force, skip source.
source_sentinel() {
    echo "$DEST_REAL/state/.migrated-from-$1-$BACKUP_ID"
}
any_source_sentinel() {
    # Any sentinel for this source tag (commit may have been run before with a
    # different backup id). Returns 0 if found.
    ls "$DEST_REAL/state/.migrated-from-$1-"* >/dev/null 2>&1
}

# Atomic per-file copy preserving mtime. Returns 0 on success.
copy_preserving_mtime() {
    local src="$1" dst="$2"
    mkdir -p "$(dirname "$dst")"
    # Atomic: cp -p to sibling tmp then mv. -p preserves mtime + mode.
    local tmp="$dst.tmp.$$"
    cp -p "$src" "$tmp" && mv -f "$tmp" "$dst"
}

# Human-readable byte size: 1234 → "1.2 KB", 5242880 → "5.0 MB", etc.
humanize_bytes() {
    local b="$1"
    [ -z "$b" ] && { echo "0 B"; return; }
    awk -v b="$b" 'BEGIN{
        split("B KB MB GB TB", u);
        for (i=1; i<=5 && b>=1024; i++) b/=1024;
        if (i==1) printf "%d %s", b, u[i]; else printf "%.1f %s", b, u[i];
    }'
}

# Format seconds as "Nm Ms" or "Nh Mm" for human-readable durations.
format_duration() {
    local s="$1"
    [ -z "$s" ] || [ "$s" -lt 1 ] && { echo "<1s"; return; }
    if [ "$s" -lt 60 ]; then printf "%ds" "$s"
    elif [ "$s" -lt 3600 ]; then printf "%dm %ds" $((s/60)) $((s%60))
    else printf "%dh %dm" $((s/3600)) $(((s%3600)/60))
    fi
}

# Pre-flight scan: count files + bytes across enabled sources, print summary,
# emit estimated copy time, and (unless --no-confirm or non-TTY) prompt the
# operator to confirm before any destructive write happens.
#
# Owner ask 2026-06-05: large-workspace migrations felt opaque — the user
# waited a long time with no signal whether things were progressing. The
# fix is two-pronged: (1) tell them upfront how big the job is, and (2)
# emit per-file progress lines during the copy itself (see commit_source).
#
# Populates PROGRESS_TOTAL (set in commit_main) by side effect via echo to
# stdout — the caller captures the count via $(...).
preflight_summary() {
    local _total_files=0 _total_bytes=0
    local _per_source_lines=""
    local tag src files bytes
    for tag in A B C; do
        case "$tag" in
            A) src="$A_REAL_OK" ;;
            B) src="$B_REAL_OK" ;;
            C) src="$C_REAL_OK" ;;
        esac
        [ -z "$src" ] && continue
        include_src "$tag" || continue
        # Walk the same surface as commit_source — but conservatively (just
        # count everything; quarantine + skip rules apply later). Overcount
        # is acceptable; this is an estimate, not an audit.
        files="$(find "$src" -type f 2>/dev/null | wc -l | tr -d ' ')"
        case "$(uname -s)" in
            Darwin)
                bytes="$(find "$src" -type f -print0 2>/dev/null | xargs -0 stat -f '%z' 2>/dev/null \
                         | awk '{s+=$1} END {print s+0}')"
                ;;
            Linux|*)
                bytes="$(find "$src" -type f -printf '%s\n' 2>/dev/null | awk '{s+=$1} END {print s+0}')"
                ;;
        esac
        _total_files=$((_total_files + files))
        _total_bytes=$((_total_bytes + bytes))
        _per_source_lines+="  $tag ($src): $files files, $(humanize_bytes "$bytes")"$'\n'
    done

    # Rough ETA: 200 MB/s sustained APFS local. Conservative; SSD-bound media
    # workloads land here. Spinning disks would be slower; cloud/network
    # mounts unpredictable — caller should sanity-check the estimate.
    local _bytes_per_sec=$((200 * 1024 * 1024))
    local _eta_sec=$((_total_bytes / _bytes_per_sec))
    [ "$_eta_sec" -lt 1 ] && _eta_sec=1

    {
        echo "sutando-migrate: pre-flight scan"
        printf "%s" "$_per_source_lines"
        echo "  TOTAL: $_total_files files, $(humanize_bytes "$_total_bytes")"
        echo "  Estimated copy time: ~$(format_duration "$_eta_sec") at ~200 MB/s sustained (rough)"
        echo
    } >&2

    # Confirm prompt — skipped on --no-confirm or when stdin is not a TTY
    # (non-interactive runs: CI, cron, scripted batch).
    if [ "$NO_CONFIRM" = "0" ] && [ -t 0 ]; then
        printf "  Proceed with copy? [y/N]: " >&2
        local _answer
        read -r _answer
        case "$_answer" in
            y|Y|yes|YES) ;;
            *)
                echo "  Aborted by operator." >&2
                exit 3
                ;;
        esac
        echo >&2
    elif [ "$NO_CONFIRM" = "0" ] && [ ! -t 0 ]; then
        echo "  (non-interactive stdin; skipping confirm — pass --no-confirm to suppress this message)" >&2
        echo >&2
    fi

    # Output total file count for the caller to capture (PROGRESS_TOTAL).
    echo "$_total_files"
}

# SHA-256 verify (macOS shasum / Linux sha256sum). Returns 0 if hashes match.
sha_match() {
    local a="$1" b="$2"
    local ha hb
    if command -v shasum >/dev/null 2>&1; then
        ha="$(shasum -a 256 "$a" 2>/dev/null | awk '{print $1}')"
        hb="$(shasum -a 256 "$b" 2>/dev/null | awk '{print $1}')"
    else
        ha="$(sha256sum "$a" 2>/dev/null | awk '{print $1}')"
        hb="$(sha256sum "$b" 2>/dev/null | awk '{print $1}')"
    fi
    [ -n "$ha" ] && [ "$ha" = "$hb" ]
}

# Per-file commit dispatch. $1=src-file abs, $2=src-relpath, $3=src-tag, $4=class
# Returns count category via stdout: "copied|kept-dest|skipped|sidecar|rehomed"
commit_one() {
    local src_file="$1" rel="$2" tag="$3" cls="$4"
    local dst_path

    case "$cls" in
        structural|collision-keep-both)
            dst_path="$DEST_REAL/$rel"
            if [ -e "$dst_path" ]; then
                # Same content (mtime+size) → identical-drop. Different →
                # keep-both: rename source-incoming to <file>.legacy-<tag>.
                local src_mt src_sz dst_mt dst_sz
                src_mt="$(stat -f %m "$src_file" 2>/dev/null || stat -c %Y "$src_file")"
                src_sz="$(stat -f %z "$src_file" 2>/dev/null || stat -c %s "$src_file")"
                dst_mt="$(stat -f %m "$dst_path" 2>/dev/null || stat -c %Y "$dst_path")"
                dst_sz="$(stat -f %z "$dst_path" 2>/dev/null || stat -c %s "$dst_path")"
                if [ "$src_mt" = "$dst_mt" ] && [ "$src_sz" = "$dst_sz" ]; then
                    echo "identical-drop"
                    return 0
                fi
                # Genuine collision: write loser as sidecar, keep dest as primary
                # only if dest is newer. If src is newer, swap.
                # Per Mini #design 2026-06-02 blocker #3 + flaky-test re-fix:
                # uniqueness needs both per-second timestamp AND a serial counter
                # (`date +%s` resolution is too coarse when commit_one fires
                # multiple times within same second across sources).
                # SIDECAR_SERIAL counter would live in subshell because
                # commit_one's echo is captured via $(); use $RANDOM (per-call
                # entropy from /dev/urandom-seeded bash PRNG) + $$ (pid) instead.
                local ts_suffix
                ts_suffix="$(date -u +%Y%m%dT%H%M%SZ)-p$$r$RANDOM"
                if [ "$src_mt" -gt "$dst_mt" ]; then
                    # dest's content (whatever was there) goes to a sidecar.
                    # Name it .legacy-prior-<src_tag>-<ts> to convey "this is
                    # what was at dest before <src_tag> overwrote it" + the
                    # timestamp ensures 3-way collisions don't clobber.
                    copy_preserving_mtime "$dst_path" "$dst_path.legacy-prior-from-$tag-$ts_suffix"
                    copy_preserving_mtime "$src_file" "$dst_path"
                    echo "src-wins-newer"
                else
                    # src loses; preserve under tagged + timestamped sidecar.
                    copy_preserving_mtime "$src_file" "$dst_path.legacy-$tag-$ts_suffix"
                    echo "dest-wins-newer"
                fi
                return 0
            else
                copy_preserving_mtime "$src_file" "$dst_path"
                echo "copied"
                return 0
            fi
            ;;
        newest-mtime)
            dst_path="$DEST_REAL/$rel"
            if [ -e "$dst_path" ]; then
                local src_mt dst_mt
                src_mt="$(stat -f %m "$src_file" 2>/dev/null || stat -c %Y "$src_file")"
                dst_mt="$(stat -f %m "$dst_path" 2>/dev/null || stat -c %Y "$dst_path")"
                if [ "$src_mt" -gt "$dst_mt" ]; then
                    copy_preserving_mtime "$src_file" "$dst_path"
                    echo "src-newer"
                else
                    echo "dest-newer"
                fi
            else
                copy_preserving_mtime "$src_file" "$dst_path"
                echo "copied"
            fi
            return 0
            ;;
        rehome-state|rehome-dated-snapshot)
            # Per Mini's #design 2026-06-02 04:54Z (state JSONs) + earlier
            # workspace audit (dated snapshots):
            #   rehome-state          → state/<base> OR state/auth/<base>
            #                           (cloud-auth/device → auth/; per-host durable;
            #                           CLAUDE.md should declare state/auth/ excluded
            #                           from transient-state cleanup)
            #   rehome-dated-snapshot → notes/archive/<base>
            #                           (pending-questions-resolved-archive-*.md etc)
            # Both are SNAPSHOT classes — newest-mtime is the right strategy
            # (newest = most accurate; older is stale-by-definition). Narrative
            # logs are a separate class (rehome-narrative-log) since they are
            # APPEND-ONLY ACCUMULATORS where newest-mtime is data-lossy.
            local base="$(basename "$rel")"
            case "$cls" in
                rehome-state)
                    case "$base" in
                        cloud-auth.json|device.json) dst_path="$DEST_REAL/state/auth/$base" ;;
                        *) dst_path="$DEST_REAL/state/$base" ;;
                    esac
                    ;;
                rehome-dated-snapshot)
                    dst_path="$DEST_REAL/notes/archive/$base"
                    ;;
            esac
            # Per-mtime swap, identical-drop, or write-fresh.
            if [ -e "$dst_path" ]; then
                local src_mt dst_mt
                src_mt="$(stat -f %m "$src_file" 2>/dev/null || stat -c %Y "$src_file")"
                dst_mt="$(stat -f %m "$dst_path" 2>/dev/null || stat -c %Y "$dst_path")"
                if [ "$src_mt" -gt "$dst_mt" ]; then
                    copy_preserving_mtime "$src_file" "$dst_path"
                    echo "rehomed-newer"
                else
                    echo "rehomed-skip-older"
                fi
            else
                copy_preserving_mtime "$src_file" "$dst_path"
                echo "rehomed"
            fi
            return 0
            ;;
        rehome-narrative-log)
            # APPEND-ONLY ACCUMULATOR (per-host voice-agent transcript history).
            # Pre-fix (2026-06-02) this shared the rehome-state/dated-snapshot
            # newest-mtime path, which silently dropped the larger file when
            # the newer one was smaller. Data-loss bug caught on owner's MBP:
            # Source A had 261K of voice-transcript history (mtime May 17);
            # Source C had a 2K phone-call snippet (mtime May 20, newer);
            # newest-mtime kept C's 2K and discarded A's 261K. Same failure
            # class as `state/*.json|newest-mtime` we previously carved out for
            # per-host state (#design 2026-06-02). General rule baked in here:
            # snapshot → newest-mtime; append-only/accumulator → append.
            # Strategy: concat each source's content to the canonical dest
            # with a divider header carrying src-tag + mtime + size so the
            # downstream reader can split if needed.
            dst_path="$DEST_REAL/logs/workspace-narrative.log"
            mkdir -p "$(dirname "$dst_path")"
            local src_mt src_sz
            src_mt="$(stat -f %m "$src_file" 2>/dev/null || stat -c %Y "$src_file")"
            src_sz="$(stat -f %z "$src_file" 2>/dev/null || stat -c %s "$src_file")"
            # Mini #design 2026-06-02 08:10Z: an `{ ... } > tmp && mv ...`
            # compound on a single line is NOT covered by `set -e` for its
            # left-side failure — the compound returns non-zero but execution
            # falls through to the next statement (which echo'd "appended").
            # If commit_one()'s caller treats that echo as success and proceeds
            # to delete the source, a half-written append becomes data-lossy.
            # Fix: split into separate commands so each failure path either
            # returns explicitly or trips the script's `set -e`. Atomicity is
            # still preserved (tmp file is dropped on failure; dest is untouched
            # until mv succeeds).
            local _tmp="$dst_path.append.$$"
            local _redirect_rc=0
            if [ -e "$dst_path" ]; then
                # Idempotency guard (Lucy #1407, folded into #1406 2026-06-02):
                # if THIS source's divider marker is already present in the
                # canonical narrative log, skip re-appending. Cheap single-line
                # grep -qF check via the source-tag-specific header substring.
                # Without this, re-running `commit` against the same surviving
                # source (e.g. accidentally, or after an aborted --delete-source)
                # would re-append the same source content again, growing
                # workspace-narrative.log linearly with each commit. The
                # divider header includes mtime + size but the tag substring
                # alone is sufficient — only one append per source-tag should
                # ever land in the canonical log.
                if grep -qF "migrated from source $tag " "$dst_path" 2>/dev/null; then
                    echo "append-skip-idempotent"
                    return 0
                fi
                # Append with divider header. Concat in canonical order.
                # Per-command RC capture (same template as --merge-append block
                # below). Brace-overall exit only catches the last command's
                # failure; per-command bitmask catches earlier-step failures
                # (e.g. cat-dst disappearing mid-merge).
                # Bitmask: 1=cat-dst  2=hdr-blank  4=hdr-line  8=trailer-blank  16=cat-src
                local _aerr=0
                {
                    cat "$dst_path"                                                          || _aerr=$((_aerr|1))
                    echo ""                                                                  || _aerr=$((_aerr|2))
                    echo "=== migrated from source $tag (mtime $src_mt, size ${src_sz}B) ===" || _aerr=$((_aerr|4))
                    echo ""                                                                  || _aerr=$((_aerr|8))
                    cat "$src_file"                                                          || _aerr=$((_aerr|16))
                } > "$_tmp"
                if [ "$_aerr" -ne 0 ]; then
                    rm -f "$_tmp"
                    echo "append-failed-inner $_aerr" >&2
                    return 1
                fi
                mv -f "$_tmp" "$dst_path" || {
                    rm -f "$_tmp"
                    echo "append-failed-mv" >&2
                    return 1
                }
                echo "appended"
            else
                # First write — include header so future appends slot in cleanly.
                # Per-command RC capture; bitmask 4=hdr-line  8=trailer-blank  16=cat-src
                # (no cat-dst since dst doesn't exist yet).
                local _afferr=0
                {
                    echo "=== migrated from source $tag (mtime $src_mt, size ${src_sz}B) ===" || _afferr=$((_afferr|4))
                    echo ""                                                                  || _afferr=$((_afferr|8))
                    cat "$src_file"                                                          || _afferr=$((_afferr|16))
                } > "$_tmp"
                if [ "$_afferr" -ne 0 ]; then
                    rm -f "$_tmp"
                    echo "append-fresh-failed-inner $_afferr" >&2
                    return 1
                fi
                mv -f "$_tmp" "$dst_path" || {
                    rm -f "$_tmp"
                    echo "append-fresh-failed-mv" >&2
                    return 1
                }
                echo "appended-fresh"
            fi
            return 0
            ;;
        append)
            # Default Strategy C: sidecar at <dest>/legacy/<tag>/<rel>.
            # --merge-append (Strategy B): concat with divider to <dest>/<rel>.
            if [ "$MERGE_APPEND" = "1" ]; then
                dst_path="$DEST_REAL/$rel"
                mkdir -p "$(dirname "$dst_path")"
                if [ -e "$dst_path" ]; then
                    # IMPORTANT: split the compound-redirect + mv + echo "merged"
                    # into separate statements with explicit error returns.
                    # Per `feedback_bash_and_compound_breaks_set_e` + Mini's
                    # PR #1424 review #1: capture RC PER-COMMAND inside the
                    # brace group, not just the brace's overall exit. A brace
                    # group's exit code = the last command's exit; an early
                    # `cat "$dst_path"` failure (e.g. file disappeared between
                    # check + read, permission flip) gets silently swallowed
                    # if the later `cat "$src_file"` returns 0. That would
                    # commit a PARTIAL merge (dst_path content missing) +
                    # print "merged" — invisible data loss.
                    #
                    # Bitmask captures which step(s) failed for stderr triage:
                    #   1=cat-dst  2=hdr-blank  4=hdr-line  8=trailer-blank  16=cat-src
                    local _err=0
                    {
                        cat "$dst_path"                                || _err=$((_err|1))
                        echo ""                                        || _err=$((_err|2))
                        echo "=== migrated from source $tag at $BACKUP_ID ===" || _err=$((_err|4))
                        echo ""                                        || _err=$((_err|8))
                        cat "$src_file"                                || _err=$((_err|16))
                    } > "$dst_path.merge.$$"
                    if [ "$_err" -ne 0 ]; then
                        rm -f "$dst_path.merge.$$"
                        echo "merge-append-failed-inner $_err" >&2
                        return 1
                    fi
                    mv -f "$dst_path.merge.$$" "$dst_path" || {
                        rm -f "$dst_path.merge.$$"
                        echo "merge-append-failed-mv" >&2
                        return 1
                    }
                    echo "merged"
                else
                    copy_preserving_mtime "$src_file" "$dst_path"
                    echo "copied"
                fi
            else
                dst_path="$DEST_REAL/legacy/$tag/$rel"
                copy_preserving_mtime "$src_file" "$dst_path"
                echo "sidecar"
            fi
            return 0
            ;;
        inflight-guard)
            # Old in-flight artifacts (tasks/task-*.txt, results/task-*.txt)
            # NEVER copy to dest's live queue (would re-fire the watcher and
            # double-process old work). Route to tasks/archive/<src-tag>/ or
            # results/archive/<src-tag>/ instead. Bug discovered when a stale
            # May 22 task migrated from B fired the watcher post-test.
            if age_safe "$src_file"; then
                local subdir="${rel%%/*}"  # tasks or results
                local file_base="${rel#*/}" # task-*.txt
                dst_path="$DEST_REAL/$subdir/archive/$tag/$file_base"
                copy_preserving_mtime "$src_file" "$dst_path"
                echo "archived-stale"
            else
                echo "skipped-inflight"
            fi
            return 0
            ;;
        quarantine-unknown)
            # Per Lucy #design 2026-06-02 + owner direction: workspace sources
            # may have user-custom content outside the canonical surface (e.g.
            # experiments/, obsidian-vault/, personal-src/). Preserve under a
            # namespaced quarantine path rather than skip-unknown'ing it.
            dst_path="$DEST_REAL/legacy/$tag/quarantine/$rel"
            copy_preserving_mtime "$src_file" "$dst_path"
            echo "quarantined"
            return 0
            ;;
        skip-ephemeral|skip-unknown|unknown)
            echo "skipped-class"
            return 0
            ;;
    esac
    echo "skipped-fallthrough"
}

commit_source() {
    local tag="$1" src="$2"
    local SENTINEL_PRESENT=0
    any_source_sentinel "$tag" && SENTINEL_PRESENT=1

    # Mini #design 2026-06-02 new blocker #2: --delete-source must NOT re-run
    # the commit walk when a sentinel exists (would duplicate appends + extra
    # sidecars). Two-phase pattern: phase 1 does commit walk + writes sentinel;
    # phase 2 (--delete-source --backup-id) skips commit walk + only does the
    # delete walk against the existing dest landing.
    local DO_COMMIT_WALK=1
    if [ "$SENTINEL_PRESENT" = "1" ] && [ "$FORCE" = "0" ]; then
        if [ "$DELETE_SOURCE" = "1" ]; then
            DO_COMMIT_WALK=0  # phase 2 mode: skip commit, just delete
            echo "--- Phase 2: --delete-source against source $tag (sentinel present; skip commit walk) ---"
        else
            echo "sutando-migrate: source $tag has prior migration sentinel — skip (use --force)"
            return 0
        fi
    fi

    echo
    [ "$DO_COMMIT_WALK" = "1" ] && echo "--- Committing source $tag ($src) ---"

    # Reuse the same walk-list logic as scan_source (including B+C quarantine).
    local -a walk_paths=()
    local sd sf _ex_a
    # Per feedback_per_source_surface_lists 2026-06-02: detect whether THIS
    # source path IS a sutando repo checkout (by content, not by tag) — if
    # so, skip dirs that exist as repo code rather than workspace data
    # (docs/, agents/, etc.). Detection: presence of `src/sutando_config.py`
    # (or the same-shape sutando-config.sh) at source root. Owner clarified
    # 2026-06-02 07:29: "We should only EXCLUDE them when they are in the
    # sutando repo root." A custom workspace path that happens to live
    # inside a sutando checkout should ALSO get the exclude.
    local IS_SUTANDO_REPO=0
    if [ -f "$src/src/sutando_config.py" ] || [ -f "$src/scripts/sutando-config.sh" ]; then
        IS_SUTANDO_REPO=1
    fi
    for sd in "${WORKSPACE_SURFACE_DIRS[@]}"; do
        if [ "$IS_SUTANDO_REPO" = "1" ]; then
            local _skip_a=0
            for _ex_a in "${SOURCE_A_EXCLUDE[@]}"; do
                [ "$sd" = "$_ex_a" ] && _skip_a=1 && break
            done
            [ "$_skip_a" = "1" ] && continue
        fi
        [ -d "$src/$sd" ] && walk_paths+=("$src/$sd")
    done
    for sf in "${WORKSPACE_SURFACE_FILES[@]}"; do
        [ -f "$src/$sf" ] && walk_paths+=("$src/$sf")
    done
    case "$tag" in
        B|C)
            local entry name skip ex already
            for entry in "$src"/* "$src"/.[!.]*; do
                [ -e "$entry" ] || continue
                name="$(basename "$entry")"
                already=0
                for sd in "${WORKSPACE_SURFACE_DIRS[@]}"; do [ "$name" = "$sd" ] && already=1 && break; done
                [ "$already" = "1" ] && continue
                for sf in "${WORKSPACE_SURFACE_FILES[@]}"; do [ "$name" = "$sf" ] && already=1 && break; done
                [ "$already" = "1" ] && continue
                skip=0
                for ex in "${QUARANTINE_EXCLUDES[@]}"; do [ "$name" = "$ex" ] && skip=1 && break; done
                [ "$skip" = "1" ] && continue
                case "$name" in
                    .migrated-from-*|.legacy-migrated-*|.notes-migrated|.build_log-migrated|.conversation-log-migrated|.status-migrated|.legacy-notice-printed|.last-pq-notify|.env|.gitignore)
                        continue ;;
                esac
                walk_paths+=("$entry")
            done
            ;;
    esac
    [ ${#walk_paths[@]} -eq 0 ] && { echo "  (nothing on surface; skip)"; return 0; }

    local n_copied=0 n_kept=0 n_skipped=0 n_sidecar=0 n_rehomed=0 n_identical=0 n_quarantined=0 n_other=0
    # Phase 2 mode skips the commit walk entirely.
    if [ "$DO_COMMIT_WALK" = "0" ]; then
        # Skip ahead to the delete block (it walks walk_paths independently).
        :
    else
    local file rel cls outcome
    while IFS= read -r -d '' file; do
        rel="${file#"$src"/}"
        case "$rel" in
            .git/*) continue ;;
            state/migration-backup-*.tar*) continue ;;
            .DS_Store|*/.DS_Store|.gitkeep|*/.gitkeep) n_skipped=$((n_skipped+1)); continue ;;
        esac
        # Mini #design 2026-06-02 new-blocker #4: recursive quarantine excludes.
        # A custom dir like experiments/node_modules/ should be skipped even
        # though experiments/ matched the quarantine catchall at top level.
        # Walk rel segments and skip if ANY matches QUARANTINE_EXCLUDES.
        local _skip_recursive=0
        local _seg
        IFS='/' read -ra _segs <<<"$rel"
        for _seg in "${_segs[@]}"; do
            for _ex in "${QUARANTINE_EXCLUDES[@]}"; do
                [ "$_seg" = "$_ex" ] && { _skip_recursive=1; break 2; }
            done
        done
        if [ "$_skip_recursive" = "1" ]; then
            n_skipped=$((n_skipped+1)); continue
        fi
        cls="$(classify "$rel")"
        outcome="$(commit_one "$file" "$rel" "$tag" "$cls")"
        # Per-file progress on stderr — visible feedback during the copy walk
        # so long migrations don't feel like a hang. Skipped when PROGRESS_TOTAL
        # is 0 (delete-source phase-2 path; pre-flight didn't run).
        if [ "$PROGRESS_TOTAL" -gt 0 ]; then
            PROGRESS_N=$((PROGRESS_N + 1))
            _fsize="$(stat -f %z "$file" 2>/dev/null || stat -c %s "$file" 2>/dev/null || echo 0)"
            printf "  [%d/%d] %s (%s) → %s\n" \
                "$PROGRESS_N" "$PROGRESS_TOTAL" "${rel:0:60}" \
                "$(humanize_bytes "$_fsize")" "$outcome" >&2
            # Every 20 files: aggregate progress + ETA refinement
            if [ $((PROGRESS_N % 20)) -eq 0 ] && [ "$PROGRESS_N" -lt "$PROGRESS_TOTAL" ]; then
                _pct=$((PROGRESS_N * 100 / PROGRESS_TOTAL))
                printf "  ─── progress: %d/%d (%d%%) ───\n" \
                    "$PROGRESS_N" "$PROGRESS_TOTAL" "$_pct" >&2
            fi
        fi
        case "$outcome" in
            copied|src-wins-newer|src-newer|rehomed|rehomed-newer|merged|archived-stale|copied-stale)
                n_copied=$((n_copied+1)) ;;
            dest-wins-newer|dest-newer|rehomed-skip-older|skipped-collision-dest)
                n_kept=$((n_kept+1)) ;;
            identical-drop)
                n_identical=$((n_identical+1)) ;;
            sidecar)
                n_sidecar=$((n_sidecar+1)) ;;
            quarantined)
                n_quarantined=$((n_quarantined+1)) ;;
            skipped-class|skipped-inflight|skipped-fallthrough)
                n_skipped=$((n_skipped+1)) ;;
            *)
                n_other=$((n_other+1)) ;;
        esac
    done < <(find "${walk_paths[@]}" -type f -print0 2>/dev/null)

    fi  # DO_COMMIT_WALK guard
    if [ "$DO_COMMIT_WALK" = "1" ]; then
        echo "  copied:      $n_copied"
        echo "  identical:   $n_identical (drop-dup, no real conflict)"
        echo "  kept-dest:   $n_kept"
        echo "  sidecar:     $n_sidecar"
        [ "$n_quarantined" -gt 0 ] && echo "  quarantined: $n_quarantined (to <dest>/legacy/$tag/quarantine/)"
        echo "  skipped:     $n_skipped"
        [ "$n_other" -gt 0 ] && echo "  other:       $n_other"

        touch "$(source_sentinel "$tag")"
        echo "  sentinel:    $(source_sentinel "$tag")"
    fi

    # Per Mini #design 2026-06-02 blocker #4: --delete-source must actually
    # delete sources after sha verification. The two-phase pattern means
    # phase 1 (no-delete) ships; phase 2 (--delete-source --backup-id <id>)
    # cleans up after ~7d observation window of no straggler writes.
    if [ "$DELETE_SOURCE" = "1" ]; then
        local n_deleted=0 n_kept_unsafe=0
        # Re-walk the surface; for each file that has a sha-matching dest
        # landing, delete the source. Skip if no match (means content didn't
        # land — keeping source is the safe default).
        while IFS= read -r -d '' file; do
            local rel_d="${file#"$src"/}"
            case "$rel_d" in
                .git/*) continue ;;
                .DS_Store|*/.DS_Store|.gitkeep|*/.gitkeep) continue ;;
            esac
            local cls_d
            cls_d="$(classify "$rel_d")"
            # In-flight + ephemeral classes were never copied; don't delete.
            # Per Mini new-blocker #3: quarantine IS copied (to
            # <dest>/legacy/<tag>/quarantine/<rel>) — must be considered
            # for sha-verified deletion alongside other classes.
            case "$cls_d" in
                skip-ephemeral|skip-unknown|inflight-guard)
                    continue ;;
            esac
            # Find any dest landing whose content matches.
            local matched=""
            for cand in \
                "$DEST_REAL/$rel_d" \
                "$DEST_REAL/legacy/$tag/$rel_d" \
                "$DEST_REAL/legacy/$tag/quarantine/$rel_d"; do
                if [ -f "$cand" ] && sha_match "$file" "$cand"; then
                    matched="$cand"; break
                fi
            done
            # Per Mini new-blocker #1: any legacy-* sidecar (any tag) might
            # hold this source's content (dest-prior naming uses overwriting
            # source's tag, not original). Walk all and sha-compare.
            if [ -z "$matched" ]; then
                local g
                for g in "$DEST_REAL/$rel_d.legacy-"*; do
                    if [ -f "$g" ] && sha_match "$file" "$g"; then
                        matched="$g"; break
                    fi
                done
            fi
            # For rehome-state class, the dest is renamed (state/auth/<base>
            # or state/<base>); check those.
            if [ -z "$matched" ] && [ "$cls_d" = "rehome-state" ]; then
                local b="$(basename "$rel_d")"
                for cand in "$DEST_REAL/state/auth/$b" "$DEST_REAL/state/$b"; do
                    if [ -f "$cand" ] && sha_match "$file" "$cand"; then
                        matched="$cand"; break
                    fi
                done
            fi
            if [ -n "$matched" ]; then
                rm -f "$file" && n_deleted=$((n_deleted+1))
            else
                n_kept_unsafe=$((n_kept_unsafe+1))
            fi
        done < <(find "${walk_paths[@]}" -type f -print0 2>/dev/null)
        echo "  deleted:     $n_deleted source files (sha verified at dest)"
        [ "$n_kept_unsafe" -gt 0 ] && echo "  KEPT unsafe: $n_kept_unsafe source files (no matching dest content; investigate)"
    fi
}

commit_main() {
    # Mini's polish: --delete-source REQUIRES --backup-id pointer. Forces
    # operator to reference a real backup before destructive op.
    if [ "$DELETE_SOURCE" = "1" ] && [ -z "$ROLLBACK_ID" ]; then
        echo "ERROR: --delete-source requires --backup-id <id> to reference a known backup." >&2
        echo "  If you want to commit + delete in one step on a fresh state, run --commit first (no-delete)," >&2
        echo "  observe ~7d for straggler writers, then re-run with --commit --delete-source --backup-id <id-from-step-1>." >&2
        echo "  Available backups:" >&2
        ls -1 "$DEST_REAL/state/migration-backup-"*.tar 2>/dev/null "$DEST_REAL/state/migration-backup-"*.tar.gz 2>/dev/null \
            | sed -E 's@.*migration-backup-(.+)\.tar(\.gz)?$@    \1@' >&2 || echo "    (none)" >&2
        exit 2
    fi

    echo "sutando-migrate: COMMIT mode"
    echo "  dest:       $DEST_REAL"
    echo "  append:     $([ "$MERGE_APPEND" = "1" ] && echo "merge (Strategy B)" || echo "sidecar (Strategy C — default)")"
    echo "  delete src: $([ "$DELETE_SOURCE" = "1" ] && echo "yes (backup-id=$ROLLBACK_ID)" || echo "no — sources preserved (default; matches workspace_m1_no_auto_commit)")"
    echo

    # Pre-flight scan: tell the operator how big the job is + estimated time +
    # ask y/N before any destructive write. Skipped on phase-2 delete-only runs
    # (no copy walk happens) and on explicit --no-confirm + non-TTY stdin.
    # Captures total file count for progress-bar denominator.
    #
    # CRITICAL: `exit N` inside the $(preflight_summary) subshell does NOT
    # propagate to this parent — bash captures stdout + exit code into $? but
    # does NOT abort the calling script. Without the explicit `|| exit $?`
    # below, a user typing "n" at the confirm prompt would see "Aborted"
    # but then backup_dest + commit_source would run anyway (data-equivalent
    # bug: the user said no, the script does it). Catch the abort here.
    # Preflight runs whenever there's a copy walk. The phase-2 delete-only
    # path (--delete-source --backup-id) is the only invocation that skips
    # the copy walk → skip preflight there too. Mini's polish at L1378
    # already hard-errors on `--delete-source` without `--backup-id`, so
    # the bare DELETE_SOURCE=0 check is sufficient.
    PROGRESS_N=0
    PROGRESS_TOTAL=0
    if [ "$DELETE_SOURCE" = "0" ]; then
        PROGRESS_TOTAL="$(preflight_summary)" || exit $?
    fi

    backup_dest
    echo

    # Order: C (richest) → A (merges atop C) → B (sentinel-deduped legacy)
    [ -n "$C_REAL_OK" ] && include_src C && commit_source C "$C_REAL_OK"
    [ -n "$A_REAL_OK" ] && include_src A && commit_source A "$A_REAL_OK"
    [ -n "$B_REAL_OK" ] && include_src B && commit_source B "$B_REAL_OK"

    # β rehome of source's .git → dest/.git is a SEPARATE step. See
    # sutando-plus/scripts/sutando-migrate-sync.sh — runs AFTER this commit
    # succeeds, only relevant to sutando-plus users who have a vault remote at
    # the customized source location.

    # Auto-invoke Claude memory import — fixes Lucy's Maddy migration report
    # 2026-06-06: previously sutando-migrate set up the M2 directories but did
    # NOT copy `~/.claude/projects/<slug>/*` into `<workspace>/.claude-sutando/
    # projects/<slug>/*`. Users assumed migrate moved their Claude Code memory;
    # it didn't, leaving the real ~517-file memory at the legacy `~/.claude/`
    # location and a 181-byte stub at the new location. Now `commit_main` ends
    # with the same `--import` rsync that worked for owner's setup (when run
    # manually). Idempotent — rsync skips files already up-to-date at dest, so
    # re-running migrate is safe. Opt out with `--no-claude-import` for tests
    # or advanced users with custom claude-config-dir layouts.
    #
    # Skipped on phase-2 delete-only runs (DELETE_SOURCE=1 with $ROLLBACK_ID
    # set means the copy walk was already done in an earlier pass).
    if [ "$NO_CLAUDE_IMPORT" = "0" ] && [ "$DELETE_SOURCE" = "0" ]; then
        local _import_script="$(dirname "$0")/sutando-shell-setup.sh"
        if [ -x "$_import_script" ] || [ -f "$_import_script" ]; then
            echo
            echo "sutando-migrate: invoking sutando-shell-setup.sh --import to copy Claude memory ..."
            if bash "$_import_script" --import; then
                echo "  Claude memory import: ok"
            else
                local _rc=$?
                # Don't hard-fail the migrate on import failure — the per-file
                # workspace migration completed successfully; the user can
                # re-run `--import` manually. Surface the failure so they know
                # to address it.
                echo "  Claude memory import: FAILED (rc=$_rc) — re-run manually: bash scripts/sutando-shell-setup.sh --import" >&2
            fi
        else
            echo "  Claude memory import: skipped (scripts/sutando-shell-setup.sh not found at expected path; run --import manually after migrate)"
        fi
    fi

    # Slug-rename bridge — runs independently of the --import call above so
    # the test (and production --no-claude-import users) still get the bridge.
    # Skipped only on phase-2 delete-only runs (no copy walk happened).
    if [ "$DELETE_SOURCE" = "0" ]; then
        # Addresses Lucy's #design follow-up 2026-06-06:
        # `--import` rsyncs same-slug, but if the user's invocation CWD
        # changed between pre-M0 (CWD=<repo>) and post-M0 (CWD=<workspace>),
        # the slug Claude reads from gains a `-workspace` suffix (or similar
        # subdir-derived suffix). The same-slug rsync leaves files at the OLD
        # slug while Claude looks at the NEW slug → stub-only at the read
        # path.
        #
        # Symptomatic detection: scan `<dest>/.claude-sutando/projects/` for
        # populated-vs-stub mismatches sharing a slug prefix. If
        # `<base-slug>/memory/` is populated AND `<base-slug>-<suffix>/memory/`
        # is a stub (≤1 .md file), bridge by `cp -an` populated → stub.
        # This is symptom-driven (no hardcoded `-workspace` suffix) and
        # handles any subdir-derived slug variant.
        local _claude_dir="$DEST_REAL/.claude-sutando"
        local _projects_dir="$_claude_dir/projects"
        if [ -d "$_projects_dir" ]; then
            local _bridged_count=0
            local _populated_dir _base_slug _populated_count _variant_dir _variant_count
            for _populated_dir in "$_projects_dir"/*; do
                [ -d "$_populated_dir/memory" ] || continue
                _base_slug="$(basename "$_populated_dir")"
                _populated_count="$(find "$_populated_dir/memory" -maxdepth 1 -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')"
                # Skip stubs themselves
                [ "$_populated_count" -lt 2 ] && continue
                # Find variant slugs sharing the prefix
                for _variant_dir in "$_projects_dir/${_base_slug}-"*; do
                    [ -d "$_variant_dir/memory" ] || continue
                    _variant_count="$(find "$_variant_dir/memory" -maxdepth 1 -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')"
                    if [ "$_variant_count" -lt 2 ]; then
                        local _variant_slug
                        _variant_slug="$(basename "$_variant_dir")"
                        echo "  Claude memory bridge: $_base_slug → $_variant_slug ($_populated_count files; stub had $_variant_count)"
                        # `cp -a` (NO -n) — clobber the stub by design. Per
                        # Lucy + Chi (Maddy v0.8 validation 2026-06-06):
                        # the gate condition `_variant_count<2` already
                        # restricts to confirmed stubs (typically just a
                        # 181-byte placeholder MEMORY.md Claude wrote on
                        # first read). `cp -an` left that stub in place,
                        # so the user's ~73KB real MEMORY.md (the memory
                        # INDEX) never made it across — silent data-loss-
                        # equivalent for the index file. Dropping `-n`
                        # makes the populated source authoritative for ALL
                        # files at the variant slug.
                        cp -a "$_populated_dir/memory/"*.md "$_variant_dir/memory/" 2>/dev/null || true
                        _bridged_count=$((_bridged_count+1))
                    fi
                done
            done
            if [ "$_bridged_count" -eq 0 ]; then
                echo "  Claude memory bridge: no stub-vs-populated mismatches detected (Claude reads same slug as source)"
            fi
        fi
    fi  # DELETE_SOURCE gate for slug-rename bridge

    # Hook bridge — Option D from owner's #design 2026-06-07 design discussion.
    # Auto-re-install Sutando-owned hooks into the per-runtime CLAUDE_CONFIG_DIR/settings.json
    # and print a notice listing any third-party hooks that referenced
    # ~/.claude/hooks/... paths (which can't move automatically). See
    # scripts/sutando-config-hooks.sh header for the full rationale.
    # Opt out with --no-hook-bridge.
    if [ "$NO_HOOK_BRIDGE" = "0" ] && [ "$DELETE_SOURCE" = "0" ]; then
        local _hook_helper="$(dirname "$0")/sutando-config-hooks.sh"
        local _new_ccd; _new_ccd="$(bash "$(dirname "$0")/sutando-config.sh" claude-sutando-config-dir 2>/dev/null || true)"
        local _new_settings="${_new_ccd}/settings.json"
        local _old_settings="$HOME/.claude/settings.json"
        if [ -x "$_hook_helper" ] || [ -f "$_hook_helper" ]; then
            if [ -n "$_new_ccd" ]; then
                echo
                echo "sutando-migrate: bridging hooks via sutando-config-hooks.sh ..."
                # Idempotent install of catchup hook; project hooks are repo-level (already in repo's .claude/settings.json).
                bash "$_hook_helper" install "$_new_settings" --with-catchup-hook || \
                    echo "  hook install: failed (rc=$?) — re-run manually: bash scripts/sutando-config-hooks.sh install \"$_new_settings\"" >&2
                # Show dropped third-party hooks (non-Sutando) the user needs to re-add.
                bash "$_hook_helper" migration-notice "$_old_settings" "$_new_settings" || true
            else
                echo "  hook bridge: skipped (couldn't resolve claude-sutando-config-dir; check sutando.config.local.json)" >&2
            fi
        else
            echo "  hook bridge: skipped (scripts/sutando-config-hooks.sh not found at expected path; run manually after migrate)"
        fi
    fi

    # Channel bridge — Option A+ from owner's #design 2026-06-07 design discussion.
    # Copy $SOURCE_CLAUDE_CONFIG_DIR/channels/ (Sutando bridge access lists +
    # .env tokens for discord/telegram/slack) into the per-runtime
    # $CLAUDE_CONFIG_DIR/channels/ so bridges resolve to the workspace-scoped
    # location after migration. Pre-#1454 the silent ~/.claude/channels/
    # fallback in claude_home_path() made this "just work" — but a boot path
    # that forgot to set CLAUDE_CONFIG_DIR would silently fall back, and the
    # /channels/ symlink became load-bearing (Lucy in #design 2026-06-07 16:19Z).
    # This bridge moves the channels store onto CCD so a CCD-set boot path is
    # self-contained.
    # Idempotent: skip if dest already populated. Honors $SOURCE_CLAUDE_CONFIG_DIR
    # for the read-from location (defaults to ~/.claude/ — matches the legacy
    # claude_home_path() fallback). Opt out with --no-channel-bridge.
    if [ "$NO_CHANNEL_BRIDGE" = "0" ] && [ "$DELETE_SOURCE" = "0" ]; then
        local _new_ccd_ch; _new_ccd_ch="$(bash "$(dirname "$0")/sutando-config.sh" claude-sutando-config-dir 2>/dev/null || true)"
        local _src_channels="${SOURCE_CLAUDE_CONFIG_DIR:-$HOME/.claude}/channels"
        local _dst_channels="${_new_ccd_ch}/channels"
        if [ -z "$_new_ccd_ch" ]; then
            echo "  channel bridge: skipped (couldn't resolve claude-sutando-config-dir; check sutando.config.local.json)" >&2
        elif [ ! -d "$_src_channels" ]; then
            echo "  channel bridge: skipped (no source $_src_channels — nothing to migrate)"
        else
            # Detect src/dst pointing at the same physical path (legacy symlink
            # $SOURCE_CLAUDE_CONFIG_DIR/channels -> $CCD/channels). Without this
            # check, cp -a would try to copy a directory into itself.
            local _src_real _dst_real
            _src_real="$(cd "$_src_channels" 2>/dev/null && pwd -P || echo "$_src_channels")"
            _dst_real="$(cd "$_dst_channels" 2>/dev/null && pwd -P || echo "$_dst_channels")"
            if [ "$_src_real" = "$_dst_real" ]; then
                echo "  channel bridge: skipped (source and dest resolve to same path: $_src_real — already migrated or symlinked through)"
            elif [ -d "$_dst_channels" ] && [ -n "$(ls -A "$_dst_channels" 2>/dev/null)" ]; then
                echo "  channel bridge: skipped ($_dst_channels already populated — idempotent)"
            else
                echo
                echo "sutando-migrate: bridging channels (Sutando bridge access lists + .env tokens) ..."
                mkdir -p "$_dst_channels"
                if cp -a "$_src_channels/." "$_dst_channels/" 2>/dev/null; then
                    echo "  channel bridge: copied $_src_channels → $_dst_channels"
                else
                    echo "  channel bridge: copy failed (rc=$?) — re-run manually: cp -a \"$_src_channels/.\" \"$_dst_channels/\"" >&2
                fi
            fi
        fi
    fi

    # .env adoption — legacy installs kept secrets in `<old-workspace>/.env`,
    # but startup.sh hard-requires them at repo root (`$REPO_DIR/.env`, with
    # GEMINI_API_KEY). The per-file copy walk excludes `.env` (per-clone file),
    # so without this the secrets stay stranded in the old workspace and the
    # first post-migrate `startup.sh` bails ("`.env` not found"). Auto-adopt the
    # legacy `.env` to repo root when repo root lacks a valid one — no human
    # step. Skipped on phase-2 delete-only runs (no copy walk happened).
    if [ "$DELETE_SOURCE" = "0" ]; then
        local _repo_env="$REPO_DIR/.env"
        if [ -f "$_repo_env" ] && grep -q '^GEMINI_API_KEY=.\+' "$_repo_env" 2>/dev/null; then
            echo "  .env adopt: $_repo_env already valid — skip"
        else
            local _legacy_env="" _c
            # Priority: legacy default workspace, the $SUTANDO_WORKSPACE override
            # (value ignored for resolution since v0.8 but still a common .env home),
            # then ~/.sutando/.env.
            for _c in "$HOME/.sutando/workspace/.env" \
                      "${SUTANDO_WORKSPACE:+${SUTANDO_WORKSPACE/#\~/$HOME}/.env}" \
                      "$HOME/.sutando/.env"; do
                # `|| :` keeps the AND-list (and the loop) at status 0 under `set -e`
                # when a candidate is absent.
                [ -n "$_c" ] && [ -f "$_c" ] && grep -q '^GEMINI_API_KEY=.\+' "$_c" 2>/dev/null && { _legacy_env="$_c"; break; } || :
            done
            # Fallback: newest .env carrying the key anywhere under ~/.sutando.
            # `|| true` — the find/grep/head pipeline returns non-zero when
            # ~/.sutando is absent or nothing matches; without it `set -euo
            # pipefail` would abort the whole commit (CI has no ~/.sutando).
            if [ -z "$_legacy_env" ]; then
                _legacy_env="$(find "$HOME/.sutando" -maxdepth 4 -name .env -type f 2>/dev/null \
                    -exec grep -l '^GEMINI_API_KEY=.\+' {} \; | xargs -r ls -t 2>/dev/null | head -1 || true)"
            fi
            if [ -n "$_legacy_env" ]; then
                { [ -f "$_repo_env" ] && cp "$_repo_env" "$_repo_env.bak-$BACKUP_ID"; } || :
                # Strip any stale `SUTANDO_WORKSPACE=` so the adopted secrets can't
                # re-point this clone back at the old workspace (also silences the
                # v0.8 deprecation nag). All other keys carry over verbatim.
                if grep -v '^SUTANDO_WORKSPACE=' "$_legacy_env" > "$_repo_env" 2>/dev/null \
                   && grep -q '^GEMINI_API_KEY=.\+' "$_repo_env" 2>/dev/null; then
                    echo "  .env adopt: $_legacy_env → $_repo_env ($(grep -cE '^[A-Z_]+=' "$_repo_env") keys; SUTANDO_WORKSPACE stripped)"
                else
                    echo "  .env adopt: adoption failed — set $_repo_env manually before startup.sh (source: $_legacy_env)" >&2
                fi
            else
                echo "  .env adopt: no legacy .env with GEMINI_API_KEY found under ~/.sutando — set $_repo_env manually before startup.sh"
            fi
        fi
    fi

    echo
    echo "sutando-migrate: COMMIT complete. Verify with: bash scripts/sutando-migrate.sh verify"
    echo "  rollback: bash scripts/sutando-migrate.sh rollback --backup-id $BACKUP_ID"
    if [ "$DELETE_SOURCE" = "0" ]; then
        # Mini's polish: explicit next-step messaging for the two-phase pattern.
        echo "  Sources NOT deleted (default). After ~7d observing no source-side writes, run:"
        echo "    bash scripts/sutando-migrate.sh commit --delete-source --backup-id $BACKUP_ID"
        echo "  Two-phase pattern keeps the (b)-style reader-fallback bridge intact during transition."
    fi
}

verify_main() {
    # Per Mini #design 2026-06-02: verify must sha-compare content, not just
    # check path existence. The previous version could pass even after a
    # collision overwrote the source content. Now: for each indexed source
    # file, locate its canonical dest landing OR sidecar, then sha-compare.
    echo "sutando-migrate: VERIFY mode"
    local pass=0 missing=0 mismatch=0
    if [ ! -s "$XSRC_INDEX" ]; then
        index_dest_for_collisions
        [ -n "$A_REAL_OK" ] && include_src A && scan_source A "$A_REAL_OK" >/dev/null
        [ -n "$B_REAL_OK" ] && include_src B && scan_source B "$B_REAL_OK" >/dev/null
        [ -n "$C_REAL_OK" ] && include_src C && scan_source C "$C_REAL_OK" >/dev/null
    fi
    # Need an inverse map: from indexed (tag, rel) back to the absolute source path.
    # The scan recorded rel relative to the source root + tag. Re-derive:
    src_path_for_tag() {
        case "$1" in
            A) echo "$A_REAL_OK" ;;
            B) echo "$B_REAL_OK" ;;
            C) echo "$C_REAL_OK" ;;
        esac
    }
    while IFS=$'\t' read -r rel tag cls mt sz; do
        [ "$tag" = "DEST" ] && continue
        case "$cls" in
            skip-ephemeral|skip-unknown|inflight-guard)
                pass=$((pass+1)); continue ;;
        esac
        # Per Mini #design 2026-06-02 new-blocker #3: quarantine MUST be
        # sha-verified at its dest landing, not pass-no-check. The walk below
        # checks dst_quarantine (=$DEST_REAL/legacy/$tag/quarantine/$rel) via
        # the standard cands loop.
        local src_root
        src_root="$(src_path_for_tag "$tag")"
        [ -z "$src_root" ] && { pass=$((pass+1)); continue; }
        # The XSRC_INDEX stores POST-classification rel (e.g. rehome-state
        # writes state/auth/cloud-auth.json but the source was at root). Try
        # the index-rel first; if source doesn't exist there, try with the
        # original surface mapping (basename for rehome classes).
        local src_file="$src_root/$rel"
        [ ! -f "$src_file" ] && src_file="$src_root/$(basename "$rel")"
        [ ! -f "$src_file" ] && { missing=$((missing+1)); [ "$missing" -le 5 ] && echo "  MISSING-SRC: $tag/$rel ($cls)"; continue; }
        # Candidate destinations to check (in order of likelihood per class).
        local dst_canonical="$DEST_REAL/$rel"
        local dst_sidecar_legacy="$DEST_REAL/legacy/$tag/$rel"
        local dst_quarantine="$DEST_REAL/legacy/$tag/quarantine/$rel"
        # Per Mini #design 2026-06-02 new-blocker #1: dest-prior sidecars are
        # named after the OVERWRITING source, not the original-content source.
        # E.g. C's content might be at <path>.legacy-prior-from-A-<ts> (because
        # A overwrote it). Looking only at legacy-$tag-* / legacy-prior-from-$tag-*
        # for source C would miss C's content there. Walk ALL legacy-*-* sidecars
        # and sha-compare to find the source's content regardless of tag.
        local landed=""
        local cands=()
        for c in "$dst_canonical" "$dst_sidecar_legacy" "$dst_quarantine"; do
            cands+=("$c")
        done
        # All possible legacy-* sidecars at canonical-rel (any source tag).
        for g in "$dst_canonical.legacy-"*; do
            [ -f "$g" ] && cands+=("$g")
        done
        for cand in "${cands[@]}"; do
            if [ -f "$cand" ]; then
                if sha_match "$src_file" "$cand"; then
                    landed="$cand"; break
                fi
            fi
        done
        if [ -n "$landed" ]; then
            pass=$((pass+1))
        elif [ -f "$dst_canonical" ] || [ -f "$dst_sidecar_legacy" ]; then
            # A dest path exists but sha doesn't match — content mismatch.
            mismatch=$((mismatch+1))
            [ "$mismatch" -le 5 ] && echo "  MISMATCH: $tag/$rel ($cls) — dest content differs from source"
        else
            missing=$((missing+1))
            [ "$missing" -le 5 ] && echo "  MISSING: $tag/$rel ($cls) — no dest path found"
        fi
    done < "$XSRC_INDEX"
    echo
    echo "verify summary: pass=$pass missing=$missing mismatch=$mismatch"
    if [ "$missing" -gt 0 ] || [ "$mismatch" -gt 0 ]; then
        echo "verify: FAIL — $missing missing + $mismatch sha mismatch. Inspect with --json or scripts/sutando-migrate.sh explain <path>."
        exit 1
    fi
    echo "verify: OK (sha-256 content match confirmed for every indexed source file)"
}

rollback_main() {
    # Disable set -e for rollback's cleanup walks — they tolerate per-file
    # failures (rm of non-existent files, grep no-match, tar of empty).
    set +e
    [ -z "$ROLLBACK_ID" ] && {
        echo "rollback: --backup-id <id> required. Available backups:" >&2
        # Bug #3 (2026-06-06): backups can be .tar.gz OR .tar (uncompressed
        # for media-heavy workspaces). List both shapes.
        ls -1 "$DEST_REAL/state/migration-backup-"*.tar 2>/dev/null "$DEST_REAL/state/migration-backup-"*.tar.gz 2>/dev/null \
            | sed -E 's@.*migration-backup-(.+)\.tar(\.gz)?$@  \1@' >&2
        exit 2
    }
    # Backup file may be .tar.gz (default) or .tar (uncompressed — for
    # media-heavy workspaces post-Bug #3). Check both, prefer the existing one.
    local backup_path=""
    if [ -f "$DEST_REAL/state/migration-backup-$ROLLBACK_ID.tar.gz" ]; then
        backup_path="$DEST_REAL/state/migration-backup-$ROLLBACK_ID.tar.gz"
    elif [ -f "$DEST_REAL/state/migration-backup-$ROLLBACK_ID.tar" ]; then
        backup_path="$DEST_REAL/state/migration-backup-$ROLLBACK_ID.tar"
    else
        echo "rollback: backup migration-backup-$ROLLBACK_ID.tar(.gz)? not found" >&2
        exit 2
    fi
    echo "sutando-migrate: ROLLBACK from $backup_path"
    # Clear sentinels for that backup id (idempotency).
    # Note: glob may match zero files; suppress set -e via || true.
    rm -f "$DEST_REAL/state/.migrated-from-"*"-$ROLLBACK_ID" 2>/dev/null || true
    # Untar into dest. BSD tar (macOS) overwrites by default; GNU tar (Linux)
    # also overwrites by default. Workspace surface only — never touches
    # state/migration-backup-*.tar.* files (they're outside the tar).
    # Empty backup (0-byte) means dest was empty at backup time; skip extract +
    # rely solely on the cleanup walk below to restore to empty state.
    # Auto-detect gz vs not — tar -xf works on both gzipped and plain tarballs
    # on both BSD and GNU implementations (uses magic-byte detection).
    if [ -s "$backup_path" ]; then
        ( cd "$DEST_REAL" && tar -xf "$backup_path" ) || {
            echo "rollback: extract failed" >&2
            exit 1
        }
    else
        echo "rollback: empty backup (dest was bootstrap-only at backup time); skipping extract"
    fi
    # Clean files added since backup that are NOT in the backup tarball.
    # Read tarball entries → reference set → find dest's surface files not in set → delete.
    local tar_listing
    tar_listing="$(mktemp -t sutando-rollback.XXXXXX)"
    if [ -s "$backup_path" ]; then
        # -tf (not -tzf): media-heavy backups above the gzip threshold are plain
        # .tar; GNU tar errors on -z for those (bsdtar auto-detects, GNU doesn't
        # in list mode) and the old `|| true` turned that error into an empty
        # listing — which the walk below read as "nothing to preserve" and
        # deleted every file just restored. -tf magic-byte-detects on both.
        tar -tf "$backup_path" 2>/dev/null > "$tar_listing" || true
        # Guard: a non-empty backup must yield a non-empty listing. An empty
        # listing here means tar failed (corrupt archive, unsupported format) —
        # abort rather than let the cleanup walk mass-delete the restored tree.
        if [ ! -s "$tar_listing" ]; then
            echo "rollback: backup is non-empty but listing came back empty (tar list failed?) — refusing post-restore cleanup" >&2
            rm -f "$tar_listing" || true
            exit 1
        fi
    else
        : > "$tar_listing"  # empty backup: nothing to preserve, everything is "added since"
    fi
    [ "${SUTANDO_MIGRATE_DEBUG:-0}" = "1" ] && echo "[debug] tar_listing size=$(wc -l < "$tar_listing") backup_path size=$(stat -f %z "$backup_path")" >&2
    local sd
    [ "${SUTANDO_MIGRATE_DEBUG:-0}" = "1" ] && echo "[debug] rollback walk: DEST_REAL=$DEST_REAL" >&2
    for sd in "${WORKSPACE_SURFACE_DIRS[@]}" "${WORKSPACE_SURFACE_FILES[@]}"; do
        [ ! -e "$DEST_REAL/$sd" ] && continue
        [ "${SUTANDO_MIGRATE_DEBUG:-0}" = "1" ] && echo "[debug] rollback walking $DEST_REAL/$sd" >&2
        while IFS= read -r f; do
            local rel="${f#"$DEST_REAL"/}"
            # If this rel is NOT in the tar listing, it was added after backup → remove.
            # -xF: match the whole line as a fixed string — rel interpolated as a
            # regex made bracket/metachar filenames fail to self-match and get
            # deleted on every rollback. `--` guards a leading-dash rel.
            if ! grep -qxF -- "$rel" "$tar_listing" 2>/dev/null; then
                [ "${SUTANDO_MIGRATE_DEBUG:-0}" = "1" ] && echo "[debug] rollback rm $rel" >&2
                rm -f "$f"
            fi
        done < <(find "$DEST_REAL/$sd" -type f 2>/dev/null)
    done
    rm -f "$tar_listing" || true
    # Drop sentinels matching this backup id
    rm -f "$DEST_REAL/state/.migrated-from-"*"-$ROLLBACK_ID" 2>/dev/null || true
    # Drop legacy/<src-tag>/ sidecars (only the script ever writes there)
    rm -rf "$DEST_REAL/legacy" 2>/dev/null || true
    echo "rollback: OK — dest restored to backup $ROLLBACK_ID"
}

emit_json() {
    # Reads XSRC_INDEX (TSV) + the resolved source paths/state and emits a
    # machine-readable JSON dump for downstream tooling (dashboards, skill
    # wrappers, the eventual --commit dry-run UI). Python3 is the bash-friendly
    # path — sutando already requires it.
    python3 - "$DEST_REAL" "$A_REAL_OK" "$B_REAL_OK" "$C_REAL_OK" "$XSRC_INDEX" <<'PY'
import json, sys, os
from collections import defaultdict
dest, a, b, c, idx_path = sys.argv[1:6]
entries = []
if os.path.exists(idx_path):
    with open(idx_path) as f:
        for line in f:
            rel, tag, cls, mt, sz = line.rstrip("\n").split("\t")
            entries.append({"rel": rel, "tag": tag, "class": cls,
                            "mtime": int(mt or 0), "size": int(sz or 0)})
by_rel = defaultdict(list)
for e in entries:
    by_rel[e["rel"]].append(e)
collisions = {k: v for k, v in by_rel.items() if len(v) > 1}
identical = sum(1 for v in collisions.values()
                if len({(e["mtime"], e["size"]) for e in v}) == 1)
genuine = len(collisions) - identical
by_class = defaultdict(int)
for v in collisions.values():
    by_class[v[0]["class"]] += 1
def has_size_mismatch(entries):
    """True if entries differ in size — real content divergence the user must reason about."""
    return len({e["size"] for e in entries}) > 1
def has_mtime_mismatch(entries):
    return len({e["mtime"] for e in entries}) > 1
# Sort by actionability: size-mismatch (real content conflict) first, then
# mtime-only diff (commit's newest-mtime resolves it), then identical
# (drop-dup). Tiebreak by class then rel.
def sort_key(item):
    k, v = item
    sz_diff = has_size_mismatch(v)
    mt_diff = has_mtime_mismatch(v)
    # priority: 0=size-diff (real), 1=mtime-only, 2=identical
    if sz_diff: prio = 0
    elif mt_diff: prio = 1
    else: prio = 2
    return (prio, v[0]["class"], k)
notable = [{"class": v[0]["class"], "rel": k,
            "size_mismatch": has_size_mismatch(v),
            "mtime_mismatch": has_mtime_mismatch(v),
            "entries": [{"tag": e["tag"], "mtime": e["mtime"], "size": e["size"]} for e in v]}
           for k, v in sorted(collisions.items(), key=sort_key)]
size_diff = sum(1 for v in collisions.values() if has_size_mismatch(v))
mtime_only = sum(1 for v in collisions.values() if not has_size_mismatch(v) and has_mtime_mismatch(v))
out = {
    "dest": dest,
    "sources": {"A": a or None, "B": b or None, "C": c or None},
    "totals": {
        "unique_relpaths": len(by_rel),
        "collisions": len(collisions),
        "identical_content": identical,
        "mtime_only_diff": mtime_only,  # commit's newest-mtime auto-resolves
        "size_mismatch": size_diff,     # the actionable subset — real content conflicts
        # Legacy "genuine_conflicts" kept for backward-compat; equals mtime_only + size_mismatch
        "genuine_conflicts": genuine,
        "by_class": dict(by_class),
    },
    "notable_collisions": notable[:50],
}
json.dump(out, sys.stdout, indent=2)
print()
PY
}

explain_main() {
    # `explain <path>` — operator dev-aid. Walks CLASS_RULES in order, prints
    # the first match + the class + the resulting destination + the rule rank.
    # Per Mini #design 2026-06-02 dev-aid suggestion.
    if [ "$#" -lt 1 ] && [ -z "${EXPLAIN_PATH:-}" ]; then
        echo "explain: usage: bash scripts/sutando-migrate.sh explain <relpath>" >&2
        echo "  e.g.: explain notes/foo.md" >&2
        echo "        explain state/cores/MBP.alive" >&2
        echo "        explain tasks/processed/old.txt" >&2
        exit 2
    fi
    local rel="${EXPLAIN_PATH:-$1}"
    local rank=0
    for rule in "${CLASS_RULES[@]}"; do
        rank=$((rank + 1))
        local glob="${rule%%|*}"
        local cls="${rule##*|}"
        # shellcheck disable=SC2254
        case "$rel" in
            $glob)
                echo "path:   $rel"
                echo "match:  rule #$rank — glob \`$glob\`"
                echo "class:  $cls"
                # Show the commit-time destination for the class:
                local dest_hint=""
                case "$cls" in
                    structural|collision-keep-both|newest-mtime|append)
                        dest_hint="<dest>/$rel"
                        [ "$cls" = "append" ] && dest_hint="$dest_hint  OR  <dest>/legacy/<src-tag>/$rel  (default sidecar; --merge-append concats)"
                        ;;
                    rehome-state)
                        local base="$(basename "$rel")"
                        case "$base" in
                            cloud-auth.json|device.json) dest_hint="<dest>/state/auth/$base" ;;
                            *) dest_hint="<dest>/state/$base" ;;
                        esac
                        ;;
                    rehome-dated-snapshot) dest_hint="<dest>/notes/archive/$(basename "$rel")" ;;
                    rehome-narrative-log) dest_hint="<dest>/logs/workspace-narrative.log  (renamed to dodge logs/conversation.log collision)" ;;
                    inflight-guard) dest_hint="<dest>/$rel  (if >${INFLIGHT_GUARD_SEC}s old)  OR  <dest>/${rel%%/*}/archive/<src-tag>/$(basename "$rel")  (route-to-archive)" ;;
                    skip-ephemeral|skip-unknown) dest_hint="(skipped — no write)" ;;
                    *) dest_hint="<unknown>" ;;
                esac
                echo "dest:   $dest_hint"
                return 0
            ;;
        esac
    done
    echo "path:  $rel"
    echo "match: none — no rule fires"
    echo "class: unknown (skipped at commit; no write)"
    return 0
}

case "$MODE" in
    explain)
        explain_main "$@"
        exit $?
        ;;
    scan)
        REPORT_LINES=("Scan report (scan-only; no writes):")
        index_dest_for_collisions
        [ -n "$A_REAL_OK" ] && include_src A && scan_source A "$A_REAL_OK"
        [ -n "$B_REAL_OK" ] && include_src B && scan_source B "$B_REAL_OK"
        [ -n "$C_REAL_OK" ] && include_src C && scan_source C "$C_REAL_OK"
        if [ "$JSON" = "1" ]; then
            emit_json
        else
            report_cross_source
            REPORT_LINES+=("")
            REPORT_LINES+=("Recommended commit order: C (richest, do first) → A (merge atop C) → B (sentinel-deduped)")
            REPORT_LINES+=("")
            REPORT_LINES+=("Next: bash scripts/sutando-migrate.sh commit  (when ready)")
            for line in "${REPORT_LINES[@]}"; do echo "$line"; done
        fi
        ;;
    commit)
        commit_main
        ;;
    verify)
        verify_main
        ;;
    rollback)
        rollback_main
        ;;
esac
