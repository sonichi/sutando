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
#   bash scripts/sutando-shell-setup.sh           # dry-run: print proposed line + target rc
#   bash scripts/sutando-shell-setup.sh --commit  # append to rc file (idempotent)
#   bash scripts/sutando-shell-setup.sh --auto    # one-shot prompt path used by startup.sh
#   bash scripts/sutando-shell-setup.sh --check   # exit 0 if alias present + path matches; 1 otherwise
#   bash scripts/sutando-shell-setup.sh --migrate # rsync ~/.claude → <workspace>/.claude-sutando (idempotent, non-destructive)
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

# Parse args (one-shot; multiple flags are ignored — first wins).
for arg in "$@"; do
  case "$arg" in
    --commit)  MODE="commit"; break ;;
    --auto)    MODE="auto"; break ;;
    --check)   MODE="check"; break ;;
    --migrate) MODE="migrate"; break ;;
    --help|-h)
      sed -n '1,40p' "$0" | grep -E '^#' | sed 's/^# *//'
      exit 0
      ;;
    *) echo "sutando-shell-setup: unknown arg '$arg' (try --help)" >&2; exit 1 ;;
  esac
done

# Resolve THIS repo's target path via the config loader. Used for --check
# (smoke-test that THIS checkout resolves cleanly) and for --migrate. The
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
  local ccd
  ccd="$(bash "$repo_root/scripts/sutando-config.sh" claude-sutando-config-dir)" || return 1
  mkdir -p "$ccd"
  CLAUDE_CONFIG_DIR="$ccd" command claude "$@"
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

  migrate)
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
    SOURCE_DIR="$HOME/.claude"
    if [ ! -d "$SOURCE_DIR" ]; then
      echo "sutando-shell-setup --migrate: source $SOURCE_DIR doesn't exist; nothing to copy" >&2
      exit 1
    fi
    if ! command -v rsync >/dev/null 2>&1; then
      echo "sutando-shell-setup --migrate: rsync not found on PATH; install it or copy manually" >&2
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
    )

    echo "sutando-shell-setup --migrate"
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
      echo "sutando-shell-setup --migrate: aborted by user"
      exit 2
    fi

    # `--stats` (legacy form, works on macOS's stock rsync 2.6.9 from 2006)
    # instead of `--info=stats1` (rsync 3.1+, brew/Linux). Same end result.
    rsync -a --stats \
      "${RSYNC_FILTERS[@]}" \
      "$SOURCE_DIR/" "$CLAUDE_DIR/"

    echo
    echo "sutando-shell-setup --migrate: done."
    echo "  ${SOURCE_DIR} is unchanged. To prune later, verify the new tree works first, then:"
    echo "    rm -rf '${SOURCE_DIR}/projects/${THIS_PROJECT_SLUG}/'  # only this project's slug"
    exit 0
    ;;
esac
