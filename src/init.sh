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
# Per-machine runtime state. Resolves $SUTANDO_WORKSPACE, defaulting to
# ~/.sutando/workspace/ (the canonical workspace per docs/workspace-design.md).
STATE_ROOT="${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}"
MODE="${1:-full}"

case "$MODE" in
  --auto|--preflight|--full|full) ;;
  *) echo "Usage: bash src/init.sh [--auto | --preflight]"; exit 2;;
esac

log() {
  # Quiet under --auto unless we're actually creating something
  if [ "$MODE" != "--auto" ]; then echo "$@"; fi
}

create_state_file_if_missing() {
  local path="$1"; local body="$2"
  if [ ! -f "$STATE_ROOT/$path" ]; then
    mkdir -p "$(dirname "$STATE_ROOT/$path")"
    printf '%s' "$body" > "$STATE_ROOT/$path"
    echo "  ✓ created $path"
  fi
}

create_state_dir_if_missing() {
  local path="$1"
  if [ ! -d "$STATE_ROOT/$path" ]; then
    mkdir -p "$STATE_ROOT/$path"
    echo "  ✓ created $path/"
  fi
}

create_repo_file_if_missing() {
  # Cross-fleet-shared files (synced via SUTANDO_PRIVATE_DIR when set) keep
  # their fallback location in the repo so the agent boots cleanly even
  # without a private sync repo configured.
  local path="$1"; local body="$2"
  if [ ! -f "$REPO/$path" ]; then
    mkdir -p "$(dirname "$REPO/$path")"
    printf '%s' "$body" > "$REPO/$path"
    echo "  ✓ created $path"
  fi
}

create_repo_dir_if_missing() {
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

# One-time migration of stale repo-root runtime state into $STATE_ROOT. Fires
# only when the migration sentinel is absent — same idempotent posture as
# workspace_default.py's _migrate_from_legacy. Non-destructive on collision:
# if a workspace copy already exists at the destination, the repo copy is
# left in place (so a partial migration never clobbers fresh workspace
# writes). One stderr line per moved item; sentinel written after the sweep.
#
# Only the items this fork workspace-roots are swept (logs/state/tasks/
# results/data + the three per-machine JSON files). notes/, build_log.md,
# and pending-questions.md are intentionally NOT migrated — the fork keeps
# those repo-rooted (cross-fleet-shared via SUTANDO_PRIVATE_DIR; see the
# create_repo_* helpers above).
migrate_legacy_runtime_state() {
  local sentinel="$STATE_ROOT/.legacy-migrated-911"
  if [ -f "$sentinel" ]; then
    return 0
  fi
  # Only migrate when the legacy repo actually has runtime state. Fresh
  # installs (already workspace-rooted) skip this path entirely.
  local have_evidence=0
  for d in logs state tasks results data; do
    if [ -d "$REPO/$d" ] && [ -n "$(ls -A "$REPO/$d" 2>/dev/null)" ]; then
      have_evidence=1
      break
    fi
  done
  if [ "$have_evidence" -eq 0 ]; then
    # Nothing to migrate; write sentinel so we don't re-check every run.
    mkdir -p "$STATE_ROOT"
    : > "$sentinel"
    return 0
  fi
  mkdir -p "$STATE_ROOT"
  local moved_any=0
  # Dirs: move whole tree iff workspace target doesn't already exist.
  for d in logs state tasks results data; do
    local src="$REPO/$d"
    local dst="$STATE_ROOT/$d"
    if [ -d "$src" ] && [ ! -e "$dst" ]; then
      if mv "$src" "$dst" 2>/dev/null; then
        echo "  → migrated $d/ from repo to workspace" >&2
        moved_any=1
      fi
    fi
  done
  # Per-machine state files: move iff workspace target absent.
  for f in core-status.json contextual-chips.json voice-state.json; do
    local src="$REPO/$f"
    local dst="$STATE_ROOT/$f"
    if [ -f "$src" ] && [ ! -e "$dst" ]; then
      if mv "$src" "$dst" 2>/dev/null; then
        echo "  → migrated $f from repo to workspace" >&2
        moved_any=1
      fi
    fi
  done
  : > "$sentinel"
  if [ "$moved_any" -eq 1 ]; then
    echo "  ✓ legacy runtime state migrated (sentinel: $sentinel)" >&2
  fi
}

# --- Tier 1: auto-bootstrap (always safe to run) ---
tier1() {
  log "Tier 1 — auto-bootstrap..."

  # First-run sweep: any stale repo-root runtime state lands in the
  # workspace before we create fresh files. Idempotent + non-destructive.
  migrate_legacy_runtime_state

  # Per-machine state directories (under SUTANDO_HOME if set, else repo)
  create_state_dir_if_missing "logs"
  create_state_dir_if_missing "state"
  create_state_dir_if_missing "tasks"
  create_state_dir_if_missing "results"
  create_state_dir_if_missing "results/archive"
  create_state_dir_if_missing "results/calls"
  create_state_dir_if_missing "data"

  # Cross-fleet-shared dir (notes are synced via SUTANDO_PRIVATE_DIR when set)
  create_repo_dir_if_missing "notes"

  # Per-machine state files
  create_state_file_if_missing "contextual-chips.json" \
    "{\"chips\":[],\"ts\":$(date +%s)}
"

  create_state_file_if_missing "core-status.json" \
    "{\"status\":\"idle\",\"ts\":$(date +%s)}
"

  create_state_file_if_missing "voice-state.json" \
    "{\"connected\":false,\"ts\":$(date +%s)}
"

  # Cross-fleet-shared files — placeholders only, content added by the agent
  create_repo_file_if_missing "build_log.md" \
    "# Sutando build log

Notes on what's built, what's next, and known issues. The proactive loop reads + updates this each pass.
"

  create_repo_file_if_missing "pending-questions.md" \
    "# Pending Questions

_(none open)_
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
  for tool in node npx python3 claude gh; do
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
