#!/bin/bash
# sutando-config-hooks.sh — hook helper for the per-runtime CLAUDE_CONFIG_DIR
# migration (Option D from #design 2026-06-07 design discussion).
#
# Background: when Sutando migrates a user from `~/.claude/` to a per-runtime
# `$CLAUDE_CONFIG_DIR` (typically `<workspace>/.claude-sutando/`), hooks that
# reference literal `~/.claude/hooks/...` paths in their `command:` strings
# can't move cleanly. Owner's design (Option D, 01:38Z): drop those hooks at
# migration time and (i) auto-re-install Sutando-owned hooks pointing at the
# correct workspace paths, and (ii) print a notice listing dropped non-Sutando
# entries so the user can re-add manually.
#
# Sutando's own hooks are not installed by this script: they register only in the
# core's launch settings (src/agent/claude/cli/build-core-settings.mjs).
#
# Subcommands:
#   migration-notice <old-settings.json> <new-settings.json>
#       Diff hook command strings between old and new. Print user-facing notice
#       listing entries that were dropped during migration (i.e. were in old
#       but not new), filtered to non-Sutando ones — those need manual re-add.
#
# Usage examples:
#   bash scripts/sutando-config-hooks.sh migration-notice ~/.claude/settings.json "$CLAUDE_CONFIG_DIR/settings.json"
#
# Exit codes:
#   0 — success
#   1 — operation failed
#   2 — jq missing
#   3 — invalid args

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if ! command -v jq >/dev/null 2>&1; then
  echo "error: jq is required" >&2
  exit 2
fi

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

  # Filter out Sutando-owned hooks: those register at core launch, not in this file.
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
    echo "  For Sutando-owned hooks (registered at core launch): no action needed."
  fi

  rm -f "$_tmp_old" "$_tmp_new" "$_tmp_dropped" "$_tmp_third"
}

# Main dispatch
MODE="${1:-}"; shift || true
case "$MODE" in
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
