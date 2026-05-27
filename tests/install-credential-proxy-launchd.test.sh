#!/bin/bash
# Tests for src/install-credential-proxy-launchd.sh
#
# Validates template rendering, path resolution helpers, and CLI modes.
# Does NOT load/unload real launchd jobs — all tests use tmp dirs and
# mocked launchctl to stay safe in CI and non-root contexts.

set -u

PASS=0
FAIL=0
SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/src/install-credential-proxy-launchd.sh"
TEMPLATE="$(cd "$(dirname "$0")/.." && pwd)/src/launchd/com.sutando.credential-proxy.plist"

ok() { echo "  PASS: $1"; ((PASS++)); }
fail() { echo "  FAIL: $1"; ((FAIL++)); }

assert_contains() {
  local label="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -qF "$needle"; then
    ok "$label"
  else
    fail "$label — expected to find: $needle"
    echo "    in: $(echo "$haystack" | head -5)"
  fi
}

assert_not_contains() {
  local label="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -qF "$needle"; then
    fail "$label — did NOT expect: $needle"
  else
    ok "$label"
  fi
}

assert_exits() {
  local label="$1" expected_code="$2"
  shift 2
  if "$@" >/dev/null 2>&1; then
    actual=0
  else
    actual=$?
  fi
  if [ "$actual" = "$expected_code" ]; then
    ok "$label"
  else
    fail "$label — expected exit $expected_code, got $actual"
  fi
}

echo "=== install-credential-proxy-launchd.sh tests ==="

# --- plist template exists and has required fields ---
if [ -f "$TEMPLATE" ]; then
  ok "plist template exists"
else
  fail "plist template missing: $TEMPLATE"
fi

plist_content="$(cat "$TEMPLATE" 2>/dev/null)"

assert_contains "plist: Label is correct" "com.sutando.credential-proxy" "$plist_content"
assert_contains "plist: KeepAlive key present" "<key>KeepAlive</key>" "$plist_content"
assert_contains "plist: KeepAlive is true" "<true/>" "$plist_content"
assert_contains "plist: ThrottleInterval key present" "<key>ThrottleInterval</key>" "$plist_content"
assert_contains "plist: ThrottleInterval value is 10" "<integer>10</integer>" "$plist_content"
assert_contains "plist: RunAtLoad key present" "<key>RunAtLoad</key>" "$plist_content"
assert_contains "plist: SUTANDO_WORKSPACE env var" "<key>SUTANDO_WORKSPACE</key>" "$plist_content"
assert_contains "plist: __NPX__ placeholder" "__NPX__" "$plist_content"
assert_contains "plist: __CREDENTIAL_PROXY__ placeholder" "__CREDENTIAL_PROXY__" "$plist_content"
assert_contains "plist: __WORKSPACE__ in log path" "__WORKSPACE__" "$plist_content"
assert_contains "plist: __NODE_BIN__ in PATH" "__NODE_BIN__" "$plist_content"
assert_contains "plist: ProcessType Background" "Background" "$plist_content"

# No StartInterval — this is a persistent daemon, not a periodic job
assert_not_contains "plist: no StartInterval (daemon not periodic)" "StartInterval" "$plist_content"

# --- installer: usage / bad argument exits non-zero ---
assert_exits "bad argument exits 2" 2 bash "$SCRIPT" --bogus-flag

# --- installer: --status when not loaded is graceful ---
# Temporarily mock launchctl to simulate "not loaded"
TMP_BIN="$(mktemp -d)"
cat > "$TMP_BIN/launchctl" <<'EOF'
#!/bin/bash
if [ "$1" = "print" ]; then
  exit 1   # simulate "not loaded"
fi
EOF
chmod +x "$TMP_BIN/launchctl"

status_out="$(PATH="$TMP_BIN:$PATH" bash "$SCRIPT" --status 2>&1)"
assert_contains "--status when not loaded prints 'not loaded'" "(not loaded)" "$status_out"
rm -rf "$TMP_BIN"

# --- template rendering: sed substitutions work ---
TMP_WORK="$(mktemp -d)"
FAKE_REPO="$TMP_WORK/repo"
FAKE_WORKSPACE="$TMP_WORK/workspace"
FAKE_NPX="$TMP_WORK/npx"
FAKE_NODE_BIN="$TMP_WORK"
FAKE_HOMEBREW="$TMP_WORK/homebrew"
FAKE_PROXY="$TMP_WORK/credential-proxy.ts"
FAKE_DEST="$TMP_WORK/output.plist"

# Create required files for the test
mkdir -p "$FAKE_REPO/src/launchd"
mkdir -p "$FAKE_REPO/src"
cp "$TEMPLATE" "$FAKE_REPO/src/launchd/com.sutando.credential-proxy.plist"
touch "$FAKE_NPX"
chmod +x "$FAKE_NPX"
touch "$FAKE_PROXY"

# Render the template manually (mirrors installer logic without launchctl)
rendered="$(sed \
    -e "s|__REPO__|$FAKE_REPO|g" \
    -e "s|__WORKSPACE__|$FAKE_WORKSPACE|g" \
    -e "s|__NPX__|$FAKE_NPX|g" \
    -e "s|__NODE_BIN__|$FAKE_NODE_BIN|g" \
    -e "s|__HOMEBREW_BIN__|$FAKE_HOMEBREW|g" \
    -e "s|__CREDENTIAL_PROXY__|$FAKE_PROXY|g" \
    -e "s|__HOME__|$TMP_WORK|g" \
    "$TEMPLATE")"

assert_contains "render: __REPO__ replaced" "$FAKE_REPO" "$rendered"
assert_contains "render: __WORKSPACE__ replaced" "$FAKE_WORKSPACE" "$rendered"
assert_contains "render: __NPX__ replaced" "$FAKE_NPX" "$rendered"
assert_contains "render: __NODE_BIN__ replaced" "$FAKE_NODE_BIN" "$rendered"
assert_contains "render: __CREDENTIAL_PROXY__ replaced" "$FAKE_PROXY" "$rendered"
assert_not_contains "render: no raw __NPX__ placeholder remains" "__NPX__" "$rendered"
assert_not_contains "render: no raw __WORKSPACE__ placeholder remains" "__WORKSPACE__" "$rendered"
assert_not_contains "render: no raw __REPO__ placeholder remains" "__REPO__" "$rendered"
assert_not_contains "render: no raw __CREDENTIAL_PROXY__ placeholder remains" "__CREDENTIAL_PROXY__" "$rendered"

# Rendered output must still be valid plist XML
if echo "$rendered" | grep -q '<?xml'; then
  ok "render: output is XML"
else
  fail "render: output is not XML"
fi

rm -rf "$TMP_WORK"

# --- installer: uninstall when plist absent is graceful ---
TMP_BIN2="$(mktemp -d)"
cat > "$TMP_BIN2/launchctl" <<'EOF'
#!/bin/bash
if [ "$1" = "print" ]; then exit 1; fi
EOF
chmod +x "$TMP_BIN2/launchctl"

uninstall_out="$(HOME="$TMP_BIN2" PATH="$TMP_BIN2:$PATH" bash "$SCRIPT" --uninstall 2>&1)"
assert_contains "uninstall when absent: graceful message" "nothing to remove" "$uninstall_out"
rm -rf "$TMP_BIN2"

# --- Summary ---
echo
TOTAL=$((PASS + FAIL))
echo "Results: $PASS/$TOTAL passed"
if [ "$FAIL" -gt 0 ]; then
  echo "FAILED"
  exit 1
else
  echo "ALL TESTS PASSED"
  exit 0
fi
