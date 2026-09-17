#!/bin/bash
# install-claude-hooks.sh — idempotent install of Sutando-owned core-session
# Claude Code hooks (PreCompact + SessionEnd + Stop).
#
# CORE-ONLY hooks install into the core's own CLAUDE_CONFIG_DIR, not
# project-level -- project scope fires for every session with this cwd.
#
# Hooks installed (4):
#   PreCompact  → src/archive-transcript.sh <workspace>/logs/conversations/
#   PreCompact  → bash src/session-handoff.sh "$TRANSCRIPT_PATH"
#   SessionEnd  → bash src/session-handoff.sh "$TRANSCRIPT_PATH"
#   Stop        → bash src/check-pending-tasks.sh
#
# SessionEnd → session-handoff.sh also fires on a clean exit, not just
# PreCompact, so the last session's tail isn't lost when no compaction ran.
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

# Guess the fallback only when the helper script itself is absent (nothing to
# compare against); if it exists but fails, fail loud — guessing risks the wrong config dir on a configured clone.
resolve_or_die() {  # resolve_or_die <subcommand> <fallback> -> sets RESOLVED
  local _sub="$1" _fallback="$2" _helper="$REPO_DIR/scripts/sutando-config.sh" _out _err
  if [ ! -f "$_helper" ]; then
    RESOLVED="$_fallback"
    return 0
  fi
  # Stdout is the value, stderr is warnings (resolver contract) — keep them
  # apart, or a success-plus-warning run bakes the warning INTO the path.
  _err="$(mktemp)"
  _out="$(bash "$_helper" "$_sub" 2>"$_err")"
  local _rc=$?
  if [ "$_rc" -ne 0 ] || [ -z "$_out" ]; then
    echo "install-claude-hooks: scripts/sutando-config.sh $_sub failed: $(cat "$_err")" >&2
    echo "install-claude-hooks: refusing to guess a config/workspace path — fix the resolver first." >&2
    rm -f "$_err"
    exit 1
  fi
  [ -s "$_err" ] && cat "$_err" >&2
  rm -f "$_err"
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

# The archiver writes under logs/, excluded from vault sync by default.
# Omitting it here drops it from HOOKS, which every phase iterates.
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
HOOK_IS_SKILL=()
for _i in "${!HOOKS[@]}"; do HOOK_PRIOR+=(""); HOOK_IS_SKILL+=("0"); done

# Skill-declared hooks via src/skill_hooks.py (the same discovery the health probe reads).
# NUL-framed (-d '') because two of the four fields embed the repo path.
while IFS= read -r -d '' _ev && IFS= read -r -d '' _tok \
   && IFS= read -r -d '' _cmd && IFS= read -r -d '' _prior; do
  [ -n "${_ev:-}" ] || continue
  HOOKS+=("$_ev|$_tok|$_cmd")
  HOOK_PRIOR+=("$_prior")
  HOOK_IS_SKILL+=("1")
done < <(python3 "$REPO_DIR/src/skill_hooks.py" "$REPO_DIR" 2>/dev/null)

# Deprecated hooks to uninstall on re-run. Each line: "<event>|<mode>|<pattern>".
# mode "sub" is a bare contains() for a marker too distinctive to collide; mode "regex" anchors (^...$) for anything a differently-shaped command could otherwise substring-match.
DEPRECATED_HOOKS=(
  # #1065 watcher-kill Stop hook — dropped from HOOKS by #1083 (turn-end
  # firing killed the live Monitor watcher every turn). Cleanup-by-re-run
  # added in #1083 follow-up. Substring is safe: no live hook's command
  # plausibly embeds this pidfile path fragment.
  "Stop|sub|watch-tasks-stream.pid"
)

# Exact anchored shape (not substring), so an operator's own differently-shaped
# command can't match; entry 2 accepts any shq()-quoted absolute path — including
# one containing an apostrophe, which shq() spells `'\''` mid-string, not `'`.
ARCHIVE_LEGACY_SHAPES=(
  "PreCompact|regex|^$(re_escape "cp \"\$TRANSCRIPT_PATH\" \"\$HOME/Desktop/sutando-conversations/\$(date +%Y-%m-%dT%H-%M-%S).jsonl\"")\$"
  "PreCompact|regex|^bash '([^']|'\\\\'')*$(re_escape "/src/archive-transcript.sh")' $(re_escape "\"\$HOME/Desktop/sutando-conversations/\"")\$"
)

# Core-scope sweep stays OMIT-gated — removing a registered hook with no
# successor is only a legitimate tradeoff where no other session can see it.
if [ "${SUTANDO_HOOKS_OMIT_TRANSCRIPT_ARCHIVE:-0}" != "1" ]; then
  DEPRECATED_HOOKS+=("${ARCHIVE_LEGACY_SHAPES[@]}")
fi

# Legacy PROJECT-level entries are swept regardless of the omit flag — that
# flag governs new archiving, not cleanup of a stale hook visible to every guest session in this repo.
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

# Real argv tokenizer (quotes/backslash honored, never expands $vars/`cmd`).
# Sets TOKENIZE_RESULT + TOKENIZE_HAS_OPERATOR; returns 1 on an unterminated quote.
tokenize_argv() {
  local s="$1" n=${#1} i=0 c cur="" in_word=0
  TOKENIZE_RESULT=()
  TOKENIZE_HAS_OPERATOR=0
  while [ "$i" -lt "$n" ]; do
    c="${s:i:1}"
    case "$c" in
      ' '|$'\t')
        if [ "$in_word" = 1 ]; then TOKENIZE_RESULT+=("$cur"); cur=""; in_word=0; fi
        i=$((i+1)) ;;
      $'\n'|';'|'&'|'|'|'<'|'>'|'('|')')
        TOKENIZE_HAS_OPERATOR=1
        if [ "$in_word" = 1 ]; then TOKENIZE_RESULT+=("$cur"); cur=""; in_word=0; fi
        i=$((i+1)) ;;
      "'")
        in_word=1; i=$((i+1))
        while :; do
          [ "$i" -ge "$n" ] && return 1
          c="${s:i:1}"
          [ "$c" = "'" ] && { i=$((i+1)); break; }
          cur="$cur$c"; i=$((i+1))
        done ;;
      '"')
        in_word=1; i=$((i+1))
        while :; do
          [ "$i" -ge "$n" ] && return 1
          c="${s:i:1}"
          [ "$c" = '"' ] && { i=$((i+1)); break; }
          if [ "$c" = '\' ]; then
            i=$((i+1)); [ "$i" -ge "$n" ] && return 1
            cur="$cur${s:i:1}"; i=$((i+1)); continue
          fi
          cur="$cur$c"; i=$((i+1))
        done ;;
      '\')
        in_word=1; i=$((i+1))
        [ "$i" -ge "$n" ] && return 1
        cur="$cur${s:i:1}"; i=$((i+1)) ;;
      *)
        in_word=1; cur="$cur$c"; i=$((i+1)) ;;
    esac
  done
  [ "$in_word" = 1 ] && TOKENIZE_RESULT+=("$cur")
  return 0
}

# Ownership test for HOOKS[$1]; returns 1 when the entry embeds no repo path
# to shape-match against. Single owner: Phase 0 and Phase 3 both use this.
owned_hook_shape() {
  local i="$1" entry rest
  entry="${HOOKS[$i]}"
  EVENT="${entry%%|*}"
  rest="${entry#*|}"
  MARKER="${rest%%|*}"
  CMD="${rest#*|}"
  REPO_DIR_TEXT="$(shq "$REPO_DIR")"; REPO_DIR_TEXT="${REPO_DIR_TEXT#\'}"; REPO_DIR_TEXT="${REPO_DIR_TEXT%\'}"
  case "$CMD" in
    *"$REPO_DIR_TEXT"*) ;;
    *) return 1 ;;
  esac
  CMD_WORD="${CMD%% *}"
  CMD_TAIL="${CMD#*"$MARKER"}"
  CMD_TAIL="${CMD_TAIL#[\"\']}"       # drop shq's closing quote, if present
  HOOK_PRIOR_CUR="${HOOK_PRIOR[$i]:-}"
  HOOK_IS_SKILL_CUR="${HOOK_IS_SKILL[$i]:-0}"
  return 0
}

# Does $1 look like something THIS installer could have written — an absolute
# path, or the legacy `$HOME/Desktop/sutando` literal — not an operator's own unexpanded `$VAR` prefix that merely contains our marker as a substring.
_is_installer_path_shape() {
  case "$1" in
    /*|'$HOME/Desktop/sutando'*) return 0 ;;
    *) return 1 ;;
  esac
}

# Matches src/skill_hooks.py's `[ -f Q ] || exit 0; exec RUNNER Q` guard
# exactly (both Q's identical); anything else, including a near-miss, is rc 1.
_skill_hook_guard_path() {
  local cand="$1" want_runner="${2:-}" mid=' ] || exit 0; exec '
  case "$cand" in '[ -f '*"$mid"*) ;; *) return 1 ;; esac
  local rest="${cand#'[ -f '}" guard tail runner exec_arg
  guard="${rest%%"$mid"*}"
  tail="${rest#*"$mid"}"
  runner="${tail%% *}"
  exec_arg="${tail#* }"
  case "$runner" in bash|python3) ;; *) return 1 ;; esac
  [ -z "$want_runner" ] || [ "$runner" = "$want_runner" ] || return 1
  [ "$guard" = "$exec_arg" ] || return 1
  tokenize_argv "$guard" || return 1
  [ "$TOKENIZE_HAS_OPERATOR" = 0 ] && [ "${#TOKENIZE_RESULT[@]}" -eq 1 ] || return 1
  printf '%s\n' "${TOKENIZE_RESULT[0]}"
}

# Is $1 (a raw .command string) a stale/foreign variant of the current entry
# (EVENT/MARKER/CMD/... from owned_hook_shape())? Real argv tokenization, not
# substring/pattern inference — the marker must sit in argv[1] exactly.
candidate_is_owned() {
  local cand="$1" include_exact="$2"
  case "$cand" in *"$MARKER"*) ;; *) return 1 ;; esac
  if [ "$cand" = "$CMD" ]; then
    [ "$include_exact" = "all" ] && return 0
    return 1
  fi
  if [ -n "$HOOK_PRIOR_CUR" ] && [ "$cand" = "$HOOK_PRIOR_CUR" ]; then
    return 0
  fi
  # Only a skill-declared entry may wear this guard shape (its CMD_WORD is
  # "[", not a repo path, so the argv[1] logic below can't judge a built-in).
  local guard_path
  if [ "$HOOK_IS_SKILL_CUR" = "1" ] \
     && guard_path="$(_skill_hook_guard_path "$cand" "${HOOK_PRIOR_CUR%% *}")"; then
    case "$guard_path" in
      *"$MARKER"*) _is_installer_path_shape "$guard_path" && return 0 ;;
    esac
    return 1
  fi
  local after
  case "$cand" in
    "$CMD_WORD "?*) after="${cand#"$CMD_WORD" }" ;;
    *) return 1 ;;
  esac
  local tokenize_rc=0
  tokenize_argv "$cand" || tokenize_rc=1
  # Past the recognized guard shape above, an unquoted control operator
  # anywhere disqualifies the candidate — it can hide a second command.
  [ "$TOKENIZE_HAS_OPERATOR" = 1 ] && return 1
  if [ "$tokenize_rc" = 0 ] && [ "${#TOKENIZE_RESULT[@]}" -ge 2 ] \
     && [ "${TOKENIZE_RESULT[0]}" = "$CMD_WORD" ]; then
    case "${TOKENIZE_RESULT[1]}" in
      *"$MARKER"*) _is_installer_path_shape "${TOKENIZE_RESULT[1]}" || return 1 ;;  # bucket: clean argv[1] match
      *)                                                  # bucket B
        # Collapse doubled slashes (mktemp -d can produce them; REPO_DIR is
        # normalized) via a plain variable -- `${v//\/\//\/}` doesn't unescape on its replacement side.
        local _sl=/
        case "${after//${_sl}${_sl}/${_sl}}" in "${REPO_DIR_TEXT//${_sl}${_sl}/${_sl}}"*) ;; *) return 1 ;; esac ;;
    esac
  else
    case "${after:0:1}" in ' '|'-') return 1 ;; esac      # bucket A
    _is_installer_path_shape "$after" || return 1
  fi
  # Mirror CMD_TAIL's own derivation onto the candidate — text after the
  # marker, with one leading close-quote stripped if present — then compare
  # by EXACT equality (no glob involved). A candidate whose repo-path
  # argument is quoted has a closing quote sitting right after the marker
  # that a plain `*"$MARKER$CMD_TAIL"` suffix check does not expect, since
  # CMD_TAIL was computed with that same quote already stripped off ours.
  local cand_tail
  cand_tail="${cand#*"$MARKER"}"
  cand_tail="${cand_tail#[\"\']}"
  [ "$cand_tail" = "$CMD_TAIL" ] || return 1
  return 0
}

# Remove, from $1's .hooks[$2], every hook whose .command EXACTLY matches a
# TO_REMOVE entry -- jq builds the set as escaped JSON strings, never a pattern.
remove_exact_commands() {
  local settings_file="$1" event="$2" remove_json
  [ "${#TO_REMOVE[@]}" -gt 0 ] || return 0
  remove_json="$(printf '%s\n' "${TO_REMOVE[@]}" | jq -R . | jq -s .)"
  TMP="$(mktemp "${settings_file}.XXXXXX")"
  jq --arg event "$event" --argjson remove "$remove_json" '
    if (.hooks // {})[$event] then
      .hooks[$event] |= map(
        .hooks |= map(select((((.command // "") as $c | $remove | index($c)) == null)))
      )
      | .hooks[$event] |= map(select((.hooks // []) | length > 0))
    else . end
  ' "$settings_file" > "$TMP" || { echo "error: jq removal failed on $event" >&2; rm -f "$TMP"; exit 1; }
  mv "$TMP" "$settings_file"
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
#   2. the marker must sit in the ONE argv slot right after the command word —
#      determined by real tokenization (candidate_is_owned()), not a regex
#      wildcard, so a flag or wrapper before the path (`bash -x …`, `bash
#      /op/wrap.sh <path> …`) is never swallowed;
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

  CANDIDATES="$(jq -r --arg event "$EVENT" \
    '(.hooks // {})[$event] // [] | map(.hooks // []) | flatten | map(.command // "") | .[]' \
    "$SETTINGS" 2>/dev/null)"
  TO_REMOVE=()
  while IFS= read -r cand; do
    [ -n "$cand" ] || continue
    candidate_is_owned "$cand" "any" && TO_REMOVE+=("$cand")
  done <<< "$CANDIDATES"
  [ "${#TO_REMOVE[@]}" -gt 0 ] || continue

  remove_exact_commands "$SETTINGS" "$EVENT"
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

# Phase 3 — migrate an install that predates the move: sweep every
# installer-owned shape (via owned_hook_shape(), no `!= $cmd` exclusion)
# out of the legacy project-level settings, since none of it belongs there.
LEGACY_REMOVED=0
if [ -f "$LEGACY_PROJECT_SETTINGS" ] && [ "$LEGACY_PROJECT_SETTINGS" != "$SETTINGS" ]; then
  for i in "${!HOOKS[@]}"; do
    owned_hook_shape "$i" || continue
    CANDIDATES="$(jq -r --arg event "$EVENT" \
      '(.hooks // {})[$event] // [] | map(.hooks // []) | flatten | map(.command // "") | .[]' \
      "$LEGACY_PROJECT_SETTINGS" 2>/dev/null)"
    TO_REMOVE=()
    while IFS= read -r cand; do
      [ -n "$cand" ] || continue
      candidate_is_owned "$cand" "all" && TO_REMOVE+=("$cand")
    done <<< "$CANDIDATES"
    [ "${#TO_REMOVE[@]}" -gt 0 ] || continue

    remove_exact_commands "$LEGACY_PROJECT_SETTINGS" "$EVENT"
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
