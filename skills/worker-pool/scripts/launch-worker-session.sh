#!/bin/bash
# skills/worker-pool/scripts/launch-worker-session.sh — a pool worker's own
# full launch: a real `claude` tmux session on this worker's delivery inbox.
# Owns everything worker-specific; core/src/ owns none of it (the placement
# rule: worker-specific behavior lives in skills/worker-pool, never as
# "if worker" branching in core/src/). Sources session-launch.sh from src/ —
# the skill depends on core, never the reverse — for the mechanics a worker
# and the canonical core both need (Python resolution, CLAUDE_CONFIG_DIR +
# onboarding seed, hooks JSON, obs telemetry, credential-proxy routing, the
# actual tmux+claude invocation). Everything else here (boot prompt, surface
# args, the worker's own env forwarding) is this script's own, and core's
# launcher (src/agent/claude/cli/start-cli.sh) carries none of it anymore.
#
# Always a FRESH tmux session: skills/worker-pool/scripts/spawn_worker.py's
# spawn() already calls session_probe() and refuses via SpawnRefused if the
# target session exists, so by the time this script runs the session is
# guaranteed new. No attach, no restart, no heal — those are core-only
# concepts (a worker is spawned once per incarnation, never re-attached
# through this script).
#
# Usage: bash skills/worker-pool/scripts/launch-worker-session.sh
# (invoked by spawn_worker.py's launcher_argv with a fully-populated env —
# see plan()/spawn() there for exactly which SUTANDO_* vars are set.)

set -e

# This script lives at skills/worker-pool/scripts/ — three levels under the
# repo root. Pure bash, no external dirname: same reasoning as the core
# launcher's own first line (#4577) — nothing about PATH can be assumed sane
# yet.
case "$0" in
  */*) _self_dir="${0%/*}" ;;
  *)   _self_dir="." ;;
esac
REPO="$(cd "$_self_dir/../../.." && pwd)"
unset _self_dir
cd "$REPO"
if [ "${SUTANDO_WORKER_RUNTIME:-claude}" = "codex" ]; then
  exec bash "$REPO/skills/worker-pool/scripts/launch-codex-worker-session.sh" "$@"
fi
# shellcheck source=../../../src/agent/claude/cli/session-launch.sh
. "$REPO/src/agent/claude/cli/session-launch.sh"

resolve_claude_py
# shellcheck source=../../../src/skill-manifest-config.sh
[ -r "$REPO/src/skill-manifest-config.sh" ] && . "$REPO/src/skill-manifest-config.sh"

TMUX_SOCKET="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"
SESSION="${SUTANDO_TMUX_SESSION:?launch-worker-session.sh needs SUTANDO_TMUX_SESSION (spawn_worker.py always sets it)}"
: "${SUTANDO_INSTANCE_ID:?launch-worker-session.sh needs SUTANDO_INSTANCE_ID (spawn_worker.py always sets it)}"
# A worker never gets the owner-facing surfaces (remote control, Chrome) —
# those are the canonical core's alone.
SURFACE_ARGS=()
# `/startup --worker` is the pool worker's own boot ceremony (orphan recovery
# and session crons are core-only; the worker gate is what it runs instead).
BOOT_PROMPT="/startup --worker"
SESSION_ARGS=()
if [ -n "${SUTANDO_CLAUDE_RESUME:-}" ]; then
  SESSION_ARGS=(--resume "$SUTANDO_CLAUDE_RESUME")
elif [ -n "${SUTANDO_CLAUDE_SESSION_ID:-}" ]; then
  SESSION_ARGS=(--session-id "$SUTANDO_CLAUDE_SESSION_ID")
fi

# A worker is not the canonical core: this marker is what makes a session
# claim the core's own bootstrap (SessionStart hook gating cron registration
# etc.), so a worker must carry it explicit-empty, not omit it — omitting an
# `-e` leaves the tmux server's stale global value in place, an explicit
# empty is the only real override.
export SUTANDO_CORE_RUNTIME=claude
ENV_ARGS=(-e SUTANDO_CORE_RUNTIME=claude -e SUTANDO_CORE_SESSION=)
[ -n "${SUTANDO_TMUX_SOCKET:-}" ] && ENV_ARGS+=(-e "SUTANDO_TMUX_SOCKET=$SUTANDO_TMUX_SOCKET")
ENV_ARGS+=(-e "SUTANDO_TMUX_SESSION=$SESSION")
ENV_ARGS+=(-e "SUTANDO_INSTANCE_ID=$SUTANDO_INSTANCE_ID")
[ -n "${SUTANDO_TASKS_DIR:-}" ] && ENV_ARGS+=(-e "SUTANDO_TASKS_DIR=$SUTANDO_TASKS_DIR")
# A worker's inbox is <ws>/deliveries/<id>, so the watcher cannot infer the
# workspace from it: unforwarded, its results/ and state/ land under deliveries/.
[ -n "${SUTANDO_WORKSPACE_DIR:-}" ] && ENV_ARGS+=(-e "SUTANDO_WORKSPACE_DIR=$SUTANDO_WORKSPACE_DIR")
# The other two halves of the same seam: what the inbox holds, and where
# answers go. Derived in-session they become deliveries/results, which no
# bridge drains.
[ -n "${SUTANDO_INBOX_KIND:-}" ] && ENV_ARGS+=(-e "SUTANDO_INBOX_KIND=$SUTANDO_INBOX_KIND")
[ -n "${SUTANDO_RESULTS_DIR:-}" ] && ENV_ARGS+=(-e "SUTANDO_RESULTS_DIR=$SUTANDO_RESULTS_DIR")
# Third half of that seam: without the resolver a worker reads the sentinel
# itself, so an unforwarded one is the zero-byte read, not a missing option.
[ -n "${SUTANDO_INBOX_RESOLVER:-}" ] && ENV_ARGS+=(-e "SUTANDO_INBOX_RESOLVER=$SUTANDO_INBOX_RESOLVER")
[ -n "${SUTANDO_INBOX_RESOLVER_TIMEOUT:-}" ] && ENV_ARGS+=(-e "SUTANDO_INBOX_RESOLVER_TIMEOUT=$SUTANDO_INBOX_RESOLVER_TIMEOUT")
# The worker gate `/startup --worker` runs, named by the spawner: this script
# is the worker's own, but the path itself is still spawn_worker.py's to name
# (it points at worker_bootstrap.py in this same skill) — forward, don't
# hardcode, so a future spawner can still override it.
[ -n "${SUTANDO_WORKER_BOOTSTRAP:-}" ] && ENV_ARGS+=(-e "SUTANDO_WORKER_BOOTSTRAP=$SUTANDO_WORKER_BOOTSTRAP")
# The done-flag writer, same seam: tmux hands a new session the SERVER's env,
# so an unforwarded writer leaves the hook complete but never reached.
[ -n "${SUTANDO_POOL_DELIVERY_SCRIPT:-}" ] && ENV_ARGS+=(-e "SUTANDO_POOL_DELIVERY_SCRIPT=$SUTANDO_POOL_DELIVERY_SCRIPT")
# This worker's own watcher command, forwarded absolute: the session's cwd is
# the spawner's --cwd, which need not be the repo, so a relative path would
# resolve against the wrong directory.
ENV_ARGS+=(-e "SUTANDO_WATCHER_CMD=$REPO/src/watch-tasks-stream.sh")
# The worker's own watcher beats state/watchers/<id>.alive, so the pool can
# observe it as a file rather than a process scan.
ENV_ARGS+=(-e "SUTANDO_WATCHER_BEAT=$REPO/skills/worker-pool/scripts/pool_beat.py")
# Canonical + executable, or EMPTY: a relative/`..` interpreter path resolves
# against the worker's cwd, and only an explicit -e overrides a stale
# server-global value.
WORKER_PY=""
if [ -n "$PY" ] && [ -x "$PY" ]; then
  _pyd="${PY%/*}"; [ "$_pyd" = "$PY" ] && _pyd="."
  _pyd="$(cd "$_pyd" 2>/dev/null && pwd -P)" && WORKER_PY="$_pyd/${PY##*/}"
  [ -n "$WORKER_PY" ] && [ -x "$WORKER_PY" ] || WORKER_PY=""
fi
ENV_ARGS+=(-e "SUTANDO_PY=$WORKER_PY")
# Forward the embedder-provided default workspace for the same reason as
# above (tmux takes the server env, not this shell's).
[ -n "${SUTANDO_DEFAULT_WORKSPACE:-}" ] && ENV_ARGS+=(-e "SUTANDO_DEFAULT_WORKSPACE=$SUTANDO_DEFAULT_WORKSPACE")
if [ "${SUTANDO_SELF_DEVELOPMENT_ENABLED+x}" = x ]; then
  ENV_ARGS+=(-e "SUTANDO_SELF_DEVELOPMENT_ENABLED=$SUTANDO_SELF_DEVELOPMENT_ENABLED")
fi
forward_skill_manifest_config
ENV_ARGS+=(${SKILL_MANIFEST_ENV_ARGS[@]+"${SKILL_MANIFEST_ENV_ARGS[@]}"})
resolve_claude_credential_proxy
if [ -n "${ANTHROPIC_BASE_URL:-}" ]; then
  ENV_ARGS+=(-e "ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL")
fi
# Test probe: dump the assembled worker env forwarding and exit — same shape
# as the core launcher's --print-core-env, so a regression suite can assert
# the allowlist/proxy-routing policy without touching tmux. No production
# caller passes this.
if [ "${1:-}" = "--print-env" ]; then
  printf '%s\n' ${ENV_ARGS[@]+"${ENV_ARGS[@]}"}
  exit 0
fi

# The watcher sees SUTANDO_INSTANCE_ID in every worker session and therefore
# requires the pool delivery writer. Fail before creating a session that could
# receive tasks but cannot safely acknowledge them.
if [ -z "${SUTANDO_POOL_DELIVERY_SCRIPT:-}" ] || \
   [ ! -f "$SUTANDO_POOL_DELIVERY_SCRIPT" ] || \
   [ ! -r "$SUTANDO_POOL_DELIVERY_SCRIPT" ]; then
  echo "launch-worker-session.sh needs SUTANDO_POOL_DELIVERY_SCRIPT to name a readable file" >&2
  exit 2
fi

# Same onboarding/hooks treatment a core launch gets: a worker is also
# headless (no TTY, --dangerously-skip-permissions) and can hang on the exact
# same unattended prompts (folder-trust, bypass-permissions, AskUserQuestion)
# if this is skipped.
install_claude_personal_hook
resolve_claude_cwd_args
resolve_claude_config_dir_and_seed
resolve_claude_settings_args
apply_claude_obs_metering
apply_claude_tmux_defaults

if ! launch_claude_session; then
  echo "  ⚠ worker session $SESSION did not come up within ~5s of launch — start FAILED." >&2
  exit 1
fi
echo "Started worker session $SESSION detached."
# The worker's inbox gets the same hosting-mode supervisor the core has; the
# remedy timer re-ensures it, so a supervisor that dies is not gone for good.
SUTANDO_PY="$WORKER_PY" bash "$REPO/skills/worker-pool/scripts/worker-watcher-supervisor.sh" \
  || echo "  ⚠ the worker's watcher supervisor did not start; the remedy timer retries within 5 min" >&2
