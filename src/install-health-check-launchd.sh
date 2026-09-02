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
    # Prefer Homebrew python3 — system /usr/bin/python3 is 3.9 on older
    # Macs and health-check.py uses 3.10+ syntax (per agent-api.py:115
    # comment).
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

# Can this interpreter actually RUN health-check.py? Imports it WITHOUT executing
# it: `main()` is behind `if __name__ == "__main__"`, so the whole import chain
# runs and no checks do.
#
# Two probes that look right and are not, both rejected after trying them:
#   - `py_compile`: the file compiles fine under a broken interpreter. The
#     failure is dlopen of a C extension at IMPORT time, not syntax.
#   - `health-check.py --help`: there is no --help. The script ignores it and
#     runs the FULL check — slow, touches the workspace, and exits non-zero on
#     any unhealthy host, so a healthy interpreter on an unhealthy host would be
#     rejected. (Observed: it reported missing .env / build_log for the probe's
#     own working tree.)
probe_python() {
    [ -n "${1:-}" ] && [ -x "$1" ] || return 1
    "$1" - "$REPO/src/health-check.py" >/dev/null 2>&1 <<'PROBE'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("hc_probe", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
sys.modules["hc_probe"] = mod
spec.loader.exec_module(mod)
PROBE
}

# resolve_python() picks the first interpreter that EXISTS. Existing is not
# working, and this installs the OS-level net that catches a wedged or dead
# session — the one component whose failure nothing else watches. Measured
# 2026-08-03: the preferred Homebrew python3 (3.14.5) could not `import
# plistlib` at all (pyexpat/libexpat symbol mismatch), so the installed job
# would have failed every 300s into a log nobody reads while `--status` still
# reported it loaded.
#
# So: keep resolve_python's preference order as the first candidate, but VERIFY
# it, and fall back to the PATH interpreter if it cannot run the script.
# $SUTANDO_PYTHON_CANDIDATES (space-separated) overrides the whole list, so an
# operator can pin the interpreter on a host where the default order is wrong,
# and the tests can inject stubs. A pinned candidate is still probed — pinning
# says which to prefer, not "skip the check".
#
# Deliberately does NOT add /usr/bin/python3 as a fallback: per REVIEW.md
# lesson 7 that path is the Xcode-CLT stub, which exists whether or not the
# tools do and pops a GUI dialog when invoked without them. An operator who
# knows CLT is installed can still select it explicitly via the env var.
resolve_python_verified() {
    __cands="${SUTANDO_PYTHON_CANDIDATES:-}"
    [ -n "$__cands" ] || __cands="$(resolve_python) $(command -v python3 2>/dev/null || true)"

    __first=""
    __seen=""
    # shellcheck disable=SC2086 # deliberate word-splitting of a space-separated list
    for __c in $__cands; do
        [ -n "$__c" ] && [ -x "$__c" ] || continue
        case " $__seen " in *" $__c "*) continue ;; esac
        __seen="$__seen $__c"
        [ -n "$__first" ] || __first="$__c"
        if probe_python "$__c"; then
            echo "$__c"
            return 0
        fi
        echo "note: $__c cannot import health-check.py — trying the next candidate" >&2
    done

    if [ -n "$__first" ]; then
        # FAIL CLOSED. An earlier version warned and installed with the broken
        # interpreter anyway, reasoning that refusing would block an install over
        # a probe that might be wrong. That trade is backwards, for two reasons
        # (review-caught, qingyun-wu on #2582):
        #
        #   1. The caller runs `bootout_if_loaded` AFTER this resolves. Proceeding
        #      therefore UNLOADS a job that may currently be working and replaces
        #      it with one already proven unable to start. Failing here preserves
        #      whatever is installed.
        #   2. The installer would report success. A safety net that reports
        #      installed and cannot run is worse than a failed install, because
        #      nothing else watches this job — that is the whole premise of the
        #      change.
        #
        # And it is not a hypothetical branch: on the host that motivated this,
        # the default list collapses to the same broken interpreter twice
        # (Homebrew, and PATH resolving to it), so ALL candidates fail.
        #
        # The probe being wrong is still handled — $SUTANDO_PYTHON_CANDIDATES
        # overrides the list — so failing closed strands nobody.
        echo "ERROR: no candidate python3 can import health-check.py." >&2
        echo "       Tried:$__seen" >&2
        echo "       Refusing to install: the job would fail every run while" >&2
        echo "       reporting success, and installing would unload any working" >&2
        echo "       job already in place." >&2
        echo "       Fix the interpreter, or pin a known-good one:" >&2
        echo "         SUTANDO_PYTHON_CANDIDATES=/path/to/python3 $0 install" >&2
        exit 1
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
        PYTHON_BIN="$(resolve_python_verified)"
        BREW_BIN="$(resolve_homebrew_bin)"
        # Canonical config dir, baked into the plist so the minimal launchd env
        # resolves channels/ag2space/.env the same way startup does (#2487 P1).
        CLAUDE_CFG="$(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1 bash "$REPO/scripts/sutando-config.sh" claude-home-path 2>/dev/null)"
        # The helper owns every supported fallback. If it cannot resolve one,
        # do not install a launchd job with an empty/legacy config path and
        # silently lose the core-independent gateway alert.
        [ -n "$CLAUDE_CFG" ] || { echo "ERROR: could not resolve canonical Claude config directory" >&2; exit 1; }
        echo "Installing $LABEL"
        echo "  repo:    $REPO"
        echo "  python:  $PYTHON_BIN"
        echo "  brew:    $BREW_BIN"
        echo "  config:  $CLAUDE_CFG"
        mkdir -p "$HOME/Library/LaunchAgents"
        mkdir -p "$WORKSPACE/logs"
        # Shared renderer: literal substitution + XML escaping + a parse
        # check, so a path with & < > | cannot install a silently-broken job.
        "$PYTHON_BIN" "$REPO/src/render_plist_template.py" "$TEMPLATE" "$DEST" \
            "REPO=$REPO" \
            "WORKSPACE=$WORKSPACE" \
            "PYTHON=$PYTHON_BIN" \
            "HOMEBREW_BIN=$BREW_BIN" \
            "CLAUDE_CONFIG_DIR=$CLAUDE_CFG" || exit 1
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
