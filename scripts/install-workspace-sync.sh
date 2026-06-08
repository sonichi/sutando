#!/bin/bash
# Install / uninstall a recurring workspace-vault sync job — the right scheduler
# for each OS, so you don't have to know the platform gotchas.
#
#   macOS → a per-user LaunchAgent in ~/Library/LaunchAgents/.
#   Linux → an idempotent crontab line.
#
# Why not just tell macOS users to add a crontab line?
#   1. FDA: the cron spool (/var/at/tabs) is TCC-protected. Editing a crontab
#      needs the *controlling terminal app* to have Full Disk Access, or
#      `crontab` fails with "Operation not permitted" (even though `crontab` is
#      setuid root — TCC gates on the responsible app, not euid). The failure
#      is silent-ish: the sync simply never gets scheduled.
#   2. Keychain: a LaunchAgent in the `gui/<uid>` domain runs inside the user's
#      GUI session, so it can unlock the login Keychain that `gh`-stored tokens
#      live in — sidestepping the `-25308` error plain cron/SSH hits.
#   launchd needs neither. It's also Apple's supported mechanism; cron is legacy.
#
# The job runs `sync-workspace.sh --default` (bidirectional pull+push) on an
# interval. Run `--init` once first (see docs/workspace-sync.md) — this only
# schedules the recurring tick.
#
# Usage:
#   bash scripts/install-workspace-sync.sh                  # install (idempotent)
#   bash scripts/install-workspace-sync.sh --interval 900   # seconds (default 900 = 15min)
#   bash scripts/install-workspace-sync.sh --uninstall
#   bash scripts/install-workspace-sync.sh --status
#
# Idempotent: re-running install replaces the existing job (macOS bootout +
# bootstrap; Linux rewrites the marked crontab line), so a `git pull` that
# changes this script is picked up by re-running it.

set -euo pipefail

LABEL="com.sutando.workspace-sync"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SYNC="$REPO/scripts/sync-workspace.sh"
INTERVAL=900   # seconds (15 min). Override with --interval.
CMD="install"

while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) CMD="uninstall" ;;
        --status)    CMD="status" ;;
        --install)   CMD="install" ;;
        --interval)  INTERVAL="${2:?--interval needs a value}"; shift ;;
        --interval=*) INTERVAL="${1#--interval=}" ;;
        *) echo "install-workspace-sync: unknown arg '$1'. Try --uninstall / --status / --interval N." >&2; exit 2 ;;
    esac
    shift
done

case "$INTERVAL" in
    ''|*[!0-9]*) echo "install-workspace-sync: --interval must be a positive integer (seconds)." >&2; exit 2 ;;
esac
[ "$INTERVAL" -ge 60 ] || { echo "install-workspace-sync: --interval must be >= 60 seconds." >&2; exit 2; }

[ -f "$SYNC" ] || { echo "install-workspace-sync: $SYNC not found — run from a sutando checkout." >&2; exit 1; }

# Log under the resolved workspace, not the repo (workspace-vs-repo split).
if [ -f "$REPO/scripts/sutando-config.sh" ]; then
    WORKSPACE="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null || true)"
fi
[ -n "${WORKSPACE:-}" ] || WORKSPACE="$REPO/workspace"
LOG="$WORKSPACE/logs/workspace-sync.log"

OS="$(uname -s)"

# --------------------------------------------------------------------------- #
# macOS — LaunchAgent                                                          #
# --------------------------------------------------------------------------- #
macos_plist_path() { echo "$HOME/Library/LaunchAgents/$LABEL.plist"; }
macos_service()    { echo "gui/$(id -u)/$LABEL"; }

macos_install() {
    local dest; dest="$(macos_plist_path)"
    mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$LOG")"
    cat > "$dest" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$SYNC</string>
        <string>--default</string>
    </array>
    <key>StartInterval</key>
    <integer>$INTERVAL</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>WorkingDirectory</key>
    <string>$REPO</string>
    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>GIT_SSH_COMMAND</key>
        <string>ssh -o BatchMode=yes</string>
    </dict>
</dict>
</plist>
PLIST
    # bootout first so a changed plist is picked up (idempotent).
    launchctl bootout "$(macos_service)" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$dest"
    echo "install-workspace-sync: installed LaunchAgent $LABEL (every ${INTERVAL}s)."
    echo "  plist: $dest"
    echo "  log:   $LOG"
    echo "  No Full Disk Access required. Manage it in System Settings → General →"
    echo "  Login Items → Allow in the Background."
}

macos_uninstall() {
    launchctl bootout "$(macos_service)" 2>/dev/null || true
    rm -f "$(macos_plist_path)"
    echo "install-workspace-sync: removed LaunchAgent $LABEL."
}

macos_status() {
    if launchctl print "$(macos_service)" >/dev/null 2>&1; then
        echo "install-workspace-sync: $LABEL is LOADED."
        launchctl print "$(macos_service)" 2>/dev/null | grep -E 'state|run interval|path' | sed 's/^/  /'
    else
        echo "install-workspace-sync: $LABEL is NOT loaded."
    fi
}

# --------------------------------------------------------------------------- #
# Linux / other — crontab                                                      #
# --------------------------------------------------------------------------- #
# Marker comment lets us find + replace our own line without disturbing others.
CRON_MARKER="# $LABEL (managed by install-workspace-sync.sh)"

cron_line() {
    local mins=$(( INTERVAL / 60 ))
    [ "$mins" -ge 1 ] || mins=1
    if [ "$mins" -gt 59 ]; then
        echo "install-workspace-sync: interval > 59min not expressible as */N on cron; capping at hourly." >&2
        echo "0 * * * * cd $REPO && bash $SYNC --default >> $LOG 2>&1"
    else
        echo "*/$mins * * * * cd $REPO && bash $SYNC --default >> $LOG 2>&1"
    fi
}

cron_current_without_ours() {
    # Print existing crontab minus our marker + the line after it.
    crontab -l 2>/dev/null | awk -v m="$CRON_MARKER" '
        $0 == m { skip = 2 }
        skip > 0 { skip--; next }
        { print }'
}

cron_install() {
    mkdir -p "$(dirname "$LOG")"
    { cron_current_without_ours; echo "$CRON_MARKER"; cron_line; } | crontab -
    echo "install-workspace-sync: installed crontab line (every $(( INTERVAL / 60 ))min)."
    echo "  log: $LOG"
}

cron_uninstall() {
    cron_current_without_ours | crontab -
    echo "install-workspace-sync: removed crontab line."
}

cron_status() {
    if crontab -l 2>/dev/null | grep -qF "$CRON_MARKER"; then
        echo "install-workspace-sync: crontab line PRESENT:"
        crontab -l 2>/dev/null | grep -A1 -F "$CRON_MARKER" | sed 's/^/  /'
    else
        echo "install-workspace-sync: no managed crontab line found."
    fi
}

# --------------------------------------------------------------------------- #
# Dispatch                                                                     #
# --------------------------------------------------------------------------- #
if [ "$OS" = "Darwin" ]; then
    case "$CMD" in
        install)   macos_install ;;
        uninstall) macos_uninstall ;;
        status)    macos_status ;;
    esac
else
    case "$CMD" in
        install)   cron_install ;;
        uninstall) cron_uninstall ;;
        status)    cron_status ;;
    esac
fi
