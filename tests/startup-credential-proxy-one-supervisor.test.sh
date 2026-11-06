#!/usr/bin/env bash
# Pins start_credential_proxy() in src/startup.sh: once launchd's job is loaded
# it is the only supervisor of :7846. A job that is loaded but not serving the
# port (stuck in xpcproxy, or between KeepAlive restarts) must NOT make startup
# spawn a second proxy under the core's tmux tree, and must not point seats at
# the dead port. The legacy child starts only when no launchd job exists.
#
# The function under test is extracted verbatim from startup.sh (not
# reimplemented), so a change to the production text changes what this asserts.

set -u
REPO_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STARTUP="$REPO_SRC/src/startup.sh"
fails=0
ok()  { echo "  ok   — $1"; }
bad() { echo "  FAIL — $1"; fails=$((fails + 1)); }

fn="$(awk '/^start_credential_proxy\(\) \{/,/^\}/' "$STARTUP")"
if [ -z "$fn" ]; then
  echo "FAIL — start_credential_proxy() not found in src/startup.sh (renamed or removed?)"
  exit 1
fi
# The range ends at the first column-0 "}", so an inner one would cut the body short; a cut body fails to parse.
if ! bash -n <(printf '%s\n' "$fn") 2>/dev/null || ! bash -c "$fn"$'\n'"declare -F start_credential_proxy >/dev/null"; then
  echo "FAIL — start_credential_proxy() extracted from src/startup.sh is truncated or unparseable"
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/repo/src" "$TMP/ws/logs"

# A repo with no installer: the install branch is skipped and the arms below
# exercise only what happens AFTER install, which is where the second proxy came from.
REPO="$TMP/repo"
WORKSPACE="$TMP/ws"
BUNDLED_MODE=0
CHILD_LOG="$TMP/child.log"
export REPO WORKSPACE BUNDLED_MODE CHILD_LOG

stub_launchctl() {  # $1 = exit code of `launchctl print`, $2 = state line value
  cat > "$TMP/bin/launchctl" <<EOF
#!/bin/sh
[ "\$1" = "print" ] || exit 1
[ "$1" = "0" ] || exit $1
printf '\tstate = %s\n\tlast exit code = 143\n' "$2"
EOF
  chmod +x "$TMP/bin/launchctl"
}
stub_lsof() {  # $1 = number of calls that report NO listener before one is reported (-1 = never)
  cat > "$TMP/bin/lsof" <<EOF
#!/bin/sh
n=\$(cat "$TMP/lsof.count" 2>/dev/null || echo 0)
echo \$((n + 1)) > "$TMP/lsof.count"
[ "$1" -ge 0 ] && [ "\$n" -ge "$1" ] && exit 0
exit 1
EOF
  chmod +x "$TMP/bin/lsof"
  rm -f "$TMP/lsof.count"
}

run_arm() {  # runs the function in a fresh bash with the stubs first on PATH
  : > "$CHILD_LOG"
  # -u: a seat's own ANTHROPIC_BASE_URL (a proxy-routed shell) must not leak in and read as an export.
  PATH="$TMP/bin:$PATH" env -u ANTHROPIC_BASE_URL bash -c "
    run_node_service() { echo \"run_node_service \$*\" >> \"\$CHILD_LOG\"; sleep 0; }
    sleep() { :; }
    $fn
    start_credential_proxy
    echo \"ANTHROPIC_BASE_URL=\${ANTHROPIC_BASE_URL:-<unset>}\"
  " 2>&1
}

# --- arm 1: the stuck-in-xpcproxy case ---------------------------------------
stub_launchctl 0 xpcproxy; stub_lsof -1
out="$(run_arm)"
if grep -q 'run_node_service' "$CHILD_LOG"; then
  bad "xpcproxy: a second proxy was started under the session ($(cat "$CHILD_LOG"))"
else
  ok "xpcproxy: no child proxy started while launchd holds the job"
fi
echo "$out" | grep -q 'ANTHROPIC_BASE_URL=<unset>' && ok "xpcproxy: seats are not pointed at the dead port" \
  || bad "xpcproxy: ANTHROPIC_BASE_URL exported for a port nobody serves: $(echo "$out" | grep ANTHROPIC_BASE_URL)"
echo "$out" | grep -q 'state=xpcproxy' && ok "xpcproxy: the loud line names launchd's state" \
  || bad "xpcproxy: the state is not named: $out"
echo "$out" | grep -q 'launchctl kickstart -k gui/' && ok "xpcproxy: the loud line names the recovery command" \
  || bad "xpcproxy: no recovery command: $out"

# --- arm 2: job loaded, proxy binds during the wait -------------------------
stub_launchctl 0 running; stub_lsof 3
out="$(run_arm)"
grep -q 'run_node_service' "$CHILD_LOG" && bad "late bind: a child was started beside the launchd job" \
  || ok "late bind: no child while launchd's proxy is coming up"
echo "$out" | grep -q 'ANTHROPIC_BASE_URL=http://localhost:7846' && ok "late bind: seats route through the supervised proxy" \
  || bad "late bind: base URL not exported once the port bound: $out"

# --- arm 3: no launchd job at all -> legacy child (older checkout) ------------
stub_launchctl 1 -; stub_lsof -1
out="$(run_arm)"
grep -q 'run_node_service credential-proxy' "$CHILD_LOG" && ok "no job: the legacy child is started" \
  || bad "no job: the legacy child was not started: $out"

# --- arm 4: port already served -> unchanged ---------------------------------
stub_launchctl 0 running; stub_lsof 0
out="$(run_arm)"
grep -q 'run_node_service' "$CHILD_LOG" && bad "already running: a child was started anyway" \
  || ok "already running: nothing started"
echo "$out" | grep -q 'already running' && echo "$out" | grep -q 'ANTHROPIC_BASE_URL=http://localhost:7846' \
  && ok "already running: reported and routed" || bad "already running: $out"

echo
if [ "$fails" = 0 ]; then echo "PASS startup-credential-proxy-one-supervisor"; else echo "FAIL startup-credential-proxy-one-supervisor ($fails)"; exit 1; fi
