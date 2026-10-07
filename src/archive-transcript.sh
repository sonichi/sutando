#!/usr/bin/env bash
# Archive the conversation transcript on PreCompact.
#
# Usage: archive-transcript.sh <dest-dir> [transcript-path]
#
# Claude Code hooks pass transcript_path via stdin JSON ONLY — there is no
# $TRANSCRIPT_PATH env var, so the bare `cp "$TRANSCRIPT_PATH" ...` this
# replaces expanded empty and wrote nothing on every compaction (#3999).
set -u

DEST="${1:-}"
if [ -z "$DEST" ]; then
  echo "✗ archive-transcript: no destination directory given" >&2
  exit 2
fi

# Resolution is shared with session-handoff.sh — one reader, so a change to
# hook-payload parsing cannot land on one and miss the other (#4001 review).
__HELPER="$(cd "$(dirname "$0")" && pwd)/hook_transcript_path.sh"
if [ -f "$__HELPER" ]; then
  # shellcheck source=hook_transcript_path.sh
  . "$__HELPER"
  TRANSCRIPT="$(resolve_hook_transcript_path "${2:-}")"
else
  echo "✗ archive-transcript: hook_transcript_path.sh not found alongside this script" >&2
  exit 7
fi
unset __HELPER

# Fail LOUD. A hook's non-zero exit is not surfaced, but silence here is what
# made the original defect invisible for as long as it was.
if [ -z "$TRANSCRIPT" ]; then
  echo "✗ archive-transcript: no transcript_path on stdin and none given as \$2 — nothing archived" >&2
  exit 3
fi
if [ ! -f "$TRANSCRIPT" ]; then
  echo "✗ archive-transcript: transcript_path does not exist: $TRANSCRIPT" >&2
  exit 4
fi

mkdir -p "$DEST" || exit 5
OUT="${DEST%/}/$(date +%Y-%m-%dT%H-%M-%S).jsonl"
cp "$TRANSCRIPT" "$OUT" || exit 6
echo "archive-transcript: $TRANSCRIPT -> $OUT"
