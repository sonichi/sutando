#!/bin/bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: gemini-run.sh [options] -- [prompt]

Wrap the local Gemini CLI from the current repo.

Options:
  --check                       Verify the gemini CLI is installed and show auth-related env hints
  --model <model>               Pass `--model` to gemini
  --approval-mode <mode>        default | auto_edit | yolo | plan
  --output-format <format>      text | json | stream-json
  --cd <dir>                    Working directory for the Gemini run
  --sandbox                     Enable Gemini sandbox mode
  --include-directory <dir>     Additional workspace directory to include (repeatable)
  --help                        Show this help

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

if ! command -v gemini >/dev/null 2>&1; then
  fail "gemini CLI not found in PATH"
fi

if [[ "$CHECK" -eq 1 ]]; then
  echo "gemini: $(command -v gemini)"
  if [[ -n "${GEMINI_API_KEY:-}" ]]; then
    echo "auth: GEMINI_API_KEY present"
  elif [[ -n "${GOOGLE_API_KEY:-}" ]]; then
    echo "auth: GOOGLE_API_KEY present"
  else
    echo "auth: no Gemini API key env var detected; relying on Gemini CLI local login/config if present"
  fi
  exit 0
fi

if [[ ! -d "$WORKDIR" ]]; then
  fail "working directory does not exist: $WORKDIR"
fi

PROMPT="${PROMPT_ARGS[*]-}"
[[ -n "$PROMPT" ]] || fail "prompt required unless --check is used"

cmd=(gemini --prompt "$PROMPT" --approval-mode "$APPROVAL_MODE" --output-format "$OUTPUT_FORMAT")
[[ -n "$MODEL" ]] && cmd+=(--model "$MODEL")
[[ "$USE_SANDBOX" -eq 1 ]] && cmd+=(--sandbox)
for dir in "${INCLUDE_DIRS[@]}"; do
  cmd+=(--include-directories "$dir")
done

(
  cd "$WORKDIR"
  "${cmd[@]}"
)

