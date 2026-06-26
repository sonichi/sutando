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

  vault-sync-include)
    # PR-3: print sync.include paths one-per-line. Consumed by
    # sync-workspace.sh::_compose_gitignore_content to drive the carrier-set
    # whitelist. Schema in sutando_config.py::resolve_vault.
    python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import resolve_vault
for p in resolve_vault().get('sync', {}).get('include', []):
    print(p)
"
    ;;

  vault-sync-exclude)
    # PR-3: print sync.exclude paths one-per-line. Explicit denies emitted
    # AFTER the include whitelist (gitignore last-match wins), so user can
    # carve out subpaths from an otherwise-included directory.
    python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import resolve_vault
for p in resolve_vault().get('sync', {}).get('exclude', []):
    print(p)
"
    ;;

  migrate-stale-hosts)
    # Print migrate.stale_hosts (one per line) — per-clone machine-<host> dirs
    # the legacy import should DROP. Lives in sutando.config.local.json (gitignored,
    # per-clone), NOT .env: this is config, not a secret. Default empty.
    python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import load_config
for h in load_config().get('migrate', {}).get('stale_hosts', []):
    print(h)
"
    ;;

  migrate-skip-skills)
    # Print migrate.skip_skills (one per line) — per-clone host-only skill names
    # the legacy import should NOT salvage to shared (stale/superseded). Same
    # gitignored per-clone config home as migrate-stale-hosts. Default empty.
    python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import load_config
for s in load_config().get('migrate', {}).get('skip_skills', []):
    print(s)
"
    ;;

  claude-sutando-config-dir)
    # Print the absolute CLAUDE_CONFIG_DIR target used by the `claude-sutando`
    # shell alias. v0.9 resolution: `core_config_dirs[type=claude].value` →
    # legacy `claude_sutando_config_dir.subdir` (deprecation-warned) →
    # `<workspace>/.claude-sutando` baked default. `synced=true` entries are
    # validated to be under the workspace at load time.
    python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import resolve_claude_sutando_config_dir
print(resolve_claude_sutando_config_dir(), end='')
"
    ;;

  claude-home-path)
    # Resolve a path under Claude Code's per-user home, mirroring
    # `src/util_paths.py:claude_home_path()` for shell scripts. Use this
    # instead of the inline `${CLAUDE_CONFIG_DIR:-$HOME/.claude}` anti-pattern
    # so the deprecation-banner-on-fallback (added in #1534 for Python)
    # also fires from bash callers when CLAUDE_CONFIG_DIR is unset.
    #
    # Resolution order (matches src/util_paths.py:claude_home_path):
    #   1. $CLAUDE_CONFIG_DIR (per-runtime, workspace-scoped post-migrate)
    #   2. $CLAUDE_HOME (legacy alt-host override, kept for tests)
    #   3. ~/.claude/ (default — vanilla `claude` users; banner fires)
    #
    # Usage:
    #   bash scripts/sutando-config.sh claude-home-path                            # base only
    #   bash scripts/sutando-config.sh claude-home-path channels discord .env     # joined sub-path
    #   bash scripts/sutando-config.sh claude-home-path skills quota-tracker/scripts/read-quota.py
    #
    # Banner suppression: SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1
    if [ -n "${CLAUDE_CONFIG_DIR:-}" ]; then
      _chp_base="$CLAUDE_CONFIG_DIR"
    elif [ -n "${CLAUDE_HOME:-}" ]; then
      _chp_base="$CLAUDE_HOME"
    else
      _chp_base="$HOME/.claude"
      if [ "${SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER:-0}" != "1" ]; then
        echo "claude-home-path: \$CLAUDE_CONFIG_DIR not set — falling back to ~/.claude/. Set CLAUDE_CONFIG_DIR before starting Sutando services (the \`claude-sutando\` shell function and src/startup.sh set it; ad-hoc launches must too) so channels/skills/hooks/sessions resolve to the workspace-scoped per-runtime location post-#1454. Suppress with SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1." >&2
      fi
    fi
    # Expand leading ~ if present (covers e.g. CLAUDE_HOME=~/.claude-alt).
    _chp_base="${_chp_base/#\~/$HOME}"
    # Drop the subcommand from "$@" so remaining args are sub-path components.
    shift
    if [ "$#" -eq 0 ]; then
      printf '%s' "$_chp_base"
    else
      _chp_joined="$_chp_base"
      for _p in "$@"; do
        _chp_joined="$_chp_joined/$_p"
      done
      printf '%s' "$_chp_joined"
    fi
    ;;

  core-config-dir-env-name)
    # v0.9 — print the env var name of the matching core_config_dirs entry.
    # Optional second arg picks by id or type; defaults to first type=claude.
    # Example: `bash sutando-config.sh core-config-dir-env-name` → CLAUDE_CONFIG_DIR
    _selector="${2:-claude}"
    python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import find_core_config_dir
entry = find_core_config_dir(type_='$_selector') or find_core_config_dir(id_='$_selector')
if entry is None:
    sys.exit(0)
print(entry['env_name'], end='')
"
    ;;

  core-config-dir-value)
    # v0.9 — print the resolved value (absolute path) of the matching
    # core_config_dirs entry. Selector semantics identical to
    # core-config-dir-env-name.
    _selector="${2:-claude}"
    python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import find_core_config_dir
entry = find_core_config_dir(type_='$_selector') or find_core_config_dir(id_='$_selector')
if entry is None:
    sys.exit(0)
print(entry['value'], end='')
"
    ;;

  core-config-dirs)
    # v0.9 — print all resolved core_config_dirs entries as JSON (one object
    # per line — JSON Lines). For tooling that wants to enumerate without
    # parsing the full merged config.
    python3 -c "
import json, sys
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import resolve_core_config_dirs
for entry in resolve_core_config_dirs():
    print(json.dumps(entry))
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
    echo "usage: $0 {workspace|vault-enabled|vault-url|vault-sync-include|vault-sync-exclude|claude-sutando-config-dir|core-config-dir-env-name [type|id]|core-config-dir-value [type|id]|core-config-dirs|dump|subdirs|bootstrap}" >&2
    exit 2
    ;;
esac
