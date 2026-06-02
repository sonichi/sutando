#!/usr/bin/env bash
# Bash wrapper around src/sutando_config.py.
#
# Shell scripts can call this instead of inlining `${SUTANDO_WORKSPACE:-...}`
# defaults — keeping the resolution contract in one place (the Python loader)
# and avoiding the split-brain bug class where bash + Python compute different
# workspace paths from the same env.
#
# Usage:
#   bash scripts/sutando-config.sh workspace     # print resolved workspace path
#   bash scripts/sutando-config.sh vault-enabled # print "true" or "false"
#   bash scripts/sutando-config.sh vault-url     # print vault remote_url (may be empty)
#   bash scripts/sutando-config.sh dump          # print full merged config as JSON
#   bash scripts/sutando-config.sh subdirs       # print canonical workspace subdir list (one per line)
#   bash scripts/sutando-config.sh bootstrap     # mkdir -p the canonical subdirs in the resolved workspace
#
# `bootstrap` is the idempotent setup step for the in-repo workspace introduced
# in M0 (PR #1395). startup.sh runs this transitively via init.sh --auto, but
# any context that doesn't go through startup.sh (e.g. a workspace path change
# without service restart, a fresh clone where the user pokes at workspace/
# directly) can call this to ensure the canonical layout exists.
#
# Stdout is the value (no trailing newline for scalar getters); stderr
# carries any warnings from the loader (legacy env, .env drift). Returns
# non-zero only on malformed config.
#
# Migration target — replace patterns like:
#   WORKSPACE="${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}"
# with:
#   WORKSPACE="$(bash scripts/sutando-config.sh workspace)"

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cmd="${1:-workspace}"

case "$cmd" in
  workspace)
    # `python3 -c` instead of `-m` so we don't pollute argv[0] with a module
    # path that confuses the loader's exe-anchored repo discovery.
    python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import resolve_workspace
print(resolve_workspace(), end='')
"
    ;;

  vault-enabled)
    python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import resolve_vault
print('true' if resolve_vault().get('enabled') else 'false', end='')
"
    ;;

  vault-url)
    python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import resolve_vault
print(resolve_vault().get('remote_url', ''), end='')
"
    ;;

  claude-sutando-config-dir)
    # M2 — print the absolute CLAUDE_CONFIG_DIR target used by the
    # `claude-sutando` shell alias. Always a sub-folder of resolve_workspace()
    # (the loader enforces; absolute paths and `..` escapes rejected).
    python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import resolve_claude_sutando_config_dir
print(resolve_claude_sutando_config_dir(), end='')
"
    ;;

  dump)
    python3 -m src.sutando_config
    ;;

  subdirs)
    # Canonical workspace subdir list. Single source of truth — keep in sync
    # with src/init.sh tier1's create_dir_if_missing calls AND with
    # docs/workspace-config.md's layout section. If you add a subdir here,
    # also document it (and consider whether init.sh / sutando-migrate.sh
    # need to mention it).
    printf 'state\ntasks\nresults\nresults/archive\nresults/calls\nnotes\nlogs\ndata\nconfig\ntelegram-inbox\n'
    ;;

  bootstrap)
    # Resolve workspace, then mkdir -p the canonical subdirs. Idempotent.
    # M1 (post-M0): ensures the in-repo workspace has the expected layout
    # for any path resolved by the loader, regardless of whether startup.sh
    # / init.sh have run since the path was set.
    ws="$(bash "$0" workspace)"
    if [ -z "$ws" ]; then echo "bootstrap: workspace path empty — config error" >&2; exit 1; fi
    bash "$0" subdirs | while IFS= read -r d; do
      mkdir -p "$ws/$d"
    done
    echo "workspace bootstrapped: $ws" >&2
    ;;

  *)
    echo "usage: $0 {workspace|vault-enabled|vault-url|dump|subdirs|bootstrap}" >&2
    exit 2
    ;;
esac
