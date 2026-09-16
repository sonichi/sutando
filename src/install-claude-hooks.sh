#!/bin/bash
# install-claude-hooks.sh — idempotent install of Sutando-owned core-session
# Claude Code hooks (PreCompact + SessionEnd + Stop).
#
# Scope: these hooks are CORE-ONLY, so they install into the core's own
# CLAUDE_CONFIG_DIR (`<workspace>/.claude-sutando/settings.json`), which no
# other session reads. `feedback_claude_code_hook_scoping` ruled out user-level
# `~/.claude/settings.json` because it fires in unrelated repos; project-level
# `.claude/settings.json` fixes the repo axis but not the session one — it fires
# for every Claude session with this cwd, which is how guests were handed the
# core's task queue to drain and how a guest's tail overwrote session-state.md.
#
# Hooks installed (4):
#   PreCompact  → src/archive-transcript.sh <workspace>/logs/conversations/
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

# Resolve a sutando-config.sh subcommand, falling back to a guessed default
# ONLY when the helper script itself does not exist (e.g. a bare test fixture
# with no scripts/ dir) — the one case nothing safe can be compared against.
# When the helper EXISTS but the command fails, guessing is unsafe: it can
# write/sweep against the wrong config dir on a configured clone (#4309 review,
# keweichen/qingyun-wu 2026-09-16, repro: a readable sutando-config.sh exiting
# 9 still let the installer "succeed" against a guessed path). Fail loud instead.
resolve_or_die() {  # resolve_or_die <subcommand> <fallback> -> sets RESOLVED
  local _sub="$1" _fallback="$2" _helper="$REPO_DIR/scripts/sutando-config.sh" _out
  if [ ! -f "$_helper" ]; then
    RESOLVED="$_fallback"
    return 0
  fi
  if ! _out="$(bash "$_helper" "$_sub" 2>&1)" || [ -z "$_out" ]; then
    echo "install-claude-hooks: scripts/sutando-config.sh $_sub failed: $_out" >&2
    echo "install-claude-hooks: refusing to guess a config/workspace path — fix the resolver first." >&2
    exit 1
  fi
  RESOLVED="$_out"
}

# These hooks belong to the CORE SESSION, not to the checkout. Project-level
# `.claude/settings.json` scopes by repo, so every other Claude session with
# this cwd — a review automation, an ad-hoc owner session, a worktree — also
# fired them: the Stop hook handed guests the core's task queue to drain, and
# session-handoff overwrote the core's session-state.md with a guest's tail.
# The core's own CLAUDE_CONFIG_DIR is read by the core and nothing else.
resolve_or_die claude-sutando-config-dir "$REPO_DIR/workspace/.claude-sutando"
CORE_CONFIG_DIR="$RESOLVED"
SETTINGS="$CORE_CONFIG_DIR/settings.json"

# Pre-move location, swept below so a re-run migrates an existing install.
LEGACY_PROJECT_SETTINGS="$REPO_DIR/.claude/settings.json"

# Transcript archives are per-user mutable state, so they live under the
# workspace (CLAUDE.md "Workspace contract"), not in ~/Desktop. logs/ is also
# named in vault.sync.exclude, so the archive stays out of the carrier set.
resolve_or_die workspace "$REPO_DIR/workspace"
WORKSPACE_DIR="$RESOLVED"
TRANSCRIPT_DIR="$WORKSPACE_DIR/logs/conversations"

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

# Escape a literal string so it can be embedded in a jq (Oniguruma) regex.
# Moved up from its former spot below the settings bootstrap so a DEPRECATED_HOOKS
# entry built below (regex mode) can call it — a function must be defined before
# its first use in a script executed top-to-bottom.
re_escape() { printf '%s' "$1" | sed 's/[][\\^$.*+?(){}|]/\\&/g'; }

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
  "PreCompact|logs/conversations/|bash $(shq "$REPO_DIR/src/archive-transcript.sh") $(shq "$TRANSCRIPT_DIR/")"
  "PreCompact|src/session-handoff.sh|bash $(shq "$REPO_DIR/src/session-handoff.sh") \"\$TRANSCRIPT_PATH\""
  "SessionEnd|src/session-handoff.sh|bash $(shq "$REPO_DIR/src/session-handoff.sh") \"\$TRANSCRIPT_PATH\""
  "Stop|src/check-pending-tasks.sh|bash $(shq "$REPO_DIR/src/check-pending-tasks.sh")"
  # Without this the Stop gate spends its one reminder and never re-arms:
  # begin_turn is the only reset and nothing else in the lifecycle calls it.
  "UserPromptSubmit|src/turn-start.sh|bash $(shq "$REPO_DIR/src/turn-start.sh")"
)

# The transcript archiver writes to ~/Desktop, OUTSIDE the vault carrier set.
# The location is not what keeps transcripts out of the vault: sync is a whitelist
# (see .git/info/exclude -- `*` then the include list), so a workspace path is
# unsynced until vault.sync.include names it. Omitting it
# drops it from HOOKS, which every phase iterates, so a registered one is untouched.
if [ "${SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE:-0}" = "1" ]; then
  _kept=()
  for _h in "${HOOKS[@]}"; do
    case "$_h" in
      "PreCompact|logs/conversations/|"*) ;;
      *) _kept+=("$_h") ;;
    esac
  done
  HOOKS=("${_kept[@]}")
fi

# One owner for the command strings. scripts/sutando-config-hooks.sh used to
# re-declare its own copies, which drifted three ways (an archiver form this
# script now sweeps as deprecated, and two quoting variants that double-register).
if [ "${1:-}" = "--print-hooks" ]; then
  for _h in "${HOOKS[@]}"; do printf '%s\n' "$_h"; done
  exit 0
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

# Deprecated hooks to uninstall on re-run.  Each line: "<event>|<mode>|<pattern>".
# mode "sub": `.command | contains(pattern)` — for a marker so distinctive
# (a pidfile path fragment, say) that no other command could plausibly embed
# it. mode "regex": `.command | test(pattern)`, pattern pre-anchored (^...$)
# by the constructor below — for anything an operator's OWN differently-shaped
# command could otherwise contain as a mid-string substring (#4309 review,
# keweichen/qingyun-wu 2026-09-16: a wrapper merely targeting the same
# directory was swept under "sub" mode). Add new entries when removing a hook
# from `HOOKS=()`; entries can be removed once the fleet has migrated (months).
DEPRECATED_HOOKS=(
  # #1065 watcher-kill Stop hook — dropped from HOOKS by #1083 (turn-end
  # firing killed the live Monitor watcher every turn). Cleanup-by-re-run
  # added in #1083 follow-up. Substring is safe: no live hook's command
  # plausibly embeds this pidfile path fragment.
  "Stop|sub|watch-tasks-stream.pid"
)

# This PR changed the archiver's command: phase 0 cannot migrate the old one (it
# embeds no repo path) and phase 1 matches exactly, so both would fire.
#
# Both pre-move forms of OUR archiver, matched by their EXACT historical shape
# (regex mode, anchored ^...$) rather than a bare directory substring — an
# operator's own command that merely targets the same directory (a different
# wrapper, extra flags) does not match an anchored full-command regex the way
# it matched a loose `contains("Desktop/sutando-conversations/")`. The ancient
# `cp` form is fully static (no $REPO_DIR — never varied per clone); the
# archive-transcript.sh form is reconstructed exactly as this clone's OWN
# prior installer would have written it (git blame 96e2e0ce9^).
ARCHIVE_LEGACY_SHAPES=(
  "PreCompact|regex|^$(re_escape "cp \"\$TRANSCRIPT_PATH\" \"\$HOME/Desktop/sutando-conversations/\$(date +%Y-%m-%dT%H-%M-%S).jsonl\"")\$"
  "PreCompact|regex|^$(re_escape "bash $(shq "$REPO_DIR/src/archive-transcript.sh") \"\$HOME/Desktop/sutando-conversations/\"")\$"
)

# CORE settings: keep this OMIT-gated. "The flag already dropped the archiver
# from HOOKS, so an ungated removal here would delete a registered hook and
# install no successor" — a real tradeoff at core scope, where there is no
# other session to leak to, so leaving an operator's working (if stale-shaped)
# core-only archiver alone under omit is a legitimate choice, not a bug.
if [ "${SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE:-0}" != "1" ]; then
  DEPRECATED_HOOKS+=("${ARCHIVE_LEGACY_SHAPES[@]}")
fi

# LEGACY PROJECT settings: always swept, never gated on omit (#4309 review,
# keweichen/qingyun-wu 2026-09-16). "Don't newly enable archiving" (the
# flag's job, honored above in HOOKS[] and in the core-scope sweep just
# above) is a different question from "clean up a stale Desktop-scoped
# PROJECT hook" — that one is visible to every guest session in this repo,
# exactly the cross-session leak this whole migration exists to close, and
# leaving it registered under omit is worse than installing no successor.
DEPRECATED_HOOKS_PROJECT_ONLY=("${ARCHIVE_LEGACY_SHAPES[@]}")

if ! command -v jq >/dev/null 2>&1; then
  echo "error: jq is required for atomic settings.json edit" >&2
  exit 2
fi

mkdir -p "$CORE_CONFIG_DIR"
# The PreCompact archive hook is a bare `cp`, which cannot create its own
# destination; without this the archiver fails on every compaction, silently.
mkdir -p "$TRANSCRIPT_DIR"
if [ ! -f "$SETTINGS" ]; then
  echo '{}' > "$SETTINGS"
fi

ADDED=0
SKIPPED=0
REMOVED=0

# Ownership test for HOOKS[$1]: sets EVENT/MARKER/CMD/SHAPE/LEGACY_SHAPE, returns
# 1 when the entry embeds no repo path (nothing safe to shape-match against — see
# Phase 0's comment on this exact tradeoff). ONE owner for this test: Phase 0 and
# Phase 3 both migrate hooks WE wrote, and a second hand-rolled copy is exactly
# how Phase 3 shipped the bare-substring bug this function replaces.
owned_hook_shape() {
  local i="$1" entry rest
  entry="${HOOKS[$i]}"
  EVENT="${entry%%|*}"
  rest="${entry#*|}"
  MARKER="${rest%%|*}"
  CMD="${rest#*|}"
  local esc
  esc="$(shq "$REPO_DIR")"; esc="${esc#\'}"; esc="${esc%\'}"
  case "$CMD" in
    *"$esc"*) ;;
    *) return 1 ;;
  esac
  local cmd_word cmd_tail
  cmd_word="${CMD%% *}"
  cmd_tail="${CMD#*"$MARKER"}"
  cmd_tail="${cmd_tail#[\"\']}"       # drop shq's closing quote, if present
  # A new shell argument starts wherever a quote sits directly against a
  # space — closing quote + space (an argument ending) or space + opening
  # quote (an argument beginning). Either shape crossed into a wrapper's
  # SEPARATE argument and got misread as installer-owned, in order: an
  # unbounded `.*` matched across close-quote+space (#4309 review
  # 2026-09-16); a lookahead excluding only THAT direction still matched
  # across space+open-quote — `bash /op/wrap.sh '<repo>/...' ...`, no quote
  # before the space at all (qingyun-wu 2026-09-16, reproduced on bd2ddd51).
  # A bare quote with NO adjacent space (an apostrophe inside an unquoted
  # legacy path, #8 below) is not a boundary either way and must stay
  # matchable — this excludes both directions, nothing more.
  SHAPE="^$(re_escape "$cmd_word") [\"']?[^ -](?:(?![\"'] | [\"']).)*$(re_escape "$MARKER")[\"']?$(re_escape "$cmd_tail")\$"
  LEGACY_SHAPE=""
  [ -n "${HOOK_PRIOR[$i]:-}" ] && LEGACY_SHAPE="^$(re_escape "${HOOK_PRIOR[$i]}")\$"
  return 0
}

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
  # PHASE 0 ONLY APPLIES TO HOOKS THAT EMBED THE REPO PATH — see
  # owned_hook_shape() above. The whole point of the sweep is migrating entries
  # whose *path* is stale (a legacy $HOME/Desktop clone, an unquoted form,
  # another checkout); a hook with no repo path has nothing that can go stale,
  # so sweeping it can only ever destroy someone else's command. Reproduced on
  # the transcript-archive hook (b21d2bf) before this skip existed.
  owned_hook_shape "$i" || continue

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

# Phase 2: uninstall deprecated hooks (mode "sub" or "regex" per entry — see
# DEPRECATED_HOOKS above). This walks every hooks group under the event,
# filters out any matching command, then rewrites the group.  Doing it
# per-event (vs deleting the whole event key) preserves any sibling hooks the
# operator may have added manually that aren't in our HOOKS list.
for entry in "${DEPRECATED_HOOKS[@]}"; do
  EVENT="${entry%%|*}"
  _rest="${entry#*|}"
  MODE="${_rest%%|*}"
  PAT="${_rest#*|}"
  JQ_TEST='contains($p)'
  [ "$MODE" = "regex" ] && JQ_TEST='test($p)'

  # Skip if no match present — keeps re-runs silent on already-migrated installs.
  if ! jq -e --arg event "$EVENT" --arg p "$PAT" \
      "(.hooks // {})[\$event] // [] | map(.hooks // []) | flatten | map(.command) | map($JQ_TEST) | any" \
      "$SETTINGS" >/dev/null 2>&1; then
    continue
  fi

  TMP="$(mktemp "${SETTINGS}.XXXXXX")"
  jq --arg event "$EVENT" --arg p "$PAT" "
    if (.hooks // {})[\$event] then
      .hooks[\$event] |= map(
        .hooks |= map(select((.command // \"\") | $JQ_TEST | not))
      )
      # Drop now-empty groups so the structure stays tidy.
      | .hooks[\$event] |= map(select((.hooks // []) | length > 0))
    else . end
  " "$SETTINGS" > "$TMP" || { echo "error: jq remove failed on $EVENT/$MODE/$PAT" >&2; rm -f "$TMP"; exit 1; }
  mv "$TMP" "$SETTINGS"
  REMOVED=$((REMOVED + 1))
done

# Phase 3 — migrate an install that predates the move: the same hooks registered
# at project level fire for every session in this cwd, so leaving them behind
# would double-register the core and keep conscripting guests. Only entries this
# script owns are removed; anything the operator added by hand stays.
#
# HOOKS entries are removed by SHAPE, via owned_hook_shape() — the same
# ownership test Phase 0 uses, not a bare substring. A bare `contains($sub)`
# here deleted an operator's OWN customized command whenever it happened to
# mention our marker too: `bash -x .../session-handoff.sh "$TRANSCRIPT_PATH"
# --operator-flag` matches the marker and was swept alongside the canonical
# entry it sits beside. Unlike Phase 0, no `. != $cmd` exclusion — at this
# location EVERY installer-owned shape (today's exact command included) must
# go, since the whole point is that none of it belongs at project level
# anymore. DEPRECATED_HOOKS entries keep the substring match Phase 2 already
# uses for them: those markers name a fully-retired hook family with no
# current command to preserve the shape of.
LEGACY_REMOVED=0
if [ -f "$LEGACY_PROJECT_SETTINGS" ] && [ "$LEGACY_PROJECT_SETTINGS" != "$SETTINGS" ]; then
  for i in "${!HOOKS[@]}"; do
    owned_hook_shape "$i" || continue
    if ! jq -e --arg event "$EVENT" --arg marker "$MARKER" --arg cmd "$CMD" \
             --arg shape "$SHAPE" --arg legacy "$LEGACY_SHAPE" \
        '(.hooks // {})[$event] // [] | map(.hooks // []) | flatten | map(.command // "")
         | map(contains($marker)
               and (. == $cmd or test($shape) or ($legacy != "" and test($legacy))))
         | any' \
        "$LEGACY_PROJECT_SETTINGS" >/dev/null 2>&1; then
      continue
    fi
    TMP="$(mktemp "${LEGACY_PROJECT_SETTINGS}.XXXXXX")"
    jq --arg event "$EVENT" --arg marker "$MARKER" --arg cmd "$CMD" \
       --arg shape "$SHAPE" --arg legacy "$LEGACY_SHAPE" '
      if (.hooks // {})[$event] then
        .hooks[$event] |= map(
          .hooks |= map(select(
            ((.command // "") | contains($marker))
            and ((.command // "")
                 | . == $cmd or test($shape) or ($legacy != "" and test($legacy)))
            | not
          ))
        )
        | .hooks[$event] |= map(select((.hooks // []) | length > 0))
      else . end
    ' "$LEGACY_PROJECT_SETTINGS" > "$TMP" || {
      echo "error: jq legacy sweep failed on $EVENT/$MARKER" >&2; rm -f "$TMP"; exit 1; }
    mv "$TMP" "$LEGACY_PROJECT_SETTINGS"
    LEGACY_REMOVED=$((LEGACY_REMOVED + 1))
  done
  for entry in "${DEPRECATED_HOOKS[@]}" "${DEPRECATED_HOOKS_PROJECT_ONLY[@]}"; do
    EVENT="${entry%%|*}"
    _rest="${entry#*|}"
    MODE="${_rest%%|*}"
    PAT="${_rest#*|}"
    [ -n "$PAT" ] || continue
    JQ_TEST='contains($p)'
    [ "$MODE" = "regex" ] && JQ_TEST='test($p)'
    if ! jq -e --arg event "$EVENT" --arg p "$PAT" \
        "(.hooks // {})[\$event] // [] | map(.hooks // []) | flatten | map(.command) | map($JQ_TEST) | any" \
        "$LEGACY_PROJECT_SETTINGS" >/dev/null 2>&1; then
      continue
    fi
    TMP="$(mktemp "${LEGACY_PROJECT_SETTINGS}.XXXXXX")"
    jq --arg event "$EVENT" --arg p "$PAT" "
      if (.hooks // {})[\$event] then
        .hooks[\$event] |= map(
          .hooks |= map(select((.command // \"\") | $JQ_TEST | not))
        )
        | .hooks[\$event] |= map(select((.hooks // []) | length > 0))
      else . end
    " "$LEGACY_PROJECT_SETTINGS" > "$TMP" || {
      echo "error: jq legacy sweep failed on $EVENT/$MODE/$PAT" >&2; rm -f "$TMP"; exit 1; }
    mv "$TMP" "$LEGACY_PROJECT_SETTINGS"
    LEGACY_REMOVED=$((LEGACY_REMOVED + 1))
  done
  [ "$LEGACY_REMOVED" -gt 0 ] && \
    echo "install-claude-hooks: removed $LEGACY_REMOVED core-only hook(s) from $LEGACY_PROJECT_SETTINGS (moved to the core config dir)"
fi

echo "install-claude-hooks: added=$ADDED skipped=$SKIPPED removed=$REMOVED → $SETTINGS"

# Register hooks in the sutando-hook-manifest so migration-notice can identify
# them without relying on the hardcoded substring list. (#1502)
HOOKS_SCRIPT="$REPO_DIR/scripts/sutando-config-hooks.sh"
if [ -f "$HOOKS_SCRIPT" ]; then
  bash "$HOOKS_SCRIPT" write-manifest "project-pre-compact-handoff" "src/session-handoff.sh" "src/install-claude-hooks.sh" 2>/dev/null || true
  bash "$HOOKS_SCRIPT" write-manifest "project-pre-compact-archive" "logs/conversations/" "src/install-claude-hooks.sh" 2>/dev/null || true
  bash "$HOOKS_SCRIPT" write-manifest "project-stop-pending-tasks" "src/check-pending-tasks.sh" "src/install-claude-hooks.sh" 2>/dev/null || true
fi
