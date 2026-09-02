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

# Resolve the Python interpreter. On a fresh Mac there is NO system python3 — the
# bare `python3` name resolves to Apple's Xcode-CLT stub, which prints an "install
# developer tools" notice and returns NOTHING, so callers that don't export
# SUTANDO_PY (e.g. the desktop's Tauri sign-in, which shells this to resolve
# CLAUDE_CONFIG_DIR) got an empty result and failed with "could not resolve the
# bundled CLAUDE_CONFIG_DIR". Prefer, in order: an explicit SUTANDO_PY (set by
# launch-sutando.sh), the bundle-vendored relocatable python next to the engine
# copy (`<engine>/runtime/python`, i.e. REPO_ROOT/../runtime), then system python3.
# Single-sourced in scripts/python-binary.sh so this cascade cannot drift from
# start-cli.sh / startup.sh. The `else PY="python3"` this replaced still shelled
# the CLT stub when neither earlier tier existed, which is the dialog itself.
. "$REPO_ROOT/scripts/python-binary.sh"
PY="$(resolve_python "$REPO_ROOT")"

# Demand Python LAZILY, at the point of use. Requiring it up front was wrong:
# 8 of this script's subcommands are pure shell (app-node-dir, node-bin,
# tsx-bin, claude-home-path, subdirs, bootstrap, tmux-socket, run-dir)
# and answering them needs no interpreter — yet an eager
# `require_python … || exit 1` failed them too. src/startup.sh:57 asks for
# `app-node-dir` under `set -e`, so on a Darwin/no-CLT host that eager demand
# terminated startup at the very first config lookup (CR #2599, @qingyun-wu).
#
# Routing every interpreter call through this wrapper keeps the demand honest
# without a hand-maintained list of which subcommands need Python: a branch
# that calls `py` requires it, a branch that does not, does not. A list would
# drift from the bodies the moment a subcommand gained or dropped a `-c`.
# Failing once, with a fix, still beats letting each call degrade into the
# shell's opaque `: command not found` (CR #2599, @john-the-dev).
py() {
  if [ -z "$PY" ]; then
    PY="$(require_python "$REPO_ROOT" "resolve Sutando configuration")" || exit 1
  fi
  "$PY" "$@"
}

cmd="${1:-workspace}"

case "$cmd" in
  workspace)
    # `python3 -c` instead of `-m` so we don't pollute argv[0] with a module
    # path that confuses the loader's exe-anchored repo discovery.
    py -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import resolve_workspace
print(resolve_workspace(), end='')
"
    ;;
  voice-pidfile)
    # Single resolver for the voice-agent pid metadata file (#2722). Canonical
    # lives under state/locks/; the root path is a ~30-day reader fallback so an
    # agent started under pre-move code stays stoppable (docs/migration-transition-window.md).
    # $2 (optional): an already-resolved workspace root — callers that hold one
    # inject it so this cannot disagree with the tree they operate on.
    if [ -n "${2:-}" ]; then _ws="$2"; else _ws="$(bash "$0" workspace)" || exit 1; fi
    _canon="$_ws/state/locks/voice-agent.pid"
    _legacy="$_ws/.voice-agent.pid"
    if [ ! -f "$_canon" ] && [ -f "$_legacy" ]; then
      echo "sutando-config: voice-pidfile using legacy $_legacy (pre-move agent; transition window per #2722)" >&2
      printf '%s' "$_legacy"
    else
      printf '%s' "$_canon"
    fi
    ;;

  vault-enabled)
    py -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import resolve_vault
print('true' if resolve_vault().get('enabled') else 'false', end='')
"
    ;;

  vault-url)
    py -c "
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
    py -c "
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
    py -c "
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
    py -c "
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
    py -c "
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
    py -c "
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
    py -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import find_core_config_dir
entry = find_core_config_dir(type_='$_selector') or find_core_config_dir(id_='$_selector')
if entry is None:
    sys.exit(0)
print(entry['env_name'], end='')
"
    ;;

  core-runtime)
    py -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import resolve_core_runtime
print(resolve_core_runtime(), end='')
"
    ;;

  core-effort)
    py -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import resolve_core_effort
print(resolve_core_effort(), end='')
"
    ;;

  core-config-dir-value)
    # v0.9 — print the resolved value (absolute path) of the matching
    # core_config_dirs entry. Selector semantics identical to
    # core-config-dir-env-name.
    _selector="${2:-claude}"
    py -c "
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
    py -c "
import json, sys
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import resolve_core_config_dirs
for entry in resolve_core_config_dirs():
    print(json.dumps(entry))
"
    ;;

  stand)
    # This instance's `Stand:` commit-trailer value, from the per-clone config
    # key `stand` (sutando.config.local.json). Empty when unset.
    #
    # Both fleet instances commit under the owner's GH-mapped email (CLA), so no
    # git field distinguishes them; the trailer is the only discriminator, and
    # consumers need it WITHOUT an environment (the scheduled cron exports
    # nothing — john-the-dev, sutando#2484). Deliberately has NO fallback guess:
    # an unset key must read as "unknown" so callers decline rather than
    # attribute a foreign identity. Never infer it from the host name — that is
    # installation-specific policy and would assign this owner's Stand values on
    # someone else's machine.
    py -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import load_config
print(str(load_config().get('stand', '') or '').strip(), end='')
"
    ;;

  host-label)
    # Per-host directory label for hosts/<host>/ paths, mirroring
    # src/util_paths.py:_host_label() for shell callers. Use this instead of the
    # inline `hostname | sed 's/\..*//'` anti-pattern: that resolves the
    # DHCP-assigned hostname, which can drift (e.g. a Comcast lease →
    # Chis-MBP) and split per-host paths from the stable Bonjour LocalHostName
    # (Chis-MacBook-Pro), spawning a second hosts/<label>/ dir. Precedence
    # (single source of truth lives in Python; #1745):
    #   1. $SUTANDO_HOST_LABEL (or legacy $SUTANDO_HOST_OVERRIDE)
    #   2. macOS `scutil --get LocalHostName` (stable Bonjour name)
    #   3. short `hostname`
    py -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from src.util_paths import _host_label
print(_host_label(), end='')
"
    ;;

  app-node-dir)
    # The bundled app runtime's node bin dir (Sutando.app ships a private node
    # for hosts with no homebrew/nvm/volta). CONFIG-RESOLVED, not hardcoded:
    # override via $SUTANDO_APP_NODE_DIR; default = the app-support engine path.
    printf '%s' "${SUTANDO_APP_NODE_DIR:-$HOME/Library/Application Support/space.ag2.app/engine/runtime/node/bin}"
    ;;

  python-bin)
    # SINGLE SOURCE OF TRUTH for the Python interpreter, mirroring node-bin.
    # $PY is resolved at the top of this file with the documented precedence:
    #   1. $SUTANDO_PY               (exported by launch-sutando.sh)
    #   2. <engine>/runtime/python   (the bundle-vendored relocatable python)
    #   3. system `python3`          (may be Apple's CLT stub — see below)
    #
    # Exposed because callers were re-deriving it and getting it WRONG in a way
    # that fails silently. src/startup-runtime.sh's managed-credential gate
    # probed `command -v python3` then Homebrew, skipping tiers 1 and 2, so a
    # host with a broken `python3` first on PATH and a perfectly good $SUTANDO_PY
    # concluded "no usable python3" and left voice disabled while a valid managed
    # credential sat on disk (sonichi/sutando#2197 review, 2026-08-02).
    #
    # FAIL-CLOSED with a SMOKE TEST (voice-reliability plan amendment T1): this
    # prints an ABSOLUTE interpreter path only after executing it and importing
    # `fcntl`, else exits non-zero with a fix. Callers building on the voice
    # lock helper (scripts/voice-lock.py) must never receive an unverified
    # interpreter — the previous contract ("prints the interpreter to TRY
    # FIRST") pushed the execution probe to every caller, and the ones that
    # skipped it shelled the Xcode-CLT stub. resolve_python() never returns
    # the stub itself (it checks `xcode-select -p` before trusting the system
    # location), so executing $PY here cannot raise the CLT install dialog.
    if [ -z "$PY" ]; then
      # Reuse require_python's actionable multi-line message, then fail.
      require_python "$REPO_ROOT" "resolve python-bin" >/dev/null || exit 1
      exit 1
    fi
    # Normalize to an absolute, physically-resolved path (the bundled tier is
    # "<repo>/../runtime/python/bin/python3", which contains a `..`).
    _pb="$PY"
    case "$_pb" in
      */*) : ;;
      *) _pb="$(command -v "$_pb" 2>/dev/null || true)" ;;
    esac
    _pb_dir="$(cd "$(dirname "$_pb")" 2>/dev/null && pwd -P || true)"
    if [ -z "$_pb_dir" ]; then
      echo "sutando: python-bin: resolved interpreter '$PY' has no resolvable directory" >&2
      exit 1
    fi
    _pb="$_pb_dir/$(basename "$_pb")"
    if ! "$_pb" -c 'import fcntl' >/dev/null 2>&1; then
      {
        echo "sutando: python-bin: '$_pb' failed the smoke test (execute + import fcntl)."
        echo "  The interpreter exists but does not run or lacks the stdlib."
        echo "  Fix: install python3 (brew install python), set SUTANDO_PY to a"
        echo "  working interpreter, or run xcode-select --install."
      } >&2
      exit 1
    fi
    printf '%s' "$_pb"
    ;;
  node-bin)
    # SINGLE SOURCE OF TRUTH for the Node executable (G1.5 node-bundle,
    # owner-adopted design + owner review 2026-07-19). Precedence:
    #   1. $SUTANDO_NODE — the EXACT executable, exported by the desktop app.
    #      AUTHORITATIVE ONCE SET: if it is set but not executable, that is a
    #      desktop packaging error — FAIL CLOSED (stderr + exit 1) instead of
    #      silently rescuing via whatever node the host happens to have, which
    #      would mask the packaging bug and void the deterministic-runtime
    #      guarantee (owner review P1-1).
    #   2. app-node-dir/node — the bundled runtime at its canonical home
    #      <engine-root>/runtime/node/bin (covers launchd jobs whose plist
    #      doesn't export SUTANDO_NODE).
    #   3. first `node` on PATH — dev/OSS hosts, unchanged behavior.
    # Prints the resolved path; empty output + exit 0 when nothing resolves
    # (caller decides), exit 1 ONLY for the invalid-explicit case.
    if [ -n "${SUTANDO_NODE:-}" ]; then
      if [ -x "${SUTANDO_NODE}" ]; then
        printf '%s' "$SUTANDO_NODE"
      else
        echo "sutando-config: SUTANDO_NODE is set but not an executable: $SUTANDO_NODE (desktop packaging error — refusing PATH fallback)" >&2
        exit 1
      fi
    else
      _app_node="${SUTANDO_APP_NODE_DIR:-$HOME/Library/Application Support/space.ag2.app/engine/runtime/node/bin}/node"
      if [ -x "$_app_node" ]; then
        printf '%s' "$_app_node"
      else
        command -v node 2>/dev/null || true
      fi
    fi
    ;;

  tsx-bin)
    # SINGLE SOURCE OF TRUTH for tsx resolution — the launchd wrapper and
    # src/startup.sh both call this instead of each duplicating the candidate
    # list. Prefers the repo-pinned node_modules/.bin/tsx (the version the repo
    # pins, and the ONLY tsx on a host with no homebrew/nvm/volta node at all),
    # then the usual global locations. Prints the resolved path, or nothing —
    # the caller falls back to `npx tsx` on empty output.
    # `|| true` inside the substitution: on a host with no ~/.nvm the `ls`
    # fails, and under `set -euo pipefail` that pipeline status would kill the
    # whole script BEFORE the candidate loop — startup.sh then dies silently at
    # its `_TSX_BIN=$(...)` line even though node_modules/.bin/tsx exists.
    _nvm_tsx="$HOME/.nvm/versions/node/$( (ls "$HOME/.nvm/versions/node/" 2>/dev/null || true) | sort -V | tail -1)/bin/tsx"
    for _p in \
      "$REPO_ROOT/node_modules/.bin/tsx" \
      /opt/homebrew/bin/tsx \
      /usr/local/bin/tsx \
      "$_nvm_tsx" \
      "$HOME/.volta/bin/tsx"; do
      [ -x "$_p" ] && { printf '%s' "$_p"; break; }
    done
    # The contract is "print the path, or nothing" — exit 0 either way. Without
    # this, a no-match run exits with the last [ -x ] test's failure status and
    # set -e callers die instead of falling back to `npx tsx`.
    true
    ;;

  dump)
    py -m src.sutando_config
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

  tmux-socket)
    # Print the tmux socket the sutando-core session runs on. Mirrors the
    # resolution in src/agent/claude/cli/start-cli.sh (`${SUTANDO_TMUX_SOCKET:-
    # /tmp/sutando-tmux.sock}`) so a caller resolves the same socket the core
    # actually launched on. Contract sibling of `workspace` — resolve via helper,
    # never hardcode /tmp/sutando-tmux.sock. NOTE: for a FOREIGN caller (e.g. the
    # desktop app, whose own env may point SUTANDO_TMUX_SOCKET at a private
    # bundled socket) prefer the env-independent `runtime` subcommand below,
    # which pins to the default OSS socket. This getter honors the ambient env
    # and is meant for same-runtime callers.
    printf '%s' "${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"
    ;;

  run-dir)
    # The runtime run-dir — where the per-runtime sockets/pids live. MIRRORS
    # #2325's ag2space runtime-api rundir.py EXACTLY, so the shell (this getter,
    # start-cli, health-check) and the daemon/CLI can never disagree on the path.
    # Resolution order (must match rundir.py.run_dir()):
    #   1. SUTANDO_RUN_DIR                                   explicit override
    #   2. darwin: ~/Library/Application Support/space.ag2.app/run   (Desktop root)
    #   3. $XDG_RUNTIME_DIR/sutando                          Linux/systemd per-user
    #   4. ~/.sutando/run                                    portable fallback
    if [ -n "${SUTANDO_RUN_DIR:-}" ]; then
      printf '%s' "$SUTANDO_RUN_DIR"
    elif [ "$(uname -s)" = "Darwin" ]; then
      printf '%s' "$HOME/Library/Application Support/space.ag2.app/run"
    elif [ -n "${XDG_RUNTIME_DIR:-}" ]; then
      printf '%s' "$XDG_RUNTIME_DIR/sutando"
    else
      printf '%s' "$HOME/.sutando/run"
    fi
    ;;

  runtime-socket)
    # The runtime-API daemon's Unix socket. The default is (actor, instance)
    # scoped, so this EXECS rundir.py rather than mirroring it: a shell copy of
    # the chain published the pre-actor flat socket while the daemon listened on
    # the scoped one, and no client could reach a fresh daemon (review P1).
    # No fallback on failure: a synthesized flat endpoint is a plausible WRONG
    # answer, and rc=0 beside it is worse than no answer at all.
    if [ -n "${SUTANDO_RUNTIME_SOCKET:-}" ]; then
      printf '%s' "$SUTANDO_RUNTIME_SOCKET"
    else
      py "$REPO_ROOT/src/runtime-api/rundir.py" --socket
    fi
    ;;

  runtime)
    # Emit this install's AgentRuntime descriptor as one JSON object — the single
    # OSS-side contract the desktop app reads to resolve which runtime to attach
    # its Terminal to, route task-drops/gateway to, and decide port-vs-new
    # (issue ag2-space/ag2space-cinny-desktop#98). Keyed on the repo; everything
    # else derives:
    #   {alive, repo, workspace, brain, socket, session, health, authenticated}
    # FOREIGN-CALLER-SAFE *and* honors custom sockets: the tmux socket is read
    # from a RUNTIME-AUTHORED source (the core's .alive heartbeat, which records
    # the socket the core actually launched on — see core_heartbeat.py), NOT the
    # caller's ambient SUTANDO_TMUX_SOCKET. So a foreign caller (the desktop app,
    # whose env points at its bundled socket) still gets THIS OSS core's real
    # socket, AND an install running on a custom socket is reported correctly
    # (start-cli.sh honors SUTANDO_TMUX_SOCKET). No fresh heartbeat -> default OSS
    # socket. Reuses src/runtime-health.py for alive/health/authenticated (single
    # liveness source of truth) and the canonical resolve_claude_sutando_config_dir()
    # for brain (honors core_config_dirs[type=claude] customization).
    py -c "
import sys, os, json, time, subprocess
sys.path.insert(0, '$REPO_ROOT')
from src.sutando_config import resolve_workspace, resolve_claude_sutando_config_dir
repo = '$REPO_ROOT'
ws = str(resolve_workspace())
brain = str(resolve_claude_sutando_config_dir())
# The socket this core runs on, from its own heartbeat: runtime-authored, so it
# is correct for custom sockets AND immune to the caller's ambient env. Only a
# FRESH heartbeat (<90s, matching core-heartbeat's liveness window) is trusted;
# stale/absent -> default OSS socket.
def _host_label():
    # Must match the WRITER's resolution exactly (core_heartbeat.py names the
    # .alive file via util_paths._host_label, honoring \$SUTANDO_HOST_LABEL /
    # \$SUTANDO_HOST_OVERRIDE). REPO_ROOT (not REPO_ROOT/src) is on sys.path here,
    # so the import is 'src.util_paths' — matching 'from src.sutando_config'
    # above; a bare 'from util_paths' silently fails to sys.path and would fall
    # back to gethostname(), losing the label (review catch on c91a68c).
    try:
        from src.util_paths import _host_label as hl
        return hl()
    except Exception:
        import socket as _s
        return _s.gethostname().split('.')[0]
probe_socket = '/tmp/sutando-tmux.sock'
try:
    with open(os.path.join(ws, 'state', 'cores', _host_label() + '.alive')) as f:
        rec = json.load(f)
    if time.time() - float(rec.get('last_beat_at', 0)) < 90 and rec.get('socket'):
        probe_socket = rec['socket']
except Exception:
    pass
# voice_ws: the WS the running voice-agent actually bound. Runtime-authored —
# voice-agent.ts writes state/voice-agent.json at listen with its real PORT — so
# a non-default PORT is reported correctly instead of a hardcoded default (same
# 'running process is the authority' principle as the socket above). Liveness is
# validated by the recorded pid, NOT a time window: the file is written once at
# startup (not refreshed like the heartbeat), so a window would wrongly expire a
# long-running agent. Absent / dead-pid / unreadable -> default OSS endpoint.
probe_voice_ws = 'ws://127.0.0.1:9900'
try:
    with open(os.path.join(ws, 'state', 'voice-agent.json')) as f:
        vrec = json.load(f)
    vpid = int(vrec.get('pid', 0) or 0)
    alive = False
    if vpid > 0:
        try:
            os.kill(vpid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True  # exists but not ours — still a live process
        except Exception:
            alive = False
    if alive and vrec.get('voice_ws'):
        probe_voice_ws = vrec['voice_ws']
except Exception:
    pass
# vision_control: the HTTP control endpoint the running vision-control server
# actually bound (:7847 default, or VISION_CONTROL_PORT). Runtime-authored the
# same way as voice_ws — vision-tools.ts writes state/vision-control.json at
# listen with its real port, pid-validated (written once at startup, so liveness
# is the pid not a time window). Consumed by the desktop 'Watch' toggle (v0.3.0
# Slice-2) to drive /vision/start|stop|state. Absent / dead-pid / unreadable ->
# default OSS endpoint.
probe_vision_control = 'http://127.0.0.1:7847'
try:
    with open(os.path.join(ws, 'state', 'vision-control.json')) as f:
        crec = json.load(f)
    cpid = int(crec.get('pid', 0) or 0)
    calive = False
    if cpid > 0:
        try:
            os.kill(cpid, 0)
            calive = True
        except ProcessLookupError:
            calive = False
        except PermissionError:
            calive = True  # exists but not ours — still a live process
        except Exception:
            calive = False
    # Only trust a loopback endpoint — the control server binds 127.0.0.1, so a
    # non-loopback scheme/host in the state file is stale or crafted; fall back to
    # the default rather than hand the desktop client a URL it should never call.
    _cv = crec.get('vision_control')
    if calive and isinstance(_cv, str) and _cv.startswith('http://127.0.0.1:'):
        probe_vision_control = _cv
except Exception:
    pass
# call_tiers: the DIRECT call endpoints this core can advertise right now, the
# runtime-authored half of the availability-driven call-tier menu (Track 9). The
# emitter (src/emit-call-tiers.ts, from reachability-endpoints.ts) writes
# state/call-tiers.json at startup; the client renders the tier picker from this
# instead of the old static 'force tier' stub (greyed Direct even when live,
# offered Local on a remote core). Only direct tiers are advertised — 'local' is
# client-relative and cloud/relay are always-available + composed client-side.
# The advertisement is a HINT: the client verifies reachability (first-reachable-
# wins), so a stale entry degrades gracefully. Absent/malformed -> [] (client
# falls back to cloud). A freshness window / re-emit-on-network-change is a
# documented follow-up.
probe_call_tiers = []
try:
    with open(os.path.join(ws, 'state', 'call-tiers.json')) as f:
        crec = json.load(f)
    ct = crec.get('call_tiers')
    if isinstance(ct, list):
        probe_call_tiers = ct
except Exception:
    pass
env = dict(os.environ, SUTANDO_TMUX_SOCKET=probe_socket)
h = {}
try:
    out = subprocess.run([sys.executable, os.path.join(repo, 'src', 'runtime-health.py')],
                         capture_output=True, text=True, timeout=15, env=env, cwd=repo)
    if out.stdout.strip():
        h = json.loads(out.stdout)
except Exception:
    pass
# code: the source-version identity of the runtime — 'which Sutando is this,
# behaviorally?' Prompts + skills + scripts in the repo determine behavior, so
# the desktop app (and fleet tooling) wants this to reason about version-compat
# and to spot a locally-modified core. Git is authoritative for checkouts;
# packaged engines fall back to the build-authored ENGINE_MANIFEST beside the
# copied repo. tree_sha is the content hash of TRACKED files (version-
# independent); packaged consumers use tree_digest instead. dirty flags
# uncommitted edits. A stronger working-tree
# 'source_sha' (hashes uncommitted + untracked behavior files) is a documented
# follow-up alongside the identity block.
# Resolve ONCE, before any spawn. Two gates, both required:
#   1. No .git marker -> not a checkout, so every field below would be None
#      anyway. The packaged app ships the engine as an rsync copy WITHOUT .git,
#      so on that install these five calls did no useful work at all.
#      os.path.exists, NOT isdir: a linked worktree or submodule has .git as a
#      FILE containing a gitdir: pointer, and isdir() blanked the identity
#      fields on those perfectly valid checkouts (@john-the-dev, #2478).
#   2. No runnable git -> on a Mac without developer tools /usr/bin/git is the
#      Xcode-CLT stub, and SPAWNING it raises the modal install dialog before it
#      can fail. Catching a non-zero exit is too late; the prompt already fired.
# The desktop app polls this descriptor (core_terminal.rs), so the old code
# leaked that dialog on every poll of a packaged, toolchain-free install.
_git_bin = None
if os.path.exists(os.path.join(repo, '.git')):
    try:
        sys.path.insert(0, os.path.join(repo, 'src'))
        from git_binary import resolve_git
        _git_bin = resolve_git()
    except Exception:
        _git_bin = None

def _git(*a):
    if _git_bin is None:
        return None
    try:
        r = subprocess.run([_git_bin, '-C', repo, *a], capture_output=True, text=True, timeout=5)
        return (r.stdout.strip() or None) if r.returncode == 0 else None
    except Exception:
        return None

def _engine_manifest():
    # Packaging authors the parent manifest; the in-repo path is compatibility-only.
    for path in (os.path.join(os.path.dirname(repo), 'ENGINE_MANIFEST.json'),
                 os.path.join(repo, 'ENGINE_MANIFEST.json')):
        try:
            with open(path) as f:
                value = json.load(f)
            if isinstance(value, dict):
                return value
        except (OSError, ValueError):
            pass
    return {}

_git_revision = _git('rev-parse', 'HEAD')
_manifest = {} if _git_revision else _engine_manifest()
_manifest_revision = _manifest.get('sha')
if not (isinstance(_manifest_revision, str) and len(_manifest_revision) >= 8
        and all(c in '0123456789abcdefABCDEF' for c in _manifest_revision)):
    _manifest_revision = None
_revision = _git_revision or _manifest_revision
_git_describe = _git('describe', '--tags', '--always', '--dirty')
_manifest_branch = _manifest.get('branch')
if not isinstance(_manifest_branch, str):
    _manifest_branch = None
_manifest_dirty = _manifest.get('dirty')
if not isinstance(_manifest_dirty, bool):
    _manifest_dirty = False
code = {
    'commit': _revision[:7] if _revision else None,
    'revision': _revision,
    'branch': _git('rev-parse', '--abbrev-ref', 'HEAD') or _manifest_branch,
    'describe': _git_describe or (_revision[:7] if _revision else None),
    'tree_sha': _git('rev-parse', 'HEAD^{tree}'),
    'dirty': bool(_git('status', '--porcelain')) if _git_revision else _manifest_dirty,
    'source': 'git' if _git_revision else ('engine-manifest' if _manifest_revision else None),
    'built_at': _manifest.get('built_at') if isinstance(_manifest.get('built_at'), str) else None,
    'tree_digest': (_manifest.get('post_build_tree_digest')
                    if isinstance(_manifest.get('post_build_tree_digest'), str) else None),
}
# run-dir + runtime-api socket: IMPORT rundir.py instead of re-deriving it. The
# socket default is (actor, instance) scoped, so a hand-mirrored copy of that
# chain published a socket no daemon listens on (review P1). NOTE: no
# shell-active chars in this comment block -- the python runs inside a bash
# double-quoted -c string, so a dollar-var or backtick here would be
# bash-expanded and break the program.
sys.path.insert(0, os.path.join(repo, 'src', 'runtime-api'))
import rundir as _rd
_rundir = str(_rd.run_dir())
_runtime_socket = _rd.socket_path()
# runtimeRoot = parent of run/ when run-dir is <root>/run (darwin App-Support,
# portable dot-sutando); for the XDG case the run-dir (XDG_RUNTIME_DIR/sutando) IS
# the app dir (its parent is the shared XDG base), so use the run-dir itself.
_runtime_root = os.path.dirname(_rundir) if os.path.basename(_rundir) == 'run' else _rundir

print(json.dumps({
    'alive': bool(h.get('core_running', False)),
    'repo': repo,
    'code': code,
    'workspace': ws,
    'brain': brain,
    'socket': h.get('tmux_socket') or probe_socket,
    'session': h.get('session', 'sutando-core'),
    # voice_ws: the WebSocket the runtime's voice-agent listens on — the endpoint
    # the desktop 'Live' page's browser VoiceTransport.connect(url) opens
    # (ag2-space/ag2space-cinny-desktop v0.3.0). Sourced from runtime-authored
    # state (probe_voice_ws above): voice-agent.ts records its actual bound PORT,
    # so a non-default-PORT install is reported correctly, not a hardcoded default.
    'voice_ws': probe_voice_ws,
    # vision_control: the HTTP control endpoint the runtime's vision-control server
    # listens on — where the desktop 'Watch' toggle POSTs /vision/start|stop and
    # polls /vision/state (ag2-space/ag2space-cinny-desktop v0.3.0 Slice-2).
    # Sourced from runtime-authored state (probe_vision_control above), so a
    # VISION_CONTROL_PORT override is reported correctly, not a hardcoded default.
    'vision_control': probe_vision_control,
    # call_tiers: the direct call endpoints this core advertises (Track 9). The
    # desktop 'Start Call' picker renders the tier menu from this — showing only
    # reachable rows, un-greying Direct(Tailscale)/Direct(LAN) when their url is
    # advertised. Runtime-authored via probe_call_tiers above (emit-call-tiers.ts).
    'call_tiers': probe_call_tiers,
    'health': h.get('health', 'unknown'),
    'authenticated': h.get('authenticated'),
    # ── additive runtime-standardization fields (standard.md task A P1; air
    # confirmed ADDITIVE 2026-07-26). Old readers ignore unknown keys, and the
    # existing socket/session fields stay for desktop back-compat. The components
    # field (the 4-window topology) is intentionally NOT emitted yet — it lands in P2
    # (NOTE: no backticks/$-vars anywhere in this python -c comment block — it runs
    # inside a bash double-quoted -c string, so a backtick would command-substitute
    # and a $-var would expand; both break the program / execute PATH binaries.)
    # with the session rename (sutando-core -> sutando + core/gateway/runtime-api/
    # monitor windows); emitting it now would misrepresent the single current
    # session.
    'schemaVersion': 1,
    'runtimeId': os.environ.get('SUTANDO_RUNTIME_ID', 'primary'),
    'runtimeRoot': _runtime_root,
    'runtimeSocket': _runtime_socket,
    'backend': {
        'type': 'tmux',
        'socket': h.get('tmux_socket') or probe_socket,
        'session': h.get('session', 'sutando-core'),
    },
}))
"
    ;;

  *)
    echo "usage: $0 {workspace|core-runtime|core-effort|vault-enabled|vault-url|vault-sync-include|vault-sync-exclude|claude-sutando-config-dir|claude-home-path <subpath>|core-config-dir-env-name [type|id]|core-config-dir-value [type|id]|core-config-dirs|host-label|tmux-socket|run-dir|runtime-socket|runtime|dump|subdirs|bootstrap}" >&2
    exit 2
    ;;
esac
