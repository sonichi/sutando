#!/usr/bin/env bash
# Wiring test for the REAL launcher: the named-gateway block of start_gateway_lanes()
# (src/startup-runtime.sh, called from startup.sh and scripts/restart-gateway-lanes.sh)
# must hand each bridge instance its own REMOTE_TASK_CHANNEL_DIR, its own token file,
# an empty REMOTE_TASK_URL and no REMOTE_PROACTIVE_ROOM, or a dev bridge inherits
# prod's channels/ag2space/ config. Runs the extracted block with the real python3 and
# the real scripts/connections.py against a stub bridge that records its environment;
# the Keychain is a stub on PATH and the workspace a temp dir.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
PY="$(command -v python3)"

# A stand-in repo: the real CLI (its sys.path resolves through the symlink) beside a
# stub bridge that dumps the env it was launched with. `--repo` names this directory.
FAKE="$TMP/repo"; mkdir -p "$FAKE/scripts" "$FAKE/src" "$TMP/ws" "$TMP/cc" "$TMP/bin" "$TMP/keychain"
ln -s "$REPO/scripts/connections.py" "$FAKE/scripts/connections.py"
cat > "$FAKE/src/remote-gateway-bridge.py" <<'STUB'
import os
with open(os.environ["ENV_DUMP"], "a") as f:
    for k in ("REMOTE_TASK_CHANNEL_DIR", "REMOTE_TASK_TOKEN_FILE", "REMOTE_TASK_URL", "GATEWAY_INSTANCE",
              "REMOTE_TASK_TOKEN", "REMOTE_PROACTIVE_ROOM", "AGENT_MXID", "AGENT_ID", "AG2_DEVICE_ENV"):
        f.write(f"{k}={os.environ.get(k, '<unset>')}\n")
STUB
# Stub Keychain so import-env's vault copy never reaches the host's.
cat > "$TMP/bin/security" <<'SEC'
#!/usr/bin/env bash
verb="$1"; shift; key=""; value=""
while [ $# -gt 0 ]; do case "$1" in -s) key="$2"; shift 2;; -w) [ "$verb" = add-generic-password ] && { value="$2"; shift 2; } || shift;; *) shift;; esac; done
case "$verb" in
  add-generic-password) printf '%s' "$value" > "$FAKE_KEYCHAIN/$key";;
  find-generic-password) [ -f "$FAKE_KEYCHAIN/$key" ] && cat "$FAKE_KEYCHAIN/$key" || exit 44;;
  delete-generic-password) rm -f "$FAKE_KEYCHAIN/$key";;
esac
SEC
chmod +x "$TMP/bin/security"

# Extract the named-gateway block from the real start_gateway_lanes() (single source:
# the test fails if the block moves or the marker comment is renamed).
LOOP="$(awk '/# Named gateways \(multi-gateway\)/,/^    fi$/' "$REPO/src/startup-runtime.sh")"
[ -n "$LOOP" ] || { echo "FAIL: named-gateway block not found in startup-runtime.sh"; exit 1; }

# The dev row as a sign-in records it, with the agent its gateway named.
seed_dev_row() {
  mkdir -p "$TMP/ws/state"
  cat > "$TMP/ws/state/connections.json" <<'ROW'
{"schema_version": 1, "connections": [{"server": "dev.example", "instance": "dev", "gateway_url": "https://dev.example/relay",
 "token_ref": "AG2_REMOTE_TOKEN_DEV", "primary": false, "added_by": "owner", "added_at": "2026-01-01T00:00:00Z",
 "verified": true, "server_source": "identity", "disabled": false, "agent_mxid": "@me:dev.example"}]}
ROW
}

run_loop() {  # $1 = extra env assignments (string), evaluated before the block; SEED=0 records no row
  ENV_DUMP="$TMP/env-dump"; : > "$ENV_DUMP"; rm -rf "$TMP/ws"; mkdir -p "$TMP/ws" "$TMP/logs"
  [ "${SEED:-1}" = 0 ] || seed_dev_row
  # The host's own gateway credentials must not shape the test; a parent proactive room
  # is always present, so a secondary that inherits it is caught in every case.
  ( unset REMOTE_TASK_TOKEN AG2_REMOTE_TOKEN REMOTE_TASK_URL REMOTE_TASK_TOKEN_FILE
    for _v in $(env | grep -o '^AG2_REMOTE_TOKEN_[A-Za-z0-9_]*' || true); do unset "$_v"; done
    export REMOTE_PROACTIVE_ROOM='!parent:example'
    # What the legacy channel env leaves behind for every lane started after it.
    export AGENT_ID='@legacy:ag2.example' AGENT_MXID='@legacy:ag2.example' AG2_DEVICE_ENV="$TMP/legacy.env"
    eval "$1"
    export ENV_DUMP PATH="$TMP/bin:$PATH" FAKE_KEYCHAIN="$TMP/keychain" SUTANDO_TEST_MODE=1 \
      SUTANDO_WORKSPACE="$TMP/ws" CLAUDE_CONFIG_DIR="$TMP/cc" SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1 \
      SUTANDO_BRIDGE_LOCK=0  # the stub bridge takes no lease
    PY="$PY"; REPO="$FAKE"; LOGS_DIR="$TMP/logs"
    eval "$LOOP" ) > "$TMP/loop-out" 2>&1
  # The bridge is launched detached; give the stub a moment to write.
  for _ in $(seq 1 50); do grep -q '^GATEWAY_INSTANCE=' "$ENV_DUMP" 2>/dev/null && break; sleep 0.1; done
  cat "$ENV_DUMP"
}

# Case 1: default convention — instance "dev" gets channels/dev-ag2space/.
OUT="$(run_loop 'export AG2_REMOTE_TOKEN_DEV=tok-dev')"
echo "$OUT" | grep -q '^REMOTE_TASK_CHANNEL_DIR=dev-ag2space$' \
  || { echo "FAIL: dev instance did not get REMOTE_TASK_CHANNEL_DIR=dev-ag2space"; echo "$OUT"; cat "$TMP/loop-out"; exit 1; }
echo "$OUT" | grep -q '^GATEWAY_INSTANCE=dev$' \
  || { echo "FAIL: dev instance did not get GATEWAY_INSTANCE=dev"; echo "$OUT"; exit 1; }
echo "  ok  dev instance defaults to dev-ag2space"
echo "$OUT" | grep -q '^REMOTE_PROACTIVE_ROOM=$' \
  || { echo "FAIL: dev instance inherited the parent's proactive room"; echo "$OUT"; exit 1; }
echo "  ok  a named instance never inherits the parent's proactive room"
grep -q 'tok-dev' "$TMP/loop-out" && { echo "FAIL: the token reached the launcher's output"; exit 1; }
echo "  ok  the token never reaches the launcher's output"
echo "$OUT" | grep -q '^AGENT_MXID=@me:dev.example$' \
  || { echo "FAIL: dev instance did not run as its own row's agent"; echo "$OUT"; exit 1; }
for _k in AGENT_ID AG2_DEVICE_ENV; do
  echo "$OUT" | grep -q "^$_k=<unset>\$" \
    || { echo "FAIL: dev instance inherited the legacy $_k"; echo "$OUT"; exit 1; }
done
echo "  ok  a named instance runs as its row's agent, not the legacy channel's"

# Case 1b: an env token with no recorded agent is refused out loud, never launched as the inherited one.
OUT="$(SEED=0 run_loop 'export AG2_REMOTE_TOKEN_DEV=tok-dev')"
[ -z "$OUT" ] || { echo "FAIL: a lane with no recorded agent was launched"; echo "$OUT"; exit 1; }
grep -q 'gateway bridge (dev) not launched' "$TMP/loop-out" \
  || { echo "FAIL: the refused launch was not reported"; cat "$TMP/loop-out"; exit 1; }
grep -q 'has no recorded agent' "$TMP/logs/remote-gateway-bridge.log" \
  || { echo "FAIL: the log does not name the missing agent"; exit 1; }
echo "  ok  a lane with no recorded agent is refused and named"

# Case 2: operator override wins.
OUT="$(run_loop 'export AG2_REMOTE_TOKEN_DEV=tok-dev REMOTE_TASK_CHANNEL_DIR_DEV=dev.ag2.space')"
echo "$OUT" | grep -q '^REMOTE_TASK_CHANNEL_DIR=dev.ag2.space$' \
  || { echo "FAIL: override REMOTE_TASK_CHANNEL_DIR_DEV not honored"; echo "$OUT"; exit 1; }
echo "  ok  per-instance override honored"

# Case 3: the instance never runs with prod's dir.
echo "$OUT" | grep -q '^REMOTE_TASK_CHANNEL_DIR=ag2space$' \
  && { echo "FAIL: instance leaked prod channel dir"; exit 1; }
echo "  ok  prod channel dir never inherited"

# Case 4: named gateways must not inherit the primary gateway URL, token file or proactive room.
: > "$TMP/dev.env"
OUT="$(run_loop 'export AG2_REMOTE_TOKEN_DEV=https://dev.example/relay\|tok-dev REMOTE_TASK_URL=https://prod.example/relay REMOTE_TASK_TOKEN_FILE=/tmp/prod.env REMOTE_TASK_TOKEN_FILE_DEV="$TMP/dev.env" REMOTE_PROACTIVE_ROOM="!owner:prod.example"')"
echo "$OUT" | grep -q '^REMOTE_TASK_URL=$' \
  || { echo "FAIL: dev instance inherited primary REMOTE_TASK_URL"; echo "$OUT"; exit 1; }
echo "$OUT" | grep -q "^REMOTE_TASK_TOKEN_FILE=$TMP/dev.env$" \
  || { echo "FAIL: dev instance did not get its own token file"; echo "$OUT"; exit 1; }
echo "$OUT" | grep -q '^REMOTE_PROACTIVE_ROOM=$' \
  || { echo "FAIL: dev instance inherited the primary's proactive room"; echo "$OUT"; exit 1; }
echo "$OUT" | grep -q '^REMOTE_TASK_TOKEN=https://dev.example/relay|tok-dev$' \
  || { echo "FAIL: dev instance did not get its own token"; echo "$OUT"; exit 1; }
echo "  ok  primary URL, token file and proactive room never inherited"

# Case 5: the seeded row keeps its launch record, without the secret.
grep -q 'tok-dev' "$TMP/ws/state/connections.json" && { echo "FAIL: the secret is in connections.json"; exit 1; }
grep -q '"ok": true' "$TMP/ws/state/connections-launch/dev.json" \
  || { echo "FAIL: no launch record for dev"; exit 1; }
echo "  ok  the launch record is where the module puts it"

# Case 6: an env token the import records unverified: row and vault copy, but no launch without an agent.
OUT="$(SEED=0 run_loop 'export AG2_REMOTE_TOKEN_DEV=https://dev.example/relay\|tok-dev')"
grep -q '"instance": "dev"' "$TMP/ws/state/connections.json" \
  || { echo "FAIL: import-env did not record the dev row"; cat "$TMP/ws/state/connections.json" 2>/dev/null; exit 1; }
grep -q 'tok-dev' "$TMP/ws/state/connections.json" && { echo "FAIL: the secret is in connections.json"; exit 1; }
[ -f "$TMP/keychain/AG2_REMOTE_TOKEN_DEV" ] || { echo "FAIL: the secret was not copied into the vault"; exit 1; }
grep -q '"reason": "no_identity"' "$TMP/ws/state/connections-launch/dev.json" \
  || { echo "FAIL: the refused launch left no no_identity record"; exit 1; }
[ -z "$OUT" ] || { echo "FAIL: an unverified row was launched"; echo "$OUT"; exit 1; }
echo "  ok  the import's row and vault copy are where the module puts them; no agent, no launch"
echo "PASS — launcher threads REMOTE_TASK_CHANNEL_DIR per instance"
