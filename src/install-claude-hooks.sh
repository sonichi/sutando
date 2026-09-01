#!/bin/bash
# install-claude-hooks.sh — idempotent install of Sutando-owned project-level
# Claude Code hooks (PreCompact + Stop).
#
# Per `feedback_claude_code_hook_scoping`: sutando hooks belong at PROJECT-level
# `.claude/settings.json` (gitignored, per-machine), NOT user-level
# `~/.claude/settings.json` — they only fire when Claude runs in this project
# context, not in unrelated sessions.
#
# Hooks installed (4):
#   PreCompact  → cp $TRANSCRIPT_PATH ~/Desktop/sutando-conversations/...
#   PreCompact  → bash src/session-handoff.sh "$TRANSCRIPT_PATH"
#   SessionEnd  → bash src/session-handoff.sh "$TRANSCRIPT_PATH"
#   Stop        → bash src/check-pending-tasks.sh
#
# The SessionEnd → session-handoff.sh hook fires session-state.md on a clean
# exit (⌘Q / crash) too, not just on PreCompact — so the last session's tail
# isn't lost when no compaction happened before close. It was previously
# installed (user-level) by catchup-after-startup's install-hook.sh; that skill
# was removed (#1737-equivalent), so the install moves here, at the correct
# PROJECT-level scope (per feedback_claude_code_hook_scoping).
#
# Historical note: a 4th hook (`Stop` → watcher-cleanup PID kill, the #1065
# fix) was removed 2026-05-24.  Claude Code's `Stop` event fires on
# turn-end (after every assistant response), NOT session-end — so the
# PID-kill block killed the live Monitor watcher every turn, triggering an
# exit-143 + Monitor-restart cycle.  Watcher orphan-cleanup is handled by
# `reap_stale_task_watcher` in `src/startup-runtime.sh`, which runs at every
# session start.  See the original #1061 /
# #1063 / #1065 thread for the orphan-watcher background.
#
# Idempotent: re-running is safe.  Existing hook entries with the same
# command string are detected per-hook and not re-added.  jq + tmp+mv for
# atomic write.
#
# Deprecated hooks: this script ALSO removes hooks listed in
# `DEPRECATED_HOOKS` (substring match on the command).  Re-running the
# installer is now a full migration tool — existing installs of an old
# hook get auto-uninstalled on next run.  See #1083 follow-up for the
# motivation: the watcher-kill Stop hook from #1065 stayed in everyone's
# settings.json after #1083 dropped it from `HOOKS=(...)` because the
# original installer was add-only.  Re-running this version of the
# installer removes the deprecated entry without manual jq.
#
# Usage:
#   bash src/install-claude-hooks.sh
#
# Exit codes:
#   0 — all current hooks present + all deprecated removed after run
#   1 — settings.json malformed / jq edit failed
#   2 — jq missing (required for atomic edit)

set -u

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS="$REPO_DIR/.claude/settings.json"

# Hook specs: each line is "<event>|<command>".  Order = install order.
# $REPO_DIR is expanded HERE, at install time, so the command written into
# settings.json carries this clone's absolute path. It used to hardcode
# $HOME/Desktop/sutando — escaped, so it landed in settings.json literally and
# was expanded at HOOK-RUN time, resolving to that one path on every host. This
# clone is at "Library/Application Support/space.ag2.app/engine/sutando" and a
# sibling is at "Documents/github/sutando"; neither is ~/Desktop, so every hook
# installed by this script would point at a directory that does not exist and
# fail silently on each fire. REPO_DIR was already derived correctly on the line
# above and simply was not used.

# Single-quote a string for safe embedding in a stored shell command.
# REPO_DIR is expanded at install time, so its literal text lands in
# settings.json and is re-parsed by a shell at hook-run time. Unquoted, a clone
# under "Library/Application Support/..." is split on the space and the hook
# dies with `bash: /Users/you/Library: No such file or directory` — the exact
# path this fix targets. Single quotes (with '\'' escaping) are metacharacter-
# proof, unlike double quotes which would still interpolate $ and `.
shq() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# Hook specs: each line is "<event>|<marker>|<command>".  Order = install order.
#
# <marker> is a stable substring identifying a hook THIS script owns, used by
# the stale-variant sweep below. It must not match a sibling hook: note
# "src/session-handoff.sh" cannot match the archive hook's
# "sutando-conversations/", so migrating one never disturbs the other.
#
# $REPO_DIR is expanded HERE, at install time, so the command written into
# settings.json carries this clone's absolute path — quoted via shq(). It used
# to hardcode $HOME/Desktop/sutando escaped, so it landed in settings.json
# literally and was expanded at HOOK-RUN time, resolving to that one path on
# every host. This clone is at "Library/Application Support/space.ag2.app/
# engine/sutando" and a sibling is at "Documents/github/sutando"; neither is
# ~/Desktop, so every hook installed by the old script pointed at a directory
# that does not exist and failed silently on each fire.
HOOKS=(
  "PreCompact|sutando-conversations/|cp \"\$TRANSCRIPT_PATH\" \"\$HOME/Desktop/sutando-conversations/\$(date +%Y-%m-%dT%H-%M-%S).jsonl\""
  "PreCompact|src/session-handoff.sh|bash $(shq "$REPO_DIR/src/session-handoff.sh") \"\$TRANSCRIPT_PATH\""
  "SessionEnd|src/session-handoff.sh|bash $(shq "$REPO_DIR/src/session-handoff.sh") \"\$TRANSCRIPT_PATH\""
  "Stop|src/check-pending-tasks.sh|bash $(shq "$REPO_DIR/src/check-pending-tasks.sh")"
)

# The transcript archiver writes OUTSIDE the workspace (~/Desktop). Omitting it
# is install-only: phase 0 skips it anyway (no repo path), so an opt-in survives.
if [ "${SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE:-0}" = "1" ]; then
  _kept=()
  for _h in "${HOOKS[@]}"; do
    case "$_h" in
      "PreCompact|sutando-conversations/|"*) ;;
      *) _kept+=("$_h") ;;
    esac
  done
  HOOKS=("${_kept[@]}")
fi

# Parallel to HOOKS by index, not another `|` field: CMD must stay last to hold a
# `|`, and a second path-bearing field cannot also be last. Sized from HOOKS.
HOOK_PRIOR=()
for _i in "${!HOOKS[@]}"; do HOOK_PRIOR+=(""); done

# Skill-declared hooks via src/skill_hooks.py (the same discovery the health probe reads).
# NUL-framed (-d '') because two of the four fields embed the repo path.
while IFS= read -r -d '' _ev && IFS= read -r -d '' _tok \
   && IFS= read -r -d '' _cmd && IFS= read -r -d '' _prior; do
  [ -n "${_ev:-}" ] || continue
  HOOKS+=("$_ev|$_tok|$_cmd")
  HOOK_PRIOR+=("$_prior")
done < <(python3 "$REPO_DIR/src/skill_hooks.py" "$REPO_DIR" 2>/dev/null)

# Deprecated hooks to uninstall on re-run.  Each line: "<event>|<substring>".
# Matching uses `.command | contains(substring)` so we don't need to track
# the exact command string an old installer wrote — just a stable token.
# Add new entries here when removing a hook from `HOOKS=()`; entries can
# be removed once you're confident the fleet has migrated (months later).
DEPRECATED_HOOKS=(
  # #1065 watcher-kill Stop hook — dropped from HOOKS by #1083 (turn-end
  # firing killed the live Monitor watcher every turn). Cleanup-by-re-run
  # added in #1083 follow-up.
  "Stop|watch-tasks-stream.pid"
)

if ! command -v jq >/dev/null 2>&1; then
  echo "error: jq is required for atomic settings.json edit" >&2
  exit 2
fi

mkdir -p "$REPO_DIR/.claude"
if [ ! -f "$SETTINGS" ]; then
  echo '{}' > "$SETTINGS"
fi

ADDED=0
SKIPPED=0
REMOVED=0

# Escape a literal string so it can be embedded in a jq (Oniguruma) regex.
re_escape() { printf '%s' "$1" | sed 's/[][\\^$.*+?(){}|]/\\&/g'; }

# Phase 0: remove STALE VARIANTS of hooks we own. This is what makes a re-run a
# real migration rather than an add-only pass:
#   * legacy "$HOME/Desktop/sutando/src/session-handoff.sh" entries;
#   * the UNQUOTED form written by earlier revisions of this very script, which
#     an exact-string comparison in phase 1 can never match (so both the broken
#     and the fixed hook would fire);
#   * an entry left behind by a different clone of this repo.
# It runs BEFORE phase 1 so the freshly-added current command is never swept.
#
# OWNERSHIP TEST — the load-bearing part. "Carries our marker" is NOT ownership:
# an operator hook that invokes the same script also contains it, and an earlier
# revision of this sweep deleted exactly those. We sweep only commands matching
# the SHAPE THIS INSTALLER WRITES, which is three conditions, each added after a
# real false positive:
#
#   1. the hook must EMBED THE REPO PATH at all — otherwise nothing about it can
#      go stale, so there is nothing to migrate (see the skip below);
#   2. the region between the command word and the marker must START LIKE A PATH
#      — optional quote, then a non-space, non-`-` character — so a flag or
#      wrapper before the path (`bash -x …`) is not swallowed by the wildcard;
#   3. the text after the marker must match EXACTLY, so customization after the
#      path fails the trailing anchor.
#
# Matching is done on the RAW command. An earlier revision normalized by
# stripping quote characters; that was lossy — `shq` rewrites `'` as `'\''`, so
# stripping leaves a stray backslash and the shape stops matching its own output
# on a path containing an apostrophe. Nothing here pre-processes the candidate.
#
# Sweeping a *different clone's* entry is intended — that shape is
# installer-generated, just not by this checkout.
for i in "${!HOOKS[@]}"; do
  entry="${HOOKS[$i]}"
  EVENT="${entry%%|*}"
  REST="${entry#*|}"
  MARKER="${REST%%|*}"
  CMD="${REST#*|}"

  # PHASE 0 ONLY APPLIES TO HOOKS THAT EMBED THE REPO PATH.
  #
  # The whole point of the sweep is migrating entries whose *path* is stale — a
  # legacy $HOME/Desktop clone, an unquoted form, another checkout. A hook with no
  # repo path in it has nothing that can go stale, so there is nothing to migrate
  # and sweeping can only ever destroy someone else's command.
  #
  # The transcript-archive hook is exactly that case: its marker is only
  # `sutando-conversations/` and its command is all $HOME. With a wildcard shape,
  # the `.*` spans the SOURCE ARGUMENT rather than a path, so an operator's
  #     cp "$CUSTOM_TRANSCRIPT_PATH" "$HOME/Desktop/sutando-conversations/…"
  # differed from ours only inside the wildcard and was deleted and replaced on
  # re-run — silently discarding their archival policy. Reproduced on b21d2bf.
  #
  # An earlier revision of this file handled this with an explicit "no repo path
  # → exact match" branch. Rewriting the shape structurally dropped that branch
  # and applied the wildcard uniformly; this restores the case as a skip, which
  # states the intent instead of encoding it as an unreachable pattern.
  # Compare against the ESCAPED body, not the raw path. `shq` wraps in single
  # quotes and rewrites an internal `'` as `'\''`, so on a checkout at
  # /repo'quote the command embeds `/repo'\''quote` and a raw `$REPO_DIR` test
  # never matches — the guard would then skip the sweep for the very hooks it
  # must run on. That is the same raw-vs-escaped confusion that produced the
  # apostrophe bug this file already fixed once; it is a property of shq, so
  # every comparison against an embedded path has to go through it.
  ESC="$(shq "$REPO_DIR")"; ESC="${ESC#\'}"; ESC="${ESC%\'}"
  case "$CMD" in
    *"$ESC"*) ;;
    *) continue ;;
  esac

  # Build the shape from the command's STRUCTURE, never by stripping quotes.
  #
  # Quote-stripping was lossy and wrong: `shq` escapes an apostrophe in the repo
  # path as `'\''`, so deleting quote characters leaves a stray backslash. On a
  # checkout at `/repo'quote` the stripped command and the stripped repo path
  # then disagree, the split finds nothing, SHAPE collapses to an exact match,
  # and the stale unquoted entry is never swept — both hooks keep firing. An
  # apostrophe is legal in a path, so this is a real checkout, not a curiosity.
  #
  # Instead: anchor on the command word, the marker (already the stable
  # structural token), and the literal tail that follows the marker.
  #
  # The region between the command word and the marker is THE PATH AND NOTHING
  # ELSE. A bare `.*` there was too permissive in one direction: customization
  # *after* the path fails the trailing anchor and survives, but customization
  # *before* it — `bash -x <path>/src/session-handoff.sh "$TRANSCRIPT_PATH"` —
  # was swallowed by the wildcard and swept as installer-owned. Reproduced:
  # before rerun 2, after rerun 1, operator's `-x` hook gone.
  #
  # So the path region must START like a path: an optional opening quote, then a
  # character that is neither a space nor `-`. That admits every form we write or
  # have written — `/abs/...`, `'/quoted path/...'`, `'/repo'\''quote/...'`, and
  # the legacy `$HOME/Desktop/...` — while a flag or wrapper token before the
  # path fails immediately. Requiring a literal `/` would have been wrong: it
  # rejects the legacy `$HOME/...` entries this sweep exists to migrate.
  CMD_WORD="${CMD%% *}"
  CMD_TAIL="${CMD#*"$MARKER"}"
  CMD_TAIL="${CMD_TAIL#[\"\']}"       # drop shq's closing quote, if present
  SHAPE="^$(re_escape "$CMD_WORD") [\"']?[^ -].*$(re_escape "$MARKER")[\"']?$(re_escape "$CMD_TAIL")\$"

  # SHAPE cannot match the runner-first entry (its first word is `[`), so match the
  # prior command exactly, taken from the emitter — `${CMD#*exec }` splits on a path.
  LEGACY_SHAPE=""
  [ -n "${HOOK_PRIOR[$i]:-}" ] && LEGACY_SHAPE="^$(re_escape "${HOOK_PRIOR[$i]}")\$"

  if ! jq -e --arg event "$EVENT" --arg marker "$MARKER" --arg cmd "$CMD" \
           --arg shape "$SHAPE" --arg legacy "$LEGACY_SHAPE" \
      '(.hooks // {})[$event] // [] | map(.hooks // []) | flatten | map(.command // "")
       | map(contains($marker) and (. != $cmd)
             and (test($shape) or ($legacy != "" and test($legacy))))
       | any' \
      "$SETTINGS" >/dev/null 2>&1; then
    continue
  fi

  TMP="$(mktemp "${SETTINGS}.XXXXXX")"
  jq --arg event "$EVENT" --arg marker "$MARKER" --arg cmd "$CMD" \
     --arg shape "$SHAPE" --arg legacy "$LEGACY_SHAPE" '
    if (.hooks // {})[$event] then
      .hooks[$event] |= map(
        .hooks |= map(select(
          ((.command // "") | contains($marker))
          and ((.command // "") != $cmd)
          and ((.command // "")
               | test($shape) or ($legacy != "" and test($legacy)))
          | not
        ))
      )
      | .hooks[$event] |= map(select((.hooks // []) | length > 0))
    else . end
  ' "$SETTINGS" > "$TMP" || { echo "error: jq stale-variant sweep failed on $EVENT" >&2; rm -f "$TMP"; exit 1; }
  mv "$TMP" "$SETTINGS"
  REMOVED=$((REMOVED + 1))
done

# Phase 1: install missing current hooks.
for entry in "${HOOKS[@]}"; do
  EVENT="${entry%%|*}"
  REST="${entry#*|}"
  CMD="${REST#*|}"

  # Detect existing entry by command-string match within this event's hooks list.
  if jq -e --arg event "$EVENT" --arg cmd "$CMD" \
      '(.hooks // {})[$event] // [] | map(.hooks // []) | flatten | map(.command) | index($cmd)' \
      "$SETTINGS" >/dev/null 2>&1; then
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  TMP="$(mktemp "${SETTINGS}.XXXXXX")"
  jq --arg event "$EVENT" --arg cmd "$CMD" '
    .hooks //= {}
    | .hooks[$event] //= [{"matcher": "", "hooks": []}]
    | (.hooks[$event][0].hooks //= [])
    | .hooks[$event][0].hooks += [{"type": "command", "command": $cmd}]
  ' "$SETTINGS" > "$TMP" || { echo "error: jq edit failed on $EVENT" >&2; rm -f "$TMP"; exit 1; }
  mv "$TMP" "$SETTINGS"
  ADDED=$((ADDED + 1))
done

# Phase 2: uninstall deprecated hooks (substring match).
# This walks every hooks group under the event, filters out any command
# containing the substring, then rewrites the group.  Doing it per-event
# (vs deleting the whole event key) preserves any sibling hooks the
# operator may have added manually that aren't in our HOOKS list.
for entry in "${DEPRECATED_HOOKS[@]}"; do
  EVENT="${entry%%|*}"
  SUBSTR="${entry#*|}"

  # Skip if no match present — keeps re-runs silent on already-migrated installs.
  if ! jq -e --arg event "$EVENT" --arg sub "$SUBSTR" \
      '(.hooks // {})[$event] // [] | map(.hooks // []) | flatten | map(.command) | map(contains($sub)) | any' \
      "$SETTINGS" >/dev/null 2>&1; then
    continue
  fi

  TMP="$(mktemp "${SETTINGS}.XXXXXX")"
  jq --arg event "$EVENT" --arg sub "$SUBSTR" '
    if (.hooks // {})[$event] then
      .hooks[$event] |= map(
        .hooks |= map(select((.command // "") | contains($sub) | not))
      )
      # Drop now-empty groups so the structure stays tidy.
      | .hooks[$event] |= map(select((.hooks // []) | length > 0))
    else . end
  ' "$SETTINGS" > "$TMP" || { echo "error: jq remove failed on $EVENT/$SUBSTR" >&2; rm -f "$TMP"; exit 1; }
  mv "$TMP" "$SETTINGS"
  REMOVED=$((REMOVED + 1))
done

echo "install-claude-hooks: added=$ADDED skipped=$SKIPPED removed=$REMOVED → $SETTINGS"

# Register hooks in the sutando-hook-manifest so migration-notice can identify
# them without relying on the hardcoded substring list. (#1502)
HOOKS_SCRIPT="$REPO_DIR/scripts/sutando-config-hooks.sh"
if [ -f "$HOOKS_SCRIPT" ]; then
  bash "$HOOKS_SCRIPT" write-manifest "project-pre-compact-handoff" "src/session-handoff.sh" "src/install-claude-hooks.sh" 2>/dev/null || true
  bash "$HOOKS_SCRIPT" write-manifest "project-pre-compact-archive" "sutando-conversations/" "src/install-claude-hooks.sh" 2>/dev/null || true
  bash "$HOOKS_SCRIPT" write-manifest "project-stop-pending-tasks" "src/check-pending-tasks.sh" "src/install-claude-hooks.sh" 2>/dev/null || true
fi
