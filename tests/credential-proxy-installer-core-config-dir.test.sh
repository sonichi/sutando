#!/usr/bin/env bash
# The credential-proxy launchd job must run with the CORE's CLAUDE_CONFIG_DIR —
# the one the config helper resolves (claude-sutando-config-dir), as startup.sh
# and start-cli.sh do — not with whatever the installing shell happens to carry.
# The proxy hashes that dir to pick its keychain item: pinned to the installer's
# environment (or its ~/.claude default) the proxy read the machine's shared
# login and put it on the core's requests. HOME/PATH redirected, launchctl stubbed.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/home/Library/LaunchAgents" "$TMP/bin"
printf '#!/bin/sh\nexit 0\n' > "$TMP/bin/launchctl"; chmod +x "$TMP/bin/launchctl"

# A throwaway repo (never the real dist/), bundled mode so no skill dir is needed.
STAGE="$TMP/repo"; mkdir -p "$STAGE/dist"
ln -s "$REPO/src" "$STAGE/src"
# shellcheck source=lib/core-config-dir-stub.sh
source "$REPO/tests/lib/core-config-dir-stub.sh"
stage_core_config_scripts "$REPO" "$STAGE"
echo "// test artifact" > "$STAGE/dist/credential-proxy.js"
INSTALLER="$STAGE/src/install-credential-proxy-launchd.sh"
PLIST="$TMP/home/Library/LaunchAgents/com.sutando.credential-proxy.plist"

CORE="$TMP/workspace/.claude-sutando"      # what the core runs with
OTHER="$TMP/some-other-claude-home"         # what an unrelated shell carries
DEFAULT_HOME="$TMP/home/.claude"            # Claude Code's user-level default

# The installer under three shells: one with no CLAUDE_CONFIG_DIR at all (a
# terminal, a launchd/app spawn), one carrying an unrelated dir, one the core's.
BARE=(env -u CLAUDE_CONFIG_DIR -u CLAUDE_HOME HOME="$TMP/home" PATH="$TMP/bin:$PATH" \
      TEST_CORE_CONFIG_DIR="$CORE" SUTANDO_NODE=/usr/bin/env)
FOREIGN=(env -u CLAUDE_HOME HOME="$TMP/home" PATH="$TMP/bin:$PATH" \
      TEST_CORE_CONFIG_DIR="$CORE" CLAUDE_CONFIG_DIR="$OTHER" SUTANDO_NODE=/usr/bin/env)

plist_get() { python3 -c 'import plistlib,sys
with open(sys.argv[1],"rb") as fh: d=plistlib.load(fh)
sys.stdout.write((d.get("EnvironmentVariables") or {}).get("CLAUDE_CONFIG_DIR",""))' "$PLIST"; }
plist_set() { python3 -c 'import plistlib,sys
with open(sys.argv[1],"rb") as fh: d=plistlib.load(fh)
d["EnvironmentVariables"]["CLAUDE_CONFIG_DIR"]=sys.argv[2]
with open(sys.argv[1],"wb") as fh: plistlib.dump(d,fh)' "$PLIST" "$1"; }

# --- 1: install from a shell with NO CLAUDE_CONFIG_DIR ------------------------
rm -f "$PLIST"
out="$("${BARE[@]}" bash "$INSTALLER" install 2>&1)"
[ -f "$PLIST" ]
check $? "install from a shell without CLAUDE_CONFIG_DIR renders a plist"
[ "$(plist_get)" = "$CORE" ]
check $? "that plist pins the CORE's config dir (got: $(plist_get))"
[ "$(plist_get)" != "$DEFAULT_HOME" ]
check $? "it does not pin Claude Code's user-level default (~/.claude)"
echo "$out" | grep -q "config:    $CORE"
check $? "the install output names the dir it pinned"

# --- 2: install from a shell carrying an UNRELATED config dir -----------------
rm -f "$PLIST"
"${FOREIGN[@]}" bash "$INSTALLER" install > /dev/null 2>&1
[ "$(plist_get)" = "$CORE" ]
check $? "an unrelated CLAUDE_CONFIG_DIR in the installing shell does not win (got: $(plist_get))"

# --- 3: is-current agrees from every shell ------------------------------------
"${BARE[@]}" bash "$INSTALLER" is-current
check $? "the job just installed reads as current from a bare shell"
"${FOREIGN[@]}" bash "$INSTALLER" is-current
check $? "and from a shell carrying an unrelated config dir"

# --- 4: an already-loaded job pinned to another dir is drift ------------------
# What an install from a bare shell used to write. is-current, run from the same
# kind of shell, re-rendered the same ~/.claude and called the job current, so
# startup never reinstalled it.
cp "$PLIST" "$TMP/current.plist"
plist_set "$DEFAULT_HOME"
[ "$(plist_get)" = "$DEFAULT_HOME" ]
check $? "PREMISE: the staged loaded job is pinned to ~/.claude"
"${BARE[@]}" bash "$INSTALLER" is-current
[ $? -ne 0 ]
check $? "a loaded job pinned to ~/.claude reads as DRIFT from a bare shell (reinstall)"

plist_set "$OTHER"
"${FOREIGN[@]}" bash "$INSTALLER" is-current
[ $? -ne 0 ]
check $? "a loaded job pinned to the installing shell's own dir reads as DRIFT"

cp "$TMP/current.plist" "$PLIST"
"${BARE[@]}" bash "$INSTALLER" is-current
check $? "CONTROL: restoring the core's dir reads as current again"

# --- 5: startup's reinstall of a drifted job lands the core's dir -------------
plist_set "$DEFAULT_HOME"
"${BARE[@]}" bash "$INSTALLER" is-current || "${BARE[@]}" bash "$INSTALLER" install > /dev/null 2>&1
[ "$(plist_get)" = "$CORE" ]
check $? "is-current || install (startup.sh's gate) repairs the pinned dir"

# --- 6: the legacy bare launch in startup.sh runs under the core's dir --------
# No launchd job on the host: the proxy is a child of startup.sh and inherits the
# CLAUDE_CONFIG_DIR startup exported from the same helper. Function extracted
# verbatim from startup.sh.
fn="$(awk '/^start_credential_proxy\(\) \{/,/^\}/' "$REPO/src/startup.sh")"
mkdir -p "$TMP/legacy/bin" "$TMP/legacy/repo/src"
printf '#!/bin/sh\nexit 1\n' > "$TMP/legacy/bin/launchctl"
printf '#!/bin/sh\nexit 1\n' > "$TMP/legacy/bin/lsof"
chmod +x "$TMP/legacy/bin/launchctl" "$TMP/legacy/bin/lsof"
# The function sends the child's own output to its log, so the stub records to a file.
PATH="$TMP/legacy/bin:$PATH" REPO="$TMP/legacy/repo" WORKSPACE="$TMP/legacy" BUNDLED_MODE=1 \
  CHILD_SEEN="$TMP/legacy/child.seen" CLAUDE_CONFIG_DIR="$CORE" bash -c "
    run_node_service() { echo \"child CLAUDE_CONFIG_DIR=\${CLAUDE_CONFIG_DIR:-<unset>}\" > \"\$CHILD_SEEN\"; }
    sleep() { :; }
    $fn
    start_credential_proxy
    wait
  " > /dev/null 2>&1
seen="$(cat "$TMP/legacy/child.seen" 2>/dev/null)"
[ "$seen" = "child CLAUDE_CONFIG_DIR=$CORE" ]
check $? "the legacy bare launch starts the proxy under the core's config dir (got: $seen)"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
