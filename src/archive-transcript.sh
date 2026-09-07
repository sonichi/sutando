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

TRANSCRIPT="${2:-}"  # Optional explicit path (manual invocations)
if [ -z "$TRANSCRIPT" ] && [ ! -t 0 ]; then
  TRANSCRIPT="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("transcript_path") or "")' 2>/dev/null || true)"
fi

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
