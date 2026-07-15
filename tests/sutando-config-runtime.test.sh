#!/bin/bash
# tests/sutando-config-runtime.test.sh — E2E smoke for the `tmux-socket` and
# `runtime` subcommands on scripts/sutando-config.sh.
#
# These are the OSS-side AgentRuntime resolution contract consumed by the
# desktop app (ag2-space/ag2space-cinny-desktop#98) to decide which runtime to
# attach its Terminal to / route task-drops to / port-vs-new. The load-bearing
# property under test is FOREIGN-CALLER SAFETY: a caller whose own env points
# SUTANDO_TMUX_SOCKET at a different (bundled) socket must still get THIS OSS
# runtime's socket from `runtime` — never the caller's — else the split-brain
# the contract exists to prevent reappears.
#
# Run: bash tests/sutando-config-runtime.test.sh
# Exit: 0 = all pass, 1 = failure

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_DIR/scripts/sutando-config.sh"
DEFAULT_SOCK="/tmp/sutando-tmux.sock"

pass=0; fail=0
report() {
  if [ "$1" = "0" ]; then
    echo "  PASS: $2"; pass=$((pass+1))
  else
    echo "  FAIL: $2"; fail=$((fail+1))
  fi
}

# -- Test 1: tmux-socket default -------------------------------------------
echo "[1] tmux-socket → default OSS socket when env unset"
out="$(env -u SUTANDO_TMUX_SOCKET bash "$SCRIPT" tmux-socket)"
[ "$out" = "$DEFAULT_SOCK" ]; report "$?" "prints $DEFAULT_SOCK ($out)"

# -- Test 2: tmux-socket honors ambient env (same-runtime caller) -----------
echo "[2] tmux-socket → honors ambient SUTANDO_TMUX_SOCKET"
out="$(SUTANDO_TMUX_SOCKET=/tmp/custom.sock bash "$SCRIPT" tmux-socket)"
[ "$out" = "/tmp/custom.sock" ]; report "$?" "prints the ambient socket ($out)"

# -- Test 3: runtime emits valid JSON with the full descriptor --------------
echo "[3] runtime → valid JSON with all descriptor keys"
json="$(bash "$SCRIPT" runtime)"
echo "$json" | python3 -c "
import json,sys
d=json.load(sys.stdin)
need={'alive','repo','code','workspace','brain','socket','session','health','authenticated'}
missing=need - set(d)
assert not missing, f'missing keys: {missing}'
assert d['repo']=='$REPO_DIR', f\"repo {d['repo']} != $REPO_DIR\"
assert d['brain']==d['workspace']+'/.claude-sutando', f\"brain {d['brain']}\"
assert d['session']=='sutando-core', d['session']
assert isinstance(d['alive'], bool), d['alive']
# code = source-version identity block (git-derived; keys always present, values may be null off-git)
c=d['code']
for k in ('commit','branch','describe','tree_sha','dirty'):
    assert k in c, f'code missing {k}'
assert isinstance(c['dirty'], bool), c['dirty']
"
report "$?" "JSON valid; repo/brain/session/alive + code block correct"

# -- Test 4: FOREIGN-CALLER SAFETY (the anti-split guarantee) ---------------
# A caller whose env points at a bundled socket must NOT leak into runtime.socket.
echo "[4] runtime → socket is env-independent (foreign-caller safe)"
sock="$(SUTANDO_TMUX_SOCKET=/tmp/bundled-fake.sock bash "$SCRIPT" runtime \
        | python3 -c 'import json,sys;print(json.load(sys.stdin)["socket"])')"
[ "$sock" = "$DEFAULT_SOCK" ]; report "$?" "socket=$sock stays $DEFAULT_SOCK despite bundled env (no re-split)"

echo
echo "sutando-config-runtime: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
