#!/bin/bash
# Shared resolver for a Claude Code hook's transcript path. Source, don't exec.
#
# Claude Code passes transcript_path via stdin JSON ONLY — there is no
# $TRANSCRIPT_PATH env var. This owns resolution; the caller decides what an empty result means.
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
