#!/usr/bin/env bash
# Every python call the installer makes must go through the resolved interpreter.
# A bare python3 on a clean Mac is the CLT stub, whose execution raises a modal.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/home/Library/LaunchAgents" "$TMP/bin"
printf '#!/bin/sh\nexit 0\n' > "$TMP/bin/launchctl"; chmod +x "$TMP/bin/launchctl"

REAL_PY="$(command -v python3)"
PATH_MARK="$TMP/path-python3-was-executed"
PINNED_MARK="$TMP/pinned-python3-was-executed"

# A PATH python3 that RECORDS being run, then behaves normally. On a clean Mac
# this position holds the stub; here it is a tripwire, so an invocation is
# observable instead of a dialog.
cat > "$TMP/bin/python3" <<EOF
#!/bin/sh
echo run >> "$PATH_MARK"
exec "$REAL_PY" "\$@"
EOF
chmod +x "$TMP/bin/python3"

cat > "$TMP/bin/pinned-python3" <<EOF
#!/bin/sh
echo run >> "$PINNED_MARK"
exec "$REAL_PY" "\$@"
EOF
chmod +x "$TMP/bin/pinned-python3"

STAGE="$TMP/repo"; mkdir -p "$STAGE"
ln -s "$REPO/src" "$STAGE/src"
ln -s "$REPO/scripts" "$STAGE/scripts"
INSTALLER="$STAGE/src/install-credential-proxy-launchd.sh"

CFG="$TMP/claude-home"
mkdir -p "$CFG/skills/quota-tracker/scripts"
echo "// proxy target (test artifact)" > "$CFG/skills/quota-tracker/scripts/credential-proxy.ts"
PLIST="$TMP/home/Library/LaunchAgents/com.sutando.credential-proxy.plist"

RUN=(env HOME="$TMP/home" PATH="$TMP/bin:$PATH" CLAUDE_CONFIG_DIR="$CFG" SUTANDO_NODE=
     SUTANDO_PY="$TMP/bin/pinned-python3")

"${RUN[@]}" bash "$INSTALLER" install > /dev/null 2>&1
[ -f "$PLIST" ]
check $? "PREMISE: the install renders a plist under the pinned interpreter"

# PREMISE for the whole file: the tripwire must be capable of firing. Without
# this, every assertion below passes on a python3 that was never reachable.
"$TMP/bin/python3" -c 'pass' 2>/dev/null
[ -s "$PATH_MARK" ]
check $? "PREMISE: the PATH tripwire records an invocation when it IS run"
: > "$PATH_MARK"

# is-current renders and then compares. Both halves must use the pinned binary.
: > "$PINNED_MARK"
"${RUN[@]}" bash "$INSTALLER" is-current
rc=$?
[ "$rc" -eq 0 ]
check $? "a freshly installed plist still reads as current under a pinned interpreter"

[ -s "$PINNED_MARK" ]
check $? "is-current runs the RESOLVED interpreter"

[ ! -s "$PATH_MARK" ]
check $? "is-current never executes the PATH python3 (the stub position)"

# The comparison half specifically: drift must be decided by the pinned binary.
python3 - "$PLIST" <<'PY'
import plistlib, sys
p = sys.argv[1]
with open(p, "rb") as fh:
    d = plistlib.load(fh)
d.setdefault("EnvironmentVariables", {})["SUTANDO_WORKSPACE"] = "/drifted"
with open(p, "wb") as fh:
    plistlib.dump(d, fh)
PY
: > "$PATH_MARK"; : > "$PINNED_MARK"
"${RUN[@]}" bash "$INSTALLER" is-current
[ $? -ne 0 ]
check $? "a drifted plist reads as drift (the comparison actually ran)"

[ ! -s "$PATH_MARK" ]
check $? "the drift comparison also avoids the PATH python3"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; fi
exit "$fail"
