#!/usr/bin/env bash
# Regression: the credential-proxy installer must validate the target the
# WRAPPER will actually use, not the dev-only TS source.
#
# A pristine bundled host ships dist/credential-proxy.js and has NO
# quota-tracker skill directory. Before this test's fix, the installer gated on
# the TS source unconditionally and exited 1; startup.sh then fell through to a
# legacy path that resolved the same missing TS file, so the host ended up with
# no credential proxy at all and lost quota/auth telemetry silently.
#
# Isolation: HOME and PATH are redirected so the real user's LaunchAgents and
# launchd domain are never touched — `launchctl` is shadowed by a no-op stub.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/home/Library/LaunchAgents" "$TMP/bin" "$TMP/empty-claude-home"
printf '#!/bin/sh\nexit 0\n' > "$TMP/bin/launchctl"; chmod +x "$TMP/bin/launchctl"

# Stage a throwaway repo so the test NEVER mutates the real dist/ — which on a
# bundled host is the live production credential-proxy.js. src/ and scripts/ are
# symlinked to the real tree (so we run the real installer + helpers), but dist/
# is a scratch dir we own. The installer resolves its REPO to this stage
# (`cd "$(dirname "$0")/.."`), so the present/absent scenarios only touch the
# scratch dist — no stash/restore of a live artifact, and an interrupt anywhere
# leaves the real tree untouched (the EXIT trap removes only $TMP; `rm -rf` on the
# symlinks drops the links, not their targets).
STAGE="$TMP/repo"; mkdir -p "$STAGE/dist"
ln -s "$REPO/src" "$STAGE/src"
ln -s "$REPO/scripts" "$STAGE/scripts"
INSTALLER="$STAGE/src/install-credential-proxy-launchd.sh"

run_install() {  # $1 = extra env assignments applied inline
  env HOME="$TMP/home" PATH="$TMP/bin:$PATH" CLAUDE_CONFIG_DIR="$TMP/empty-claude-home" \
      SUTANDO_NODE="${SUTANDO_NODE_OVERRIDE:-}" bash "$INSTALLER" install 2>&1
}

# 1. bundled mode + dist present -> must NOT reject over the missing TS source
echo "// test artifact" > "$STAGE/dist/credential-proxy.js"
out="$(SUTANDO_NODE_OVERRIDE=/usr/bin/env run_install)"
echo "$out" | grep -q "quota-tracker skill not found"; [ $? -ne 0 ]
check $? "bundled mode does not gate on the dev-only TS source"

# 2. bundled mode + dist MISSING -> must fail closed, naming the packaging error
rm -f "$STAGE/dist/credential-proxy.js"
out="$(SUTANDO_NODE_OVERRIDE=/usr/bin/env run_install)"
echo "$out" | grep -q "desktop packaging error"
check $? "bundled mode fails closed when dist/credential-proxy.js is absent"

# 3. dev mode (no SUTANDO_NODE, repo outside an app bundle) still requires the TS source
out="$(SUTANDO_NODE_OVERRIDE= run_install)"
echo "$out" | grep -q "quota-tracker skill not found"
check $? "dev mode still requires credential-proxy.ts"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
