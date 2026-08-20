#!/usr/bin/env bash
# Pins the gateway supervision predicate in src/startup-runtime.sh
# (start_gateway_lanes(); relocated there from startup.sh by #3147).
#
# The predicate must answer "is launchd's OWN job running?" — not "does any
# process with a matching argv exist?". Those disagree whenever a bare or
# named-instance bridge is alive while the job is dead, and the disagreement is
# silent: the launcher reports the bridge supervised and skips kickstart recovery.
#
# The function under test is extracted verbatim from startup-runtime.sh (not reimplemented),
# so a change to the production text changes what this asserts.

set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STARTUP="$REPO/src/startup-runtime.sh"
fails=0
ok()   { echo "  ok   — $1"; }
bad()  { echo "  FAIL — $1"; fails=$((fails + 1)); }

# --- extract the production function verbatim -------------------------------
# Indented one level deeper than before: the predicate now lives INSIDE
# start_gateway_lanes(), not at top level of startup.sh.
fn="$(awk '/^    _gw_job_pid\(\) \{/,/^    \}/' "$STARTUP")"
if [ -z "$fn" ]; then
  echo "FAIL — _gw_job_pid() not found in src/startup-runtime.sh (renamed or removed?)"
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"; [ -n "${live_pid:-}" ] && kill "$live_pid" 2>/dev/null' EXIT
mkdir -p "$TMP/bin"

_GW_LABEL="com.sutando.gateway-bridge"
eval "$fn"

stub_launchctl() {  # $1 = the PID column value for our label
  cat > "$TMP/bin/launchctl" <<EOF
#!/bin/sh
[ "\$1" = "list" ] || exit 1
printf 'PID\tStatus\tLabel\n'
printf '%s\t0\tcom.apple.something\n' 4242
printf '%s\t%s\t$_GW_LABEL\n' "$1" "\${2:-0}"
EOF
  chmod +x "$TMP/bin/launchctl"
}

# --- case 1: job running -> predicate yields its pid ------------------------
stub_launchctl 12345
PATH="$TMP/bin:$PATH" ; got="$(_gw_job_pid)"
[ "$got" = "12345" ] && ok "running job reports its pid" \
                     || bad "running job: expected 12345, got '${got:-<empty>}'"

# --- case 2: job loaded but NOT running -> empty ----------------------------
stub_launchctl -
got="$(_gw_job_pid)"
[ -z "$got" ] && ok "idle job (pid '-') reports not-running" \
              || bad "idle job: expected empty, got '$got'"

# --- case 3: the discriminating case ---------------------------------------
# Job dead, but a process with the exact gateway argv is alive. This is the
# state that made the old predicate report a dead job as supervised.
cat > "$TMP/remote-gateway-bridge.py" <<'PY'
import time
time.sleep(60)
PY
python3 "$TMP/remote-gateway-bridge.py" &
live_pid=$!
sleep 1

if ! pgrep -f "remote-gateway-bridge\.py$" > /dev/null 2>&1; then
  bad "control setup: no live process matched the gateway argv — case 3 proves nothing"
else
  # Control: the OLD predicate is positive here. If this ever goes negative the
  # test below stops discriminating and silently passes for the wrong reason.
  ok "control — old pgrep predicate IS positive with the job dead"
  got="$(_gw_job_pid)"
  [ -z "$got" ] && ok "new predicate ignores a live non-job bridge" \
                || bad "new predicate: expected empty, got '$got'"
fi

# --- case 4: the label must be matched exactly, not as a substring ----------
cat > "$TMP/bin/launchctl" <<EOF
#!/bin/sh
[ "\$1" = "list" ] || exit 1
printf 'PID\tStatus\tLabel\n'
printf '99999\t0\t$_GW_LABEL.helper\n'
printf -- '-\t0\t$_GW_LABEL\n'
EOF
chmod +x "$TMP/bin/launchctl"
got="$(_gw_job_pid)"
[ -z "$got" ] && ok "a similarly-named job does not satisfy the predicate" \
              || bad "substring match leaked: got '$got' from a .helper label"

echo
[ "$fails" -eq 0 ] && { echo "PASS — gateway job-liveness predicate"; exit 0; }
echo "FAIL — $fails assertion(s)"; exit 1
