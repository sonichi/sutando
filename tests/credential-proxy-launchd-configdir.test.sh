#!/usr/bin/env bash
# Pins the plist's CLAUDE_CONFIG_DIR: launchd inherits no env, so without the pin
# the wrapper resolves a different proxy path than the installer validated.
#
# Asserts the installer persists the resolved dir, the wrapper resolves the SAME
# path under env -i + the plist's EnvironmentVariables, and that dropping the pin
# demonstrably diverges — the negative control that makes the other two mean something.
#
# Isolation: HOME/PATH redirected and launchctl stubbed, so it never touches the
# real LaunchAgents or launchd domain.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/home/Library/LaunchAgents" "$TMP/bin"
printf '#!/bin/sh\nexit 0\n' > "$TMP/bin/launchctl"; chmod +x "$TMP/bin/launchctl"

# Staged repo WITHOUT skills/quota-tracker — the checkout-missing-skill case.
# src/ and scripts/ symlink to the real tree so the real installer + wrapper
# + sutando-config run; dist/ stays empty (dev mode, not bundled).
STAGE="$TMP/repo"; mkdir -p "$STAGE"
ln -s "$REPO/src" "$STAGE/src"
ln -s "$REPO/scripts" "$STAGE/scripts"
INSTALLER="$STAGE/src/install-credential-proxy-launchd.sh"
WRAPPER="$STAGE/src/launchd/credential-proxy-wrapper.sh"

# Namespaced claude-home that DOES carry the skill — what the installer
# validates against interactively.
CFG="$TMP/claude-home"
mkdir -p "$CFG/skills/quota-tracker/scripts"
echo "// proxy target (test artifact)" > "$CFG/skills/quota-tracker/scripts/credential-proxy.ts"
EXPECTED="$CFG/skills/quota-tracker/scripts/credential-proxy.ts"

PLIST="$TMP/home/Library/LaunchAgents/com.sutando.credential-proxy.plist"

# 1. Install with the namespaced CLAUDE_CONFIG_DIR (interactive env).
out="$(env HOME="$TMP/home" PATH="$TMP/bin:$PATH" CLAUDE_CONFIG_DIR="$CFG" SUTANDO_NODE= \
      bash "$INSTALLER" install 2>&1)"
[ -f "$PLIST" ]
check $? "installer renders the plist for a checkout-missing-skill install"

# 2. The plist persists the resolved CLAUDE_CONFIG_DIR.
grep -A1 "<key>CLAUDE_CONFIG_DIR</key>" "$PLIST" | grep -q "<string>$CFG</string>"
check $? "plist persists the install-time CLAUDE_CONFIG_DIR"

# 3. Launchd-like environment: env -i plus ONLY what the plist's
#    EnvironmentVariables provides. The wrapper must resolve the exact path
#    the installer validated.
resolved="$(env -i HOME="$TMP/home" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
    CLAUDE_CONFIG_DIR="$CFG" SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1 \
    bash "$WRAPPER" --resolve-only 2>/dev/null)"
[ "$resolved" = "$EXPECTED" ]
check $? "wrapper under plist env resolves the installer-validated path"

# 4. Divergence proof: the PRE-FIX launchd env (no CLAUDE_CONFIG_DIR)
#    resolves somewhere else — the ~/.claude fallback the crash-loop hit.
prefix_resolved="$(env -i HOME="$TMP/home" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
    SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1 \
    bash "$WRAPPER" --resolve-only 2>/dev/null)"
[ "$prefix_resolved" != "$EXPECTED" ] && echo "$prefix_resolved" | grep -q "/.claude/"
check $? "without the pin, resolution falls back to ~/.claude (the bug class)"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
