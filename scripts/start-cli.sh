#!/bin/bash
# scripts/start-cli.sh — canonical launch script for the sutando-core tmux
# session. Single source of truth for the "how to start Claude Code" command,
# so startup.sh + Sutando.app's Restart Core menu can both invoke it without
# duplicating the launch arguments.
#
# Usage:
#   bash scripts/start-cli.sh           # start (or attach if running)
#   bash scripts/start-cli.sh --restart # kill existing session then start fresh
#
# Per Chi's prompt 2026-05-05 ("shall we add core CLI-related commands in
# sutando app"): extracting the launch command from startup.sh's inline tmux
# block lets the menu-bar app's Restart Core action invoke the same canonical
# entry without re-implementing the tmux flags.

set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

TMUX_SOCKET="/tmp/sutando-tmux.sock"
SESSION="sutando-core"

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
# MODEL_ARGS / SETTINGS_ARGS.
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
# Defense in depth:
#   - M0 helper missing → silent fallback (legacy install, extracted tarball).
#   - Helper present + config valid → export env for every claude invocation
#     below (no-tmux fallback at L~75, TTY exec at L~115, no-TTY detached at
#     L~120 all inherit it).
#   - Helper present + config violates the workspace-sub-folder invariant →
#     refuse to start. Silently falling back to ~/.claude/ would hide a real
#     config error AND scatter state into a location the M2 vault sync engine
#     doesn't include.
if [ -x "$REPO/scripts/sutando-config.sh" ]; then
  _ccd_err="$(mktemp -t start-cli-ccd.XXXXXX)"
  if _ccd="$(bash "$REPO/scripts/sutando-config.sh" claude-sutando-config-dir 2>"$_ccd_err")"; then
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
    # the global bypassPermissionsModeAccepted gate — that's the user's to accept.
    # This is the single launch chokepoint (Sutando.app's launchCore, the
    # terminal-server Core CLI pane, and src/startup.sh all exec this script),
    # so seeding here covers every path.
    if command -v python3 > /dev/null 2>&1; then
      _ccd="$_ccd" _cwd="${SUTANDO_CLAUDE_WORKING_DIR:-}" python3 - <<'PY' || echo "  ⚠ onboarding-seed skipped (non-fatal)"
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
if cfg.get("hasCompletedOnboarding") is not True:
    cfg["hasCompletedOnboarding"] = True
    changed = True
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
if changed:
    tmp = target + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, target)
    print("  ✓ onboarding-seed: hasCompletedOnboarding set in .claude.json")
    if trusted_dir:
        print("  ✓ trust-seed: hasTrustDialogAccepted set for %s" % trusted_dir)
PY
    fi
  else
    echo "start-cli: claude_sutando_config_dir invalid — refusing to start core" >&2
    cat "$_ccd_err" >&2
    rm -f "$_ccd_err"
    exit 1
  fi
  rm -f "$_ccd_err"
fi

# Optional context-window pin (graceful-degradation hook for the 1M
# usage-credit-gate wedge — see src/health-check.py recover_core_if_wedged).
# When SUTANDO_CORE_MODEL is set we pass it through as `--model`; otherwise we
# add NO flag, so the core inherits the user's global model (e.g. `opus[1m]`
# from ~/.claude/settings.json) and 1M stays the default — we never disable it.
# health-check's --recover-core escalation only sets SUTANDO_CORE_MODEL=opus
# AFTER a 1M restart fails to hold, so a re-wedging core falls back to standard
# 200K context (no gate) and keeps working instead of looping. The
# ${arr[@]+...} guard keeps an empty array safe on bash 3.2 even under `set -u`
# (mirrors the empty-array care in PR #1391).
MODEL_ARGS=()
if [ -n "${SUTANDO_CORE_MODEL:-}" ]; then
  MODEL_ARGS=(--model "$SUTANDO_CORE_MODEL")
fi

# ---- obs hooks (registered ONLY when an export endpoint is set) -------------
# Register Claude Code hooks via `--settings` (merges with the user's settings
# at highest precedence, per-session — no persistent file edit) ONLY when an
# endpoint is configured. With no endpoint we inject no --settings at all, so
# PreToolUse/PostToolUse never fork obs-hook.sh on the tool-call hot path —
# capture is truly zero-cost (not just a no-op fork) when off. The endpoint comes
# from $SUTANDO_OBS_ENDPOINT (exported so the hook — which runs in the session's
# inherited env — resolves it at hook-time).
OBS_ENDPOINT="${SUTANDO_OBS_ENDPOINT:-}"
export SUTANDO_OBS_ENDPOINT="$OBS_ENDPOINT"

# Inject --settings (and thus the per-event hooks) only when an endpoint exists.
# The ${arr[@]+...} guard keeps the empty array safe on bash 3.2 under `set -u`
# (same pattern as MODEL_ARGS above). The settings JSON is built by a node helper,
# NOT shell string interpolation: hand-rolled interpolation broke when $REPO held
# a space (split the command) or a `"` (broke the JSON). The helper POSIX
# single-quotes the path inside the command and JSON-escapes the payload. Its 10
# event keys are all valid CC hook events (code.claude.com/docs/en/hooks.md).
SETTINGS_ARGS=()
if [ -z "$OBS_ENDPOINT" ]; then
  echo "obs hooks: not registered (no export endpoint — set SUTANDO_OBS_ENDPOINT to enable capture)"
elif ! command -v node > /dev/null 2>&1; then
  echo "obs hooks: node unavailable — cannot safely build --settings JSON; capture disabled this session" >&2
else
  HOOKS_JSON="$(node "$REPO/src/observability/claude/hooks/build-hook-settings.mjs" "$REPO/src/observability/claude/hooks/obs-hook.sh")"
  if [ -n "$HOOKS_JSON" ]; then
    SETTINGS_ARGS=(--settings "$HOOKS_JSON")
    echo "obs hooks: → $OBS_ENDPOINT/ingest/claude-code-hooks (collector)"
  else
    echo "obs hooks: settings build failed — capture disabled this session" >&2
  fi
fi

# ---- obs metering (CC native OTel token + cost) -----------------------------
# Hooks give obs events but carry NO tokens. Claude Code's OTel
# `claude_code.token.usage` / `cost.usage` metrics are the authoritative usage
# source, so when an export endpoint is set we also turn on CC telemetry and
# point its OTLP exporter at the collector (which serves /v1/metrics). Enable
# ONLY metrics — logs/traces stay off so hooks remain the sole obs source (no
# duplicate events). JSON OTLP so the collector parses it without protobuf.
# Gated on the same endpoint; honors any pre-set OTEL_* so a real OTel backend
# isn't overridden.
if [ -n "$OBS_ENDPOINT" ] && [ -z "${OTEL_EXPORTER_OTLP_ENDPOINT:-}" ]; then
  export CLAUDE_CODE_ENABLE_TELEMETRY=1
  export OTEL_METRICS_EXPORTER=otlp
  export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
  export OTEL_EXPORTER_OTLP_ENDPOINT="$OBS_ENDPOINT"
  export OTEL_METRIC_EXPORT_INTERVAL="${OTEL_METRIC_EXPORT_INTERVAL:-10000}" # ms; 10s (CC default 60s)
  echo "obs metering: → $OBS_ENDPOINT/v1/metrics (CC OTel token+cost, every ${OTEL_METRIC_EXPORT_INTERVAL}ms)"
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
if [ "$1" = "--restart" ]; then
  if pgrep -f "claude.*--name.*$SESSION" > /dev/null 2>&1; then
    echo "Killing existing $SESSION session..."
    tmux -S "$TMUX_SOCKET" kill-session -t "$SESSION" 2>/dev/null || true
    # Poll for actual shutdown — robust on slow machines, faster on fast
    # ones (~1s ceiling) than a fixed sleep.
    for _ in 1 2 3 4 5; do
      pgrep -f "claude.*--name.*$SESSION" > /dev/null 2>&1 || break
      sleep 0.2
    done
  fi
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

# Already running — attach if interactive, else exit cleanly. This branch
# also catches the !--restart path so re-running the script is idempotent.
if pgrep -f "claude.*--name.*$SESSION" > /dev/null 2>&1; then
  apply_tmux_defaults
  if [ -t 1 ] && command -v tmux > /dev/null 2>&1; then
    echo "Attaching to existing $SESSION (Ctrl-b d to detach)..."
    exec tmux -S "$TMUX_SOCKET" attach -t "$SESSION"
  fi
  echo "$SESSION already running."
  echo "To attach: tmux -S $TMUX_SOCKET attach -t $SESSION"
  exit 0
fi

# Auto-install tmux via Homebrew if missing. Sutando.app's
# watcher-auto-restart depends on a tmux-wrapped CLI pane.
if ! command -v tmux > /dev/null 2>&1 && command -v brew > /dev/null 2>&1; then
  echo "tmux not found — installing via Homebrew (~30s, required for Sutando.app watcher-auto-restart)..."
  brew install tmux 2>&1 | tail -3
fi

# Fall back to a bare `exec claude` if tmux is still missing.
if ! command -v tmux > /dev/null 2>&1; then
  echo "  ⚠ tmux not found — running without tmux wrapper"
  echo "    (Sutando.app's watcher-auto-restart won't work; brew install tmux to enable)"
  [ -n "${SUTANDO_CLAUDE_WORKING_DIR:-}" ] && cd "$SUTANDO_CLAUDE_WORKING_DIR"
  exec claude --name "$SESSION" ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"} --remote-control "Sutando" --dangerously-skip-permissions --add-dir "$HOME" \
    ${SETTINGS_ARGS[@]+"${SETTINGS_ARGS[@]}"} \
    -- "/schedule-crons"
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
  exec tmux -S "$TMUX_SOCKET" new-session -A -s "$SESSION" ${CWD_ARGS[@]+"${CWD_ARGS[@]}"} \
    claude --name "$SESSION" ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"} --remote-control "Sutando" --dangerously-skip-permissions --add-dir "$HOME" \
    ${SETTINGS_ARGS[@]+"${SETTINGS_ARGS[@]}"} \
    -- "/schedule-crons"
else
  tmux -S "$TMUX_SOCKET" new-session -d -s "$SESSION" ${CWD_ARGS[@]+"${CWD_ARGS[@]}"} \
    claude --name "$SESSION" ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"} --remote-control "Sutando" --dangerously-skip-permissions --add-dir "$HOME" \
    ${SETTINGS_ARGS[@]+"${SETTINGS_ARGS[@]}"} \
    -- "/schedule-crons"
  echo "Started $SESSION detached. Attach via Open Core CLI in menu bar, or:"
  echo "  tmux -S $TMUX_SOCKET attach -t $SESSION"
fi
