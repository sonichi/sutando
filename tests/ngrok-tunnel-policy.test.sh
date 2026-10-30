#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
POLICY="$REPO/src/ngrok-tunnel-policy.sh"
failures=0
fail() { echo "FAIL: $*" >&2; failures=$((failures + 1)); }

if [ -f "$POLICY" ]; then
  # shellcheck source=src/ngrok-tunnel-policy.sh
  source "$POLICY"
else
  fail "ngrok tunnel policy exists"
fi

expect_valid() {
  local url="$1" expected_host="${2:-}"
  if ! webhook_base_url_valid "$url" "$expected_host"; then
    fail "expected valid webhook origin: $url"
  fi
}

expect_invalid() {
  local url="$1" expected_host="${2:-}"
  if webhook_base_url_valid "$url" "$expected_host"; then
    fail "expected invalid webhook origin: $url"
  fi
}

if [ -f "$POLICY" ]; then
  expect_valid 'https://abc-12.ngrok-free.app'
  expect_valid 'https://x.ngrok.io:443/' 'X.NGROK.IO'
  expect_valid 'https://reserved.example/' 'RESERVED.EXAMPLE'

  expect_invalid 'http://x.ngrok.io'
  expect_invalid 'https://x.ngrok.io$(touch /tmp/p)'
  expect_invalid 'https://x`id`.io'
  expect_invalid 'https://x;id'
  expect_invalid 'https://a b'
  expect_invalid $'https://x.ngrok.io\nmore'
  expect_invalid 'https://x|y'
  expect_invalid 'https://x&y'
  expect_invalid 'https://other.example' 'reserved.example'
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/lsof" <<'SH'
#!/bin/bash
printf '%s' "$LSOF_OUTPUT"
SH
chmod +x "$tmp/lsof"
PATH="$tmp:$PATH"
export PATH

if [ -f "$POLICY" ]; then
  LSOF_OUTPUT='n127.0.0.1:4041'
  export LSOF_OUTPUT
  [ "$(ngrok_api_port 1234)" = "4041" ] || fail "extracts loopback API port from ngrok PID"
  LSOF_OUTPUT='n*:4040'
  [ -z "$(ngrok_api_port 1234)" ] || fail "ignores non-loopback API listener"
  LSOF_OUTPUT=''
  [ -z "$(ngrok_api_port 1234)" ] || fail "returns empty when ngrok has no API listener"
fi

if grep -Fq 'localhost:4040/api/tunnels' "$REPO/src/startup.sh"; then
  fail "startup no longer uses the default localhost ngrok API port"
fi
if ! grep -Fq 'source "$REPO/src/ngrok-tunnel-policy.sh"' "$REPO/src/startup.sh"; then
  fail "startup sources ngrok tunnel policy"
fi

if [ "$failures" -ne 0 ]; then
  echo "FAIL: $failures ngrok tunnel policy assertion(s)" >&2
  exit 1
fi
echo "PASS: ngrok tunnel policy"
