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
chmod +x "$stub_dir/curl"
PATH="$stub_dir:$PATH"
export PATH

CURL_RESPONSE='{"ok":true,"service":"sutando-observability-collector"}'
export CURL_RESPONSE
obs_collector_healthy 4000 || fail "collector identity should pass"

CURL_RESPONSE='{"ok":true,"service":"unrelated-local-service"}'
export CURL_RESPONSE
obs_collector_healthy 4000 && fail "foreign listener must fail closed"

echo "PASS: observability startup policy"
