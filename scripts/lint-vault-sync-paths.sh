#!/usr/bin/env bash
# Sutando lint: per-host carried paths must be hostname-qualified.
#
# The workspace sync (scripts/sync-workspace.sh) is branch-per-host: a host
# pushes to host/<hostname>/<wsId> and PULLS peers via 3-way merge. That merge
# isolates by branch on push, but RE-COLLIDES same-path files on pull — so any
# carried file at a path with no per-host qualifier gets cross-host merged.
# For genuinely per-host data (channel allowlists/tokens, device identity, a
# host's cron set) that is data loss: e.g. every host's
# `channels/<svc>/access.json` would merge into one. See
# docs/workspace-per-host-paths.md for the full rationale.
#
# Rule enforced here: a `vault.sync.include` entry that matches a known
# per-host-prone path MUST be hostname-qualified (carry a `<hostname>` /
# `$(hostname)` token, or a `*` glob segment that expands to per-host files).
# Bare per-host paths in the carrier set fail the lint.
#
# Usage:
#   bash scripts/lint-vault-sync-paths.sh            # lint sutando.config*.json
#   bash scripts/lint-vault-sync-paths.sh <file>...  # lint specific config files
#
# Exit 0 = clean, 1 = a bare per-host path is carried.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Paths that hold per-host data and therefore collapse if carried without a
# hostname qualifier. Matched as substrings against each include entry.
PER_HOST_PRONE=(
  "channels/"          # access.json (allowlists/TOFU/tiers), .env (tokens)
  "state/auth"         # device.json, cloud-auth.json — per-host identity
  "device.json"
  "cloud-auth.json"
  "state/cores"        # <hostname>.alive liveness
  "crons.json"         # a bare crons.json is per-host config; carry it under hosts/<hostname>/crons.json
)

# A carried entry is "hostname-qualified" if it contains an explicit host token
# or a glob segment (so each host expands to its own file, like
# build_log/<hostname>.md or crons/<hostname>.json written per host).
_is_host_qualified() {
  local p="$1"
  case "$p" in
    *'<hostname>'*|*'$(hostname)'*|*'${hostname}'*|*'*'*) return 0 ;;
    *) return 1 ;;
  esac
}

_config_files() {
  if [ "$#" -gt 0 ]; then
    printf '%s\n' "$@"
  else
    for f in "$SCRIPT_DIR/sutando.config.json" "$SCRIPT_DIR/sutando.config.local.json"; do
      [ -f "$f" ] && printf '%s\n' "$f"
    done
  fi
}

fail=0
while IFS= read -r cfg; do
  [ -z "$cfg" ] && continue
  # Extract vault.sync.include entries (one per line) without a JSON dep.
  includes="$(python3 - "$cfg" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for p in (d.get("vault", {}).get("sync", {}).get("include", []) or []):
    print(p)
PY
)"
  while IFS= read -r entry; do
    [ -z "$entry" ] && continue
    for prone in "${PER_HOST_PRONE[@]}"; do
      case "$entry" in
        *"$prone"*)
          if ! _is_host_qualified "$entry"; then
            echo "FAIL: $cfg → vault.sync.include carries per-host-prone path '$entry' without a hostname qualifier."
            echo "      Per-host data collapses on cross-host pull-merge. Use a hostname-qualified path"
            echo "      (e.g. '<dir>/<hostname>.json') or drop it from the carrier set. See docs/workspace-per-host-paths.md."
            fail=1
          fi
          ;;
      esac
    done
  done <<<"$includes"
done < <(_config_files "$@")

if [ "$fail" -eq 0 ]; then
  echo "lint-vault-sync-paths: OK — no bare per-host paths in any vault.sync.include."
fi
exit "$fail"
