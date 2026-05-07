#!/bin/bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: codex-run.sh [options] -- [prompt]

Wrap the local Codex CLI from the current repo.

Options:
  --check                         Verify the codex CLI is installed and logged in
  --review                        Use `codex review` instead of `codex exec`
  --uncommitted                   Review uncommitted changes
  --base <branch>                 Review changes against a base branch
  --goal                          Spec-driven one-shot build mode (prepends `/goal` to prompt,
                                  defaults sandbox to workspace-write, enables --full-auto).
                                  Use for self-contained HTML/CSS/JS prototypes from a tight spec.
  --model <model>                 Pass `-m` to `codex exec`
  --sandbox <mode>                read-only | workspace-write | danger-full-access
  --cd <dir>                      Working directory to hand to Codex
  --full-auto                     Pass `--full-auto` to `codex exec`
  --json                          Pass `--json` to `codex exec`
  --output-last-message <file>    Write the last Codex message to a file
  --help                          Show this help

Examples:
  codex-run.sh -- "Find the likely cause of the failing tests"
  codex-run.sh --review --uncommitted -- "Review for bugs and missing tests"
  codex-run.sh --goal -- "$(cat spec.md)"
EOF
}

fail() {
  echo "codex-run.sh: $*" >&2
  exit 1
}

require_arg() {
  local flag="$1"
  local value="${2:-}"
  [[ -n "$value" ]] || fail "missing value for $flag"
}

CHECK=0
MODE="exec"
UNCOMMITTED=0
FULL_AUTO=0
JSON=0
GOAL=0
BASE=""
MODEL=""
SANDBOX="workspace-write"
WORKDIR="${PWD}"
OUTPUT_LAST_MESSAGE=""
PROMPT_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      CHECK=1
      shift
      ;;
    --review)
      MODE="review"
      shift
      ;;
    --uncommitted)
      UNCOMMITTED=1
      shift
      ;;
    --goal)
      GOAL=1
      FULL_AUTO=1
      shift
      ;;
    --base)
      require_arg "$1" "${2:-}"
      BASE="$2"
      shift 2
      ;;
    --model)
      require_arg "$1" "${2:-}"
      MODEL="$2"
      shift 2
      ;;
    --sandbox)
      require_arg "$1" "${2:-}"
      SANDBOX="$2"
      shift 2
      ;;
    --cd)
      require_arg "$1" "${2:-}"
      WORKDIR="$2"
      shift 2
      ;;
    --full-auto)
      FULL_AUTO=1
      shift
      ;;
    --json)
      JSON=1
      shift
      ;;
    --output-last-message)
      require_arg "$1" "${2:-}"
      OUTPUT_LAST_MESSAGE="$2"
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

if ! command -v codex >/dev/null 2>&1; then
  fail "codex CLI not found in PATH"
fi

if [[ "$CHECK" -eq 1 ]]; then
  echo "codex: $(command -v codex)"
  codex login status
  exit 0
fi

if [[ ! -d "$WORKDIR" ]]; then
  fail "working directory does not exist: $WORKDIR"
fi

[[ "$GOAL" -eq 1 && "$MODE" == "review" ]] && fail "--goal cannot be combined with --review"

PROMPT="${PROMPT_ARGS[*]-}"

if [[ "$MODE" == "review" ]]; then
  cmd=(codex review)
  [[ "$UNCOMMITTED" -eq 1 ]] && cmd+=(--uncommitted)
  [[ -n "$BASE" ]] && cmd+=(--base "$BASE")
  [[ -n "$PROMPT" ]] && cmd+=("$PROMPT")
  (
    cd "$WORKDIR"
    "${cmd[@]}"
  )
  exit $?
fi

[[ -n "$PROMPT" ]] || fail "prompt required unless --check is used"

if [[ "$GOAL" -eq 1 && "$PROMPT" != /goal\ * ]]; then
  PROMPT="/goal $PROMPT"
fi

cmd=(codex exec -C "$WORKDIR" -s "$SANDBOX" --skip-git-repo-check)
[[ -n "$MODEL" ]] && cmd+=(-m "$MODEL")
[[ "$FULL_AUTO" -eq 1 ]] && cmd+=(--full-auto)
[[ "$JSON" -eq 1 ]] && cmd+=(--json)
[[ -n "$OUTPUT_LAST_MESSAGE" ]] && cmd+=(-o "$OUTPUT_LAST_MESSAGE")
cmd+=("$PROMPT")

"${cmd[@]}"

