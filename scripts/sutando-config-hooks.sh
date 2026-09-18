#!/bin/bash
# sutando-config-hooks.sh — hook helper for the per-runtime CLAUDE_CONFIG_DIR
# migration (Option D from #design 2026-06-07 design discussion).
#
# A `~/.claude/` → `$CLAUDE_CONFIG_DIR` migration can't carry literal
# `~/.claude/hooks/...` command strings, so this re-installs Sutando's own
# and notices any dropped non-Sutando entries for manual re-add.
#
# Parametric on the target settings.json path — unlike the two existing
# installers, which each write one fixed file, this one can target any.
#
# Subcommands:
#   detect-missing <settings.json>
#       Returns 0 if all known Sutando hooks are present, 1 if any missing.
#       Prints a list of missing hooks to stderr.
#
#   install <settings.json> [--with-catchup-hook] [--with-project-hooks]
#       Idempotent re-install of Sutando hook entries. Default: catchup hook
#       only (the SessionEnd → session-handoff.sh entry from #1056). Add
#       --with-project-hooks to also install the PreCompact + Stop entries
#       from src/install-claude-hooks.sh.
#
#   migration-notice <old-settings.json> <new-settings.json>
#       Diff hook command strings between old and new. Print user-facing notice
#       listing entries that were dropped during migration (i.e. were in old
#       but not new), filtered to non-Sutando ones — those need manual re-add.
#
# Usage examples:
#   bash scripts/sutando-config-hooks.sh detect-missing "$CLAUDE_CONFIG_DIR/settings.json"
#   bash scripts/sutando-config-hooks.sh install "$CLAUDE_CONFIG_DIR/settings.json"
#   bash scripts/sutando-config-hooks.sh migration-notice ~/.claude/settings.json "$CLAUDE_CONFIG_DIR/settings.json"
#
# Exit codes:
#   0 — success
#   1 — operation failed (missing hooks for detect; jq edit failed for install)
#   2 — jq missing
#   3 — invalid args

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if ! command -v jq >/dev/null 2>&1; then
  echo "error: jq is required" >&2
  exit 2
fi

# Canonical command forms, matched against what the two installers write.
# --with-project-hooks is named for its origin, not its scope: both hook sets are core-session (CLAUDE_CONFIG_DIR), same as this script's migration target.

# install-claude-hooks.sh owns the command strings; this asks it for them. Exit
# codes differ: 1 = genuinely absent (safe to guess a fallback); 2 = present but this hook didn't come back — never guess, only skip or propagate.
_installer_hook_command() {  # $1 = event, $2 = marker
  local repo="${SUTANDO_REPO_DIR:-$REPO_DIR}" line out
  [ -f "$repo/src/install-claude-hooks.sh" ] || return 1
  out="$(bash "$repo/src/install-claude-hooks.sh" --print-hooks 2>/dev/null)" || return 2
  while IFS= read -r line; do
    case "$line" in
      "$1|$2|"*) printf '%s\n' "${line#*|*|}"; return 0 ;;
    esac
  done <<< "$out"
  return 2
}

# Propagates _installer_hook_command's rc=2 (installer present but failed)
# instead of masking it as a guessable absence — callers must check the exit code.
_catchup_hook_command() {
  local repo="${SUTANDO_REPO_DIR:-$REPO_DIR}" cmd rc
  cmd="$(_installer_hook_command SessionEnd "src/session-handoff.sh")"; rc=$?
  case "$rc" in
    0) printf '%s\n' "$cmd" ;;
    1) printf '%s\n' "bash \"$repo/src/session-handoff.sh\" \"\$TRANSCRIPT_PATH\"" ;;
    *) return "$rc" ;;
  esac
}

# Resolves the workspace via the shared config helper, not a hardcoded
# $REPO_DIR/workspace guess — sutando.config.local.json can relocate it.
_workspace_dir() {
  local dir helper out err
  dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  helper="$dir/sutando-config.sh"
  if [ ! -f "$helper" ]; then
    printf '%s\n' "$REPO_DIR/workspace"
    return 0
  fi
  # Stdout is the value, stderr is warnings — a success-plus-warning run
  # must not bake the warning into the resolved path (resolver contract).
  err="$(mktemp)"
  out="$(bash "$helper" workspace 2>"$err")"
  local rc=$?
  if [ "$rc" -ne 0 ] || [ -z "$out" ]; then
    echo "sutando-config-hooks: workspace resolution failed: $(cat "$err")" >&2
    rm -f "$err"
    return 1
  fi
  [ -s "$err" ] && cat "$err" >&2
  rm -f "$err"
  printf '%s\n' "$out"
}

_sutando_hook_manifest() {
  # Path to the manifest that installers write and _known_sutando_substrings reads.
  # Routes via the M0 claude-home-path helper (#1536) so $CLAUDE_CONFIG_DIR is
  # honored and the deprecation banner fires on fallback.
  bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/sutando-config.sh" claude-home-path sutando-hook-manifest.json
}

_known_sutando_substrings() {
  # Merge the manifest's registered substrings with the hardcoded fallback list,
  # deduped. Manifest is written by install-claude-hooks.sh and
  # skills/catchup-after-startup/scripts/install-hook.sh on each install run —
  # but a host where only some installers have run yet (e.g. catchup but not
  # project hooks) would have an incomplete manifest. Merging with the
  # hardcoded fallback ensures the full known-Sutando set is always recognized,
  # so migration-notice never false-positively flags a real Sutando hook as
  # "dropped third-party" on partial-install hosts.
  # See: https://github.com/sonichi/sutando/issues/1502
  local manifest; manifest="$(_sutando_hook_manifest)"
  local from_manifest=""
  if [ -f "$manifest" ]; then
    from_manifest="$(jq -r '.sutando_owned_hooks // [] | .[].command_substring' "$manifest" 2>/dev/null || true)"
  fi
  # Hardcoded fallback: the 5 stable substrings known at #1500 ship time.
  # New installers should write to the manifest in addition (not instead of)
  # the hardcoded list. The merge dedupes so manifest + hardcoded overlap is fine.
  {
    [ -n "$from_manifest" ] && echo "$from_manifest"
    cat <<EOF
src/session-handoff.sh
src/check-pending-tasks.sh
src/watch-tasks-stream.sh
sutando/src/
sutando-plus/scripts/sync-workspace.sh
EOF
  } | awk 'NF && !seen[$0]++'
}

# _write_hook_manifest <id> <command_substring> <installed_by>
# Idempotent: no-op if <id> is already present in the manifest.
# Called by install-claude-hooks.sh and install-hook.sh after installing each hook.
_write_hook_manifest() {
  local id="$1" substring="$2" installed_by="$3"
  local manifest; manifest="$(_sutando_hook_manifest)"
  mkdir -p "$(dirname "$manifest")"
  [ -f "$manifest" ] || printf '{"version":1,"sutando_owned_hooks":[]}\n' > "$manifest"
  # Validate before edit (don't clobber a corrupt manifest with a worse one).
  if ! jq empty "$manifest" 2>/dev/null; then
    echo "  [manifest] $manifest is not valid JSON — skipping write (run jq-check to diagnose)" >&2
    return 1
  fi
  # Idempotent: skip if id already present.
  if jq -e --arg id "$id" '.sutando_owned_hooks // [] | map(.id == $id) | any' "$manifest" >/dev/null 2>&1; then
    return 0
  fi
  local tmp; tmp="$(mktemp)"
  jq --arg id "$id" --arg sub "$substring" --arg by "$installed_by" \
    '.sutando_owned_hooks //= [] | .sutando_owned_hooks += [{"id":$id,"command_substring":$sub,"installed_by":$by}]' \
    "$manifest" > "$tmp" && mv "$tmp" "$manifest"
  echo "  [manifest] registered hook $id → $manifest"
}

_validate_json() {
  # Per Mini's PR #1500 review: previously the script silently `|| true`'d past
  # malformed JSON, hiding real corruption (manual edits gone wrong, partial
  # writes). This validator gives a clean error message instead.
  local file="$1"
  if ! jq empty "$file" 2>/dev/null; then
    echo "error: $file is not valid JSON — fix or back up + re-init" >&2
    echo "  diagnostic:" >&2
    jq empty "$file" 2>&1 | sed 's/^/    /' >&2 || true
    return 1
  fi
  return 0
}

cmd_detect_missing() {
  local settings="${1:-}"
  if [ -z "$settings" ]; then
    echo "usage: detect-missing <settings.json>" >&2
    exit 3
  fi
  if [ ! -f "$settings" ]; then
    echo "detect-missing: $settings — file not found" >&2
    echo "  (treating as missing all Sutando hooks)" >&2
    exit 1
  fi
  if ! _validate_json "$settings"; then
    return 1
  fi
  local want_cmd
  if ! want_cmd="$(_catchup_hook_command)"; then
    echo "detect-missing: installer present but failed to yield the catchup hook command — refusing to guess" >&2
    return 1
  fi
  # Check SessionEnd entries for the catchup hook (the one we auto-install).
  local found
  found="$(jq --arg cmd "$want_cmd" '
    [.hooks.SessionEnd // [] | .[] | .hooks // [] | .[] | select(.type=="command" and .command==$cmd)] | length
  ' "$settings" 2>/dev/null || echo 0)"
  if [ "${found:-0}" -ge 1 ]; then
    echo "detect-missing: ok — catchup hook present"
    return 0
  else
    echo "detect-missing: MISSING — SessionEnd catchup hook not found in $settings" >&2
    echo "  expected command: $want_cmd" >&2
    return 1
  fi
}

cmd_install() {
  local settings="${1:-}"; shift || true
  if [ -z "$settings" ]; then
    echo "usage: install <settings.json> [--with-catchup-hook] [--with-project-hooks]" >&2
    exit 3
  fi
  local with_catchup=1  # default on
  local with_project=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --with-catchup-hook) with_catchup=1; shift ;;
      --no-catchup-hook) with_catchup=0; shift ;;
      --with-project-hooks) with_project=1; shift ;;
      *) echo "install: unknown flag $1" >&2; exit 3 ;;
    esac
  done

  mkdir -p "$(dirname "$settings")"
  [ -f "$settings" ] || echo '{}' > "$settings"
  # Validate JSON before any jq edit — per Mini's PR #1500 review, malformed
  # input would previously cause `mv tmp settings` to silently overwrite with
  # a partial state.
  if ! _validate_json "$settings"; then
    exit 1
  fi

  if [ "$with_catchup" = "1" ]; then
    local cmd
    if ! cmd="$(_catchup_hook_command)"; then
      echo "install: installer present but failed to yield the catchup hook command — refusing to guess" >&2
      return 1
    fi
    # Idempotent jq edit: skip if an equivalent command already exists.
    local exists
    exists="$(jq --arg cmd "$cmd" '
      [.hooks.SessionEnd // [] | .[] | .hooks // [] | .[] | select(.type=="command" and .command==$cmd)] | length
    ' "$settings")"
    if [ "${exists:-0}" -ge 1 ]; then
      echo "install: SessionEnd catchup hook already present — skipping"
    else
      local tmp; tmp="$(mktemp)"
      jq --arg cmd "$cmd" '
        .hooks //= {} | .hooks.SessionEnd //= [] | .hooks.SessionEnd += [{
          "hooks": [{"type": "command", "command": $cmd}]
        }]
      ' "$settings" > "$tmp"
      mv "$tmp" "$settings"
      echo "install: added SessionEnd catchup hook → $settings"
    fi
  fi

  if [ "$with_project" = "1" ]; then
    # Each of the 3 hooks below only guesses a fallback on rc=1 (installer
    # absent); rc=2 (failed or omitted) skips it. Use `rc=0; x="$(fn)" || rc=$?`, never bare — under `set -e` a bare failing assignment exits before any case runs.
    local pre1 pre2 stop1 failed=0 rc
    if [ "${SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE:-0}" = "1" ]; then
      pre1=""  # opt-out: never fabricate a fallback for the omitted hook
    else
      rc=0; pre1="$(_installer_hook_command PreCompact "logs/conversations/")" || rc=$?
      case "$rc" in
        0) ;;
        1)
          local ws
          if ws="$(_workspace_dir)"; then
            pre1="bash \"$REPO_DIR/src/archive-transcript.sh\" \"$ws/logs/conversations/\""
          else
            echo "install: cannot resolve the workspace for the archive-hook fallback" >&2
            failed=1; pre1=""
          fi
          ;;
        *)
          echo "install: could not resolve the PreCompact archive hook command — refusing to guess" >&2
          failed=1; pre1="" ;;
      esac
    fi
    rc=0; pre2="$(_installer_hook_command PreCompact "src/session-handoff.sh")" || rc=$?
    case "$rc" in
      0) ;;
      1) pre2="bash \"$REPO_DIR/src/session-handoff.sh\" \"\$TRANSCRIPT_PATH\"" ;;
      *) echo "install: could not resolve the PreCompact session-handoff hook command — refusing to guess" >&2
         failed=1; pre2="" ;;
    esac
    rc=0; stop1="$(_installer_hook_command Stop "src/check-pending-tasks.sh")" || rc=$?
    case "$rc" in
      0) ;;
      1) stop1="bash \"$REPO_DIR/src/check-pending-tasks.sh\"" ;;
      *) echo "install: could not resolve the Stop check-pending-tasks hook command — refusing to guess" >&2
         failed=1; stop1="" ;;
    esac
    for spec in "PreCompact|$pre1" "PreCompact|$pre2" "Stop|$stop1"; do
      local event="${spec%%|*}"
      local cmd="${spec#*|}"
      [ -n "$cmd" ] || continue   # omitted or unresolved — nothing to install
      local exists
      exists="$(jq --arg ev "$event" --arg cmd "$cmd" '
        [.hooks[$ev] // [] | .[] | .hooks // [] | .[] | select(.type=="command" and .command==$cmd)] | length
      ' "$settings")"
      if [ "${exists:-0}" -ge 1 ]; then
        echo "install: $event project hook already present — skipping"
      else
        local tmp; tmp="$(mktemp)"
        jq --arg ev "$event" --arg cmd "$cmd" '
          .hooks //= {} | .hooks[$ev] //= [] | .hooks[$ev] += [{
            "hooks": [{"type": "command", "command": $cmd}]
          }]
        ' "$settings" > "$tmp"
        mv "$tmp" "$settings"
        echo "install: added $event project hook → $settings"
      fi
    done
    [ "$failed" = "0" ] || return 1
  fi
}

cmd_migration_notice() {
  local old="${1:-}"; local new="${2:-}"
  if [ -z "$old" ] || [ -z "$new" ]; then
    echo "usage: migration-notice <old-settings.json> <new-settings.json>" >&2
    exit 3
  fi
  if [ ! -f "$old" ]; then
    # No old settings.json — nothing to drop.
    return 0
  fi
  # Per Mini's PR #1500 review: validate input JSON instead of silently
  # `|| true`'ing through malformed files. Warn-but-continue here (vs hard
  # error in detect/install) since migration-notice is informational and
  # malformed input simply means "we can't compute the diff cleanly."
  if ! _validate_json "$old"; then
    echo "  (migration-notice: skipping — old settings.json malformed)" >&2
    return 0
  fi
  if [ -f "$new" ] && ! _validate_json "$new"; then
    echo "  (migration-notice: skipping — new settings.json malformed)" >&2
    return 0
  fi
  # Build comparable command strings: <event>|<command>.
  local _flatten_jq='
    [(.hooks // {}) | to_entries[] | .key as $ev | (.value // [] | .[] | .hooks // [] | .[] | select(.type=="command") | "\($ev)|\(.command)")] | sort | .[]
  '
  local _tmp_old _tmp_new
  _tmp_old="$(mktemp)"; _tmp_new="$(mktemp)"
  jq -r "$_flatten_jq" "$old" > "$_tmp_old" 2>/dev/null || true
  if [ -f "$new" ]; then
    jq -r "$_flatten_jq" "$new" > "$_tmp_new" 2>/dev/null || true
  else
    : > "$_tmp_new"
  fi
  # Lines in old but not new = dropped.
  local _tmp_dropped; _tmp_dropped="$(mktemp)"
  comm -23 "$_tmp_old" "$_tmp_new" > "$_tmp_dropped" || true

  # Filter out Sutando-owned hooks (we know those got migrated by the install
  # subcommand or are simply re-installable from canonical sources).
  local _tmp_third; _tmp_third="$(mktemp)"
  local sutando_pat; sutando_pat="$(_known_sutando_substrings | paste -sd '|' -)"
  grep -vE "$sutando_pat" "$_tmp_dropped" > "$_tmp_third" || true

  if [ -s "$_tmp_third" ]; then
    echo
    echo "~ Hooks dropped during migration (literal ~/.claude/ paths or third-party scripts can't move to workspace automatically):"
    while IFS='|' read -r ev cmd; do
      [ -z "$ev" ] && continue
      # Truncate long commands for display.
      local disp="$cmd"
      [ "${#disp}" -gt 120 ] && disp="${disp:0:117}..."
      echo "    - $ev: $disp"
    done < "$_tmp_third"
    echo "  Re-add manually by editing $new under \"hooks\" — match the existing entry shape."
    echo "  For Sutando-owned hooks (auto-restored): no action needed."
  fi

  rm -f "$_tmp_old" "$_tmp_new" "$_tmp_dropped" "$_tmp_third"
}

# Main dispatch
MODE="${1:-}"; shift || true
case "$MODE" in
  detect-missing) cmd_detect_missing "$@" ;;
  install) cmd_install "$@" ;;
  migration-notice) cmd_migration_notice "$@" ;;
  write-manifest) _write_hook_manifest "$@" ;;
  show-manifest)
    manifest="$(_sutando_hook_manifest)"
    if [ -f "$manifest" ]; then cat "$manifest"; else echo "(manifest not found at $manifest)"; fi
    ;;
  ""|--help|-h|help)
    cat <<EOF
sutando-config-hooks.sh — hook helper for per-runtime CLAUDE_CONFIG_DIR migration

Subcommands:
  detect-missing <settings.json>
  install <settings.json> [--with-catchup-hook] [--with-project-hooks]
  migration-notice <old-settings.json> <new-settings.json>
  write-manifest <id> <command_substring> <installed_by>
  show-manifest

See file header for design context (Option D from #design 2026-06-07).
See https://github.com/sonichi/sutando/issues/1502 for manifest design.
EOF
    [ "$MODE" = "" ] && exit 3 || exit 0
    ;;
  *)
    echo "sutando-config-hooks: unknown subcommand: $MODE" >&2
    echo "Try: bash $0 --help" >&2
    exit 3
    ;;
esac
