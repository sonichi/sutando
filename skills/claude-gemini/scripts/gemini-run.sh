#!/bin/bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: gemini-run.sh [options] -- [prompt]

Wrap the local Antigravity CLI (`agy`) from the current repo. The standalone `gemini` CLI this
script used to wrap is retired (Google folded it into Antigravity CLI in 2026); `agy` is its
replacement and is what this script drives now.

Options:
  --check                       Verify the agy CLI is installed and show auth-related hints
  --model <model>               Pass `--model` to agy
  --approval-mode <mode>        default | auto_edit | yolo | plan  (maps to agy's --mode /
                                 --dangerously-skip-permissions — see below)
  --output-format <format>      text | json | stream-json
  --cd <dir>                    Working directory for the Gemini run
  --sandbox                     Enable agy's sandbox mode
  --include-directory <dir>     Additional workspace directory to include (repeatable)
  --help                        Show this help

--approval-mode mapping (agy has no direct --approval-mode flag; this script translates):
  plan       -> --mode plan                       (read-only, agy's own default-safe mode)
  auto_edit  -> --mode accept-edits                (agy auto-approves edits, still asks for the rest)
  yolo       -> --dangerously-skip-permissions      (agy auto-approves everything, no --mode)
  default    -> neither flag passed                (agy's own interactive-approval default)

Examples:
  gemini-run.sh -- "Audit the handoff flow in this repository"
  gemini-run.sh --output-format json -- "Summarize likely failure modes"
EOF
}

fail() {
  echo "gemini-run.sh: $*" >&2
  exit 1
}

require_arg() {
  local flag="$1"
  local value="${2:-}"
  [[ -n "$value" ]] || fail "missing value for $flag"
}

CHECK=0
MODEL=""
APPROVAL_MODE="plan"
OUTPUT_FORMAT="text"
WORKDIR="${PWD}"
USE_SANDBOX=0
INCLUDE_DIRS=()
PROMPT_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      CHECK=1
      shift
      ;;
    --model)
      require_arg "$1" "${2:-}"
      MODEL="$2"
      shift 2
      ;;
    --approval-mode)
      require_arg "$1" "${2:-}"
      APPROVAL_MODE="$2"
      shift 2
      ;;
    --output-format)
      require_arg "$1" "${2:-}"
      OUTPUT_FORMAT="$2"
      shift 2
      ;;
    --cd)
      require_arg "$1" "${2:-}"
      WORKDIR="$2"
      shift 2
      ;;
    --sandbox)
      USE_SANDBOX=1
      shift
      ;;
    --include-directory)
      require_arg "$1" "${2:-}"
      INCLUDE_DIRS+=("$2")
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --)
      shift
      PROMPT_ARGS+=("$@")
      break
      ;;
    *)
      PROMPT_ARGS+=("$1")
      shift
      ;;
  esac
done

if ! command -v agy >/dev/null 2>&1; then
  fail "agy (Antigravity CLI) not found in PATH"
fi

if [[ "$CHECK" -eq 1 ]]; then
  echo "agy: $(command -v agy)"
  # agy's Gemini-API-key path needs BOTH the settings.json provider AND the env var --
  # the env var alone does nothing, which is easy to miss, so check both explicitly.
  SETTINGS_FILE="${HOME}/.gemini/antigravity-cli/settings.json"
  if [[ -f "$SETTINGS_FILE" ]] && grep -q '"modelProvider"[[:space:]]*:[[:space:]]*"gemini"' "$SETTINGS_FILE" 2>/dev/null; then
    echo "settings: modelProvider=gemini set in $SETTINGS_FILE"
  else
    echo "settings: modelProvider=gemini NOT set in $SETTINGS_FILE -- GEMINI_API_KEY alone will not authenticate agy"
  fi
  if [[ -n "${GEMINI_API_KEY:-}" ]]; then
    echo "auth: GEMINI_API_KEY present"
  else
    echo "auth: GEMINI_API_KEY not set; relying on agy's own signed-in Google auth if present"
  fi
  exit 0
fi

if [[ ! -d "$WORKDIR" ]]; then
  fail "working directory does not exist: $WORKDIR"
fi

PROMPT="${PROMPT_ARGS[*]-}"
[[ -n "$PROMPT" ]] || fail "prompt required unless --check is used"

cmd=(agy --prompt "$PROMPT" --output-format "$OUTPUT_FORMAT")
case "$APPROVAL_MODE" in
  plan) cmd+=(--mode plan) ;;
  auto_edit) cmd+=(--mode accept-edits) ;;
  yolo) cmd+=(--dangerously-skip-permissions) ;;
  default) : ;;  # agy's own interactive-approval default -- no extra flag
  *) fail "unknown --approval-mode: $APPROVAL_MODE (expected default|auto_edit|yolo|plan)" ;;
esac
[[ -n "$MODEL" ]] && cmd+=(--model "$MODEL")
[[ "$USE_SANDBOX" -eq 1 ]] && cmd+=(--sandbox)
# bash 3.2 (macOS default) treats an empty array as "unbound" under `set -u`,
# so guard on the element count before expanding INCLUDE_DIRS.
if [[ ${#INCLUDE_DIRS[@]} -gt 0 ]]; then
  for dir in "${INCLUDE_DIRS[@]}"; do
    cmd+=(--add-dir "$dir")
  done
fi

(
  cd "$WORKDIR"
  "${cmd[@]}"
)

