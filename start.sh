#!/usr/bin/env bash
# One-command entry point: menu bar app + core + dashboard in the browser.
#
# Deliberately thin. src/startup.sh remains the supported low-level entry and
# owns every prerequisite check; this only adds the two conveniences a desktop
# user wants and a headless/CI caller does not. Extra args pass straight through.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_URL="${SUTANDO_DASHBOARD_URL:-http://localhost:7844}"

# Backgrounded: startup.sh ends in `exec`, so anything sequenced after it never
# runs. Poll rather than sleep — the port is ready long before a fixed guess.
if [ "${SUTANDO_OPEN_DASHBOARD:-1}" = "1" ] && command -v open >/dev/null 2>&1; then
    (
        for _ in $(seq 1 60); do
            if curl -fsS -o /dev/null --max-time 1 "$DASHBOARD_URL" 2>/dev/null; then
                open "$DASHBOARD_URL"
                exit 0
            fi
            sleep 1
        done
        echo "start.sh: dashboard did not answer at $DASHBOARD_URL within 60s;" >&2
        echo "  the core is still starting — open it manually once it's up." >&2
    ) &
fi

exec bash "$REPO/src/startup.sh" --with-app "$@"
