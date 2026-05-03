#!/bin/bash
# Sutando init — idempotent first-run + every-start bootstrap.
# Usage:
#   bash src/init.sh             # Tier 1 + Tier 2 (verbose)
#   bash src/init.sh --auto      # Tier 1 only (silent, called from startup.sh)
#   bash src/init.sh --preflight # Tier 2 only (env + perms + tools)
#
# Tier 1: create-if-missing files and dirs. Never clobbers existing content.
# Tier 2: preflight checks. Warns loudly but never blocks startup.

set -e

REPO="${SUTANDO_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
MODE="${1:-full}"

case "$MODE" in
  --auto|--preflight|--full|full) ;;
  *) echo "Usage: bash src/init.sh [--auto | --preflight]"; exit 2;;
esac

log() {
  # Quiet under --auto unless we're actually creating something
  if [ "$MODE" != "--auto" ]; then echo "$@"; fi
}

create_file_if_missing() {
  local path="$1"; local body="$2"
  if [ ! -f "$REPO/$path" ]; then
    mkdir -p "$(dirname "$REPO/$path")"
    printf '%s' "$body" > "$REPO/$path"
    echo "  ✓ created $path"
  fi
}

create_dir_if_missing() {
  local path="$1"
  if [ ! -d "$REPO/$path" ]; then
    mkdir -p "$REPO/$path"
    echo "  ✓ created $path/"
  fi
}

copy_if_missing() {
  local src="$1"; local dst="$2"
  if [ ! -f "$REPO/$dst" ] && [ -f "$REPO/$src" ]; then
    cp "$REPO/$src" "$REPO/$dst"
    echo "  ✓ created $dst (from $src)"
  fi
}

# --- Tier 1: auto-bootstrap (always safe to run) ---
tier1() {
  log "Tier 1 — auto-bootstrap..."

  # Directories
  create_dir_if_missing "logs"
  create_dir_if_missing "state"
  create_dir_if_missing "tasks"
  create_dir_if_missing "results"
  create_dir_if_missing "results/archive"
  create_dir_if_missing "results/calls"
  create_dir_if_missing "notes"
  create_dir_if_missing "data"

  # Files — placeholders only, content added by the agent later
  create_file_if_missing "build_log.md" \
    "# Sutando build log

Notes on what's built, what's next, and known issues. The proactive loop reads + updates this each pass.
"

  create_file_if_missing "pending-questions.md" \
    "# Pending Questions

_(none open)_
"

  create_file_if_missing "contextual-chips.json" \
    "{\"chips\":[],\"ts\":$(date +%s)}
"

  create_file_if_missing "core-status.json" \
    "{\"status\":\"idle\",\"ts\":$(date +%s)}
"

  create_file_if_missing "voice-state.json" \
    "{\"connected\":false,\"ts\":$(date +%s)}
"

  # crons.json — copy from the example if present
  copy_if_missing "skills/schedule-crons/crons.example.json" "skills/schedule-crons/crons.json"
}

# --- Tier 2: preflight (warn, don't block) ---
preflight() {
  log "Tier 2 — preflight checks..."

  local required_ok=0
  local required_total=0
  local optional_ok=0
  local optional_total=0
  local cli_missing=()

  # .env required keys
  required_total=$((required_total + 1))
  if [ -f "$REPO/.env" ]; then
    if grep -qE '^GEMINI_API_KEY=.+' "$REPO/.env"; then
      required_ok=$((required_ok + 1))
    else
      log "  ✗ GEMINI_API_KEY missing from .env (required for voice)"
    fi
  else
    log "  ✗ .env missing (cp .env.example .env if it exists)"
  fi

  # .env optional keys — count what's set in the repo .env
  local optional_keys="TWILIO_ACCOUNT_SID NGROK_DOMAIN CARTESIA_API_KEY X_API_KEY ANTHROPIC_API_KEY GOOGLE_APPLICATION_CREDENTIALS"
  for key in $optional_keys; do
    optional_total=$((optional_total + 1))
    if [ -f "$REPO/.env" ] && grep -qE "^${key}=.+" "$REPO/.env"; then
      optional_ok=$((optional_ok + 1))
    fi
  done

  # External channel envs — Discord / Telegram bot tokens live outside the repo .env
  optional_total=$((optional_total + 1))
  if [ -f "$HOME/.claude/channels/discord/.env" ] && grep -qE '^DISCORD_BOT_TOKEN=.+' "$HOME/.claude/channels/discord/.env"; then
    optional_ok=$((optional_ok + 1))
  fi
  optional_total=$((optional_total + 1))
  if [ -f "$HOME/.claude/channels/telegram/.env" ] && grep -qE '^TELEGRAM_BOT_TOKEN=.+' "$HOME/.claude/channels/telegram/.env"; then
    optional_ok=$((optional_ok + 1))
  fi

  # CLI tools
  for tool in node npx python3 fswatch claude gh; do
    if ! command -v "$tool" > /dev/null 2>&1; then
      cli_missing+=("$tool")
    fi
  done

  # macOS permissions — non-fatal, just a hint
  local perms_warn=0
  if ! screencapture -x /tmp/sutando-permcheck.png 2>/dev/null; then
    log "  ⚠ Screen Recording not granted (System Settings → Privacy → Screen Recording)"
    perms_warn=1
  else
    rm -f /tmp/sutando-permcheck.png
  fi
  if ! osascript -e 'tell application "System Events" to get name of first process whose frontmost is true' > /dev/null 2>&1; then
    log "  ⚠ Accessibility not granted (System Settings → Privacy → Accessibility)"
    perms_warn=1
  fi

  # One-line summary regardless of mode (this is the value-add)
  local cli_str="all-ok"
  if [ ${#cli_missing[@]} -gt 0 ]; then cli_str="missing: ${cli_missing[*]}"; fi
  local perms_str="ok"
  if [ "$perms_warn" -eq 1 ]; then perms_str="incomplete"; fi
  echo "[Preflight] required=${required_ok}/${required_total}  optional=${optional_ok}/${optional_total}  cli=${cli_str}  perms=${perms_str}"
}

case "$MODE" in
  --auto)       tier1 ;;
  --preflight)  preflight ;;
  *)            tier1; preflight ;;
esac
