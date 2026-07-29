#!/usr/bin/env bash
# Evidence for PR #2154: the tsx/node resolution for a host with no
# homebrew/nvm/volta. Both the launchd wrapper and startup.sh resolve tsx +
# the app-node dir via the SINGLE sutando-config.sh source of truth.
set -uo pipefail
cd "$(dirname "$0")/.."
CFG="scripts/sutando-config.sh"
fail=0
say() { echo "  $1"; }

# --- app-node-dir: config-resolved, not hardcoded (ask: config-resolve path) ---
out="$(SUTANDO_APP_NODE_DIR=/custom/node/bin bash "$CFG" app-node-dir)"
[ "$out" = "/custom/node/bin" ] && say "PASS app-node-dir honors override" || { say "FAIL override: $out"; fail=1; }
out="$(unset SUTANDO_APP_NODE_DIR; bash "$CFG" app-node-dir)"
case "$out" in *space.ag2.app*node*bin) say "PASS app-node-dir default = app-support engine";; *) say "FAIL default: $out"; fail=1;; esac

# --- tsx-bin: centralized resolver; repo-pinned tsx preferred (the no-homebrew case) ---
out="$(bash "$CFG" tsx-bin)"
if [ -n "$out" ]; then
  [ -x "$out" ] && say "PASS tsx-bin returns an executable ($out)" || { say "FAIL not executable: $out"; fail=1; }
  # BEFORE/AFTER evidence: on a host with no global tsx, the repo-pinned
  # node_modules/.bin/tsx is what makes the proxy start. When present it MUST win.
  if [ -x "node_modules/.bin/tsx" ]; then
    case "$out" in */node_modules/.bin/tsx) say "PASS repo-pinned tsx preferred (no-homebrew fallback works)";; *) say "FAIL repo tsx not preferred: $out"; fail=1;; esac
  fi
else
  say "SKIP tsx-bin empty (no tsx installed) — caller falls back to npx tsx"
fi

# --- wrapper (supervised launchd path) also config-resolves the app node dir ---
# Codex #2154: the launchd wrapper is the preferred proxy path on installed
# systems; it must honor app-node-dir too, not hardcode the bundle path.
WRAP="src/launchd/credential-proxy-wrapper.sh"
if grep -q 'space.ag2.app/engine/runtime/node/bin/node' "$WRAP"; then
  say "FAIL wrapper still hardcodes the app-bundle node path"; fail=1
else
  say "PASS wrapper does not hardcode the app-bundle node path"
fi
grep -q 'sutando-config.sh" app-node-dir' "$WRAP" && say "PASS wrapper config-resolves app-node-dir" || { say "FAIL wrapper missing app-node-dir call"; fail=1; }

# --- tsx-bin on a host with NO ~/.nvm (regression: pipefail killed the script) ---
# The _nvm_tsx candidate assignment pipes `ls ~/.nvm/versions/node/` into
# sort|tail; under set -euo pipefail a missing ~/.nvm made that pipeline's
# status fatal BEFORE the candidate loop, so tsx-bin exited 1 with no output
# and startup.sh died silently at its `_TSX_BIN=$(...)` line. The old
# assertions above never caught this: empty output landed in the SKIP branch.
_fake_home="$(mktemp -d)"
out="$(HOME="$_fake_home" bash "$CFG" tsx-bin)"; rc=$?
rmdir "$_fake_home"
[ "$rc" -eq 0 ] && say "PASS tsx-bin exits 0 with no ~/.nvm" || { say "FAIL tsx-bin exit $rc with no ~/.nvm (pipefail regression)"; fail=1; }
case "$out" in */node_modules/.bin/tsx) say "PASS repo tsx still resolved with no ~/.nvm";; *) say "FAIL no-nvm resolution: '$out'"; fail=1;; esac

[ "$fail" -eq 0 ] && echo "PASS — tsx/app-node resolution centralized + config-resolved (startup + launchd)" || { echo "FAIL"; exit 1; }
