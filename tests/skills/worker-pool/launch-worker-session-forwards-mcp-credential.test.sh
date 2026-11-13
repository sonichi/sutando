#!/usr/bin/env bash
# A worker seat's s-space MCP proxy must mint as the worker's own agent: the launcher forwards
# AG2_MCP_WORKER_CREDENTIAL, the JSON argv of `scripts/connections.py worker-mcp-credential
# --worker <id>` (connections.mcp_worker_credential_argv), across the tmux boundary.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"
printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/lsof"
printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/launchctl"
chmod +x "$TMP"/bin/*
STUB_PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"
WS="$TMP/ws"; WID="w-mcptestworker"; mkdir -p "$WS/deliveries/$WID"

python3 - "$REPO" "$WS" "$WID" > "$TMP/plan" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/skills/worker-pool/scripts")
import spawn_worker
p = spawn_worker.plan(sys.argv[2], sys.argv[1], runtime="claude",
                      socket="/tmp/sutando-test.sock", worker_id=sys.argv[3])
for k, v in sorted(p["env"].items()):
    print(f"{k}={v}")
PY
# shellcheck disable=SC2046
env -i HOME="$HOME" PATH="$STUB_PATH" $(cat "$TMP/plan") \
    bash "$REPO/skills/worker-pool/scripts/launch-worker-session.sh" --print-env > "$TMP/fwd" 2>"$TMP/err"
grep -q "^AG2_MCP_WORKER_CREDENTIAL=" "$TMP/fwd"
check $? "AG2_MCP_WORKER_CREDENTIAL crosses into the session"
python3 - "$TMP/fwd" "$REPO" "$WID" <<'PY'
import json, sys
fwd, repo, wid = sys.argv[1:]
line = next(l for l in open(fwd) if l.startswith("AG2_MCP_WORKER_CREDENTIAL="))
argv = json.loads(line.split("=", 1)[1])
assert isinstance(argv, list) and all(isinstance(a, str) for a in argv), argv
assert argv[1] == repo + "/scripts/connections.py", argv
assert argv[2] == "--workspace" and argv[3].startswith("/"), argv
assert argv[4:] == ["worker-mcp-credential", "--worker", wid], argv
assert argv[0], "an interpreter is named"
PY
check $? "the value is the JSON argv of worker-mcp-credential --worker <this worker> in this repo, naming the workspace"
python3 - "$TMP/fwd" "$WS" <<'PY'
import json, sys
fwd, ws = sys.argv[1:]
line = next(l for l in open(fwd) if l.startswith("AG2_MCP_WORKER_CREDENTIAL="))
argv = json.loads(line.split("=", 1)[1])
assert argv[3] == ws, (argv[3], ws)
PY
check $? "the workspace named is the seat's SUTANDO_WORKSPACE_DIR"
! grep -q "no MCP credential resolver" "$TMP/err"
check $? "the launcher composed the argv without a warning"

# A compose failure must not read as "not a worker seat": the proxy treats `[]` as an opted-in seat
# with no resolver and refuses to start, where an empty value would fall back to the core's seat.
REAL_PY="$(command -v python3)"
mkdir -p "$TMP/stub"
printf '#!/bin/sh\ncase "$*" in *worker-mcp-credential-argv*) echo "connections: fixture refusal" >&2; exit 1;; esac\nexec %s "$@"\n' "$REAL_PY" > "$TMP/stub/python3"
chmod +x "$TMP/stub/python3"
# shellcheck disable=SC2046
env -i HOME="$HOME" PATH="$TMP/stub:$STUB_PATH" $(cat "$TMP/plan") \
    bash "$REPO/skills/worker-pool/scripts/launch-worker-session.sh" --print-env > "$TMP/fwd2" 2>"$TMP/err2"
grep -qxF -- "AG2_MCP_WORKER_CREDENTIAL=[]" "$TMP/fwd2"
check $? "a failed compose exports [] (an opted-in seat with no resolver), never an empty value"
grep -q "no MCP credential resolver for this worker (connections: fixture refusal)" "$TMP/err2"
check $? "the warning names the resolver's reason"

echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
