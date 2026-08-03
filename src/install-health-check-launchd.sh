#!/bin/bash
# Install / uninstall the launchd-supervised health-check FALLBACK job.
#
# Role: OS-level safety net for "all of Sutando is dead." Sutando.app's
# in-process Timer (PR #613) is the primary 30min health-check while the
# menu-bar app is alive. This job is the redundant supervisor that keeps
# running even when Sutando.app exits / crashes / signs out — closing the
# circular-dependency gap that motivated PR #616.
#
# What this does:
#   - Renders src/launchd/com.sutando.health-check-fallback.plist with
#     absolute paths and writes it to
#     ~/Library/LaunchAgents/com.sutando.health-check-fallback.plist
#   - Loads it via `launchctl bootstrap gui/$UID` (the modern Sequoia idiom).
#   - Result: macOS runs `python3 src/health-check.py --emit-task
#     --notify-on-fail --notify-slack --quiet` every 5min,
#     independent of any other Sutando process. Failures surface as tasks (for
#     the agent to act on), macOS notifications (so the human sees them even if
#     all of Sutando is dead), AND a direct Slack DM to the owner (remote-visible
#     self-report for outages — fires even when the core loop is wedged). The
#     Slack DM no-ops if no token / owner is configured.
#   - NOTE (#2246): the --recover-core destructive auto-restart was REMOVED from
#     the default args. It decided "wedged" from core-status.json ts freshness
#     alone and false-positived on a busy-but-healthy core, restart-looping and
#     killing in-flight tasks. Alerting is kept; auto-restart stays off until a
#     durable 2-independent-signal fix lands (#2246).
#
# What the user sees first time they install:
#   - One macOS "Background Item Added" notification banner (Apple's own UX,
#     not Sutando's). Dismissable.
#   - A new "Sutando — Health Check" entry in System Settings → General →
#     Login Items → "Allow in the Background" with a toggle. Disable any
#     time without breaking Sutando.
#
# Strictly opt-in: not called by startup.sh. Run this script when you want
# OS-supervised health detection.
#
# Usage:
#   bash src/install-health-check-launchd.sh             # install (idempotent)
#   bash src/install-health-check-launchd.sh --uninstall # remove (idempotent)
#   bash src/install-health-check-launchd.sh --status    # print job state
#
# Idempotent: re-running install bootouts the existing job before
# bootstrapping the new one, so a `git pull` that updates the template is
# picked up by re-running this script.

set -e

LABEL="com.sutando.health-check-fallback"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO/src/launchd/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"

# Resolve runtime workspace via the shared post-M0 helper (PR #1395, single
# source at src/workspace_resolve.sh). Launchd job writes its log under
# $WORKSPACE/logs/ instead of the repo-root legacy path (per PR #911's
# workspace-vs-repo split). Defensive fallback for non-checkout installs.
# Helper resolution: prefer $REPO/src/, fall back to script-sibling (cross-
# checkout safety — see init.sh comment).
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
    # Pick an interpreter that can ACTUALLY RUN health-check.py, by running it.
    #
    # This used to pick the first interpreter that EXISTS, preferring Homebrew
    # because "/usr/bin/python3 is 3.9 on older Macs and health-check.py uses
    # 3.10+ syntax". Existing is not the same as working, and the job this
    # installs is the OS-level net that catches a wedged or dead session — the
    # one component whose failure nothing else is watching. Pointing it at a
    # broken interpreter installs a job that fails every 300s into a log file
    # nobody reads, while `--status` still reports it loaded.
    #
    # Measured on a live host 2026-08-03:
    #     /opt/homebrew/bin/python3  3.14.5  -> ImportError: pyexpat (dlopen,
    #                                           libexpat symbol mismatch), so
    #                                           `import plistlib` dies and
    #                                           health-check cannot start
    #     /usr/bin/python3           3.9.6   -> runs health-check fine
    # The preference order was exactly inverted from what worked, and the 3.10+
    # rationale no longer holds: health-check.py compiles and runs on 3.9.6.
    #
    # Keep the preference order — Homebrew first is still right when it works —
    # but require each candidate to pass a probe before accepting it.
    #
    # The probe IMPORTS health-check.py without executing it. `main()` is behind
    # `if __name__ == "__main__"`, so loading the module exercises the whole
    # import chain — which is where the failure is — and runs no checks.
    #
    # Two probes that look right and are not:
    #   - `py_compile`: the file compiles fine under the broken interpreter.
    #     The failure is dlopen of a C extension at IMPORT time, not syntax.
    #   - `health-check.py --help`: there is no --help. The script ignores it
    #     and runs the FULL check, which is slow, touches the workspace, and
    #     exits non-zero on any unhealthy host — so a healthy interpreter on an
    #     unhealthy host would be rejected. (Found by running it: it reported
    #     missing .env / build_log for the probe's own working tree.)
    __probe_python() {
        [ -n "${1:-}" ] && [ -x "$1" ] || return 1
        "$1" - "$REPO/src/health-check.py" >/dev/null 2>&1 <<'PROBE'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("hc_probe", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
sys.modules["hc_probe"] = mod
spec.loader.exec_module(mod)
PROBE
    }

    # Candidates in preference order. `$SUTANDO_PYTHON_CANDIDATES` (space-
    # separated) overrides it — an operator can pin the interpreter on a host
    # where the default order is wrong, and the test suite can inject stubs to
    # verify the selection logic without depending on what is installed.
    # A pinned candidate is still PROBED: pinning says which to prefer, not
    # "skip the check", so a pin that stops working is reported rather than
    # silently installing a broken job.
    __candidates="${SUTANDO_PYTHON_CANDIDATES:-}"
    if [ -z "$__candidates" ]; then
        __candidates="/opt/homebrew/bin/python3 /usr/local/bin/python3 $(command -v python3 2>/dev/null || true) /usr/bin/python3"
    fi

    __first_existing=""
    __seen=""
    # shellcheck disable=SC2086 # deliberate word-splitting: space-separated list
    for __c in $__candidates; do
        [ -n "$__c" ] && [ -x "$__c" ] || continue
        # `command -v python3` usually resolves to one of the literals above;
        # without this the same interpreter is probed twice and reported twice.
        case " $__seen " in *" $__c "*) continue ;; esac
        __seen="$__seen $__c"
        [ -n "$__first_existing" ] || __first_existing="$__c"
        if __probe_python "$__c"; then
            echo "$__c"
            return 0
        fi
        echo "note: $__c cannot import health-check.py — trying the next candidate" >&2
    done

    if [ -n "$__first_existing" ]; then
        # Every candidate failed the probe. Refusing outright would block an
        # install over a probe that might itself be wrong (an unreadable repo, a
        # --help regression), so fall back to the old behaviour — but say so,
        # loudly, instead of installing a silently-broken job while looking fine.
        echo "WARNING: no python3 could run health-check.py. Installing with" >&2
        echo "         $__first_existing anyway; the job will likely fail every" >&2
        echo "         run. Fix the interpreter, then re-run this installer." >&2
        echo "$__first_existing"
        return 0
    fi

    echo "ERROR: no python3 found" >&2
    exit 1
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
        # Render the template. Use a delimiter unlikely to appear in paths.
        sed \
            -e "s|__REPO__|$REPO|g" \
            -e "s|__WORKSPACE__|$WORKSPACE|g" \
            -e "s|__PYTHON__|$PYTHON_BIN|g" \
            -e "s|__HOMEBREW_BIN__|$BREW_BIN|g" \
            "$TEMPLATE" > "$DEST"
        bootout_if_loaded
        launchctl bootstrap "$DOMAIN" "$DEST"
        echo "  Loaded via $SERVICE"
        echo
        echo "Sutando — Health Check (fallback) is now running every 5min."
        echo "  • Failures fire macOS notifications + write tasks/task-health-*.txt"
        echo "  • Plus a Slack DM to the owner if SLACK_BOT_TOKEN (channel/.env) + access.json are set"
        echo "  • Auto-restarts an alive-but-wedged core (guarded; keeps 1M, no-op when healthy)"
        echo "  • View status:  bash $0 --status"
        echo "  • Uninstall:    bash $0 --uninstall"
        echo "  • Disable temporarily: System Settings → General → Login Items"
        echo "    → 'Allow in the Background' → toggle off Sutando — Health Check"
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
