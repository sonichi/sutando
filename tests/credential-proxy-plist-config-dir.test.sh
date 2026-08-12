#!/usr/bin/env bash
# The credential-proxy launchd job must carry CLAUDE_CONFIG_DIR. Assertions read
# the RENDERED plist; a grep for the sed line passes even if the template drops.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/home/Library/LaunchAgents" "$TMP/bin"
printf '#!/bin/sh\nexit 0\n' > "$TMP/bin/launchctl"; chmod +x "$TMP/bin/launchctl"

# Stage a throwaway repo so the test never mutates the real dist/, which on a
# bundled host is the live production credential-proxy.js.
STAGE="$TMP/repo"; mkdir -p "$STAGE/dist"
ln -s "$REPO/src" "$STAGE/src"
ln -s "$REPO/scripts" "$STAGE/scripts"
INSTALLER="$STAGE/src/install-credential-proxy-launchd.sh"
DEST="$TMP/home/Library/LaunchAgents/com.sutando.credential-proxy.plist"
echo "// test artifact" > "$STAGE/dist/credential-proxy.js"

# Bundled mode (SUTANDO_NODE set) so the install completes without a
# quota-tracker skill dir present. CLAUDE_CONFIG_DIR drives claude-home-path.
run_install() {  # $1 = config dir handed to the installer
  env HOME="$TMP/home" PATH="$TMP/bin:$PATH" CLAUDE_CONFIG_DIR="$1" \
      SUTANDO_NODE=/usr/bin/env bash "$INSTALLER" install 2>&1
}

# Read one EnvironmentVariables value out of the rendered plist. Parsing (rather
# than grepping) is also what proves the substitution produced well-formed XML.
plist_env() {  # $1 = key
  python3 -c '
import plistlib, sys
with open(sys.argv[1], "rb") as fh:
    d = plistlib.load(fh)
print(d.get("EnvironmentVariables", {}).get(sys.argv[2], ""))' "$DEST" "$1"
}

# --- 1: the rendered job declares the key, with the resolved value ------------
CFG="$TMP/claude-home"; mkdir -p "$CFG"
rm -f "$DEST"
out="$(run_install "$CFG")" || true
[ -f "$DEST" ]
check $? "install renders a plist"

python3 -c '
import plistlib, sys
with open(sys.argv[1], "rb") as fh:
    plistlib.load(fh)' "$DEST" 2>/dev/null
check $? "the rendered plist is well-formed and parses"

[ "$(plist_env CLAUDE_CONFIG_DIR)" = "$CFG" ]
check $? "EnvironmentVariables.CLAUDE_CONFIG_DIR is the resolved config dir"

# An empty value is the exact failure this pins: the proxy would fall back to
# the vanilla keychain item, which is indistinguishable from the key's absence.
[ -n "$(plist_env CLAUDE_CONFIG_DIR)" ]
check $? "the value is non-empty"

grep -q '__CLAUDE_CONFIG_DIR__' "$DEST"; [ $? -ne 0 ]
check $? "no unsubstituted placeholder survives in the installed plist"

# The neighbouring keys must still render — a new sed clause that clobbered one
# of them would otherwise pass every assertion above.
[ -n "$(plist_env SUTANDO_WORKSPACE)" ] && [ -n "$(plist_env HOME)" ]
check $? "the pre-existing environment keys still render"

# --- 2: a config dir containing & renders valid XML, not a corrupt plist ------
# & is the character that turns an unescaped substitution into an XML parse
# error; without the escaping the load below raises and the value never matches.
AMP="$TMP/we&ird-home"; mkdir -p "$AMP"
rm -f "$DEST"
out="$(run_install "$AMP")" || true
[ -f "$DEST" ] && python3 -c '
import plistlib, sys
with open(sys.argv[1], "rb") as fh:
    plistlib.load(fh)' "$DEST" 2>/dev/null
check $? "a config dir containing & still renders a parseable plist"

[ "$(plist_env CLAUDE_CONFIG_DIR)" = "$AMP" ]
check $? "the & path round-trips to the literal value launchd will export"

# --- 3: an unresolvable config dir fails the install, it does not ship empty --
# A stub scripts/ fails only the bare claude-home-path lookup: shipping an empty
# value is the silent-wrong-account outcome, so the install must refuse.
STAGE2="$TMP/repo-noconfig"; mkdir -p "$STAGE2/dist" "$STAGE2/scripts" "$STAGE2/src"
# Per-file symlinks, not a symlinked dir: the kernel resolves `src/..` through a
# dir symlink to the real repo, reaching the real scripts/ and skipping the stub.
for f in install-credential-proxy-launchd.sh workspace_resolve.sh launchd; do
  ln -s "$REPO/src/$f" "$STAGE2/src/$f"
done
echo "// test artifact" > "$STAGE2/dist/credential-proxy.js"
cat > "$STAGE2/scripts/sutando-config.sh" << STUB
#!/bin/bash
if [ "\$1" = "claude-home-path" ] && [ \$# -eq 1 ]; then exit 0; fi
exec bash "$REPO/scripts/sutando-config.sh" "\$@"
STUB
chmod +x "$STAGE2/scripts/sutando-config.sh"

rm -f "$DEST"
out="$(env HOME="$TMP/home" PATH="$TMP/bin:$PATH" CLAUDE_CONFIG_DIR="$CFG" \
      SUTANDO_NODE=/usr/bin/env bash "$STAGE2/src/install-credential-proxy-launchd.sh" install 2>&1)"
rc=$?
[ "$rc" -ne 0 ]
check $? "an unresolvable config dir fails the install"

echo "$out" | grep -q "could not resolve canonical Claude config directory"
check $? "the refusal names the reason"

[ ! -f "$DEST" ]
check $? "no plist is installed when the config dir cannot be resolved"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
