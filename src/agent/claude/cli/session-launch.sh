#!/bin/bash
# src/agent/claude/cli/session-launch.sh — shared claude-CLI session launch
# mechanics, sourced by the core's own launcher and by a pool worker's own
# launcher (a separate script, outside core/src/). Neither caller is named or
# imported here: this file knows nothing about "core" or "worker," only about
# starting a claude session correctly. Bash-3.2-safe
# (macOS ships that by default) — no namerefs, no associative arrays; every
# function reads/writes well-known global variable names, the same idiom the
# rest of this launcher family already uses.
#
# Caller contract: source this after setting REPO, then call the functions
# below in order (each documents what it reads/sets). The literal `tmux
# new-session ... claude ... -- "$BOOT_PROMPT"` invocation and its liveness
# poll live in launch_claude_session(), which reads TMUX_SOCKET, SESSION,
# ENV_ARGS, CWD_ARGS, SURFACE_ARGS, SETTINGS_ARGS, SESSION_ARGS, BOOT_PROMPT.

# Resolve the Python interpreter (same policy as scripts/sutando-config.sh). On a
# fresh Mac there is NO system python3 — bare `python3` resolves to Apple's
# Xcode-CLT stub, which returns nothing, so the onboarding seed (which runs an
# inline python3) would silently no-op and a detached session hangs at a
# prompt it never gets to answer. Sets PY (possibly empty — callers must
# tolerate that, same contract as before this file existed).
resolve_claude_py() {
  PY=""
  if [ -r "$REPO/scripts/python-binary.sh" ]; then
    . "$REPO/scripts/python-binary.sh"
    PY="$(resolve_python "$REPO")"
  fi
}

# Sutando-friendly tmux defaults (mouse scrollback + alt-screen wheel fix).
# Idempotent: re-applying to an already-configured server is a no-op. Reads
# TMUX_SOCKET.
apply_claude_tmux_defaults() {
  command -v tmux > /dev/null 2>&1 || return 0
  tmux -S "$TMUX_SOCKET" start-server 2>/dev/null || true
  tmux -S "$TMUX_SOCKET" set-option -g mouse on 2>/dev/null || true
  # Clear any stale model pin. `setenv -u` with no -t hits tmux's DEFAULT session,
  # which on a multi-session socket is not necessarily the caller's, so target each.
  tmux -S "$TMUX_SOCKET" setenv -gu SUTANDO_CORE_MODEL 2>/dev/null || true
  _pin_sessions="$(tmux -S "$TMUX_SOCKET" list-sessions -F '#{session_name}' 2>/dev/null || true)"
  while IFS= read -r _pin_sess; do
    [ -n "$_pin_sess" ] || continue
    tmux -S "$TMUX_SOCKET" setenv -t "=$_pin_sess" -u SUTANDO_CORE_MODEL 2>/dev/null || true
  done <<< "$_pin_sessions"
  # Wheel-scroll fix (sutando-plus#46): predicate on mouse_any_flag, NOT
  # alternate_on — Claude Code 2.1.150 stopped using the alternate screen, so an
  # alt-screen predicate silently drops wheel events. mouse_any_flag asks the
  # question that actually matters: does the pane app want mouse input?
  tmux -S "$TMUX_SOCKET" bind -n WheelUpPane if-shell -F -t = '#{mouse_any_flag}' 'send-keys -M' 'copy-mode -e; send-keys -M' 2>/dev/null || true
  tmux -S "$TMUX_SOCKET" bind -n WheelDownPane send-keys -M 2>/dev/null || true
}

# A named session is alive when the tmux session EXISTS and a `claude --name
# <name>` process is running under it. Do NOT gate on the pane's current
# foreground command: a healthy session mid-tool shows bash/python3/node/etc,
# so a pane-command match would falsely report it dead. Reads TMUX_SOCKET,
# SESSION.
claude_named_pids() {
  # -a: BSD/macOS pgrep excludes the caller's ANCESTORS by default, so when
  # this script runs from inside the very session it's checking (e.g. an
  # in-session restart), the live process would otherwise be invisible.
  # `read -r pid _` tolerates procps -a's "pid cmdline" output too.
  pgrep -ax claude 2>/dev/null | while read -r pid _; do
    args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    case "$args" in
      *"--name $SESSION"*|*"--name=$SESSION"*) echo "$pid" ;;
    esac
  done
}

claude_named_process_running() {
  [ -n "$(claude_named_pids)" ]
}

claude_named_tmux_session_exists() {
  command -v tmux > /dev/null 2>&1 || return 1
  tmux -S "$TMUX_SOCKET" has-session -t "$SESSION" 2>/dev/null
}

claude_named_session_running() {
  claude_named_tmux_session_exists || return 1
  claude_named_process_running
}

# Optional working-directory override. Unset: no override, launches from cwd.
# Set: canonicalize once (physical absolute path — Claude Code keys the
# folder-trust dialog and the project/auto-memory slug off getcwd() with
# symlinks resolved) and reuse everywhere — mkdir, tmux `-c`, AND the
# trust-dialog seed below all need the identical value. Reads
# SUTANDO_CLAUDE_WORKING_DIR; sets CWD_ARGS and re-exports the canonicalized
# SUTANDO_CLAUDE_WORKING_DIR.
resolve_claude_cwd_args() {
  CWD_ARGS=()
  if [ -n "${SUTANDO_CLAUDE_WORKING_DIR:-}" ]; then
    _cwd_exp="${SUTANDO_CLAUDE_WORKING_DIR/#\~/$HOME}"
    mkdir -p "$_cwd_exp" || { echo "  ✗ can't create working dir: $_cwd_exp" >&2; exit 1; }
    SUTANDO_CLAUDE_WORKING_DIR="$(cd "$_cwd_exp" && pwd -P)"
    export SUTANDO_CLAUDE_WORKING_DIR
    CWD_ARGS=(-c "$SUTANDO_CLAUDE_WORKING_DIR")
    echo "  ✓ working dir: $SUTANDO_CLAUDE_WORKING_DIR"
  fi
}

# Resolve workspace-scoped CLAUDE_CONFIG_DIR and seed the onboarding/theme/
# trust/bypass-mode state a detached, no-TTY session can never answer
# interactively. Reads REPO, PY, SUTANDO_CLAUDE_WORKING_DIR (already
# canonicalized by resolve_claude_cwd_args), SUTANDO_ACCEPT_BYPASS_PERMISSIONS.
# Sets/exports CLAUDE_CONFIG_DIR. Exits 1 on an unrecoverable resolve failure
# (mirrors the pre-extraction behavior exactly — callers ran under `set -e`).
resolve_claude_config_dir_and_seed() {
  source "$REPO/src/claude_config_dir.sh"
  if _ccd="$(resolve_claude_config_dir "$REPO" start-cli)"; then
    mkdir -p "$_ccd"
    export CLAUDE_CONFIG_DIR="$_ccd"
    echo "  ✓ CLAUDE_CONFIG_DIR=$_ccd"
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
    with open(os.path.join(os.path.expanduser("~"), ".claude.json")) as f:
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
# Claude-in-Chrome onboarding seed: a session launched with --chrome shows a
# "Claude in Chrome" acknowledgement prompt on first run in a fresh scoped
# config, which --dangerously-skip-permissions does not bypass. Pre-accept it
# the same way as hasCompletedOnboarding.
if cfg.get("hasCompletedClaudeInChromeOnboarding") is not True:
    cfg["hasCompletedClaudeInChromeOnboarding"] = True
    changed = True
    chrome_seeded = True
if cfg.get("theme") is None and glob.get("theme") is not None:
    cfg["theme"] = glob["theme"]
    changed = True
# Trust-seed for the explicitly-configured working dir. Claude Code keys the
# folder-trust dialog on projects[<abs cwd>].hasTrustDialogAccepted; a fresh
# scoped config lacks it for a custom cwd, so a detached session would hang on
# the prompt. Only pre-trust the one dir the caller explicitly chose.
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
# Dangerous-mode seed (env-gated, detached-only). A session launched with
# --dangerously-skip-permissions shows a "Bypass Permissions mode / Yes, I
# accept" prompt on first run in a fresh scoped config, which that flag does
# NOT bypass — a detached no-TTY session hangs on it forever. Pre-accept by
# seeding skipDangerousModePermissionPrompt in <ccd>/settings.json, gated on
# the dedicated SUTANDO_ACCEPT_BYPASS_PERMISSIONS opt-in so only a caller that
# explicitly asked for headless auto-accept gets it.
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
    # 2 = caller already scoped the config dir; nothing to seed, still reaches
    # the intended credential store.
    [ "$_ccd_rc" = "2" ] || exit 1
    echo "  ✓ CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR (caller-provided; config helper absent)"
  fi
}

# One `--settings` flag carries every hook a headless session needs (multiple
# --settings flags are undocumented/last-wins, so compose into a single JSON):
#  * AskUserQuestion guard — ALWAYS registered. No interactive user can answer
#    it, so a PreToolUse `deny` short-circuits the call instead of hanging.
#  * obs collector hooks — added to the SAME JSON only when an export
#    endpoint is set ($SUTANDO_OBS_ENDPOINT).
# Built by node helpers, not shell string interpolation (a $REPO with a space
# or a `"` broke hand-rolled interpolation in the past). Reads REPO. Sets
# SETTINGS_ARGS and exports SUTANDO_OBS_ENDPOINT.
resolve_claude_settings_args() {
  OBS_ENDPOINT="${SUTANDO_OBS_ENDPOINT:-}"
  export SUTANDO_OBS_ENDPOINT="$OBS_ENDPOINT"
  SETTINGS_ARGS=()
  if ! command -v node > /dev/null 2>&1; then
    echo "session hooks: node unavailable — cannot safely build --settings JSON; AskUserQuestion guard + obs disabled this session" >&2
    return 0
  fi
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
  CLAUDE_SETTINGS_JSON="$(node "$REPO/src/agent/claude/cli/build-core-settings.mjs" "$REPO/hooks/skip-ask-user-question.py" "$OBS_JSON" "$REPO/hooks/skill-usage-telemetry.py" "$REPO/hooks/gmail-write-guard.py")"
  if [ -n "$CLAUDE_SETTINGS_JSON" ]; then
    SETTINGS_ARGS=(--settings "$CLAUDE_SETTINGS_JSON")
    echo "session hooks: AskUserQuestion guard registered (PreToolUse deny — a headless session can't answer it)"
  else
    echo "session hooks: settings build failed — AskUserQuestion guard NOT registered this session" >&2
  fi
}

# Claude Code's native OTel token+cost metrics. Hooks give obs EVENTS but no
# tokens, so when an export endpoint is set also turn on CC telemetry and
# point its OTLP exporter at the collector. Metrics only (no logs/traces, so
# hooks stay the sole event source). Honors any pre-set OTEL_* so a real
# backend is never replaced. Reads SUTANDO_OBS_METRICS_ENDPOINT / OBS_ENDPOINT
# (set by resolve_claude_settings_args).
apply_claude_obs_metering() {
  METRICS_ENDPOINT="${SUTANDO_OBS_METRICS_ENDPOINT:-${OBS_ENDPOINT:-}}"
  if [ -n "$METRICS_ENDPOINT" ] && [ -z "${OTEL_EXPORTER_OTLP_ENDPOINT:-}" ]; then
    export CLAUDE_CODE_ENABLE_TELEMETRY=1
    export OTEL_METRICS_EXPORTER=otlp
    export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
    export OTEL_EXPORTER_OTLP_ENDPOINT="$METRICS_ENDPOINT"
    export OTEL_METRIC_EXPORT_INTERVAL="${OTEL_METRIC_EXPORT_INTERVAL:-10000}" # ms; 10s (CC default 60s)
    echo "obs metering: → $METRICS_ENDPOINT/v1/metrics (CC OTel token+cost, every ${OTEL_METRIC_EXPORT_INTERVAL}ms)"
  fi
}

# Route through the credential proxy when one is live (quota telemetry,
# #2211/#2288). Guarded twice: honor a caller-set ANTHROPIC_BASE_URL, and only
# wire up when a LISTENer actually holds the proxy port — never point at a
# dead port. Exports ANTHROPIC_BASE_URL when applicable; caller decides
# whether/how to forward it into a new session's env (same
# `[ -n "${ANTHROPIC_BASE_URL:-}" ]` check as before this file existed).
resolve_claude_credential_proxy() {
  _proxy_listener_up() {
    lsof -nP -iTCP:7846 -sTCP:LISTEN > /dev/null 2>&1
  }
  if [ -z "${ANTHROPIC_BASE_URL:-}" ]; then
    # A loaded launchd job means the proxy is EXPECTED on this host even when
    # its listener hasn't bound yet.
    PROXY_EXPECTED=""
    if launchctl print "gui/$(id -u)/com.sutando.credential-proxy" > /dev/null 2>&1; then
      PROXY_EXPECTED=1
    fi
    if [ -n "$PROXY_EXPECTED" ]; then
      # Bounded wait (~10s): a supervised proxy can bind seconds after this
      # session on a cold boot; a one-shot check would leave it unrouted for life.
      for _ in $(seq 1 20); do
        _proxy_listener_up && break
        sleep 0.5
      done
    fi
    if _proxy_listener_up; then
      export ANTHROPIC_BASE_URL=http://localhost:7846
    elif [ -n "$PROXY_EXPECTED" ]; then
      echo "  ⚠ credential proxy expected (launchd job loaded) but :7846 never bound within ~10s — session runs unrouted this launch (no proxy protection, no quota telemetry)" >&2
    fi
  fi
}

# Any installed skill's manifest.json "config" block, forwarded the same way
# every other env var in a caller's own env-args array is. Set-ness wins, not
# non-emptiness: an explicit empty value must not be re-filled from a
# manifest. Appends to the global array SKILL_MANIFEST_ENV_ARGS (initialized
# here) — the caller merges that into its own env-args array afterward, same
# bash-3.2-safe "shared array by well-known name" idiom as everything else in
# this file. Reads REPO, PY.
forward_skill_manifest_config() {
  SKILL_MANIFEST_ENV_ARGS=()
  declare -F skill_manifest_config_pending >/dev/null || return 0
  _mc_seen=" "
  while IFS= read -r -d '' _mcrec; do
    _mck=${_mcrec%%=*}
    _mcv=${_mcrec#*=}
    [ -n "$_mck" ] || continue
    case "$_mck" in
      [!A-Za-z_]* | *[!A-Za-z0-9_]*) continue ;;
    esac
    case "$_mc_seen" in
      *" $_mck "*)
        # Every other rejection in this path reports itself; a duplicate must
        # too, or the losing skill's value vanishes by glob order alone.
        printf 'skill-manifest-config: %s declared by more than one skill; keeping the first\n' \
          "$_mck" >&2
        continue
        ;;
    esac
    _mc_seen="$_mc_seen$_mck "
    if [ "${!_mck+x}" = x ]; then
      SKILL_MANIFEST_ENV_ARGS+=(-e "$_mck=${!_mck}")
    else
      export "$_mck=$_mcv"
      SKILL_MANIFEST_ENV_ARGS+=(-e "$_mck=$_mcv")
    fi
  done < <(skill_manifest_config_pending "$REPO" "$PY")
  unset _mc_seen _mcrec
}

# Registers the PERSONAL_CLAUDE.md compaction-reinject hook. Idempotent.
install_claude_personal_hook() {
  bash "$REPO/scripts/install-personal-claude-hook.sh" || echo "session-launch: personal-claude hook install failed (rc=$?) — hook may be absent" >&2
}

# Creates a new tmux session running claude with the fully-assembled args, then
# polls up to ~5s for it to actually come up (a `new-session` accepting the
# command proves tmux liked it, not that the child process is alive). Reads
# TMUX_SOCKET, SESSION, ENV_ARGS, CWD_ARGS, SURFACE_ARGS, SETTINGS_ARGS,
# SESSION_ARGS, BOOT_PROMPT. Returns 0 if the session came up live, 1 otherwise
# (caller decides what that means — abort, retry, or a heal path).
launch_claude_session() {
  tmux -S "$TMUX_SOCKET" new-session -d -s "$SESSION" ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} ${CWD_ARGS[@]+"${CWD_ARGS[@]}"} \
    claude --name "$SESSION" ${SURFACE_ARGS[@]+"${SURFACE_ARGS[@]}"} --dangerously-skip-permissions --add-dir "$HOME" \
    ${SETTINGS_ARGS[@]+"${SETTINGS_ARGS[@]}"} ${SESSION_ARGS[@]+"${SESSION_ARGS[@]}"} \
    -- "$BOOT_PROMPT" || true
  for _ in $(seq 1 25); do
    claude_named_session_running && return 0
    sleep 0.2
  done
  return 1
}
