#!/bin/bash
# Install / uninstall the launchd-supervised cron-runner job.
#
# Role: the RELIABLE scheduler for Sutando's recurring prompts. Session
# `CronCreate` jobs are best-effort — they only fire while the Claude REPL is
# idle at the fire minute, carry jitter, and die with the session. On
# 2026-07-02 the 6:02 loop-engineering digest silently never delivered; the
# owner asked to "make the schedule reliably run". This job is that path: an
# OS-level supervisor that runs independent of any Claude session.
#
# What this does:
#   - Renders src/launchd/com.sutando.cron-runner.plist with absolute paths and
#     writes it to ~/Library/LaunchAgents/com.sutando.cron-runner.plist
#   - Loads it via `launchctl bootstrap gui/$UID` (the modern Sequoia idiom).
#   - Result: macOS runs `python3 src/cron-runner.py` every 60s, independent of
#     any Claude session. Each tick reads the per-host crons.json and emits a
#     task file into tasks/ for every `"launchd": true` entry that is DUE since
#     its last recorded fire. The streaming watcher hands each task to the
#     session, which executes the prompt and delivers the result.
#
# Ownership / no double-fire: only crons.json entries flagged `"launchd": true`
# are handled here; the session `/schedule-crons` path skips those same
# entries, so exactly one scheduler owns each cron.
#
# What the user sees first time they install:
#   - One macOS "Background Item Added" notification banner (Apple's own UX).
#   - A new entry in System Settings → General → Login Items → "Allow in the
#     Background" with a toggle. Disable any time without breaking Sutando.
#
# Claude-core hosts opt in manually. The Codex launcher calls this installer
# automatically after reconciling fixed schedules because Codex has no
# session CronCreate owner for them.
#
# Usage:
#   bash src/install-cron-runner-launchd.sh             # install (idempotent)
#   bash src/install-cron-runner-launchd.sh --uninstall # remove (idempotent)
#   bash src/install-cron-runner-launchd.sh --status    # print job state
#
# Idempotent: re-running install bootouts the existing job before bootstrapping
# the new one, so a `git pull` that updates the template is picked up by
# re-running this script.

set -e

LABEL="com.sutando.cron-runner"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO/src/launchd/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"

# Resolve runtime workspace via the shared post-M0 helper (PR #1395, single
# source at src/workspace_resolve.sh). Launchd job writes its log under
# $WORKSPACE/logs/. Defensive fallback for non-checkout installs. Helper
# resolution: prefer $REPO/src/, fall back to script-sibling (cross-checkout
# safety — see init.sh comment).
__HELPER="$REPO/src/workspace_resolve.sh"
[ -f "$__HELPER" ] || __HELPER="$(cd "$(dirname "$0")" && pwd)/workspace_resolve.sh"
if [ -f "$__HELPER" ]; then
  # shellcheck source=workspace_resolve.sh
  source "$__HELPER"
  resolve_workspace_or_die
elif [ -n "${SUTANDO_WORKSPACE:-}" ]; then
  WORKSPACE="${SUTANDO_WORKSPACE/#\~/$HOME}"
else
  echo "${0##*/}: cannot resolve workspace — workspace_resolve.sh not found and \$SUTANDO_WORKSPACE not set." >&2
  exit 1
fi
unset __HELPER

cmd="${1:-install}"

bootout_if_loaded() {
    if launchctl print "$SERVICE" >/dev/null 2>&1; then
        echo "  Existing job found, removing first..."
        launchctl bootout "$SERVICE" 2>/dev/null || true
        # bootout is async — wait for the service to actually disappear so
        # the subsequent bootstrap doesn't race.
        for _ in $(seq 1 10); do
            launchctl print "$SERVICE" >/dev/null 2>&1 || break
            sleep 0.3
        done
    fi
}

resolve_python() {
    # Prefer Homebrew python3 — NOT for the version (cron-runner.py runs on 3.9)
    # but because /usr/bin/python3 is the Xcode-CLT stub, REVIEW.md lesson 7.
    if [ -x /opt/homebrew/bin/python3 ]; then
        echo /opt/homebrew/bin/python3
    elif [ -x /usr/local/bin/python3 ]; then
        echo /usr/local/bin/python3
    elif command -v python3 >/dev/null 2>&1; then
        command -v python3
    else
        echo "ERROR: no python3 found" >&2
        exit 1
    fi
}

resolve_homebrew_bin() {
    # Apple Silicon vs Intel — both prefixes work; pick whichever exists.
    if [ -d /opt/homebrew/bin ]; then
        echo /opt/homebrew/bin
    elif [ -d /usr/local/bin ]; then
        echo /usr/local/bin
    else
        echo /usr/bin
    fi
}

case "$cmd" in
    install)
        if [ ! -f "$TEMPLATE" ]; then
            echo "ERROR: template not found: $TEMPLATE" >&2
            exit 1
        fi
        PYTHON_BIN="$(resolve_python)"
        BREW_BIN="$(resolve_homebrew_bin)"
        echo "Installing $LABEL"
        echo "  repo:    $REPO"
        echo "  python:  $PYTHON_BIN"
        echo "  brew:    $BREW_BIN"
        mkdir -p "$HOME/Library/LaunchAgents"
        mkdir -p "$WORKSPACE/logs"
        # Shared renderer: literal substitution + XML escaping + a parse
        # check, so a path with & < > | cannot install a silently-broken job.
        "$PYTHON_BIN" "$REPO/src/render_plist_template.py" "$TEMPLATE" "$DEST" \
            "REPO=$REPO" \
            "WORKSPACE=$WORKSPACE" \
            "PYTHON=$PYTHON_BIN" \
            "HOMEBREW_BIN=$BREW_BIN" || exit 1
        bootout_if_loaded
        launchctl bootstrap "$DOMAIN" "$DEST"
        echo "  Loaded via $SERVICE"

        # Post-install self-test: prove one launchd-spawned tick can WRITE the
        # workspace. A launchd agent touching a workspace under ~/Documents
        # needs the python binary to hold a Documents/Full Disk Access TCC
        # grant — without it the job looks healthy while every workspace write
        # EPERMs silently, which is exactly the silent failure this runner
        # exists to prevent. cron-runner.py persists its state file on every
        # tick whenever crons.json has entries, so a forced tick + fresh state
        # write is an end-to-end proof.
        H="$(hostname | sed 's/\..*//')"
        CRONS_FILE="$WORKSPACE/hosts/$H/crons.json"
        STATE_FILE="$WORKSPACE/state/cron-runner-state.json"
        if ! "$PYTHON_BIN" -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])) else 1)' "$CRONS_FILE" 2>/dev/null; then
            echo "  Self-test: skipped — $CRONS_FILE is missing or empty (state file"
            echo "    is only written when crons.json has entries). Re-run --status after"
            echo "    adding entries and check $STATE_FILE appears."
        else
            before_epoch="$(date +%s)"
            launchctl kickstart "$SERVICE" 2>/dev/null || true
            st_ok=""
            for _ in $(seq 1 20); do
                if [ -f "$STATE_FILE" ]; then
                    st_m="$(stat -f %m "$STATE_FILE" 2>/dev/null || echo 0)"
                    if [ "$st_m" -ge "$before_epoch" ]; then st_ok=1; break; fi
                fi
                sleep 0.5
            done
            if [ -n "$st_ok" ]; then
                echo "  Self-test: OK — forced tick wrote $STATE_FILE (workspace writable from launchd)."
            else
                echo "  Self-test: FAILED — no fresh $STATE_FILE within 10s of a forced tick."
                echo "    The job is loaded but its workspace writes are likely blocked by macOS TCC"
                echo "    (workspace under ~/Documents needs a grant for the python binary)."
                echo "    Grant: System Settings → Privacy & Security → Full Disk Access → add:"
                echo "      $PYTHON_BIN"
                echo "    then verify with: bash $0 --status"
                exit 1
            fi
        fi
        echo
        echo "Sutando — cron-runner is now running every 60s."
        echo "  • Emits tasks/task-cron-*.txt for each due \"launchd\": true crons.json entry"
        echo "  • Missed fires (asleep/off) catch up exactly once on the next tick"
        echo "  • View status:  bash $0 --status"
        echo "  • Uninstall:    bash $0 --uninstall"
        echo "  • Disable temporarily: System Settings → General → Login Items"
        echo "    → 'Allow in the Background' → toggle off Sutando — cron-runner"
        ;;
    --uninstall|uninstall)
        echo "Uninstalling $LABEL"
        bootout_if_loaded
        if [ -f "$DEST" ]; then
            rm "$DEST"
            echo "  Removed $DEST"
        else
            echo "  (no plist on disk; nothing to remove)"
        fi
        echo "Done."
        ;;
    --status|status)
        echo "Service: $SERVICE"
        if launchctl print "$SERVICE" >/dev/null 2>&1; then
            launchctl print "$SERVICE" | grep -E '^\s+(state|pid|last exit code|runs|path)' || true
        else
            echo "  (not loaded)"
        fi
        ;;
    *)
        echo "Usage: $0 [install|--uninstall|--status]" >&2
        exit 2
        ;;
esac
