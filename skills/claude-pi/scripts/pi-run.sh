#!/bin/bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: pi-run.sh [options] -- [prompt]

Wrap the local Pi CLI (pi.dev) from the current repo.

Options:
  --check                       Verify the pi CLI is installed and the provider is authenticated
  --provider <name>             Provider name (default: kimi-coding)
  --model <model>               Model pattern or ID (default: kimi-for-coding)
  --mode <mode>                 Output mode: text (default) | json
  --thinking <level>            off | minimal | low | medium | high | xhigh | max
  --read-only                   Restrict pi to the read tool (no bash/edit/write)
  --cd <dir>                    Working directory for the Pi run
  --help                        Show this help

Examples:
  pi-run.sh -- "Audit the handoff flow in this repository"
  pi-run.sh --read-only -- "Summarize likely failure modes"
  pi-run.sh --provider anthropic --model claude-sonnet-5 -- "Review src/outbox.py"
EOF
}

fail() {
  echo "pi-run.sh: $*" >&2
  exit 1
}

require_arg() {
  local flag="$1"
  local value="${2:-}"
  [[ -n "$value" ]] || fail "missing value for $flag"
}

CHECK=0
PROVIDER="kimi-coding"
MODEL="kimi-for-coding"
MODE="text"
THINKING=""
READ_ONLY=0
WORKDIR="${PWD}"
PROMPT_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      CHECK=1
      shift
      ;;
    --provider)
      require_arg "$1" "${2:-}"
      PROVIDER="$2"
      shift 2
      ;;
    --model)
      require_arg "$1" "${2:-}"
      MODEL="$2"
      shift 2
      ;;
    --mode)
      require_arg "$1" "${2:-}"
      MODE="$2"
      shift 2
      ;;
    --thinking)
      require_arg "$1" "${2:-}"
      THINKING="$2"
      shift 2
      ;;
    --read-only)
      READ_ONLY=1
      shift
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

# The user-local install (~/.local/bin) is not on PATH in cron/bridge shells.
PI_BIN="$(command -v pi 2>/dev/null || true)"
[[ -z "$PI_BIN" && -x "$HOME/.local/bin/pi" ]] && PI_BIN="$HOME/.local/bin/pi"
[[ -n "$PI_BIN" ]] || fail "pi CLI not found in PATH (install: curl -fsSL https://pi.dev/install.sh | sh)"

if [[ "$CHECK" -eq 1 ]]; then
  echo "pi: $PI_BIN ($("$PI_BIN" --version))"
  echo "provider: $PROVIDER ($("$PI_BIN" auth check --provider "$PROVIDER"))"
  exit 0
fi

if [[ ! -d "$WORKDIR" ]]; then
  fail "working directory does not exist: $WORKDIR"
fi

PROMPT="${PROMPT_ARGS[*]-}"
[[ -n "$PROMPT" ]] || fail "prompt required unless --check is used"

cmd=("$PI_BIN" -p --provider "$PROVIDER" --model "$MODEL")
[[ "$MODE" != "text" ]] && cmd+=(--mode "$MODE")
[[ -n "$THINKING" ]] && cmd+=(--thinking "$THINKING")
[[ "$READ_ONLY" -eq 1 ]] && cmd+=(--tools read)
cmd+=(-- "$PROMPT")

(
  cd "$WORKDIR"
  "${cmd[@]}"
)
