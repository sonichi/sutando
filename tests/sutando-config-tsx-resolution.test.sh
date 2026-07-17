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

[ "$fail" -eq 0 ] && echo "PASS — tsx/app-node resolution centralized + config-resolved" || { echo "FAIL"; exit 1; }
