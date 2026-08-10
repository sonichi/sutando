#!/usr/bin/env bash
# Pins the plist's CLAUDE_CONFIG_DIR: launchd inherits no env, so without it the
# wrapper resolves a different path. HOME/PATH redirected, launchctl stubbed.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/home/Library/LaunchAgents" "$TMP/bin"
printf '#!/bin/sh\nexit 0\n' > "$TMP/bin/launchctl"; chmod +x "$TMP/bin/launchctl"

# Staged repo WITHOUT skills/quota-tracker, src/+scripts/ symlinked to the real
# tree: exercises the checkout-missing-skill path in dev (not bundled) mode.
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

# 3. env -i plus ONLY the plist's EnvironmentVariables — the launchd environment.
resolved="$(env -i HOME="$TMP/home" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
    CLAUDE_CONFIG_DIR="$CFG" SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1 \
    bash "$WRAPPER" --resolve-only 2>/dev/null)"
[ "$resolved" = "$EXPECTED" ]
check $? "wrapper under plist env resolves the installer-validated path"

# 4. Negative control: without the pin, resolution diverges.
prefix_resolved="$(env -i HOME="$TMP/home" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
    SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1 \
    bash "$WRAPPER" --resolve-only 2>/dev/null)"
[ "$prefix_resolved" != "$EXPECTED" ] && echo "$prefix_resolved" | grep -q "/.claude/"
check $? "without the pin, resolution falls back to ~/.claude (the bug class)"

# A loaded-but-old plist must read as drift. SUTANDO_NODE alone cannot see it:
# it is empty on both sides of a dev host, so that comparison passes.
IS_CURRENT=(env HOME="$TMP/home" PATH="$TMP/bin:$PATH" CLAUDE_CONFIG_DIR="$CFG" SUTANDO_NODE=)

# 5. The plist just installed IS current.
"${IS_CURRENT[@]}" bash "$INSTALLER" is-current
check $? "a freshly installed plist reads as current"

# plistlib, not PlistBuddy: the latter is macOS-only, so on Linux every read below
# would return empty and the drift cases would pass without exercising anything.
plist_get() { python3 -c 'import plistlib,sys
with open(sys.argv[1],"rb") as fh: d=plistlib.load(fh)
sys.stdout.write((d.get("EnvironmentVariables") or {}).get(sys.argv[2],""))' "$1" "$2"; }
plist_has() { python3 -c 'import plistlib,sys
with open(sys.argv[1],"rb") as fh: d=plistlib.load(fh)
sys.exit(0 if sys.argv[2] in (d.get("EnvironmentVariables") or {}) else 1)' "$1" "$2"; }
plist_set() { python3 -c 'import plistlib,sys
with open(sys.argv[1],"rb") as fh: d=plistlib.load(fh)
d["EnvironmentVariables"][sys.argv[2]]=sys.argv[3]
with open(sys.argv[1],"wb") as fh: plistlib.dump(d,fh)' "$1" "$2" "$3"; }
plist_del() { python3 -c 'import plistlib,sys
with open(sys.argv[1],"rb") as fh: d=plistlib.load(fh)
d["EnvironmentVariables"].pop(sys.argv[2],None)
with open(sys.argv[1],"wb") as fh: plistlib.dump(d,fh)' "$1" "$2"; }

# 6. An OLD plist predating the config pin: the key is absent entirely.
cp "$PLIST" "$TMP/current.plist"
plist_del "$PLIST" CLAUDE_CONFIG_DIR
plist_has "$PLIST" CLAUDE_CONFIG_DIR
[ $? -ne 0 ]
check $? "PREMISE: the staged old plist really has no CLAUDE_CONFIG_DIR key"
"${IS_CURRENT[@]}" bash "$INSTALLER" is-current
[ $? -ne 0 ]
check $? "an old plist WITHOUT the config pin reads as DRIFT (reinstall)"

# 7. Present but pointing at another clone — same verdict, different cause.
cp "$TMP/current.plist" "$PLIST"
plist_set "$PLIST" CLAUDE_CONFIG_DIR "$TMP/other-home"
"${IS_CURRENT[@]}" bash "$INSTALLER" is-current
[ $? -ne 0 ]
check $? "a plist pinned to a DIFFERENT config dir reads as drift"

# 8. Restoring it flips the verdict back — proves 6 and 7 track the pin, not a
#    check that fails for any reason once the plist has been touched.
cp "$TMP/current.plist" "$PLIST"
"${IS_CURRENT[@]}" bash "$INSTALLER" is-current
check $? "restoring the correct pin reads as current again"

# 9. The drift cases must not pass merely because the reader is broken — a dead
#    reader returns empty, which reads as drift for any pin at all.
[ "$(plist_get "$PLIST" CLAUDE_CONFIG_DIR)" = "$CFG" ]
check $? "CONTROL: the plist reader really returns the pin, so drift means drift"

# 10. A job rendered from ANOTHER CHECKOUT: node + config dir match, everything that
#     identifies the clone does not. Enumerating fields is what missed this.
cp "$TMP/current.plist" "$PLIST"
python3 - "$PLIST" "$TMP/other-checkout" <<'PY' >/dev/null
import plistlib, sys
with open(sys.argv[1], "rb") as fh: d = plistlib.load(fh)
d["ProgramArguments"][1] = sys.argv[2] + "/src/launchd/credential-proxy-wrapper.sh"
d["WorkingDirectory"] = sys.argv[2]
d["EnvironmentVariables"]["SUTANDO_WORKSPACE"] = sys.argv[2] + "/workspace"
with open(sys.argv[1], "wb") as fh: plistlib.dump(d, fh)
PY
[ "$(plist_get "$PLIST" CLAUDE_CONFIG_DIR)" = "$CFG" ]
check $? "PREMISE: the cross-checkout plist still carries the SAME config pin"
"${IS_CURRENT[@]}" bash "$INSTALLER" is-current
[ $? -ne 0 ]
check $? "a job rendered from ANOTHER CHECKOUT reads as drift"
cp "$TMP/current.plist" "$PLIST"

# 11. Wiring: a drift check startup.sh does not call is a no-op.
grep -q 'is-current' "$REPO/src/startup.sh"
check $? "startup.sh gates the reinstall on the installer's is-current"
grep -q 'EnvironmentVariables:SUTANDO_NODE' "$REPO/src/startup.sh"
[ $? -ne 0 ]
check $? "startup.sh no longer compares SUTANDO_NODE by itself"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
