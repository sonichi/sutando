# shellcheck shell=bash
# Shared resolver for a Claude Code hook's transcript path. Source, don't exec.
#
# Claude Code passes transcript_path via stdin JSON ONLY — there is no
# $TRANSCRIPT_PATH env var. Two hooks need that fact (session-handoff.sh and
# archive-transcript.sh), and each carried its own copy of the parse until
# #4001; the next change to hook-payload parsing would have landed on one
# reader and missed the other silently.
#
# This owns RESOLUTION only. Each caller keeps its own policy for an empty
# result — session-handoff falls through to --latest, archive-transcript exits
# loud — so behaviour is unchanged by centralising the parse.
#
#   resolve_hook_transcript_path "$explicit"   # echoes the path, possibly empty
#
# stdin is consumed when read, so call this at most once per process.

resolve_hook_transcript_path() {
  explicit="${1:-}"
  if [ -n "$explicit" ]; then          # manual invocation wins; stdin untouched
    printf '%s' "$explicit"
    return 0
  fi
  # `[ ! -t 0 ]` = stdin is piped (a hook), not a terminal (interactive run).
  if [ ! -t 0 ]; then
    python3 -c 'import json,sys; print(json.load(sys.stdin).get("transcript_path") or "")' 2>/dev/null || true
  fi
}
