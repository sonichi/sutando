#!/bin/bash
# Stop all Sutando services (shortcut for restart.sh --scope all --stop-only)

# --scope all explicitly: the default core scope leaves the web client, the
# credential proxy and the desktop app running, which is not "all services".
exec "$(dirname "$0")/restart.sh" --scope all --stop-only
