#!/bin/bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: route-ai.sh [options] -- [prompt]

Route a prompt to codex or gemini.

Options:
  --check                 Verify both local wrappers are present
  --engine <name>         Force codex or gemini
  --dry-run               Print the selected engine and command without running it
  --cd <dir>              Working directory for the delegated run
  --help                  Show this help

Examples:
  route-ai.sh -- "Review the current diff for bugs"
  route-ai.sh --engine gemini -- "Trace dependencies across this repo"
EOF
}

fail() {
  echo "route-ai.sh: $*" >&2
  exit 1
}

require_arg() {
  local flag="$1"
  local value="${2:-}"
  [[ -n "$value" ]] || fail "missing value for $flag"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
CODEX_WRAPPER="$SKILLS_DIR/claude-codex/scripts/codex-run.sh"
GEMINI_WRAPPER="$SKILLS_DIR/claude-gemini/scripts/gemini-run.sh"

CHECK=0
DRY_RUN=0
ENGINE=""
WORKDIR="$PWD"
PROMPT_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      CHECK=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --engine)
      require_arg "$1" "${2:-}"
      ENGINE="$2"
      shift 2
      ;;
    --cd)
      require_arg "$1" "${2:-}"
      WORKDIR="$2"
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

[[ -x "$CODEX_WRAPPER" ]] || fail "missing Codex wrapper: $CODEX_WRAPPER"
[[ -x "$GEMINI_WRAPPER" ]] || fail "missing Gemini wrapper: $GEMINI_WRAPPER"

if [[ "$CHECK" -eq 1 ]]; then
  echo "codex-wrapper: $CODEX_WRAPPER"
  echo "gemini-wrapper: $GEMINI_WRAPPER"
  bash "$CODEX_WRAPPER" --check
  bash "$GEMINI_WRAPPER" --check
  exit 0
fi

PROMPT="${PROMPT_ARGS[*]-}"
[[ -n "$PROMPT" ]] || fail "prompt required unless --check is used"
[[ -d "$WORKDIR" ]] || fail "working directory does not exist: $WORKDIR"

PROMPT_LC="$(printf '%s' "$PROMPT" | tr '[:upper:]' '[:lower:]')"

if [[ -z "$ENGINE" ]]; then
  if [[ "$PROMPT_LC" == *"codex"* ]]; then
    ENGINE="codex"
  elif [[ "$PROMPT_LC" == *"gemini"* ]]; then
    ENGINE="gemini"
  elif [[ "$PROMPT_LC" == *"review"* ]] || [[ "$PROMPT_LC" == *"diff"* ]] || [[ "$PROMPT_LC" == *"regression"* ]] || [[ "$PROMPT_LC" == *"bug"* ]] || [[ "$PROMPT_LC" == *"implement"* ]] || [[ "$PROMPT_LC" == *"patch"* ]]; then
    ENGINE="codex"
  elif [[ "$PROMPT_LC" == *"architecture"* ]] || [[ "$PROMPT_LC" == *"repo-wide"* ]] || [[ "$PROMPT_LC" == *"entire repo"* ]] || [[ "$PROMPT_LC" == *"whole repo"* ]] || [[ "$PROMPT_LC" == *"trace"* ]] || [[ "$PROMPT_LC" == *"dependency"* ]] || [[ "$PROMPT_LC" == *"dependencies"* ]] || [[ "$PROMPT_LC" == *"summarize"* ]] || [[ "$PROMPT_LC" == *"json"* ]] || [[ "$PROMPT_LC" == *"multimodal"* ]]; then
    ENGINE="gemini"
  else
    ENGINE="codex"
  fi
fi

case "$ENGINE" in
  codex)
    cmd=(bash "$CODEX_WRAPPER" --cd "$WORKDIR" -- "$PROMPT")
    ;;
  gemini)
    cmd=(bash "$GEMINI_WRAPPER" --cd "$WORKDIR" --approval-mode plan -- "$PROMPT")
    ;;
  *)
    fail "unsupported engine: $ENGINE"
    ;;
esac

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "engine: $ENGINE"
  printf 'command:'
  for part in "${cmd[@]}"; do
    printf ' %q' "$part"
  done
  printf '\n'
  exit 0
fi

"${cmd[@]}"

