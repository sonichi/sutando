#!/bin/bash
# src/agent/claude/cli/start-cli.sh — canonical launch script for the sutando-core
# tmux session. Single source of truth for the "how to start Claude Code" command,
# so startup.sh + Sutando.app's Restart Core menu can both invoke it without
# duplicating the launch arguments.
#
# Usage:
#   bash src/agent/claude/cli/start-cli.sh           # start (or attach if running)
#   bash src/agent/claude/cli/start-cli.sh --restart # kill existing session then start fresh
#
# Per Chi's prompt 2026-05-05 ("shall we add core CLI-related commands in
# sutando app"): extracting the launch command from startup.sh's inline tmux
# block lets the menu-bar app's Restart Core action invoke the same canonical
# entry without re-implementing the tmux flags.

set -e

# This script lives at src/agent/claude/cli/ — four levels under the repo root.
REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
cd "$REPO"

# Honor a caller-provided socket (e.g. a desktop app that runs a user-private tmux
# runtime under its app-support dir); default to the shared /tmp socket for dev/CLI.
# Backward-compatible: unset → identical to the previous hardcoded value.
TMUX_SOCKET="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"
SESSION="sutando-core"

# Marker identifying THIS process as the long-lived sutando-core session (as
# opposed to an ad-hoc `claude` in the same checkout — PR review, codex, etc.).
# The SessionStart hook (src/schedule-crons-session-hint.sh) gates its
# /startup bootstrap reminder on this so only the core triggers cron
# registration, never every session in the checkout. Exported so the no-tmux
# `exec claude` fallback inherits it directly; injected into the tmux launch
# branches via `new-session -e` (below) since tmux runs the command under the
# server's environment, not necessarily this shell's.
export SUTANDO_CORE_SESSION=1
CORE_ENV_ARGS=(-e SUTANDO_CORE_SESSION=1)
# Forward the embedder-provided default workspace into the core session for the
# SAME reason as above (tmux takes the server env, not this shell's). Without
# this the core's own resolve_workspace() (proactive-loop, task scripts) misses
# $SUTANDO_DEFAULT_WORKSPACE and falls back to {repo}/workspace — while the
# gateway window (which gets it explicitly) resolves to that path: a split-brain
# where the two watch different tasks/ dirs. Companion to the resolver change
# (#2094); conditional so non-bundled/OSS installs are untouched.
[ -n "${SUTANDO_DEFAULT_WORKSPACE:-}" ] && CORE_ENV_ARGS+=(-e "SUTANDO_DEFAULT_WORKSPACE=$SUTANDO_DEFAULT_WORKSPACE")

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
    if command -v python3 > /dev/null 2>&1; then
      _ccd="$_ccd" _cwd="${SUTANDO_CLAUDE_WORKING_DIR:-}" _accept_bypass="${SUTANDO_ACCEPT_BYPASS_PERMISSIONS:-}" python3 - <<'PY' || echo "  ⚠ onboarding-seed skipped (non-fatal)"
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
# bash 3.2 under `set -u` (same pattern as MODEL_ARGS above).
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
  CORE_SETTINGS_JSON="$(node "$REPO/src/agent/claude/cli/build-core-settings.mjs" "$REPO/hooks/skip-ask-user-question.py" "$OBS_JSON")"
  if [ -n "$CORE_SETTINGS_JSON" ]; then
    SETTINGS_ARGS=(--settings "$CORE_SETTINGS_JSON")
    echo "core hooks: AskUserQuestion guard registered (PreToolUse deny — headless core can't answer it)"
  else
    echo "core hooks: settings build failed — AskUserQuestion guard NOT registered this session" >&2
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
  if tmux -S "$TMUX_SOCKET" has-session -t "$SESSION" 2>/dev/null; then
    echo "Killing existing $SESSION session..."
    tmux -S "$TMUX_SOCKET" kill-session -t "$SESSION" 2>/dev/null || true
    # Poll for actual shutdown — robust on slow machines, faster on fast
    # ones (~1s ceiling) than a fixed sleep.
    for _ in 1 2 3 4 5; do
      tmux -S "$TMUX_SOCKET" has-session -t "$SESSION" 2>/dev/null || break
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
  local ws mon_out
  ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" || return 0
  [ -n "$ws" ] || return 0
  mon_out="$ws/state/core-supervisor.json"
  if pgrep -f "core-input-watch\.py .*--socket ${TMUX_SOCKET} .*--out ${mon_out}" > /dev/null 2>&1; then
    return 0   # a monitor for this exact core is already running
  fi
  python3 "$REPO/src/core-input-watch.py" \
    --socket "$TMUX_SOCKET" --session "$SESSION" --out "$mon_out" \
    > /tmp/core-input-watch.log 2>&1 &
}

# Already running — attach if interactive, else exit cleanly. This branch
# also catches the !--restart path so re-running the script is idempotent.
if tmux -S "$TMUX_SOCKET" has-session -t "$SESSION" 2>/dev/null; then
  apply_tmux_defaults
  ensure_core_monitor   # re-ensure the supervisor monitor on every attach/re-run
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
  ensure_core_monitor   # backgrounded child survives the exec below
  exec tmux -S "$TMUX_SOCKET" new-session -A -s "$SESSION" ${CORE_ENV_ARGS[@]+"${CORE_ENV_ARGS[@]}"} ${CWD_ARGS[@]+"${CWD_ARGS[@]}"} \
    claude --name "$SESSION" ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"} --remote-control "Sutando" --dangerously-skip-permissions --add-dir "$HOME" \
    ${SETTINGS_ARGS[@]+"${SETTINGS_ARGS[@]}"} \
    -- "/schedule-crons"
else
  tmux -S "$TMUX_SOCKET" new-session -d -s "$SESSION" ${CORE_ENV_ARGS[@]+"${CORE_ENV_ARGS[@]}"} ${CWD_ARGS[@]+"${CWD_ARGS[@]}"} \
    claude --name "$SESSION" ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"} --remote-control "Sutando" --dangerously-skip-permissions --add-dir "$HOME" \
    ${SETTINGS_ARGS[@]+"${SETTINGS_ARGS[@]}"} \
    -- "/schedule-crons"
  ensure_core_monitor   # canonical session now exists — start the supervisor monitor
  echo "Started $SESSION detached. Attach via Open Core CLI in menu bar, or:"
  echo "  tmux -S $TMUX_SOCKET attach -t $SESSION"
fi
