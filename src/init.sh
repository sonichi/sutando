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

# Resolve runtime workspace. Same resolution shape as src/workspace_default.py
# and startup.sh: $SUTANDO_WORKSPACE override (tilde-expanded), fallback to
# ~/.sutando/workspace/. Runtime state files (logs, state, tasks, results,
# notes, data, pending-questions.md, …) live here; loose status .json files
# (core-status.json, voice-state.json, …) live under state/. Repo stays for
# the code + skills + the schedule-crons.json copy below.
if [ -n "${SUTANDO_WORKSPACE:-}" ]; then
  WORKSPACE="${SUTANDO_WORKSPACE/#\~/$HOME}"
else
  WORKSPACE="$HOME/.sutando/workspace"
fi

case "$MODE" in
  --auto|--preflight|--full|full) ;;
  *) echo "Usage: bash src/init.sh [--auto | --preflight]"; exit 2;;
esac

log() {
  # Quiet under --auto unless we're actually creating something
  if [ "$MODE" != "--auto" ]; then echo "$@"; fi
}

# Workspace-rooted helpers — runtime state (logs, state, tasks, results, …).
create_file_if_missing() {
  local path="$1"; local body="$2"
  if [ ! -f "$WORKSPACE/$path" ]; then
    mkdir -p "$(dirname "$WORKSPACE/$path")"
    printf '%s' "$body" > "$WORKSPACE/$path"
    echo "  ✓ created $path"
  fi
}

create_dir_if_missing() {
  local path="$1"
  if [ ! -d "$WORKSPACE/$path" ]; then
    mkdir -p "$WORKSPACE/$path"
    echo "  ✓ created $path/"
  fi
}

# Repo-rooted copy helper — for shipping example configs from the checkout
# into a stable location. Used today only for skills/schedule-crons/crons.json
# which lives in the repo, NOT the workspace.
copy_if_missing() {
  local src="$1"; local dst="$2"
  if [ ! -f "$REPO/$dst" ] && [ -f "$REPO/$src" ]; then
    cp "$REPO/$src" "$REPO/$dst"
    echo "  ✓ created $dst (from $src)"
  fi
}

# One-time migration of stale repo-root runtime state into $WORKSPACE. Fires
# only when the migration sentinel is absent — same idempotent posture as
# workspace_default.py's _migrate_from_legacy (PR #762). Non-destructive on
# collision: if a workspace copy already exists at the destination, the repo
# copy is left in place (so a partial migration on a prior pass never
# clobbers fresh workspace writes).
#
# Surfaces a single stderr line per moved item + writes a sentinel
# `$WORKSPACE/.legacy-migrated-911` after a successful sweep. Second run
# sees the sentinel and bails — no log noise on every startup.
#
# Triggered by Susan's PR #913 review (2026-05-19). Without this, installs
# that pre-date #911 keep two copies: stale repo-root logs/state/tasks/...
# alongside the new workspace copies. git status pollution + future
# debugging confusion.
migrate_legacy_runtime_state() {
  local sentinel="$WORKSPACE/.legacy-migrated-911"
  if [ -f "$sentinel" ]; then
    return 0
  fi
  # Only migrate when the legacy repo actually has runtime state. New
  # installs (post-#911) skip this path entirely.
  local have_evidence=0
  for d in logs state tasks results notes data; do
    if [ -d "$REPO/$d" ] && [ -n "$(ls -A "$REPO/$d" 2>/dev/null)" ]; then
      have_evidence=1
      break
    fi
  done
  if [ "$have_evidence" -eq 0 ]; then
    # Nothing to migrate; write sentinel so we don't re-check every run.
    mkdir -p "$WORKSPACE"
    : > "$sentinel"
    return 0
  fi
  mkdir -p "$WORKSPACE"
  local moved_any=0
  # Dirs: move whole tree iff workspace target doesn't already exist.
  for d in logs state tasks results notes data; do
    local src="$REPO/$d"
    local dst="$WORKSPACE/$d"
    if [ -d "$src" ] && [ ! -e "$dst" ]; then
      if mv "$src" "$dst" 2>/dev/null; then
        echo "  → migrated $d/ from repo to workspace" >&2
        moved_any=1
      fi
    fi
  done
  # Top-level state files: move iff workspace target absent.
  for f in pending-questions.md core-status.json contextual-chips.json voice-state.json build_log.md; do
    local src="$REPO/$f"
    local dst="$WORKSPACE/$f"
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

# One-time sweep of loose workspace-root status .json files into state/.
# Bash twin of workspace_default.py's _migrate_root_status — shares the
# `.status-migrated` sentinel so whichever runs first does the move and the
# other short-circuits. init.sh runs before the Python services (startup.sh),
# so without this sweep the create_file_if_missing calls below would seed a
# fresh state/ file and strand a real workspace-root copy. Non-destructive on
# collision; file list matches _STATUS_FILES.
migrate_root_status_to_state() {
  local sentinel="$WORKSPACE/.status-migrated"
  if [ -f "$sentinel" ]; then
    return 0
  fi
  mkdir -p "$WORKSPACE/state"
  local moved_any=0
  # Single source of truth for this list is `_STATUS_FILES` in
  # src/workspace_default.py — keep the two in sync. Adding a 6th status
  # file there without adding it here (or vice versa) silently drifts the
  # Python and bash migrator twins.
  for f in core-status.json voice-state.json contextual-chips.json dynamic-content.json quota-state.json; do
    local src="$WORKSPACE/$f"
    local dst="$WORKSPACE/state/$f"
    if [ -f "$src" ] && [ ! -e "$dst" ]; then
      if mv "$src" "$dst" 2>/dev/null; then
        echo "  → migrated $f into state/" >&2
        moved_any=1
      fi
    fi
  done
  : > "$sentinel"
  if [ "$moved_any" -eq 1 ]; then
    echo "  ✓ workspace-root status files swept into state/" >&2
  fi
}

# --- Tier 1: auto-bootstrap (always safe to run) ---
tier1() {
  log "Tier 1 — auto-bootstrap..."

  # First-run sweep: any stale repo-root runtime state lands in workspace
  # before we start creating fresh files. Idempotent + non-destructive.
  migrate_legacy_runtime_state
  # Then sweep loose workspace-root status files into state/ — must run
  # before the create_file_if_missing calls below.
  migrate_root_status_to_state

  # Directories
  create_dir_if_missing "logs"
  create_dir_if_missing "state"
  create_dir_if_missing "tasks"
  create_dir_if_missing "results"
  create_dir_if_missing "results/archive"
  create_dir_if_missing "results/calls"
  create_dir_if_missing "notes"
  create_dir_if_missing "data"

  # Files — placeholders only, content added by the agent later.
  # build_log.md lives under $SUTANDO_WORKSPACE per workspace contract; seeded
  # there by workspace_default.py + dashboard/health-check readers expect it
  # at WORKSPACE_DIR / "build_log.md". Not seeded here.

  create_file_if_missing "pending-questions.md" \
    "# Pending Questions

_(none open)_
"

  # Status files live under state/ (the workspace root is structural —
  # directories only). create_file_if_missing mkdir's the parent.
  create_file_if_missing "state/contextual-chips.json" \
    "{\"chips\":[],\"ts\":$(date +%s)}
"

  create_file_if_missing "state/core-status.json" \
    "{\"status\":\"idle\",\"ts\":$(date +%s)}
"

  create_file_if_missing "state/voice-state.json" \
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

  # macOS permissions — non-fatal, just a hint.
  # macOS 15+ silently writes a tiny PNG when perm is denied (exit 0).
  # Denied artifacts are <2KB; real captures are hundreds-of-KB to MB.
  # Black 5120x2880 PNG compresses to ~43KB, so 5KB is the safe floor.
  local perms_warn=0
  local permcheck_ok=1
  screencapture -x /tmp/sutando-permcheck.png 2>/dev/null || permcheck_ok=0
  if [ "$permcheck_ok" -eq 1 ]; then
    # wc -c is portable across BSD (macOS) and GNU coreutils (Homebrew may override).
    local permcheck_size
    permcheck_size=$(wc -c < /tmp/sutando-permcheck.png 2>/dev/null | tr -d ' ' || echo 0)
    if [ "${permcheck_size:-0}" -lt 5000 ]; then permcheck_ok=0; fi
  fi
  rm -f /tmp/sutando-permcheck.png
  if [ "$permcheck_ok" -eq 0 ]; then
    log "  ⚠ Screen Recording not granted (System Settings → Privacy → Screen Recording → grant the app running this terminal, then quit + relaunch it)"
    perms_warn=1
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
