#!/usr/bin/env bash
# sutando-shell-setup — configure the `claude-sutando` shell alias.
#
# Sets up an alias of the form:
#   alias claude-sutando='CLAUDE_CONFIG_DIR=<workspace>/.claude-sutando claude'
#
# The `<workspace>/.claude-sutando` path is resolved via
#   `bash scripts/sutando-config.sh claude-sutando-config-dir`
# which reads `claude_sutando_config_dir.subdir` from sutando.config.json
# (default `.claude-sutando`) and concatenates under the resolved workspace.
#
# Why this exists: CLAUDE_CONFIG_DIR is an undocumented-but-supported env
# var (string present in the `claude` binary). Setting it per-workspace lets
# the user keep Sutando-specific Claude state (sessions, memory, skills) inside
# the workspace tree, which the M2 vault sync engine then includes via the
# vault.sync.include allowlist. The sub-folder-of-workspace constraint is
# load-bearing for sync coherence.
#
# Usage:
#   bash scripts/sutando-shell-setup.sh                # dry-run: print proposed line + target rc
#   bash scripts/sutando-shell-setup.sh --commit       # append to rc file (idempotent)
#   bash scripts/sutando-shell-setup.sh --auto         # one-shot prompt path used by startup.sh
#   bash scripts/sutando-shell-setup.sh --check        # exit 0 if alias present + path matches; 1 otherwise
#   bash scripts/sutando-shell-setup.sh --import       # rsync ~/.claude → <workspace>/.claude-sutando (idempotent, non-destructive)
#   bash scripts/sutando-shell-setup.sh --repair-paths # re-pin hardcoded SOURCE_DIR paths in runtime files to CLAUDE_DIR (idempotent)
#
# Deprecated aliases (kept for one release):
#   --migrate      # alias for --import; emits a stderr deprecation warning. Per
#                  # `feedback_import_not_migrate`: "import" describes the
#                  # actual non-destructive-copy semantic; "migrate" implied a
#                  # one-way structural change.
#
# Modifier flags (can combine with any MODE-setting flag):
#   --force         # override the "user-defined claude-sutando() function detected" guard in --commit
#   --from=<PATH>   # for --import / --repair-paths: ALSO rewrite this OLD claude-config-dir
#                   # location (in addition to the default SOURCE_CLAUDE_CONFIG_DIR / ~/.claude).
#                   # Use when the workspace was moved: previously-rewritten paths point at the
#                   # old workspace's .claude-sutando; --from re-pins them to the current target.
#                   # Note: uses `=`-joined form (--from=/path), not space-separated.
#
# Idempotency: --commit grep-guards on the alias key `^alias claude-sutando=`,
# not the body. If the workspace path changes (config edit or repo relocate),
# re-running --commit cleanly REPLACES the line with the new resolved path.
# This is the same pattern as `feedback_universal_key_cleanup_over_narrow_guard`.
#
# Exit codes:
#   0 — already configured + path matches (no-op); OR dry-run completed; OR
#       --commit applied successfully.
#   1 — config invalid (loader rejected the subdir invariants) or rc write failed.
#   2 — user declined the --auto prompt.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="dry-run"
FORCE=0
FROM_DIR=""
# INVOKED_AS — the literal flag the user passed for the MODE-setting choice.
# Used in status messages so they echo back what the user typed (e.g. when
# `--migrate` is the deprecated alias, we still say "sutando-shell-setup
# --migrate: done." rather than the canonical name they didn't use).
INVOKED_AS=""
# Deprecation tracking — set when a deprecated alias was used so the case-arm
# can print a single stderr warning on top of the normal flow.
DEPRECATED_FLAG_MIGRATE=0

# Parse args in two passes so modifiers (`--force`, `--from=PATH`) get
# parsed regardless of where they appear relative to the MODE-setting flag.
# The MODE-setting flags `break;` (first wins, by existing convention) and
# would otherwise eat any modifier that appears after them on the command
# line (e.g. `--repair-paths --from=/x` would not set FROM_DIR).
#
# Pass 1: scan ALL args for modifiers. Doesn't break.
for arg in "$@"; do
  case "$arg" in
    --force)   FORCE=1 ;;
    --from=*)  FROM_DIR="${arg#--from=}" ;;
  esac
done
# Pass 2: find the FIRST mode-setting flag and stop. Original behavior.
# Modifiers already consumed by pass 1 — silently no-op them here so the
# unknown-arg arm doesn't fire on a legit modifier.
for arg in "$@"; do
  case "$arg" in
    --force|--from=*) ;;  # consumed by pass 1
    --commit)  MODE="commit"; INVOKED_AS="--commit"; break ;;
    --auto)    MODE="auto"; INVOKED_AS="--auto"; break ;;
    --check)   MODE="check"; INVOKED_AS="--check"; break ;;
    --import)  MODE="import"; INVOKED_AS="--import"; break ;;
    # Deprecated 1-release alias for --import (per `feedback_import_not_migrate`).
    # Routes to the same case-arm + sets the deprecation-warning flag.
    --migrate) MODE="import"; INVOKED_AS="--migrate"; DEPRECATED_FLAG_MIGRATE=1; break ;;
    --repair-paths) MODE="repair-paths"; INVOKED_AS="--repair-paths"; break ;;
    --help|-h)
      sed -n '1,40p' "$0" | grep -E '^#' | sed 's/^# *//'
      exit 0
      ;;
    *) echo "sutando-shell-setup: unknown arg '$arg' (try --help)" >&2; exit 1 ;;
  esac
done

# Resolve THIS repo's target path via the config loader. Used for --check
# (smoke-test that THIS checkout resolves cleanly) and for --import. The
# function we install below does its own per-invocation resolve based on the
# caller's cwd, so this CLAUDE_DIR isn't baked into the rc file — multiple
# Sutando checkouts on the same machine each map to their own .claude-sutando.
#
# stderr captured separately so the loader's legacy-env-var deprecation warn
# (which contains colons) doesn't get embedded into the path string — rsync
# would then read the colon as `host:path` and try to do a remote copy.
_resolve_err="$(mktemp -t sutando-shell-setup-resolve-err.XXXXXX)"
if ! CLAUDE_DIR="$(bash "$REPO_ROOT/scripts/sutando-config.sh" claude-sutando-config-dir 2>"$_resolve_err")"; then
  echo "sutando-shell-setup: failed to resolve claude_sutando_config_dir for $REPO_ROOT" >&2
  cat "$_resolve_err" >&2
  rm -f "$_resolve_err"
  exit 1
fi
rm -f "$_resolve_err"

# Marker-block convention. The whole block (markers + body) is owned by this
# script; anything between BEGIN and END gets replaced atomically on rewrite.
# Idempotency keys on the markers, not on body content — so we can evolve the
# function body (or swap alias↔function) without bricking existing rc files.
MARKER_BEGIN='# >>> sutando-shell-setup managed block — do not edit between markers'
MARKER_END='# <<< sutando-shell-setup managed block'

# The function we install. Per-invocation it:
#   1. Finds repo root via `git rev-parse` on the caller's cwd
#   2. Calls scripts/sutando-config.sh claude-sutando-config-dir to resolve
#      that repo's workspace-scoped CLAUDE_CONFIG_DIR
#   3. mkdir's the resolved path (idempotent)
#   4. Execs claude with CLAUDE_CONFIG_DIR set, passing through all args
#
# Failure modes are explicit (refused with stderr, non-zero return) rather
# than silent fallback to ~/.claude — owner directive: each Sutando instance
# must use its own config dir, not accidentally share via the default.
read -r -d '' FUNCTION_BODY <<'EOF_FUNC' || true
claude-sutando() {
  local repo_root
  repo_root="$(git -C "${PWD}" rev-parse --show-toplevel 2>/dev/null)" || {
    echo "claude-sutando: not inside a git repo (cd into a Sutando checkout)" >&2
    return 1
  }
  if [ ! -x "$repo_root/scripts/sutando-config.sh" ]; then
    echo "claude-sutando: $repo_root is not a Sutando checkout (missing scripts/sutando-config.sh)" >&2
    return 1
  fi
  # v0.9 — read env var NAME and VALUE from core_config_dirs (per-runtime
  # surface, type=claude entry). Honors user-set `env_name` so the wrapper
  # could in principle target a non-CLAUDE_CONFIG_DIR var; for Claude
  # specifically that name doesn't change in practice. Loader has already
  # validated `synced=true` entries are workspace-relative — if the user set
  # `synced: false`, they've explicitly opted out of M2 sync coverage for
  # this memory tree (no warning, by design).
  local env_name ccd
  env_name="$(bash "$repo_root/scripts/sutando-config.sh" core-config-dir-env-name claude)"
  ccd="$(bash "$repo_root/scripts/sutando-config.sh" core-config-dir-value claude)" || return 1
  [ -z "$env_name" ] && env_name="CLAUDE_CONFIG_DIR"
  [ -z "$ccd" ] && {
    echo "claude-sutando: failed to resolve CLAUDE_CONFIG_DIR via core_config_dirs (config missing or invalid)" >&2
    return 1
  }
  mkdir -p "$ccd"
  env "$env_name=$ccd" command claude "$@"
}
EOF_FUNC

# Helper: build the canonical block (markers + body) that we write into the rc.
build_managed_block() {
  printf '%s\n%s\n%s\n' "$MARKER_BEGIN" "$FUNCTION_BODY" "$MARKER_END"
}

# Detect the user's shell and the rc file we'd write to.
#   zsh  → ~/.zshrc
#   bash → ~/.bashrc on Linux, ~/.bash_profile on macOS (login-shell convention)
SHELL_NAME="$(basename "${SHELL:-bash}")"
case "$SHELL_NAME" in
  zsh)
    RC_FILE="$HOME/.zshrc"
    ;;
  bash)
    if [[ "$(uname)" == "Darwin" ]]; then
      RC_FILE="$HOME/.bash_profile"
    else
      RC_FILE="$HOME/.bashrc"
    fi
    ;;
  *)
    # Unknown shell — print and let user choose where to put it
    echo "sutando-shell-setup: shell '$SHELL_NAME' is not zsh/bash; can't auto-detect rc file." >&2
    echo "Proposed function (paste into the appropriate rc file yourself):" >&2
    build_managed_block >&2
    exit 1
    ;;
esac

# Helper: does the rc file contain our managed marker block (regardless of
# the body content inside)?
managed_block_present() {
  [ -f "$RC_FILE" ] && grep -qF "$MARKER_BEGIN" "$RC_FILE"
}

# Helper: is the managed block present AND does its content match what we'd
# write today? Used by --check to detect drift after an update to the function
# body in this script.
managed_block_current() {
  managed_block_present || return 1
  local expected actual
  expected="$(build_managed_block)"
  # Extract everything from MARKER_BEGIN to MARKER_END inclusive, then compare.
  actual="$(awk -v b="$MARKER_BEGIN" -v e="$MARKER_END" '
    $0 == b { inblk=1 }
    inblk { print }
    $0 == e { inblk=0 }
  ' "$RC_FILE")"
  [ "$expected" = "$actual" ]
}

# Helper: detect a LEGACY pre-marker alias (from before the function rewrite).
# Used by --check / --commit to migrate users who set up before this version.
legacy_alias_present() {
  [ -f "$RC_FILE" ] && grep -qE '^alias claude-sutando=' "$RC_FILE"
}

# Helper: detect a user-defined `claude-sutando` function OUTSIDE our
# managed marker block. Set up because --commit otherwise appends the
# managed block alongside any pre-existing function, and bash's
# last-definition-wins makes the user's function dead code with no warning
# (Mini PR #1415 review #6 → issue #1418).
#
# Per Mini PR #1424 review #3: regex expanded to catch all common
# function-definition shapes, including the `function NAME { ... }`
# syntax and leading-whitespace variants:
#   - `claude-sutando()`              (POSIX function, column 0)
#   - `  claude-sutando()`            (leading whitespace — common in
#                                       rc-files with conditional blocks)
#   - `claude-sutando ()`             (space before parens)
#   - `function claude-sutando`       (keyword form, no parens)
#   - `function claude-sutando {`     (keyword form, brace on same line)
#   - `function claude-sutando () {`  (keyword form + parens)
#   - leading-whitespace variants of any of the above
#
# Awk strips the managed block first so we don't false-positive on our
# own injected function. Grep alternation covers both shapes.
user_defined_function_present() {
  [ -f "$RC_FILE" ] || return 1
  awk -v b="$MARKER_BEGIN" -v e="$MARKER_END" '
    $0 == b { inblk=1; next }
    $0 == e { inblk=0; next }
    !inblk { print }
  ' "$RC_FILE" | grep -qE '^[[:space:]]*(claude-sutando[[:space:]]*\(\)|function[[:space:]]+claude-sutando([[:space:]]|\(|\{|$))'
}

# Helper: rewrite hardcoded SOURCE_DIR/ → CLAUDE_DIR/ in runtime-critical
# files under CLAUDE_DIR. Shared by --import (post-rsync) and --repair-paths
# (standalone re-pin without rsync — useful when the workspace moves).
#
# Why an allowlist (+globbed patterns) instead of recursive sed:
#   - The migrated tree contains immutable history (`projects/*/*.jsonl`,
#     `history.jsonl`), per-migration backups (`*.before-migrate.*`), shell
#     snapshots (`shell-snapshots/`), and edit-history blobs (`file-history/`).
#     Rewriting THOSE would corrupt frozen-truth records.
#   - The set below is the audited set of files claude-code reads at runtime
#     to make decisions (hooks, plugin manifests, slash-commands, skill
#     bodies/scripts with literal paths). Two layers:
#       (a) Explicit-file list — files we know exist by name at runtime.
#       (b) Globbed-by-pattern  — commands/*.md and skills/**/* for files
#           whose existence depends on what the user has installed. Each
#           candidate is filtered through `grep -q source_dir` before sed
#           fires, so absent paths just no-op rather than churn empty files.
#
# Issue #1416 (Mini PR #1415 review #2): expanded from a 4-file explicit
# allowlist to (a)+(b) so user-installed slash-commands + third-party skills
# with hardcoded ~/.claude paths don't slip through. Still allowlist-shaped
# (vs full recursive sed) — keeps the contract grep'able and avoids touching
# the immutable surfaces above.
#
# Idempotency: rewriting destpath → destpath is a no-op, so this is safe to
# run repeatedly. Uses `|` as the sed delimiter so the `/` in paths doesn't
# need escaping; macOS user paths never contain `|`.
#
# BSD-sed compatibility: the `-i.bak <expr> file` form works on both macOS
# stock sed and GNU sed. We delete the .bak immediately after.
_rewrite_runtime_paths() {
  local source_dir="${SOURCE_CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  # Layer (a) — known-by-name runtime files.
  local runtime_files=(
    "$CLAUDE_DIR/settings.json"
    "$CLAUDE_DIR/plugins/installed_plugins.json"
    "$CLAUDE_DIR/plugins/known_marketplaces.json"
  )
  # Layer (b) — globbed patterns. Enumerate to absolute paths and append.
  # Commands: every .md file under commands/ (slash-command bodies may
  # embed literal hook/bash paths — openacp:handoff.md was the original
  # known case; this covers any future commands the user installs).
  if [ -d "$CLAUDE_DIR/commands" ]; then
    while IFS= read -r f; do
      runtime_files+=("$f")
    done < <(find "$CLAUDE_DIR/commands" -maxdepth 1 -type f -name '*.md' 2>/dev/null)
  fi
  # Skills: SKILL.md (the prompt) + any scripts/ folder shell/python/ts/js.
  # Skip plugins/cache/ (vendored plugin source; already covered by
  # known_marketplaces.json + installed_plugins.json rewriting).
  if [ -d "$CLAUDE_DIR/skills" ]; then
    while IFS= read -r f; do
      runtime_files+=("$f")
    done < <(find "$CLAUDE_DIR/skills" -type f \( -name 'SKILL.md' -o -name '*.sh' -o -name '*.py' -o -name '*.ts' -o -name '*.js' \) 2>/dev/null)
  fi
  local sed_script="s|${source_dir}/|${CLAUDE_DIR}/|g"
  local rewrote=0 unchanged=0 missing=0
  # Mini PR #1415 review #3 → issue #1417: support --from=<old-path> for
  # workspace-move scenarios. When set, rewrite BOTH source_dir → CLAUDE_DIR
  # (the default) AND from_dir → CLAUDE_DIR (the move-from). Two passes per
  # file so a file containing both old strings ends up fully re-pinned.
  #
  # Validation (Mini PR #1424 review #2): a typo or empty value would be
  # catastrophic. `--from=/` builds `s|/|$CLAUDE_DIR/|g` which rewrites
  # EVERY slash in every candidate file. Reject before sed:
  #   - empty / "/" / root-ish paths
  #   - paths that don't end in /.claude or /.claude-sutando (the only
  #     two shapes we ever rehome from)
  #   - paths that don't exist on disk (typo guard)
  local from_sed_script=""
  if [ -n "$FROM_DIR" ]; then
    local _from_norm="${FROM_DIR%/}"
    if [ -z "$_from_norm" ] || [ "$_from_norm" = "" ]; then
      echo "--from= cannot be empty or root" >&2
      return 1
    fi
    case "$_from_norm" in
      */.claude|*/.claude-sutando|*/.claude.*)
        : ;;  # accepted shapes
      *)
        echo "--from=$FROM_DIR rejected — must end in /.claude or /.claude-sutando" >&2
        echo "  (guard against typos: --from=/ would rewrite every slash in every candidate file)" >&2
        return 1
        ;;
    esac
    if [ ! -d "$_from_norm" ]; then
      echo "--from=$FROM_DIR rejected — directory does not exist" >&2
      echo "  (guard against typos: silent no-op vs explicit failure)" >&2
      return 1
    fi
    from_sed_script="s|${_from_norm}/|${CLAUDE_DIR}/|g"
  fi
  echo
  echo "  Re-pinning hardcoded paths:"
  echo "    source    : ${source_dir}/"
  if [ -n "$FROM_DIR" ]; then
    echo "    move-from : ${FROM_DIR%/}/  (also re-pinned to target)"
  fi
  echo "    target    : ${CLAUDE_DIR}/"
  echo "    candidates: ${#runtime_files[@]} files (explicit + commands/*.md + skills/**/{SKILL.md,scripts})"
  local f
  for f in "${runtime_files[@]}"; do
    if [ ! -f "$f" ]; then
      echo "    (skip — not present)  ${f#${CLAUDE_DIR}/}"
      missing=$((missing + 1))
      continue
    fi
    local touched=0
    if grep -q "${source_dir}/" "$f" 2>/dev/null; then
      sed -i.bak "$sed_script" "$f" && rm -f "$f.bak"
      touched=1
    fi
    if [ -n "$from_sed_script" ] && grep -q "${FROM_DIR%/}/" "$f" 2>/dev/null; then
      sed -i.bak "$from_sed_script" "$f" && rm -f "$f.bak"
      touched=1
    fi
    if [ $touched -eq 1 ]; then
      echo "    rewrote               ${f#${CLAUDE_DIR}/}"
      rewrote=$((rewrote + 1))
    else
      echo "    (unchanged)           ${f#${CLAUDE_DIR}/}"
      unchanged=$((unchanged + 1))
    fi
  done
  echo "  Summary: ${rewrote} rewrote, ${unchanged} unchanged, ${missing} not-present"
}

case "$MODE" in
  check)
    if managed_block_current; then
      echo "ok: $RC_FILE has the current claude-sutando managed block"
      exit 0
    elif managed_block_present; then
      echo "drift: $RC_FILE has the managed block but body differs from current script"
      echo "  --commit will rewrite the block in place."
      exit 1
    elif legacy_alias_present; then
      echo "legacy: $RC_FILE has a pre-managed-block claude-sutando alias"
      echo "  current : $(grep -E '^alias claude-sutando=' "$RC_FILE" | head -1)"
      echo "  --commit will remove the legacy alias and install the managed function block."
      exit 1
    else
      echo "absent: $RC_FILE has no claude-sutando configuration"
      exit 1
    fi
    ;;

  dry-run)
    echo "Target rc file        : $RC_FILE"
    echo "This checkout's dir   : $CLAUDE_DIR (smoke-test only — function resolves per-cwd)"
    if managed_block_current; then
      echo "Status                : already configured + body current (no-op on --commit)"
    elif managed_block_present; then
      echo "Status                : MANAGED BLOCK DRIFT — --commit will rewrite block in place"
    elif legacy_alias_present; then
      echo "Status                : LEGACY ALIAS PRESENT — --commit will remove + install managed block"
      echo "                        current : $(grep -E '^alias claude-sutando=' "$RC_FILE" | head -1)"
    else
      echo "Status                : not configured — --commit will append managed block"
    fi
    echo
    echo "Proposed block:"
    build_managed_block | sed 's/^/  /'
    echo
    echo "Rerun with --commit to apply."
    exit 0
    ;;

  commit | auto)
    # --auto guard: prompt once per host so startup.sh doesn't re-pester. The
    # sentinel lives in the workspace (per-host since hostnames differ; same
    # convention as state/cores/<hostname>.alive). If sentinel exists, exit 0
    # silently — user already saw the prompt and either accepted or declined.
    if [ "$MODE" = "auto" ]; then
      WORKSPACE="$(bash "$REPO_ROOT/scripts/sutando-config.sh" workspace)"
      SENTINEL="$WORKSPACE/state/.shell-setup-prompted-$(hostname -s)"
      if [ -e "$SENTINEL" ] && managed_block_current; then
        # Configured + current — fall through to commit (no-op) to stay idempotent.
        :
      elif [ -e "$SENTINEL" ]; then
        # Prompted before; user may have declined or script body has updated.
        # Exit silently — user has to re-run manually to pick up new function body.
        exit 0
      else
        # First time on this host. Print a one-screen explanation and ask.
        cat >&2 <<EOF
sutando-shell-setup: I'd like to add a 'claude-sutando' shell function to $RC_FILE.

The function resolves CLAUDE_CONFIG_DIR per-invocation based on the current
Sutando checkout (git rev-parse on cwd), so multiple Sutando instances on
this machine each map to their own workspace's .claude-sutando.

This checkout's resolved path: $CLAUDE_DIR

Reply 'y' to add now, anything else to skip (you can re-run manually anytime).
EOF
        # /dev/tty so this works under launchd / non-interactive parents that
        # don't have stdin attached but the user does have a terminal.
        if [ -r /dev/tty ]; then
          read -r -p "Add managed function block? [y/N] " reply < /dev/tty || reply=""
        else
          reply=""
        fi
        mkdir -p "$WORKSPACE/state"
        touch "$SENTINEL"
        reply_lc="$(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]')"
        if [ "$reply_lc" != "y" ]; then
          echo "sutando-shell-setup: skipped per user (sentinel set so this won't re-prompt on next startup)" >&2
          exit 2
        fi
      fi
    fi

    # mkdir THIS checkout's target so first `claude-sutando` here works cleanly.
    # Function resolves per-cwd at runtime, but bootstrapping this one is helpful.
    mkdir -p "$CLAUDE_DIR"

    # Mini PR #1415 review #6 → issue #1418: refuse if a user-defined
    # `claude-sutando` function exists OUTSIDE our managed block. Without
    # this guard, --commit appends our managed block alongside the user's
    # function — bash's last-definition-wins makes their function dead code
    # silently. `--force` overrides for users who explicitly want our block
    # to win — BUT prints an explicit "OVERWRITING" warning so a user who
    # passed --force without thinking about the consequence sees what's
    # about to happen (Mini PR #1424 review #3).
    if user_defined_function_present; then
      if [ "$FORCE" != "1" ]; then
        echo "sutando-shell-setup: refusing to overwrite — $RC_FILE already contains a user-defined" >&2
        echo "  \`claude-sutando\` function outside the managed marker block." >&2
        echo "  Adding our managed block here would make your function dead code" >&2
        echo "  (last definition wins in bash; the managed block goes after)." >&2
        echo "" >&2
        echo "  Options:" >&2
        echo "    1. Remove your existing claude-sutando definition from $RC_FILE, then re-run." >&2
        echo "    2. Pass --force to commit the managed block anyway (your function will be shadowed)." >&2
        echo "    3. Leave as-is if your function does what you want; --commit isn't required." >&2
        exit 1
      else
        # --force was passed. Print an explicit overwrite warning so the
        # consequence is on-screen, not buried in a man-page.
        echo "sutando-shell-setup: ⚠ --force — OVERWRITING user-defined claude-sutando function" >&2
        echo "  in $RC_FILE. Your function will be DEAD CODE after this commit (the managed" >&2
        echo "  block is appended after yours; bash's last-definition-wins resolves to ours)." >&2
        echo "  If this wasn't intended: Ctrl+C now, remove --force, and read the refusal" >&2
        echo "  message for the 3 options. Continuing in 2s if no interrupt..." >&2
        sleep 2 2>/dev/null || true
      fi
    fi

    # Apply (idempotent): handles four states.
    #   1. Managed block present + current → no-op
    #   2. Managed block present + drift   → rewrite block in-place
    #   3. Legacy alias present (no block) → remove alias + append fresh block
    #   4. Nothing                          → append fresh block
    if managed_block_current; then
      echo "sutando-shell-setup: $RC_FILE managed block already current (no-op)"
      exit 0
    fi

    new_block="$(build_managed_block)"

    if managed_block_present; then
      # State 2: replace the existing block (between markers) atomically.
      # awk's -v can't carry multi-line strings (treats embedded newlines as
      # string terminators), so we stage the new block to a tmpfile and let
      # awk read it line-by-line via getline at the BEGIN-marker boundary.
      tmp="$(mktemp -t sutando-shell-setup.XXXXXX)"
      block_tmp="$(mktemp -t sutando-shell-setup-block.XXXXXX)"
      printf '%s\n' "$new_block" > "$block_tmp"
      awk -v b="$MARKER_BEGIN" -v e="$MARKER_END" -v bf="$block_tmp" '
        $0 == b {
          while ((getline line < bf) > 0) print line
          close(bf)
          skipping=1
          next
        }
        skipping { if ($0 == e) skipping=0; next }
        { print }
      ' "$RC_FILE" > "$tmp"
      mv "$tmp" "$RC_FILE"
      rm -f "$block_tmp"
      echo "sutando-shell-setup: rewrote managed block in $RC_FILE"
    elif legacy_alias_present; then
      # State 3: strip the legacy alias line (single line) and append fresh.
      tmp="$(mktemp -t sutando-shell-setup.XXXXXX)"
      grep -vE '^alias claude-sutando=' "$RC_FILE" > "$tmp"
      mv "$tmp" "$RC_FILE"
      {
        echo
        echo "$new_block"
      } >> "$RC_FILE"
      echo "sutando-shell-setup: removed legacy alias + appended managed block to $RC_FILE"
    else
      # State 4: clean append.
      {
        echo
        echo "$new_block"
      } >> "$RC_FILE"
      echo "sutando-shell-setup: appended managed block to $RC_FILE"
    fi

    echo "Restart your shell or run: source $RC_FILE"
    exit 0
    ;;

  import)
    # Deprecation warning for the legacy alias --migrate. Per
    # `feedback_import_not_migrate`: the rename happened because "migrate"
    # implied a one-way structural change; the actual semantic is a
    # non-destructive copy with source preserved (= "import"). Keep the
    # alias working for one release; users get a single stderr nudge.
    if [ "$DEPRECATED_FLAG_MIGRATE" = "1" ]; then
      echo "⚠ --migrate is deprecated, use --import instead. (Behavior is identical.)" >&2
      echo "  The --migrate alias will be removed in a future release." >&2
    fi
    # Mirror ~/.claude → $CLAUDE_DIR via rsync. Non-destructive: source stays
    # intact so manual `claude` (without the alias) keeps working against the
    # original tree. Idempotent: rsync -a only re-copies changed files based
    # on mtime+size. Run again anytime to top up.
    #
    # Scope: copy EVERYTHING except `projects/*` — and within projects/, ONLY
    # this checkout's slug. Other projects/ subdirs are owner's transcripts
    # from OTHER claude-code work, irrelevant to this workspace. Saves disk +
    # keeps the new tree clean.
    #
    # Excludes:
    # - debug/, plugins/*/cache/, statsig/ — transient / regeneratable
    # - projects/<other-slug>/ — handled by the include/exclude pair below
    # SOURCE_CLAUDE_CONFIG_DIR = where the migration READS FROM (vanilla claude's
    # historical state location). Defaults to ~/.claude; override for CI / custom
    # installs / staging fixtures (e.g. SOURCE_CLAUDE_CONFIG_DIR=/tmp/seed-state).
    # Companion to CLAUDE_CONFIG_DIR (the destination where Sutando writes to).
    SOURCE_DIR="${SOURCE_CLAUDE_CONFIG_DIR:-$HOME/.claude}"
    if [ ! -d "$SOURCE_DIR" ]; then
      echo "sutando-shell-setup $INVOKED_AS: source $SOURCE_DIR doesn't exist; nothing to copy" >&2
      echo "  (set SOURCE_CLAUDE_CONFIG_DIR to override the migration source location)" >&2
      exit 1
    fi
    if ! command -v rsync >/dev/null 2>&1; then
      echo "sutando-shell-setup $INVOKED_AS: rsync not found on PATH; install it or copy manually" >&2
      exit 1
    fi

    mkdir -p "$CLAUDE_DIR"

    # Compute this checkout's project slug. Claude Code's encoding rule:
    # replace `/` with `-` in the absolute cwd. So /Users/x/repo becomes
    # -Users-x-repo.
    THIS_PROJECT_SLUG="$(printf '%s' "$REPO_ROOT" | tr '/' '-')"

    # Build the include set by enumerating candidate slugs in ~/.claude/projects/
    # and confirming each one against the filesystem. For a slug starting with
    # `${THIS_PROJECT_SLUG}-`, the remainder decodes back to a path:
    #   `--` → `/-`  (the encoded leading-dash dir name)
    #   `-`  → `/`   (path separator)
    # We then check if `${REPO_ROOT}/${decoded}` is a real directory under
    # this checkout — if yes, it's a TRUE SUBDIR variant (the user cd'd into
    # a subdir and ran claude there); if no, it's a sibling repo with a
    # similar name (`sutando-plus`, `sutando-v07`, etc.) and we skip it.
    #
    # The exact slug is always included regardless of filesystem state.
    INCLUDE_SLUGS=("$THIS_PROJECT_SLUG")
    if [ -d "$SOURCE_DIR/projects" ]; then
      for entry in "$SOURCE_DIR/projects/"*; do
        [ -d "$entry" ] || continue
        slug="$(basename "$entry")"
        case "$slug" in
          "$THIS_PROJECT_SLUG")
            continue  # already in the set
            ;;
          "${THIS_PROJECT_SLUG}-"*)
            suffix="${slug#${THIS_PROJECT_SLUG}-}"
            # Decode: `--` → `/-` first (preserves leading-dash dirnames),
            # then remaining `-` → `/` (path separators).
            decoded="$(printf '%s' "$suffix" | sed 's|--|/-|g; s|-|/|g')"
            if [ -d "$REPO_ROOT/$decoded" ]; then
              INCLUDE_SLUGS+=("$slug")
            fi
            ;;
        esac
      done
    fi

    # Build rsync filter list. Include each confirmed slug + its contents,
    # then exclude all other projects/*. Filter ordering matters — rsync uses
    # first-match semantics so includes must precede the matching exclude.
    RSYNC_FILTERS=(--include='projects/')
    for s in "${INCLUDE_SLUGS[@]}"; do
      RSYNC_FILTERS+=(--include="projects/$s/" --include="projects/$s/***")
    done
    RSYNC_FILTERS+=(
      --exclude='projects/*'
      --exclude='debug/'
      --exclude='plugins/*/cache/'
      --exclude='statsig/'
      # NOTE: channels/*/*.env (bot tokens) + channels/*/access.json.bak*
      # are intentionally COPIED in this version. Owner directive 2026-06-03
      # (PR #1424 import-UX thread): option (B) "copy + warn" — bridges work
      # immediately post-import, user gets an explicit vault-sync warning
      # at end-of-import flagging the secret files that were copied. The
      # earlier exclude (Mini PR #1415 review #4) was the secret-safety
      # baseline; this is the user-experience layer on top, accepting the
      # vault-sync coordination burden in exchange for zero-friction
      # bridges. See the post-rsync warning block below.
      # Runtime artifacts that won't survive a rehome anyway.
      --exclude='*.sock'
      --exclude='*.pid'
      # Weight-reduction excludes (owner directive 2026-06-03, PR #1424
      # import-UX thread). These are heavy claude-code state artifacts that
      # have NO Sutando dependency and either auto-regenerate or are
      # rarely-used past-session data. Saves ~150 MB / 3700+ files on a
      # typical owner profile.
      --exclude='shell-snapshots/'   # zsh snapshots, auto-regen per invocation
      --exclude='history.jsonl'      # per-session command history, rebuilds
      --exclude='file-history/'      # 102 MB / 3618 files; past-session edit-undo blobs
    )

    echo "sutando-shell-setup $INVOKED_AS"
    echo "  Source           : $SOURCE_DIR"
    echo "  Target           : $CLAUDE_DIR"
    echo "  Mode             : non-destructive copy (source preserved)"
    echo "  Project scope    : ${#INCLUDE_SLUGS[@]} confirmed project slug(s) — exact + sub-folder variants:"
    for s in "${INCLUDE_SLUGS[@]}"; do
      echo "                     • $s"
    done
    echo

    # Dry-run preview first so user sees what would change. Stage to a tmpfile
    # then head from it — piping rsync directly to `head` under `set -o pipefail`
    # propagates SIGPIPE when head closes early, surfacing as exit 141/255.
    _preview_tmp="$(mktemp -t sutando-shell-setup-preview.XXXXXX)"
    rsync -a --dry-run --itemize-changes \
      "${RSYNC_FILTERS[@]}" \
      "$SOURCE_DIR/" "$CLAUDE_DIR/" > "$_preview_tmp"
    head -50 "$_preview_tmp"
    rm -f "$_preview_tmp"

    echo
    # `read ... < /dev/tty` is the right pattern when stdin is /dev/null but
    # the user has a controlling terminal. We probe /dev/tty's openability in
    # a subshell first — `[ -r /dev/tty ]` returns true even when /dev/tty
    # exists as a device node but isn't "configured" (CI / headless), where
    # the subsequent `< /dev/tty` redirect would emit a parse-time error.
    reply=""
    if ( exec </dev/tty ) 2>/dev/null; then
      read -r -p "Proceed with copy? [y/N] " reply < /dev/tty || reply=""
    else
      reply="y"
      echo "(non-interactive — proceeding)"
    fi

    # Lowercase reply for the y-test in a portable way (avoid bash 4+ ${var,,}
    # which fails on macOS's stock bash 3.2).
    reply_lc="$(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]')"
    if [ "$reply_lc" != "y" ]; then
      echo "sutando-shell-setup $INVOKED_AS: aborted by user"
      exit 2
    fi

    # `--stats` (legacy form, works on macOS's stock rsync 2.6.9 from 2006)
    # instead of `--info=stats1` (rsync 3.1+, brew/Linux). Same end result.
    rsync -a --stats \
      "${RSYNC_FILTERS[@]}" \
      "$SOURCE_DIR/" "$CLAUDE_DIR/"

    # Post-copy: rewrite hardcoded SOURCE_DIR → CLAUDE_DIR in runtime files.
    # Without this, copied settings.json / plugin manifests / slash-commands
    # still point at the legacy path, and silently break the moment legacy
    # ~/.claude is pruned. See _rewrite_runtime_paths() for scope + rationale.
    _rewrite_runtime_paths

    echo
    echo "sutando-shell-setup $INVOKED_AS: done."
    echo "  ${SOURCE_DIR} is unchanged. To prune later, verify the new tree works first, then:"
    echo "    rm -rf '${SOURCE_DIR}/projects/${THIS_PROJECT_SLUG}/'  # only this project's slug"
    # Vault-sync warning (option (B) from the import-UX thread 2026-06-03):
    # bot tokens + auth backups were copied this time (no exclude). The
    # bridges will work immediately, but the M2 vault sync engine — when
    # it lands — will include these files in the synced bundle unless the
    # user adds an exclude. Surface the exact paths so the user can add
    # them to their vault sync exclude policy.
    #
    # Glob note: bash's `*` does NOT match dot-prefixed names by default
    # (unlike rsync's `*` which does). So `channels/*/*.env` would catch
    # `relay-client.env` but MISS the canonical `channels/discord/.env`.
    # Enumerate both patterns explicitly: `channels/*/.env` (dotfile form,
    # the standard bot-token shape) + `channels/*/*.env` (any other .env
    # variant like relay-client.env).
    # Redirect to stderr (Lucy PR #1429 review nit): the vault-sync warning
    # is the highest-stakes line in this script (data-loss / secret-leak
    # prevention), and the source-missing / rsync-missing errors above
    # already go to stderr. Parity dictates this warning should too — and
    # it ensures the warning is visible even if the user pipes stdout
    # somewhere (e.g. `sutando-shell-setup --import > install.log`).
    _secrets_found=0
    for env in "${CLAUDE_DIR}/channels/"*/.env "${CLAUDE_DIR}/channels/"*/*.env "${CLAUDE_DIR}/channels/"*/access.json.bak*; do
      [ -f "$env" ] || continue
      if [ "$_secrets_found" = "0" ]; then
        echo >&2
        echo "  ⚠ Imported host-specific secrets — review before vault sync:" >&2
        _secrets_found=1
      fi
      _mode="$(stat -f '%Mp%Lp' "$env" 2>/dev/null || stat -c '%a' "$env" 2>/dev/null || echo '?')"
      echo "      ${env#${CLAUDE_DIR}/}  (mode $_mode)" >&2
    done
    if [ "$_secrets_found" = "1" ]; then
      echo >&2
      echo "    These files contain bot tokens / stale auth state and are host-coupled." >&2
      echo "    If you sync this workspace to a remote (M2 vault), add an exclude rule" >&2
      echo "    for channels/*/.env + channels/*/*.env + channels/*/access.json.bak*" >&2
      echo "    in your sync policy BEFORE the next sync to avoid leaking secrets remotely." >&2
    fi
    exit 0
    ;;

  repair-paths)
    # Standalone path re-pin. Runs ONLY the sed pass — no rsync, no project
    # discovery, no preview/confirm. Use case: workspace moved (mv / rename /
    # config-driven relocate) and previously-rewritten paths in the migrated
    # tree are now stale. Re-running --import would be heavy (full rsync from
    # ~/.claude) and unnecessary if no source-side changes have happened.
    #
    # Also useful immediately after upgrading to a version of this script that
    # adds the rewrite pass — re-pins a previously-migrated tree without a
    # fresh rsync.
    if [ ! -d "$CLAUDE_DIR" ]; then
      echo "sutando-shell-setup --repair-paths: target $CLAUDE_DIR doesn't exist; run --import first" >&2
      exit 1
    fi
    echo "sutando-shell-setup --repair-paths"
    _rewrite_runtime_paths
    echo
    echo "sutando-shell-setup --repair-paths: done."
    exit 0
    ;;
esac
