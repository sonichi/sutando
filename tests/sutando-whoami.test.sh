#!/usr/bin/env bash
# Tests for scripts/sutando-whoami.sh — the instance-identity primitive.
#
#   bash tests/sutando-whoami.test.sh
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fails=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; fails=$((fails + 1)); }

out="$(bash "$REPO/scripts/sutando-whoami.sh")"

# 1) output is a single valid JSON object with the contract fields
if WHOAMI_OUT="$out" REPO_EXPECT="$REPO" python3 - <<'PY'
import json, os
d = json.loads(os.environ["WHOAMI_OUT"])
assert set(d) == {
    "instance_id", "host", "agent_id", "workspace", "repo", "config_dir", "runtime"
}, d.keys()
assert d["repo"] == os.environ["REPO_EXPECT"], d["repo"]
assert d["workspace"].startswith("/"), d["workspace"]
assert d["instance_id"] and d["host"]
# agent_id / config_dir are optional (null before a device is connected / an
# unresolvable config), but must be str-or-None when present.
assert d["agent_id"] is None or isinstance(d["agent_id"], str), d["agent_id"]
assert d["config_dir"] is None or isinstance(d["config_dir"], str), d["config_dir"]
# runtime is always an object with the four liveness fields.
rt = d["runtime"]
assert set(rt) == {"core_running", "gateway_running", "tmux_socket", "session"}, rt.keys()
assert isinstance(rt["core_running"], bool) and isinstance(rt["gateway_running"], bool)
assert rt["tmux_socket"].startswith("/") and rt["session"]
PY
then ok "valid JSON with contract fields"; else bad "JSON contract"; fi

# 2) workspace matches the M0 resolver's answer (no parallel resolution logic)
ws_json="$(WHOAMI_OUT="$out" python3 -c 'import json,os; print(json.loads(os.environ["WHOAMI_OUT"])["workspace"])')"
ws_helper="$(bash "$REPO/scripts/sutando-config.sh" workspace)"
if [ "$ws_json" = "$ws_helper" ]; then ok "workspace == sutando-config.sh workspace"; else bad "workspace mismatch: $ws_json vs $ws_helper"; fi

# 3) instance_id falls back to unprovisioned-<host> when device.json is absent.
#    Point the resolver at an empty temp workspace via the sanctioned test-mode
#    env override (SUTANDO_WORKSPACE is only honored under SUTANDO_TEST_MODE=1).
T="$(mktemp -d)"
mkdir -p "$T/ws"
out2="$(SUTANDO_TEST_MODE=1 SUTANDO_WORKSPACE="$T/ws" bash "$REPO/scripts/sutando-whoami.sh")"
if WHOAMI_OUT="$out2" python3 - <<'PY'
import json, os
d = json.loads(os.environ["WHOAMI_OUT"])
assert d["instance_id"].startswith("unprovisioned-"), d["instance_id"]
assert d["workspace"].endswith("/ws"), d["workspace"]
PY
then ok "unprovisioned fallback when device.json absent"; else bad "unprovisioned fallback"; fi
rm -rf "$T"

# 4) agent_id is actually READ from <config_dir>/channels/ag2space/.env AGENT_ID.
#    Guards the identity path the desktop Connect UI depends on: a schema-only
#    check would stay green even if the parser silently stopped reading the file
#    (always emitting null). Write a real device-env fixture and assert the value.
T2="$(mktemp -d)"
mkdir -p "$T2/ws"
ccd="$(SUTANDO_TEST_MODE=1 SUTANDO_WORKSPACE="$T2/ws" bash "$REPO/scripts/sutando-config.sh" claude-sutando-config-dir 2>/dev/null || true)"
if [ -n "$ccd" ]; then
  mkdir -p "$ccd/channels/ag2space"
  printf 'AGENT_ID="@agent:ag2.space"\nREMOTE_TASK_TOKEN=x\n' > "$ccd/channels/ag2space/.env"
  out3="$(SUTANDO_TEST_MODE=1 SUTANDO_WORKSPACE="$T2/ws" bash "$REPO/scripts/sutando-whoami.sh")"
  if WHOAMI_OUT="$out3" python3 - <<'PY'
import json, os
d = json.loads(os.environ["WHOAMI_OUT"])
assert d["agent_id"] == "@agent:ag2.space", d["agent_id"]
assert d["config_dir"] and d["config_dir"].endswith(".claude-sutando"), d["config_dir"]
PY
  then ok "agent_id read from device-auth env"; else bad "agent_id extraction (parser did not read AGENT_ID)"; fi
else
  bad "config-dir resolver returned empty under test mode"
fi
rm -rf "$T2"

printf '\n%s\n' "$([ "$fails" -eq 0 ] && echo 'PASS — sutando-whoami green' || echo "FAIL — $fails failing")"
exit "$fails"
