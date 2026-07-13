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
assert set(d) == {"instance_id", "host", "workspace", "repo"}, d.keys()
assert d["repo"] == os.environ["REPO_EXPECT"], d["repo"]
assert d["workspace"].startswith("/"), d["workspace"]
assert d["instance_id"] and d["host"]
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

printf '\n%s\n' "$([ "$fails" -eq 0 ] && echo 'PASS — sutando-whoami green' || echo "FAIL — $fails failing")"
exit "$fails"
