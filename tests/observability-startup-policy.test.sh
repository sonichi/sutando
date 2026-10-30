#!/bin/bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=src/observability/startup-policy.sh
source "$REPO/src/observability/startup-policy.sh"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

unset SUTANDO_OBS_COLLECTOR
obs_collector_enabled || fail "collector should default on"
obs_hooks_enabled && fail "prompt/tool hooks must remain opt-in"

SUTANDO_OBS_COLLECTOR=0
obs_collector_enabled && fail "explicit zero must disable collector"
obs_hooks_enabled && fail "explicit zero must disable hooks"

SUTANDO_OBS_COLLECTOR=1
obs_collector_enabled || fail "explicit one must enable collector"
obs_hooks_enabled || fail "explicit one must retain hook opt-in"

stub_dir="$(mktemp -d)"
trap 'rm -rf "$stub_dir"' EXIT
cat > "$stub_dir/curl" <<'SH'
#!/bin/bash
printf '%s' "$CURL_RESPONSE"
SH
cat > "$stub_dir/lsof" <<'SH'
#!/bin/bash
printf '%s' "$LSOF_OUTPUT"
SH
chmod +x "$stub_dir/curl"
chmod +x "$stub_dir/lsof"
PATH="$stub_dir:$PATH"
export PATH

CURL_RESPONSE='{"ok":true,"service":"sutando-observability-collector"}'
export CURL_RESPONSE
LSOF_OUTPUT="u$(id -u)"
export LSOF_OUTPUT
obs_collector_healthy 4000 || fail "collector identity should pass"

LSOF_OUTPUT='u99999'
if obs_collector_healthy 4000; then rc=0; else rc=$?; fi
[ "$rc" -eq 2 ] || fail "another account's listener should return ownership status 2"

LSOF_OUTPUT=''
if obs_collector_healthy 4000; then rc=0; else rc=$?; fi
[ "$rc" -eq 3 ] || fail "an unmeasurable listener should return status 3, not be called foreign"

LSOF_OUTPUT="u$(id -u)
u99999"
if obs_collector_healthy 4000; then rc=0; else rc=$?; fi
[ "$rc" -eq 2 ] || fail "a listener owned by mixed accounts should count as foreign"

LSOF_OUTPUT="u$(id -u)"
CURL_RESPONSE='{"ok":true,"service":"unrelated-local-service"}'
export CURL_RESPONSE
if obs_collector_healthy 4000; then rc=0; else rc=$?; fi
[ "$rc" -eq 1 ] || fail "an owned listener with the wrong marker should return health status 1"

echo "PASS: observability startup policy"
