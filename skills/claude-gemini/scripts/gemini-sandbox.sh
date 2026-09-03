#!/usr/bin/env bash
# Read-only Gemini sandbox for non-owner (team/other) tasks.
#
# Same contract as `codex exec --sandbox read-only -o FILE -- PROMPT`, so the
# bridge's two-stage rulebook needs no other change when sandbox.runtime is
# gemini:
#   - runs headless (no TTY, no prompts, stdin closed)
#   - read-only: --approval-mode plan makes no edits, --sandbox confines the
#     process (macOS seatbelt, Docker or Podman elsewhere)
#   - writes ONLY the final answer to FILE, nothing on failure
#   - exits with gemini's own status. A clean exit with an empty answer writes
#     no file and exits 0, which is the Stage-2 "exited 0 with no output" case.
#
# Usage: gemini-sandbox.sh --cd DIR -o FILE [--model M] -- PROMPT...
#
# Env: GEMINI_API_KEY (or whatever auth the gemini CLI is configured with).
#      SEATBELT_PROFILE defaults to restrictive-open on macOS: strict file
#      restrictions, network allowed (the model call needs it).
set -euo pipefail

WORKDIR=""
OUT=""
MODEL=""
usage() {
  sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cd) WORKDIR="${2:-}"; shift 2 ;;
    -o|--output) OUT="${2:-}"; shift 2 ;;
    --model) MODEL="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    *) echo "gemini-sandbox: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
PROMPT="${*:-}"
[[ -n "$WORKDIR" ]] || { echo "gemini-sandbox: --cd DIR is required" >&2; exit 2; }
[[ -n "$OUT" ]] || { echo "gemini-sandbox: -o FILE is required" >&2; exit 2; }
[[ -n "$PROMPT" ]] || { echo "gemini-sandbox: prompt required after --" >&2; exit 2; }
[[ -d "$WORKDIR" ]] || { echo "gemini-sandbox: working directory does not exist: $WORKDIR" >&2; exit 2; }
if ! command -v gemini >/dev/null 2>&1; then
  echo "gemini-sandbox: gemini CLI not found in PATH (npm i -g @google/gemini-cli)" >&2
  exit 127
fi

# Headless runs have nobody to answer the workspace-trust prompt.
export GEMINI_CLI_TRUST_WORKSPACE=true
if [[ "$(uname -s)" == "Darwin" ]]; then
  export SEATBELT_PROFILE="${SEATBELT_PROFILE:-restrictive-open}"
fi

cmd=(gemini --prompt "$PROMPT" --approval-mode plan --sandbox --output-format json)
[[ -n "$MODEL" ]] && cmd+=(--model "$MODEL")

set +e
raw="$(cd "$WORKDIR" && "${cmd[@]}" < /dev/null)"
rc=$?
set -e
if [[ $rc -ne 0 ]]; then
  echo "gemini-sandbox: gemini exited $rc" >&2
  exit "$rc"
fi

# The JSON envelope is {"response": str, "stats": {...}, "error"?: {...}}.
# An error object with exit 0 is still a failure, and a non-JSON body is too.
response="$(printf '%s' "$raw" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as exc:
    sys.stderr.write(f"gemini-sandbox: output is not JSON: {exc}\n")
    sys.exit(1)
err = d.get("error")
if err:
    sys.stderr.write("gemini-sandbox: gemini reported an error: %s\n" % (err,))
    sys.exit(1)
sys.stdout.write(str(d.get("response") or ""))
')" || exit 1

if [[ -z "${response//[[:space:]]/}" ]]; then
  echo "gemini-sandbox: gemini exited 0 with no output" >&2
  exit 0
fi
printf '%s\n' "$response" > "$OUT"
