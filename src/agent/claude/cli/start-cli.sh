#!/bin/bash
# src/agent/claude/cli/start-cli.sh — canonical launch script for the sutando-core
# tmux session. Single source of truth for the "how to start Claude Code" command,
# so startup.sh + Sutando.app's Restart Core menu can both invoke it without
# duplicating the launch arguments.
#
# Usage:
#   bash src/agent/claude/cli/start-cli.sh           # start (or attach if running)
#   bash src/agent/claude/cli/start-cli.sh --restart # kill existing session then start fresh
#   ... --restart --visible / --visible               # additionally open a Terminal window attached (macOS)
#
# Per Chi's prompt 2026-05-05 ("shall we add core CLI-related commands in
# sutando app"): extracting the launch command from startup.sh's inline tmux
# block lets the menu-bar app's Restart Core action invoke the same canonical
# entry without re-implementing the tmux flags.

set -e

# This script lives at src/agent/claude/cli/ — four levels under the repo root.
REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
cd "$REPO"
# Shared with the codex launcher: one owner for the in-session restart policy.
. "$REPO/src/agent/restart-guard.sh"

# Resolve the Python interpreter (same policy as scripts/sutando-config.sh). On a
# fresh Mac there is NO system python3 — bare `python3` resolves to Apple's
# Xcode-CLT stub, which returns nothing, so the onboarding seed below (which runs
# an inline python3 to write hasCompletedClaudeInChromeOnboarding) would silently
# no-op and the detached core hangs at the Chrome prompt — the exact bug the seed
# exists to prevent. Prefer SUTANDO_PY (set by launch-sutando.sh), else the
# bundle-vendored relocatable python (`<engine>/runtime/python`, i.e. REPO/../runtime),
# else system python3.
# Single-sourced in scripts/python-binary.sh (see there for why the bare-name
# fallback this replaced was the CLT-dialog trigger).
# Source OPTIONALLY. tests/start-cli-claude-config-dir.test.sh pins a contract
# older than this change: a checkout without the M0 helper must still spawn
# claude ("helper missing -> silent fallback"). A hard exit here broke that, so
# the guard lives at each call site instead — which is what CR #2599 actually
# needs (no `"" -c ...`), without turning a missing helper into a launch failure.
PY=""
if [ -r "$REPO/scripts/python-binary.sh" ]; then
  . "$REPO/scripts/python-binary.sh"
  PY="$(resolve_python "$REPO")"
fi

# Registers the PERSONAL_CLAUDE.md compaction-reinject hook, idempotent.
# Single Claude launch chokepoint — covers startup.sh, --restart, menu bar.
bash "$REPO/scripts/install-personal-claude-hook.sh" || echo "start-cli: personal-claude hook install failed (rc=$?) — hook may be absent" >&2

# Honor a caller-provided socket (e.g. a desktop app that runs a user-private tmux
# runtime under its app-support dir); default to the shared /tmp socket for dev/CLI.
# Backward-compatible: unset → identical to the previous hardcoded value.
TMUX_SOCKET="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"

# A tmux server inherits its GLOBAL environment from whichever process starts it,
# and `start-server` on a serverless socket is a no-op — so unset before any tmux.
unset SUTANDO_CORE_MODEL
SESSION="${SUTANDO_TMUX_SESSION:-sutando-core}"

# Marker identifying THIS process as the long-lived sutando-core session (as
# opposed to an ad-hoc `claude` in the same checkout — PR review, codex, etc.).
# The SessionStart hook (src/schedule-crons-session-hint.sh) gates its
# /startup bootstrap reminder on this so only the core triggers cron
# registration, never every session in the checkout. Exported so the no-tmux
# `exec claude` fallback inherits it directly; injected into the tmux launch
# branches via `new-session -e` (below) since tmux runs the command under the
# server's environment, not necessarily this shell's.
# Snapshot what we INHERITED before the export below overwrites it: the
# in-session restart guard must not read the marker this script sets itself.
CALLER_CORE_SESSION="${SUTANDO_CORE_SESSION:-}"
export SUTANDO_CORE_SESSION=1
export SUTANDO_CORE_RUNTIME=claude
CORE_ENV_ARGS=(-e SUTANDO_CORE_SESSION=1 -e SUTANDO_CORE_RUNTIME=claude)
[ -n "${SUTANDO_TMUX_SOCKET:-}" ] && CORE_ENV_ARGS+=(-e "SUTANDO_TMUX_SOCKET=$SUTANDO_TMUX_SOCKET")
[ -n "${SUTANDO_TMUX_SESSION:-}" ] && CORE_ENV_ARGS+=(-e "SUTANDO_TMUX_SESSION=$SUTANDO_TMUX_SESSION")
# Forward the embedder-provided default workspace into the core session for the
# SAME reason as above (tmux takes the server env, not this shell's). Without
# this the core's own resolve_workspace() (proactive-loop, task scripts) misses
# $SUTANDO_DEFAULT_WORKSPACE and falls back to {repo}/workspace — while the
# gateway window (which gets it explicitly) resolves to that path: a split-brain
# where the two watch different tasks/ dirs. Companion to the resolver change
# (#2094); conditional so non-bundled/OSS installs are untouched.
[ -n "${SUTANDO_DEFAULT_WORKSPACE:-}" ] && CORE_ENV_ARGS+=(-e "SUTANDO_DEFAULT_WORKSPACE=$SUTANDO_DEFAULT_WORKSPACE")
# Product deployments can disable autonomous repo development while keeping
# owner tasks, health checks, and the task watcher active. Explicitly forward
# the override because tmux may use an older server environment.
if [ "${SUTANDO_SELF_DEVELOPMENT_ENABLED+x}" = x ]; then
  CORE_ENV_ARGS+=(-e "SUTANDO_SELF_DEVELOPMENT_ENABLED=$SUTANDO_SELF_DEVELOPMENT_ENABLED")
fi
# Route the core through the credential proxy when one is live (quota
# telemetry, #2211/#2288). startup.sh exports ANTHROPIC_BASE_URL for cores
# it launches, but a start-cli-launched core (app restart-intercept,
# --restart, supervisor) never runs startup.sh — the proxy sits idle,
# quota-state.json goes stale, and the proactive loop's budget governor
# runs blind. Guarded twice: honor a caller-set ANTHROPIC_BASE_URL, and
# only wire up when a LISTENer actually holds the proxy port — never point
# the core at a dead port (the #1086/#1291 failure class; same
# LISTEN-not-any-socket rule as src/restart.sh).
proxy_listener_up() {
  lsof -nP -iTCP:7846 -sTCP:LISTEN > /dev/null 2>&1
}
if [ -z "${ANTHROPIC_BASE_URL:-}" ]; then
  # A loaded launchd job means the proxy is EXPECTED on this host even when its
  # listener hasn't bound yet (same loaded-job check as startup.sh/restart.sh).
  PROXY_EXPECTED=""
  if launchctl print "gui/$(id -u)/com.sutando.credential-proxy" > /dev/null 2>&1; then
    PROXY_EXPECTED=1
  fi
  if [ -n "$PROXY_EXPECTED" ]; then
    # Bounded wait (~10s): a supervised proxy can bind seconds after the core on
    # a cold boot, and a one-shot check would leave the core unrouted for life.
    for _ in $(seq 1 20); do
      proxy_listener_up && break
      sleep 0.5
    done
  fi
  if proxy_listener_up; then
    export ANTHROPIC_BASE_URL=http://localhost:7846
  elif [ -n "$PROXY_EXPECTED" ]; then
    echo "  ⚠ credential proxy expected (launchd job loaded) but :7846 never bound within ~10s — core runs unrouted this session (no proxy protection, no quota telemetry)" >&2
  fi
fi
if [ -n "${ANTHROPIC_BASE_URL:-}" ]; then
  CORE_ENV_ARGS+=(-e "ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL")
fi
# Test probe: dump the assembled core env forwarding and exit — lets the
# regression suite assert the proxy-routing policy (live listener forwards,
# dead port omits, caller preset wins) against the REAL CORE_ENV_ARGS under
# a stubbed lsof, without touching tmux. No production caller passes this.
if [ "${1:-}" = "--print-core-env" ]; then
  printf '%s\n' ${CORE_ENV_ARGS[@]+"${CORE_ENV_ARGS[@]}"}
  exit 0
fi

tmux_available() {
  command -v tmux > /dev/null 2>&1
}

tmux_session_exists() {
  tmux_available || return 1
  tmux -S "$TMUX_SOCKET" has-session -t "$SESSION" 2>/dev/null
}

# A managed core is alive when the tmux session EXISTS and a `claude --name
# sutando-core` process is running under it. Do NOT gate on the pane's current
# foreground command: a healthy core that is mid-tool shows the pane cmd as
# bash/python3/node/etc, so a pane-command match would falsely report it dead
# and (on --restart) tear down a live core mid-task. Existence + the core claude
# process is the correct liveness signal.
tmux_core_session_running() {
  tmux_session_exists || return 1
  core_claude_running
}

core_claude_pids() {
  # -a: BSD/macOS pgrep excludes the caller's ANCESTORS by default, so when this
  # script runs from inside the core (startup.sh via the core's own Bash tool)
  # the live core is invisible → "core Claude is gone" → heal spawns a duplicate
  # task consumer. `read -r pid _` tolerates procps -a's "pid cmdline" output.
  pgrep -ax claude 2>/dev/null | while read -r pid _; do
    args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    case "$args" in
      *"--name $SESSION"*|*"--name=$SESSION"*) echo "$pid" ;;
    esac
  done
}

core_claude_running() {
  [ -n "$(core_claude_pids)" ]
}

# Optional working-directory override for the core `claude` process.
#   - Unset (upstream default): no override — the core launches from $REPO (the
#     script's cwd), exactly as before. Zero behavior change for OSS installs.
#   - Set (e.g. Sutando.app exports SUTANDO_CLAUDE_WORKING_DIR=$HOME/.sutando/repo):
#     anchor the core's CWD there instead. Claude Code slugs its project /
#     auto-memory dir off the cwd, so a stable cwd (not $REPO, which moves across
#     upgrades / app bundles) keeps that project + memory continuous.
#     SCOPE: this sets ONLY the cwd. It does NOT relocate CLAUDE_CONFIG_DIR
#     (sessions / memory / config) — that is resolved independently from
#     sutando.config's `workspace.path` (read via scripts/sutando-config.sh,
#     below). If $REPO is a moving bundle path, set `workspace.path` to a stable
#     location too; this env var alone won't move CLAUDE_CONFIG_DIR off $REPO.
# Applied uniformly via a tmux `-c` arg array (and a plain `cd` on the no-tmux
# fallback) so every launch branch agrees. The ${arr[@]+...} expansions below
# keep the empty (unset) case safe on bash 3.2 under `set -u`, same pattern as
# SETTINGS_ARGS.
#
# CANONICALIZE ONCE, REUSE EVERYWHERE: Claude Code keys the folder-trust dialog
# (and the project/auto-memory slug) by the process's ABSOLUTE cwd — getcwd(),
# with symlinks resolved. So resolve the override to its physical absolute path
# here and re-export it; the SAME value then feeds mkdir, tmux `-c` / the no-tmux
# `cd`, AND the trust-dialog seed in the onboarding block below. A raw value with
# a leading ~, a relative segment, or a symlinked parent would otherwise seed
# projects[<wrong key>] in .claude.json, and the detached no-TTY core would still
# hang at the trust prompt. Expand a leading ~, create the dir (fail loud with a
# scoped message if we can't — better than chdir'ing into the wrong place under
# set -e's raw error), then resolve via `cd … && pwd -P`.
CWD_ARGS=()
if [ -n "${SUTANDO_CLAUDE_WORKING_DIR:-}" ]; then
  _cwd_exp="${SUTANDO_CLAUDE_WORKING_DIR/#\~/$HOME}"
  mkdir -p "$_cwd_exp" || { echo "  ✗ can't create core working dir: $_cwd_exp" >&2; exit 1; }
  SUTANDO_CLAUDE_WORKING_DIR="$(cd "$_cwd_exp" && pwd -P)"
  export SUTANDO_CLAUDE_WORKING_DIR
  CWD_ARGS=(-c "$SUTANDO_CLAUDE_WORKING_DIR")
  echo "  ✓ core working dir: $SUTANDO_CLAUDE_WORKING_DIR"
fi

# Resolve workspace-scoped CLAUDE_CONFIG_DIR. The interactive `claude-sutando`
# shell function does the same per-invocation; this is the machine-spawn
# equivalent so the tmux-wrapped core process writes sessions / memory / state
# into the workspace tree rather than the global ~/.claude/.
#
# Resolve-or-refuse is shared with src/startup.sh; only the seeding is ours.
source "$REPO/src/claude_config_dir.sh"
if _ccd="$(resolve_claude_config_dir "$REPO" start-cli)"; then
    mkdir -p "$_ccd"
    export CLAUDE_CONFIG_DIR="$_ccd"
    echo "  ✓ CLAUDE_CONFIG_DIR=$_ccd"
    # Onboarding-state seed — fixes "the core re-runs the 'let's get started'
    # flow on every restart". Claude Code gates the welcome/theme flow on
    # `hasCompletedOnboarding` in $CLAUDE_CONFIG_DIR/.claude.json. A
    # workspace-scoped config dir starts without it, and the core runs
    # detached/non-interactively (-- below) so it never *completes* onboarding
    # to persist the flag — every launch dead-ends at the welcome flow the
    # moment the user attaches the Core CLI. Seed only that flag (merge — never
    # clobber oauthAccount/projects/mcpServers/credentials), carrying `theme`
    # from the user's global ~/.claude.json when present so the theme picker is
    # skipped too. Idempotent + atomic. When SUTANDO_CLAUDE_WORKING_DIR is set we
    # ALSO pre-accept the folder-trust dialog for that ONE directory (trust-seed
    # in the python below) — otherwise a fresh node's first detached launch
    # dead-ends at "Do you trust the files in this folder?", which
    # --dangerously-skip-permissions does NOT bypass, and the core hangs with no
    # TTY to answer it. Gated on the opt-in env var so we only ever trust the dir
    # the operator explicitly chose, never arbitrary paths. We still do NOT touch
    # the dangerous-mode acknowledgement gate on a normal start — that's the
    # user's to accept. EXCEPTION: when SUTANDO_ACCEPT_BYPASS_PERMISSIONS=1 is set
    # (a deliberately detached, no-TTY core that explicitly opted in — the bundled
    # desktop's launch-sutando.sh sets it), we ALSO seed
    # skipDangerousModePermissionPrompt below, because there is no TTY to answer
    # that prompt and the core hangs forever otherwise (owner-hit 2026-07-14).
    # Dedicated opt-in (NOT the broader SUTANDO_CLAUDE_WORKING_DIR trust gate) so
    # only the truly-headless bundled core auto-accepts — the interactive
    # terminal-server pane still prompts the user to accept it themselves.
    # This is the single launch chokepoint (Sutando.app's launchCore, the
    # terminal-server Core CLI pane, and src/startup.sh all exec this script),
    # so seeding here covers every path.
    if [ -n "$PY" ] && "$PY" -c 'import sys' > /dev/null 2>&1; then
      _ccd="$_ccd" _cwd="${SUTANDO_CLAUDE_WORKING_DIR:-}" _accept_bypass="${SUTANDO_ACCEPT_BYPASS_PERMISSIONS:-}" "$PY" - <<'PY' || echo "  ⚠ onboarding-seed skipped (non-fatal)"
import json, os
ccd = os.environ["_ccd"]
target = os.path.join(ccd, ".claude.json")
try:
    cfg = json.load(open(target)) if os.path.exists(target) else {}
    if not isinstance(cfg, dict):
        cfg = {}
except Exception:
    cfg = {}
glob = {}
try:
    with open(os.path.expanduser("~/.claude.json")) as f:
        g = json.load(f)
        if isinstance(g, dict):
            glob = g
except Exception:
    pass
changed = False
chrome_seeded = False
if cfg.get("hasCompletedOnboarding") is not True:
    cfg["hasCompletedOnboarding"] = True
    changed = True
# Claude-in-Chrome onboarding seed. The core launches with --chrome (see the
# CORE_CMD below), which on first run in a fresh scoped config shows a "Claude
# in Chrome" acknowledgement prompt ("Enter to confirm · Esc to cancel").
# --dangerously-skip-permissions does NOT bypass it, so a detached no-TTY core
# hangs there — process alive but never reaching /schedule-crons, and the
# desktop onboarding "Say hello" local probe times out with no reply
# (owner-hit 2026-07-28, fresh install). Pre-accept it the same way as
# hasCompletedOnboarding: Claude Code records acceptance as
# hasCompletedClaudeInChromeOnboarding=true in .claude.json.
if cfg.get("hasCompletedClaudeInChromeOnboarding") is not True:
    cfg["hasCompletedClaudeInChromeOnboarding"] = True
    changed = True
    chrome_seeded = True
if cfg.get("theme") is None and glob.get("theme") is not None:
    cfg["theme"] = glob["theme"]
    changed = True
# Trust-seed for the explicitly-configured working dir. Claude Code keys the
# folder-trust dialog on projects[<abs cwd>].hasTrustDialogAccepted; a fresh
# scoped config lacks it for a custom cwd, so the detached core would hang on
# the prompt. Only pre-trust the one dir the operator chose (env-gated).
cwd = os.environ.get("_cwd") or ""
trusted_dir = None
if cwd:
    projects = cfg.get("projects")
    if not isinstance(projects, dict):
        projects = cfg["projects"] = {}
    entry = projects.get(cwd)
    if not isinstance(entry, dict):
        entry = projects[cwd] = {}
    if entry.get("hasTrustDialogAccepted") is not True:
        entry["hasTrustDialogAccepted"] = True
        changed = True
        trusted_dir = cwd
# Dangerous-mode seed (env-gated, detached-core only). The core launches with
# --dangerously-skip-permissions; on first run in a fresh scoped config Claude
# Code shows a "Bypass Permissions mode / Yes, I accept" acknowledgement prompt.
# --dangerously-skip-permissions does NOT bypass THAT prompt, so a detached
# no-TTY core (the bundled desktop app) hangs on it forever — process alive but
# never reaching /schedule-crons (owner-hit 2026-07-14 on the mini; distinct from
# the folder-trust dialog above). Pre-accept it by seeding
# skipDangerousModePermissionPrompt in <ccd>/settings.json. Gated on the DEDICATED
# SUTANDO_ACCEPT_BYPASS_PERMISSIONS opt-in (set only by the bundled desktop's
# launch-sutando.sh) — NOT the broader SUTANDO_CLAUDE_WORKING_DIR trust gate — so
# only the truly-headless bundled core auto-accepts; the interactive terminal-server
# pane (also a working-dir launch) still prompts the user. Merge — never clobber
# existing settings. Idempotent + atomic. Empirically verified on claude v2.1.209
# (the bundled version): accepting the prompt writes exactly this settings.json key.
if os.environ.get("_accept_bypass"):
    settings_path = os.path.join(ccd, "settings.json")
    try:
        st = json.load(open(settings_path)) if os.path.exists(settings_path) else {}
        if not isinstance(st, dict):
            st = {}
    except Exception:
        st = {}
    if st.get("skipDangerousModePermissionPrompt") is not True:
        st["skipDangerousModePermissionPrompt"] = True
        s_tmp = settings_path + ".tmp"
        with open(s_tmp, "w") as f:
            json.dump(st, f, indent=2)
        os.replace(s_tmp, settings_path)
        print("  ✓ dangerous-mode-seed: skipDangerousModePermissionPrompt set in settings.json")
if changed:
    tmp = target + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, target)
    print("  ✓ onboarding-seed: hasCompletedOnboarding set in .claude.json")
    if chrome_seeded:
        print("  ✓ chrome-seed: hasCompletedClaudeInChromeOnboarding set in .claude.json")
    if trusted_dir:
        print("  ✓ trust-seed: hasTrustDialogAccepted set for %s" % trusted_dir)
PY
    fi
else
  _ccd_rc=$?
  # 2 = caller already scoped the config dir; nothing to seed, and the core
  # still reaches the intended credential store.
  [ "$_ccd_rc" = "2" ] || exit 1
  echo "  ✓ CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR (caller-provided; config helper absent)"
fi

# NO --model flag: the core inherits the user's global model, so 1M stays the
# default. An ambient env pin is indistinguishable from a deliberate choice.

# ---- core --settings hooks (AskUserQuestion guard always; obs when enabled) --
# One `--settings` flag carries every hook the core needs (multiple --settings
# flags are undocumented / last-wins, so we compose into a single JSON):
#
#   * AskUserQuestion guard — ALWAYS registered. The core runs headless (no
#     interactive user), so an AskUserQuestion tool call would block the session
#     forever; a PreToolUse `deny` short-circuits it (hooks/skip-ask-user-question.py).
#   * obs collector hooks — added to the SAME JSON only when an export endpoint
#     is set, so PreToolUse/PostToolUse only fork obs-hook.sh on the tool-call
#     hot path when capture is actually on. Endpoint comes from
#     $SUTANDO_OBS_ENDPOINT (exported so the hook resolves it at hook-time).
#
# The JSON is built by node helpers, NOT shell string interpolation: hand-rolled
# interpolation broke when $REPO held a space (split the command) or a `"` (broke
# the JSON). The helpers POSIX single-quote the path inside the command and
# JSON-escape the payload. The ${arr[@]+...} guard keeps the empty array safe on
# bash 3.2 under `set -u`.
OBS_ENDPOINT="${SUTANDO_OBS_ENDPOINT:-}"
export SUTANDO_OBS_ENDPOINT="$OBS_ENDPOINT"

SETTINGS_ARGS=()
if ! command -v node > /dev/null 2>&1; then
  echo "core hooks: node unavailable — cannot safely build --settings JSON; AskUserQuestion guard + obs disabled this session" >&2
else
  # Obs hooks are optional; the guard is not. Build the obs blob first (empty
  # string when capture is off) and let the composer array-concat it with the
  # always-on guard.
  OBS_JSON=""
  if [ -z "$OBS_ENDPOINT" ]; then
    echo "obs hooks: not registered (no export endpoint — set SUTANDO_OBS_ENDPOINT to enable capture)"
  else
    OBS_JSON="$(node "$REPO/src/observability/claude/hooks/build-hook-settings.mjs" "$REPO/src/observability/claude/hooks/obs-hook.sh")"
    if [ -n "$OBS_JSON" ]; then
      echo "obs hooks: → $OBS_ENDPOINT/ingest/claude-code-hooks (collector)"
    else
      echo "obs hooks: settings build failed — capture disabled this session" >&2
    fi
  fi
  CORE_SETTINGS_JSON="$(node "$REPO/src/agent/claude/cli/build-core-settings.mjs" "$REPO/hooks/skip-ask-user-question.py" "$OBS_JSON" "$REPO/hooks/skill-usage-telemetry.py")"
  if [ -n "$CORE_SETTINGS_JSON" ]; then
    SETTINGS_ARGS=(--settings "$CORE_SETTINGS_JSON")
    echo "core hooks: AskUserQuestion guard registered (PreToolUse deny — headless core can't answer it)"
  else
    echo "core hooks: settings build failed — AskUserQuestion guard NOT registered this session" >&2
  fi
fi

# Optional feature-owned task handler.  The adapter injects the capability at
# the edge; the generic watcher remains unaware of concrete skills and falls
# back to its legacy TASK_FILE event whenever this script is absent/unhandled.
# ---- obs metering (CC native OTel token + cost) -----------------------------
# Hooks give obs events but carry NO tokens. Claude Code's OTel
# `claude_code.token.usage` / `cost.usage` metrics are the authoritative usage
# source, so when an export endpoint is set we also turn on CC telemetry and
# point its OTLP exporter at the collector (which serves /v1/metrics). Enable
# ONLY metrics — logs/traces stay off so hooks remain the sole obs source (no
# duplicate events). JSON OTLP so the collector parses it without protobuf.
# Metrics may use the default-on local collector without enabling plaintext
# prompt/tool hooks. An explicit SUTANDO_OBS_ENDPOINT retains the legacy
# combined behavior. Honor any pre-set OTEL_* so a real backend is not replaced.
METRICS_ENDPOINT="${SUTANDO_OBS_METRICS_ENDPOINT:-$OBS_ENDPOINT}"
if [ -n "$METRICS_ENDPOINT" ] && [ -z "${OTEL_EXPORTER_OTLP_ENDPOINT:-}" ]; then
  export CLAUDE_CODE_ENABLE_TELEMETRY=1
  export OTEL_METRICS_EXPORTER=otlp
  export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
  export OTEL_EXPORTER_OTLP_ENDPOINT="$METRICS_ENDPOINT"
  export OTEL_METRIC_EXPORT_INTERVAL="${OTEL_METRIC_EXPORT_INTERVAL:-10000}" # ms; 10s (CC default 60s)
  echo "obs metering: → $METRICS_ENDPOINT/v1/metrics (CC OTel token+cost, every ${OTEL_METRIC_EXPORT_INTERVAL}ms)"
fi

# --restart: kill any existing session before starting fresh. Without this,
# the script's "already running → attach" path returns and the old session
# keeps running.
#
# HAZARD: --restart MUST NOT be invoked from inside the sutando-core
# session itself — kill-session terminates the running agent mid-task.
# Safe callers: Sutando.app menu, terminal one-off, future health-check
# emit-task. Unsafe: a future agent processing a "restart core" task by
# exec'ing this script from within sutando-core. Per Mini's #608 review.
# --visible (sonichi#2410): after boot (or on an already-running no-TTY
# re-run), open a Terminal window attached to the core session via a generated
# .command file + `open -a Terminal` — the TCC-free GUI-exec path proven in the
# 2026-07-29 recovery (no AppleEvents, no Automation permission, creates the
# window when none exists). No-op off macOS / without `open` / headless-only
# callers that don't pass it.
VISIBLE=0
for _arg in "$@"; do
  [ "$_arg" = "--visible" ] && VISIBLE=1
done

open_visible_terminal() {
  [ "$(uname)" = "Darwin" ] || return 0
  command -v open > /dev/null 2>&1 || return 0
  local ws cmdfile
  ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" || return 0
  [ -n "$ws" ] || return 0
  mkdir -p "$ws/state"
  cmdfile="$ws/state/attach-core.command"
  {
    echo '#!/bin/bash'
    echo '# Auto-generated by start-cli.sh --visible (sonichi#2410) — attaches this'
    echo '# Terminal window to the core session. Safe to re-run; delete freely.'
    echo "exec tmux -S '$TMUX_SOCKET' attach -t '$SESSION'"
  } > "$cmdfile"
  chmod +x "$cmdfile"
  open -a Terminal "$cmdfile" 2>/dev/null || true
}

# Two modes, per sonichi's "force restart should be separate from restart":
#   --restart       graceful: SIGTERM + wait; if the core won't stop cleanly it
#                   ABORTS (it may be mid-task) — never SIGKILLs on its own.
#   --force-restart SIGTERM → SIGKILL escalation for a wedged/unresponsive core.
# Both then fall through to the create path, which verifies the new core is live
# before exit 0 (no silent false-success). Every attempt is logged (timestamped)
# to logs/restart-attempts.log so a failed restart is diagnosable even when the
# caller (Sutando.app) routes our stdout to /dev/null — the gap that made the
# 2026-07-30 outage invisible.
RESTART_REQUESTED=""
FORCE_RESTART=""
case "${1:-}" in
  --restart)       RESTART_REQUESTED=1 ;;
  --force-restart) RESTART_REQUESTED=1; FORCE_RESTART=1 ;;
esac
log_restart_attempt() {
  local ws; ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" || return 0
  [ -n "$ws" ] || return 0
  mkdir -p "$ws/logs" 2>/dev/null || true
  printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "${FORCE_RESTART:+force-restart}${FORCE_RESTART:-restart}" "$1" \
    >> "$ws/logs/restart-attempts.log" 2>/dev/null || true
}
# Enforces the HAZARD above; the decision and its message are shared with
# the codex launcher, this adapter keeps only its own attempt logging.
if [ -n "$RESTART_REQUESTED" ] && sutando_restart_guard_refuses "$CALLER_CORE_SESSION"; then
  sutando_restart_guard_explain
  log_restart_attempt "$SUTANDO_RESTART_GUARD_REASON"
  exit 1
fi
if [ -n "$RESTART_REQUESTED" ]; then
  log_restart_attempt "begin (session=$(tmux_session_exists && echo up || echo none) core=$(core_claude_running && echo up || echo none))"
  if tmux_session_exists || core_claude_running; then
    echo "Killing existing $SESSION session..."
    tmux -S "$TMUX_SOCKET" kill-session -t "$SESSION" 2>/dev/null || true
    core_claude_pids | while read -r pid; do
      [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    done
    # Graceful window: Claude Code flushes telemetry/state on shutdown and can
    # take several seconds — longer than the old ~1s (5×0.2s) ceiling that let a
    # still-dying core slip into the "orphan reuse → exit 0" guard below. Poll ~3s.
    for _ in $(seq 1 15); do
      tmux_session_exists || core_claude_running || break
      sleep 0.2
    done
    if tmux_session_exists || core_claude_running; then
      if [ -n "$FORCE_RESTART" ]; then
        # force-restart: the core is wedged; escalate to SIGKILL, then poll ~3s.
        echo "  core still alive ~3s after SIGTERM — force-restart escalating to SIGKILL" >&2
        tmux -S "$TMUX_SOCKET" kill-session -t "$SESSION" 2>/dev/null || true
        core_claude_pids | while read -r pid; do
          [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null || true
        done
        for _ in $(seq 1 15); do
          tmux_session_exists || core_claude_running || break
          sleep 0.2
        done
      else
        # plain restart must NOT hammer: the core may be legitimately mid-task.
        # Abort loud and point at force-restart rather than risk killing work.
        echo "  ⚠ $SESSION core did not stop within ~3s of SIGTERM." >&2
        echo "    'restart' won't SIGKILL a busy/wedged core — it may be mid-task." >&2
        echo "    Re-run as: bash $0 --force-restart   (or wait and retry)." >&2
        log_restart_attempt "abort: core would not stop gracefully (needs --force-restart)"
        exit 1
      fi
    fi
    # After force escalation, still alive → hard abort rather than stack a second
    # core on a survivor (double task-consumer) or exit-0 a half-torn-down state.
    if tmux_session_exists || core_claude_running; then
      echo "  ⚠ $SESSION core did not die after SIGKILL — aborting force-restart." >&2
      echo "    Investigate the stuck pid; rerun once it's gone." >&2
      log_restart_attempt "abort: core survived SIGKILL"
      exit 1
    fi
  fi
  log_restart_attempt "kill-complete; creating fresh core"
fi

# Sutando-friendly tmux defaults (mouse scrollback + alt-screen wheel fix).
# Defined as a function so it runs on EVERY invocation — including the
# "already running → attach" path below. 2026-06-11: Chi's scroll broke
# again because the live server (started 2026-05-30) somehow lacked these
# options even though #688/#1304 predate it; rather than depend on the
# session-creation path alone, re-apply on every start/attach/restart so
# any rerun of this script heals the server. Idempotent: re-applying to an
# already-configured server is a no-op.
apply_tmux_defaults() {
  command -v tmux > /dev/null 2>&1 || return 0
  tmux -S "$TMUX_SOCKET" start-server 2>/dev/null || true
  tmux -S "$TMUX_SOCKET" set-option -g mouse on 2>/dev/null || true
  # Clear any stale model pin. `setenv -u` with no -t hits tmux's DEFAULT session,
  # which on a multi-session socket is not necessarily the core's, so target each.
  tmux -S "$TMUX_SOCKET" setenv -gu SUTANDO_CORE_MODEL 2>/dev/null || true
  _pin_sessions="$(tmux -S "$TMUX_SOCKET" list-sessions -F '#{session_name}' 2>/dev/null || true)"
  while IFS= read -r _pin_sess; do
    [ -n "$_pin_sess" ] || continue
    tmux -S "$TMUX_SOCKET" setenv -t "=$_pin_sess" -u SUTANDO_CORE_MODEL 2>/dev/null || true
  done <<< "$_pin_sessions"
  # Wheel-scroll fix (sutando-plus#46, re-broken 2026-06-11): predicate on
  # mouse_any_flag, NOT alternate_on. Claude Code 2.1.150 stopped using the
  # alternate screen, so the old alt-screen predicate forwarded wheel events
  # to an app that never requested mouse input — they were silently dropped
  # and scrollback became unreachable. mouse_any_flag asks the question we
  # actually care about: does the pane app WANT mouse events? If yes (vim
  # with mouse=a, future Claude Code versions), forward them; if no, enter
  # copy-mode so WheelUp always reaches tmux scrollback regardless of the
  # app's screen mode. WheelDown passes through so normal scrolling works.
  tmux -S "$TMUX_SOCKET" bind -n WheelUpPane if-shell -F -t = '#{mouse_any_flag}' 'send-keys -M' 'copy-mode -e; send-keys -M' 2>/dev/null || true
  tmux -S "$TMUX_SOCKET" bind -n WheelDownPane send-keys -M 2>/dev/null || true
}

# Agent Shepherd M1 monitor (PR #2100). Watch the CANONICAL sutando-core session
# for blocked-on-input gates the no-TTY core can't answer (/login, a mid-session
# permission prompt, an unknown dialog) and write state/core-supervisor.json —
# consumed by the desktop "Action needed" banner and the communicator relay.
# Launched HERE, the one place that knows the canonical TMUX_SOCKET + SESSION —
# NOT from startup.sh, whose $TMUX is empty in the Sutando.app/background path
# (so a $TMUX-derived wiring would never start the monitor for the real core).
# The guard is scoped to THIS socket + out path so a monitor for a different
# core/socket can never suppress this one.
ensure_core_monitor() {
  local ws mon_out relay_pid_file relay_state
  ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" || return 0
  [ -n "$ws" ] || return 0
  mon_out="$ws/state/core-supervisor.json"
  # Monitor (PR #2100): launch unless one for this exact socket+out is running.
  if [ -n "$PY" ] && ! pgrep -f "core-input-watch\.py .*--socket ${TMUX_SOCKET} .*--out ${mon_out}" > /dev/null 2>&1; then
    "$PY" "$REPO/src/core-input-watch.py" \
      --socket "$TMUX_SOCKET" --session "$SESSION" --out "$mon_out" \
      > /tmp/core-input-watch.log 2>&1 &
  fi
  # Communicator relay (PR #2101): the monitor only WRITES core-supervisor.json;
  # nothing escalated it, so a hard-blocker (blocked-human / logged-out) on a
  # no-TTY core never reached an away owner. Run the one-shot relay on a cadence
  # so those states escalate to the owner's most-recently-active channel
  # (--active-from). The relay debounces internally (--state-file), so re-running
  # every 30s escalates a given stuck episode exactly once. Pidfile + kill -0
  # guard so an attach/re-run never double-starts the loop. Only blocked-human /
  # logged-out escalate (relay's HARD_ESCALATE); hung/crashed are RECOVER's job.
  relay_pid_file="$ws/state/core-supervisor-relay-loop.pid"
  relay_state="$ws/state/core-supervisor-relay.state"
  if ! { [ -f "$relay_pid_file" ] && kill -0 "$(cat "$relay_pid_file" 2>/dev/null)" 2>/dev/null; }; then
    # Redirect the WHOLE subshell (not just the inner python) to the log, so the
    # backgrounded loop does not inherit/hold this script's stdout/stderr — else a
    # caller that captures start-cli.sh's output (e.g. tests/start-cli-*.test.py)
    # blocks on the pipe until this infinite loop closes it (never) and times out.
    # Mirrors the monitor launch above, which redirects to /tmp/core-input-watch.log.
    [ -n "$PY" ] && ( while true; do
        "$PY" "$REPO/src/core-supervisor-relay.py" \
          --signal "$mon_out" --state-file "$relay_state" \
          --active-from "$ws/state/last-owner-activity.json"
        sleep 30
      done ) >> /tmp/core-supervisor-relay.log 2>&1 &
    echo $! > "$relay_pid_file"
  fi
}

# Already running — attach if interactive, else exit cleanly. A managed core is
# live when the tmux session exists AND a `claude --name sutando-core` process
# runs under it (tmux_core_session_running). Re-running the script is idempotent:
# we attach/no-op instead of spawning a second core (→ duplicate task consumers).
if tmux_core_session_running; then
  apply_tmux_defaults
  ensure_core_monitor   # re-ensure the supervisor monitor on every attach/re-run
  if [ -t 1 ] && command -v tmux > /dev/null 2>&1; then
    echo "Attaching to existing $SESSION (Ctrl-b d to detach)..."
    exec tmux -S "$TMUX_SOCKET" attach -t "$SESSION"
  fi
  if [ "$VISIBLE" = 1 ]; then
    open_visible_terminal
    echo "$SESSION already running — opened a Terminal window attached to it."
    exit 0
  fi
  echo "$SESSION already running."
  echo "To attach: tmux -S $TMUX_SOCKET attach -t $SESSION"
  exit 0
fi

# Orphaned core process, no tmux session — a `claude --name sutando-core` is
# running detached from any managed session (e.g. its tmux server died but the
# child claude survived). Adopt it: do NOT start a second core, which would
# double the task-consumer count. On a non-restart start we reuse the existing
# process; the operator can `--restart` to cleanly recycle it.
#
# Under --restart, do NOT adopt an orphan: we just tore the core down, so a
# claude seen here is either still dying (the SIGKILL escalation above should
# have reaped it) or one a competing launcher spawned in the race window.
# Reusing it would defeat the restart and re-introduce the false-success path.
if [ -z "$RESTART_REQUESTED" ] && core_claude_running; then
  echo "$SESSION claude process already running (no tmux session) — reusing it." >&2
  echo "To recycle it cleanly: bash $0 --restart"
  exit 0
fi

if tmux_session_exists; then
  # Session alive but the core claude is gone. The old behavior here was
  # kill-session — but the desktop runtime keeps SIBLING windows in this
  # session (gateway, monitor; launch-sutando.sh), so nuking the session tore
  # those down with the dead core: the G10 all-or-nothing gap. Heal WINDOW-
  # SCOPED instead: recreate the core as a new window in the surviving session,
  # with the same env/cwd/flags as the create path below. Target index 0 (the
  # core's conventional home, freed when its process died); fall back to any
  # free index if 0 is somehow occupied. This also makes sutando-ctl.sh's
  # restart-core (kill core window → rerun this script) truly window-scoped.
  echo "  ⚠ $SESSION exists but core Claude is gone — healing core window (sibling windows preserved)" >&2
  apply_tmux_defaults
  CORE_CMD=(claude --name "$SESSION" --remote-control "Sutando" --chrome --dangerously-skip-permissions --add-dir "$HOME" \
    ${SETTINGS_ARGS[@]+"${SETTINGS_ARGS[@]}"} -- "/startup")
  # -P -F prints the index the window ACTUALLY landed on: when index 0 is
  # occupied (e.g. a sibling drifted there) the fallback creates the core at a
  # nonzero index, and selecting a hardcoded :0 would activate the WRONG window
  # (review-caught: attach/Console then shows the gateway, not the healed core).
  healed_idx="$(tmux -S "$TMUX_SOCKET" new-window -dP -F '#{window_index}' -t "$SESSION:0" ${CORE_ENV_ARGS[@]+"${CORE_ENV_ARGS[@]}"} ${CWD_ARGS[@]+"${CWD_ARGS[@]}"} "${CORE_CMD[@]}" 2>/dev/null \
    || tmux -S "$TMUX_SOCKET" new-window -dP -F '#{window_index}' -t "$SESSION" ${CORE_ENV_ARGS[@]+"${CORE_ENV_ARGS[@]}"} ${CWD_ARGS[@]+"${CWD_ARGS[@]}"} "${CORE_CMD[@]}")"
  # Make the healed core the active window so attach/Console show it, not the
  # quiet gateway (same reason launch-sutando.sh creates siblings with -d).
  tmux -S "$TMUX_SOCKET" select-window -t "$SESSION:${healed_idx:-0}" 2>/dev/null || true
  ensure_core_monitor
  if [ -t 1 ]; then
    echo "Attaching to healed $SESSION (Ctrl-b d to detach)..."
    exec tmux -S "$TMUX_SOCKET" attach -t "$SESSION"
  fi
  echo "Healed core window in $SESSION (session + sibling windows preserved)."
  exit 0
fi

# Auto-install tmux via Homebrew if missing. Sutando.app's
# watcher-auto-restart depends on a tmux-wrapped CLI pane.
if ! command -v tmux > /dev/null 2>&1 && command -v brew > /dev/null 2>&1; then
  echo "tmux not found — installing via Homebrew (~30s, required for Sutando.app watcher-auto-restart)..."
  brew install tmux 2>&1 | tail -3
fi

# Stamp the core session start into an append-only per-boot log. One JSONL
# line per launch; consecutive entries bound each session's lifetime, which
# is what session-recap tooling needs to pick the right transcript (owner
# ask 2026-07-13). Best-effort: never block the launch on it.
if _ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" && [ -n "$_ws" ]; then
  mkdir -p "$_ws/state" 2>/dev/null || true
  printf '{"host":"%s","session_started_at":%s,"iso":"%s","source":"start-cli"}\n' \
    "$(hostname | sed 's/\..*//')" "$(date +%s)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >> "$_ws/state/session-starts.log" 2>/dev/null || true
fi

# Fall back to a bare `exec claude` if tmux is still missing.
if ! command -v tmux > /dev/null 2>&1; then
  echo "  ⚠ tmux not found — running without tmux wrapper"
  echo "    (Sutando.app's watcher-auto-restart won't work; brew install tmux to enable)"
  [ -n "${SUTANDO_CLAUDE_WORKING_DIR:-}" ] && cd "$SUTANDO_CLAUDE_WORKING_DIR"
  exec claude --name "$SESSION" --remote-control "Sutando" --chrome --dangerously-skip-permissions --add-dir "$HOME" \
    ${SETTINGS_ARGS[@]+"${SETTINGS_ARGS[@]}"} \
    -- "/startup"
fi

# Explicit -S socket path so Sutando.app (which runs under a different
# TMPDIR due to macOS sandboxing when launched via `open`) can reach the
# same tmux server as the user shell (per #PR_444 watcher-auto-restart).
#
# Sutando-friendly tmux defaults — applied to the server before the session
# attaches (see apply_tmux_defaults above for the full rationale).
#
# Tradeoff: `mouse on` intercepts native Cmd+drag text selection in the pane.
# To copy text the macOS-native way, hold Option while dragging (Terminal.app,
# iTerm2, Ghostty all honor Option-drag as a tmux-bypass). Documenting here
# so future readers don't think this is a regression.
apply_tmux_defaults
#
# Branch on whether we have a TTY:
#   - TTY (user running from terminal): exec attach so the user sees the
#     Claude Code prompt and the script process IS the tmux client.
#   - No TTY (Sutando.app's Restart Core or any background invocation):
#     start detached so we don't hang, server keeps running.
#
# NOTE: the working dir (`-c` in CWD_ARGS) applies only when `new-session -A`
# CREATES the session. If the session already exists, `-A` attaches and the
# start-directory is silently dropped — so re-anchoring a running core to a new
# working dir must go through `--restart` (kill-then-create), not a bare rerun.
if [ -t 1 ]; then
  ensure_core_monitor   # backgrounded child survives the exec below
  exec tmux -S "$TMUX_SOCKET" new-session -A -s "$SESSION" ${CORE_ENV_ARGS[@]+"${CORE_ENV_ARGS[@]}"} ${CWD_ARGS[@]+"${CWD_ARGS[@]}"} \
    claude --name "$SESSION" --remote-control "Sutando" --chrome --dangerously-skip-permissions --add-dir "$HOME" \
    ${SETTINGS_ARGS[@]+"${SETTINGS_ARGS[@]}"} \
    -- "/startup"
else
  tmux -S "$TMUX_SOCKET" new-session -d -s "$SESSION" ${CORE_ENV_ARGS[@]+"${CORE_ENV_ARGS[@]}"} ${CWD_ARGS[@]+"${CWD_ARGS[@]}"} \
    claude --name "$SESSION" --remote-control "Sutando" --chrome --dangerously-skip-permissions --add-dir "$HOME" \
    ${SETTINGS_ARGS[@]+"${SETTINGS_ARGS[@]}"} \
    -- "/startup"
  # Verify the core actually came up before reporting success. Without this a
  # failed launch (tmux server refusal, claude crash-on-start, a bad flag) still
  # exits 0 and Sutando.app reports "Core restarted" while nothing is serving —
  # the same false-success class as the --restart kill race above. Poll ~5s for
  # the session AND a live `claude --name` under it; exit non-zero otherwise so
  # the caller surfaces a real failure instead of a silent dead core.
  for _ in $(seq 1 25); do
    tmux_core_session_running && break
    sleep 0.2
  done
  if ! tmux_core_session_running; then
    echo "  ⚠ $SESSION did not come up within ~5s of launch — start FAILED." >&2
    [ -n "$RESTART_REQUESTED" ] && log_restart_attempt "FAILED: core did not come up within ~5s"
    exit 1
  fi
  [ -n "$RESTART_REQUESTED" ] && log_restart_attempt "success: core live"
  ensure_core_monitor   # canonical session now exists — start the supervisor monitor
  if [ "$VISIBLE" = 1 ]; then
    open_visible_terminal
    echo "Started $SESSION detached — opened a Terminal window attached to it."
  else
    echo "Started $SESSION detached. Attach via Open Core CLI in menu bar, or:"
    echo "  tmux -S $TMUX_SOCKET attach -t $SESSION"
  fi
fi
