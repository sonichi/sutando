#!/bin/bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: gemini-run.sh [options] -- [prompt]

Wrap the local Antigravity CLI (`agy`) from the current repo. The standalone `gemini` CLI this
script used to wrap is retired (Google folded it into Antigravity CLI in 2026); `agy` is its
replacement and is what this script drives now. If `agy` is not installed but the legacy `gemini`
CLI still is, this script falls back to driving `gemini` directly with its original flags so a
gemini-only host keeps working.

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

# The user-local install (~/.local/bin) is not on PATH in cron/bridge shells.
AGY_BIN="$(command -v agy 2>/dev/null || true)"
[[ -z "$AGY_BIN" && -x "$HOME/.local/bin/agy" ]] && AGY_BIN="$HOME/.local/bin/agy"

# Legacy fallback: a gemini-only host (agy not installed) keeps working via the
# original gemini CLI, since the skill's manifest is still stable 1.0.0.
GEMINI_BIN="$(command -v gemini 2>/dev/null || true)"

if [[ -n "$AGY_BIN" ]]; then
  BACKEND="agy"
elif [[ -n "$GEMINI_BIN" ]]; then
  BACKEND="gemini"
else
  fail "agy (Antigravity CLI) not found in PATH, and no legacy gemini CLI fallback found either"
fi

case "$APPROVAL_MODE" in
  default|auto_edit|yolo|plan) : ;;
  *) fail "unknown --approval-mode: $APPROVAL_MODE (expected default|auto_edit|yolo|plan)" ;;
esac

if [[ "$CHECK" -eq 1 ]]; then
  if [[ "$BACKEND" == "agy" ]]; then
    echo "agy: $AGY_BIN"
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
  else
    echo "agy: not found in PATH or \$HOME/.local/bin/agy; falling back to legacy gemini CLI"
    echo "gemini: $GEMINI_BIN"
    if [[ -n "${GEMINI_API_KEY:-}" ]]; then
      echo "auth: GEMINI_API_KEY present"
    elif [[ -n "${GOOGLE_API_KEY:-}" ]]; then
      echo "auth: GOOGLE_API_KEY present"
    else
      echo "auth: no Gemini API key env var detected; relying on Gemini CLI local login/config if present"
    fi
  fi
  exit 0
fi

if [[ ! -d "$WORKDIR" ]]; then
  fail "working directory does not exist: $WORKDIR"
fi

PROMPT="${PROMPT_ARGS[*]-}"
[[ -n "$PROMPT" ]] || fail "prompt required unless --check is used"

if [[ "$BACKEND" == "agy" ]]; then
  cmd=("$AGY_BIN" --prompt "$PROMPT" --output-format "$OUTPUT_FORMAT")
  case "$APPROVAL_MODE" in
    plan) cmd+=(--mode plan) ;;
    auto_edit) cmd+=(--mode accept-edits) ;;
    yolo) cmd+=(--dangerously-skip-permissions) ;;
    default) : ;;  # agy's own interactive-approval default -- no extra flag
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
else
  # Legacy gemini backend -- parent commit's original argv mapping, preserved verbatim.
  cmd=("$GEMINI_BIN" --prompt "$PROMPT" --approval-mode "$APPROVAL_MODE" --output-format "$OUTPUT_FORMAT")
  [[ -n "$MODEL" ]] && cmd+=(--model "$MODEL")
  [[ "$USE_SANDBOX" -eq 1 ]] && cmd+=(--sandbox)
  if [[ ${#INCLUDE_DIRS[@]} -gt 0 ]]; then
    for dir in "${INCLUDE_DIRS[@]}"; do
      cmd+=(--include-directories "$dir")
    done
  fi
fi

(
  cd "$WORKDIR"
  "${cmd[@]}"
)

