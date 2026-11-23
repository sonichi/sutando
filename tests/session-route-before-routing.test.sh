#!/usr/bin/env bash
# First launch after a rollback: the skills' `on_session_route` hooks run BEFORE the launch
# decides its API routing, so a proxy a hook turns back on is used by that very launch.
# Pinned in both launchers (order) and in the shared mechanics (behavior, with stubs).
set -uo pipefail
REPO_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

# 1. Order in both launchers: the route hooks come before the routing decision.
for f in src/agent/claude/cli/start-cli.sh skills/worker-pool/scripts/launch-worker-session.sh; do
  hook="$(grep -n 'apply_skill_session_route' "$REPO_SRC/$f" | head -1 | cut -d: -f1)"
  route="$(grep -n '^resolve_claude_credential_proxy$' "$REPO_SRC/$f" | head -1 | cut -d: -f1)"
  [ -n "$hook" ] && [ -n "$route" ] && [ "$hook" -lt "$route" ]
  check $? "$f runs the route hooks (line ${hook:-none}) before routing (line ${route:-none})"
done

# 2. Behavior: a route hook that turns the proxy back on makes the same launch use it.
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/repo/src" "$TMP/bin"
cp "$REPO_SRC/src/claude_routing.sh" "$TMP/repo/src/"
# The hook runner's stand-in: --route "restores" the proxy, which then listens.
cat > "$TMP/repo/src/session_config_hooks.py" <<EOF
import sys
if sys.argv[1:] == ["--route"]:
    open("$TMP/listening", "w").write("1")
EOF
printf '#!/bin/sh\n[ -f "%s/listening" ]\n' "$TMP" > "$TMP/bin/lsof"
printf '#!/bin/sh\n[ -f "%s/listening" ]\n' "$TMP" > "$TMP/bin/launchctl"
printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/pgrep"
printf '#!/bin/sh\nexit 0\n' > "$TMP/bin/sleep"
chmod +x "$TMP"/bin/*

launch() {  # $1: run the route hooks first (yes|no)
  rm -f "$TMP/listening"
  env -i HOME="$HOME" PATH="$TMP/bin:/usr/bin:/bin" REPO="$TMP/repo" PY="$(command -v python3)" \
      QUOTA_STATUSLINE_ENABLED=false FIRST="$1" SRC="$REPO_SRC" bash -c '
    . "$SRC/src/agent/claude/cli/session-launch.sh"
    [ "$FIRST" = yes ] && apply_skill_session_route
    resolve_claude_credential_proxy 2>/dev/null
    echo "ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL:-<unset>}"'
}
out="$(launch yes)"
echo "$out" | grep -qx "ANTHROPIC_BASE_URL=http://localhost:7846"
check $? "route hook before routing: the first launch after a rollback uses the proxy"
out="$(launch no)"
echo "$out" | grep -qx "ANTHROPIC_BASE_URL=<unset>"
check $? "control: without the hook first, that launch would go direct ($out)"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
