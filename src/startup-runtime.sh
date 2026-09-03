#!/bin/bash
# Runtime/credential decisions shared by startup and behavior-level tests.

# reap_stale_task_watcher() resolves sentinel ownership through this helper, so
# the dependency is declared here rather than left to each caller's source order
# — a consumer that sourced only this file got `command not found` at reap time.
# shellcheck source=watcher_sentinel.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/watcher_sentinel.sh"

# Resolve the selected core before startup touches runtime-specific credentials.
# The normal .env load happens later in configure_startup_runtime(); use a
# subshell here so an invocation-scoped SUTANDO_CORE_RUNTIME stored there still
# participates without exposing every .env value earlier than before.
# The durable repo that supplies this src/ — a symlinked bundle wrapper must not
# answer this question. Located relative to THIS file, resolved inside the helper.
# A missing helper must fail LOUDLY: silently continuing leaves every _repo empty,
# so .env never loads and credentials vanish with no error — worse than the bug.
# shellcheck source=src/repo_root.sh
if ! . "$(dirname "${BASH_SOURCE[0]}")/repo_root.sh" 2>/dev/null; then
  echo "FATAL: src/repo_root.sh not found next to startup-runtime.sh" >&2
  return 1 2>/dev/null || exit 1
fi

resolve_startup_core_runtime() {
  local _repo
  _repo="$(sutando_repo_root)"
  (
    if [ -f "$_repo/.env" ]; then
      set -a
      # shellcheck disable=SC1091
      source "$_repo/.env"
      set +a
    fi
    bash "$_repo/scripts/sutando-config.sh" core-runtime 2>/dev/null
  ) || true
}

claude_auth_carry_enabled() {
  [ "${1:-claude}" = "claude" ]
}

# Fail before services launch when the SELECTED core cannot authenticate.
# Claude keeps its existing rich auth-preflight gate. Codex uses the same
# configured CODEX_HOME as its launcher, then asks the CLI itself for status.
preflight_selected_core_auth() {
  local _runtime="${1:-claude}" _claude_config_dir="${2:-}"
  local _repo _config_env _config_value
  _repo="$(sutando_repo_root)"

  case "$_runtime" in
    claude)
      if [ -n "$_claude_config_dir" ]; then
        bash "$_repo/src/auth-preflight-gate.sh" "$_claude_config_dir"
      fi
      ;;
    codex)
      _config_env="$(bash "$_repo/scripts/sutando-config.sh" core-config-dir-env-name codex)" || {
        echo "startup: could not resolve the Codex config-dir environment" >&2
        return 1
      }
      _config_value="$(bash "$_repo/scripts/sutando-config.sh" core-config-dir-value codex)" || {
        echo "startup: could not resolve the Codex config directory" >&2
        return 1
      }
      if [ -n "$_config_env" ] && [ -n "$_config_value" ]; then
        mkdir -p "$_config_value"
        export "$_config_env=$_config_value"
      fi
      if [ "${SUTANDO_SKIP_AUTH_PREFLIGHT:-0}" = "1" ]; then
        echo "codex-auth-preflight: skipped (SUTANDO_SKIP_AUTH_PREFLIGHT=1)"
        return 0
      fi
      if ! command -v codex >/dev/null 2>&1; then
        echo "startup: Codex CLI is not installed — install it, run 'codex login', then retry" >&2
        return 127
      fi
      if ! codex login status >/dev/null 2>&1; then
        echo "startup: Codex CLI is not authenticated for ${CODEX_HOME:-the configured Codex home} — run 'codex login' and retry" >&2
        return 1
      fi
      echo "codex-auth-preflight: OK — ${CODEX_HOME:-the configured Codex home} can boot authenticated"
      ;;
    *)
      echo "startup: unsupported core runtime: $_runtime" >&2
      return 2
      ;;
  esac
}

# True when the canonical managed-credentials file carries a usable voice key.
#
# Deliberately reads the JSON here rather than calling resolveCredential() from
# src/credential-resolver.ts: a bare app-bundle runtime ships `node` only — no
# tsx, npx or node_modules (see startup.sh's run_tsx notes) — and that bundle is
# exactly the managed/AU install this tier serves. A gate that shelled out to the
# TS resolver would fail on the one install type it exists to enable.
#
# The workspace itself is NOT re-derived: sutando-config.sh is the same canonical
# resolver resolveWorkspace() uses, so there is no second copy of the workspace
# fallback chain. Only the slot-lookup rule is restated, and
# tests/startup-voice-gate.test.sh pins it to the same fixtures as the TS
# resolver so the two cannot drift apart silently.
# Path of the canonical managed-credentials file, resolved through
# sutando-config.sh (the same canonical resolver resolveWorkspace() uses).
# Prints the path; returns 1 when the workspace cannot be resolved.
_voice_managed_credentials_file() {
  local _repo _ws
  _repo="$(sutando_repo_root)"
  _ws="$(bash "$_repo/scripts/sutando-config.sh" workspace 2>/dev/null)" || return 1
  [ -n "$_ws" ] || return 1
  printf '%s\n' "$_ws/state/auth/managed-credentials.json"
}

# Resolve an interpreter that actually RUNS for the credential-gate JSON reads.
# A bare `python3` on a fresh Mac resolves to the Xcode Command Line Tools
# stub, which exits non-zero — and the gate returns 1 on any failure, so a
# stub would be indistinguishable from "no managed credential configured".
# That is a silent wrong answer, not a crash: startup would proceed BYO-only
# while a managed credential sat on disk.
#
# PROBE, don't path-match. `command -v` finds the stub because the stub IS on
# PATH; only running it tells you whether it works.
#
# ORDER comes from scripts/sutando-config.sh, asked for rather than restated.
# An earlier version of this gate probed `command -v python3` and then Homebrew
# and stopped there, skipping the two tiers that actually matter on a bundled
# install: $SUTANDO_PY and <engine>/runtime/python. A host with a broken
# `python3` first on PATH and a valid $SUTANDO_PY therefore concluded "no usable
# python3" and left voice disabled while a managed credential sat on disk —
# exactly the silent managed-user outage this gate exists to prevent, and
# inconsistent with the workspace lookup in _voice_managed_credentials_file,
# which resolves fine because sutando-config.sh honours $SUTANDO_PY internally.
#
# `python-bin` is consulted FIRST so the precedence has one definition. The
# explicit tiers after it are a fallback for the case where that script cannot
# run at all; Homebrew stays last, beyond the canonical order, because it was
# added for a real host and dropping it would regress that case. brew's
# location is asked for, never written down (REVIEW.md hardcoded-paths).
#
# Prints the interpreter path; returns 1 (printing nothing) when no candidate
# is usable. Callers own the loud warning — silence at the CALL SITE is the
# defect the stub tests pin.
_voice_gate_python() {
  local _repo
  _repo="$(sutando_repo_root)"
  _usable_python() {
    # No `-x` test: `python-bin` may return a bare command name, and a name that
    # is not on PATH simply fails to execute. Running it IS the test.
    [ -n "${1:-}" ] && "$1" -c 'import json' >/dev/null 2>&1
  }
  local _py="" _cand _brew=""
  command -v brew >/dev/null 2>&1 && _brew="$(brew --prefix 2>/dev/null)/bin/python3"
  for _cand in \
      "$(bash "$_repo/scripts/sutando-config.sh" python-bin 2>/dev/null)" \
      "${SUTANDO_PY:-}" \
      "$_repo/../runtime/python/bin/python3" \
      "$(command -v python3 2>/dev/null)" \
      "$_brew"; do
    if _usable_python "$_cand"; then
      _py="$_cand"
      break
    fi
  done
  [ -n "$_py" ] || return 1
  printf '%s\n' "$_py"
}

# The committed voice credential-source preference (design 2b; amendment S1).
# Prints exactly one of: managed | byok | unset. Every failure mode — no
# workspace, no file, no usable python, malformed JSON, out-of-vocabulary
# value — prints "unset": the legacy resolution every pre-preference install
# runs under. The quarantine/byok enforcement itself never rides on this
# helper alone: _managed_voice_credential_present re-checks the marker file
# directly, so an unreadable preference degrades to legacy behavior, never to
# a managed key satisfying an explicit BYOK/quarantined state.
_voice_credential_preference() {
  local _file _py
  _file="$(_voice_managed_credentials_file)" || { echo "unset"; return 0; }
  [ -f "$_file" ] || { echo "unset"; return 0; }
  _py="$(_voice_gate_python)" || { echo "unset"; return 0; }
  "$_py" - "$_file" <<'PY' 2>/dev/null || echo "unset"
import json, sys
try:
    with open(sys.argv[1]) as fh:
        doc = json.load(fh)
    pref = doc.get("voicePreference") if isinstance(doc, dict) else None
except Exception:
    pref = None
print(pref if pref in ("managed", "byok") else "unset")
PY
}

_managed_voice_credential_present() {
  local _file _py
  _file="$(_voice_managed_credentials_file)" || return 1
  [ -f "$_file" ] || return 1

  # Mirrors CAPABILITY_FALLBACKS['gemini-voice'] = ['gemini-voice','gemini-text'].
  # Malformed/unreadable files skip the tier rather than throwing, matching
  # readManaged()'s try/catch contract. Interpreter resolution lives in
  # _voice_gate_python (probe-first, canonical order); the LOUD warning stays
  # here because "no usable python" must never read as "no managed credential".
  if ! _py="$(_voice_gate_python)"; then
    echo "  ~ managed-credential gate: no usable python3 —" \
         "cannot read $_file; treating as UNKNOWN, not as absent" >&2
    return 1
  fi

  "$_py" - "$_file" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as fh:
        doc = json.load(fh) or {}
    caps = doc.get("capabilities") or {}
    if not isinstance(caps, dict):
        raise ValueError("capabilities is not an object")
except Exception:
    sys.exit(1)
# S1 truth table (design 2b): a quarantined file's entries are ABSENT in every
# mode (signed-out quarantine — the token stays on disk for later renewal but
# must never satisfy a consumer), and under an explicit BYOK preference the
# managed tier is skipped entirely. Same guards as the TS/python resolvers and
# health-check.py; tests/voice-preference-consumers.test.sh pins the agreement.
if doc.get("quarantined") is True or doc.get("voicePreference") == "byok":
    sys.exit(1)
for slot in ("gemini-voice", "gemini-text"):
    entry = caps.get(slot)
    key = entry.get("key") if isinstance(entry, dict) else None
    if isinstance(key, str) and key:
        sys.exit(0)
sys.exit(1)
PY
}

configure_startup_runtime() {
  # Repo-relative, not cwd-relative: the app bundle invokes startup from its own
  # working directory, where a bare `.env` silently resolves to nothing.
  local _repo
  _repo="$(sutando_repo_root)"
  if [ -f "$_repo/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$_repo/.env"
    set +a
  else
    echo "  ~ .env not found — continuing with credential-free services"
  fi

  # Order mirrors resolveCredential() under the S1 truth table (design 2b):
  # an explicit `voicePreference: managed` is decided by the managed gate
  # ALONE, otherwise managed tier and BYO env both enable (only the *reason*
  # differs). tests/voice-preference-consumers.test.sh pins this against the
  # resolver, health-check.py, and the desktop supervisor's spawn-env twin.
  local _voice_pref
  _voice_pref="$(_voice_credential_preference)"
  if [ "$_voice_pref" = "managed" ]; then
    # S1: ONLY a non-quarantined managed entry satisfies a managed
    # preference — a present env key must NOT silently satisfy it (that is
    # the logout-quarantine bypass the design closes: quarantined managed
    # entries with a leftover BYO env key would otherwise boot voice).
    if _managed_voice_credential_present; then
      unset SKIP_VOICE
      echo "  ✓ voice agent enabled (managed credentials)"
    else
      export SKIP_VOICE=1
      echo "  ~ voice agent disabled (voicePreference=managed but no usable" \
           "managed credential — quarantined or missing in" \
           "<workspace>/state/auth/managed-credentials.json; sign in to renew" \
           "the managed key or switch the preference to BYOK)"
    fi
  elif [ -n "${GEMINI_VOICE_API_KEY:-${GEMINI_API_KEY:-}}" ]; then
    unset SKIP_VOICE
  elif _managed_voice_credential_present; then
    unset SKIP_VOICE
    echo "  ✓ voice agent enabled (managed credentials)"
  else
    export SKIP_VOICE=1
    # Say WHY, and name the two ways out — a gate that disables a feature without
    # explaining itself is the screen-capture failure mode repeated.
    if [ "$_voice_pref" = "byok" ]; then
      echo "  ~ voice agent disabled (BYOK preference set (managed entries" \
           "ignored); set GEMINI_VOICE_API_KEY or GEMINI_API_KEY)"
    else
      echo "  ~ voice agent disabled (no managed credentials in" \
           "<workspace>/state/auth/managed-credentials.json; set GEMINI_VOICE_API_KEY" \
           "or GEMINI_API_KEY for a BYO key)"
    fi
  fi
}

phone_stack_enabled() {
  [ "${SKIP_PHONE:-}" != "1" ] && [ "${SKIP_VOICE:-}" != "1" ]
}

# Voice-agent (:9900) must NOT go through startup.sh's generic
# reap_wedged_listener: `lsof -ti :9900 | xargs kill` signals whatever owns the
# port on a port match alone — exactly the unvalidated-kill class the
# voice-reliability plan removes (amendments S4/T4/U1). The wedge probe is the
# same real-HTTP liveness check, but the kill-and-replace transaction is
# delegated to ONE guarded `voice-lock.py takeover` invocation: validate
# identity (lock pid = :9900 LISTEN pid, realpath'd entry shape, startTimeMs)
# → TERM → wait → KILL → revalidate → unlink, all under the held fcntl guard.
# Identity mismatch or an unknown/absent lock ⇒ takeover-blocked: nothing is
# signaled, the lock is left untouched, and the start path just reports the
# port occupied. Interpreter unavailable ⇒ fail closed (skip the reap, warn) —
# never signal without validation.
#
# Expects REPO, WORKSPACE and PY (resolved interpreter, may be empty) from the
# caller. Lives here (sourceable) so the wedge-recovery test runs the real
# function instead of a copy.
reap_wedged_voice_agent() {
  local port="$1" rc=0 out
  lsof -i :"$port" -sTCP:LISTEN > /dev/null 2>&1 || return 0
  curl -s -o /dev/null -m 10 "http://127.0.0.1:$port/__liveness_probe__" || rc=$?
  [ "$rc" -eq 28 ] || return 0
  echo "  ⚠ voice-agent (port $port) listening but unresponsive — attempting guarded takeover"
  if [ -z "${PY:-}" ]; then
    echo "  ⚠ no usable python3 for the guarded voice lock helper — not killing blindly (fail closed)"
    return 0
  fi
  if out="$("$PY" "$REPO/scripts/voice-lock.py" takeover \
      --pidfile "$(bash "$REPO/scripts/sutando-config.sh" voice-pidfile "$WORKSPACE")" \
      --guard "$WORKSPACE/.voice-agent.lock.guard" \
      --workspace "$WORKSPACE" \
      --mode adopted --port "$port" \
      --entry "$REPO/src/voice-agent.ts" \
      --entry "$REPO/dist/voice-agent.js" 2>&1)"; then
    echo "  ✓ guarded takeover of wedged voice-agent: $out"
    sleep 1
  else
    echo "  ⚠ guarded takeover blocked/failed — leaving the listener untouched (a live lock is never removed): $out"
  fi
  return 0
}

# A pid alone cannot say WHICH watcher it names: the OS reissues the numbers of
# exited processes, so a live watcher can wear a dead predecessor's pid and match
# both the value in the sentinel and the `ps` argv check. Ownership is resolved by
# src/watcher_sentinel.sh, which asks the OS whether the process is old enough to
# have written the file. Nothing here decides ownership locally.
reap_stale_task_watcher() {
  local pid_file="$1" stale_pid
  [ -f "$pid_file" ] || return 0
  stale_pid="$(cat "$pid_file" 2>/dev/null || true)"

  # `ps` failing is NOT "the pid is not a watcher". A denied or unavailable ps
  # skipped the ownership check entirely and still fell through to the release
  # below, deleting a live watcher's sentinel on a pid-byte match.
  local ps_err ps_out ps_rc=0
  ps_err="$(mktemp)"
  ps_out="$(ps -p "$stale_pid" -o args= 2>"$ps_err")" || ps_rc=$?
  if [ -s "$ps_err" ]; then
    echo "  ⚠ cannot determine whether pid $stale_pid is a watcher (ps: $(head -1 "$ps_err")); leaving the sentinel alone"
    rm -f "$ps_err"
    return 0
  fi
  rm -f "$ps_err"

  if [ -n "$stale_pid" ] && printf '%s' "$ps_out" | grep -q "watch-tasks-stream"; then
    # A watcher younger than the sentinel did not write it, so it is a NEW
    # watcher on a reissued pid — signalling it would kill a live drain.
    # errexit-safe: a bare call here terminates startup.sh (set -e) on rc 1/2
    # before either branch below can run.
    local owned_rc=0
    sentinel_pid_wrote_file "$stale_pid" "$pid_file" || owned_rc=$?
    if [ "$owned_rc" -eq 1 ]; then
      echo "  ⚠ pid $stale_pid is a watcher but started AFTER this sentinel — reissued pid, not its owner; leaving both alone"
      return 0
    fi
    if [ "$owned_rc" -ne 0 ]; then
      # Unmeasurable ownership is not permission. Killing here reaped a live drain.
      echo "  ⚠ pid $stale_pid is a watcher but its ownership of the sentinel is UNMEASURABLE; leaving both alone"
      return 0
    fi
    kill "$stale_pid" 2>/dev/null || true
    echo "  ✓ reaped stale watch-tasks-stream watcher (pid $stale_pid)"
  fi

  sentinel_release_if_owner "$pid_file" "$stale_pid"
  if [ -f "$pid_file" ]; then
    echo "  ⚠ watch-tasks-stream sentinel changed under the reap — a live watcher owns it, leaving it in place"
  fi
  return 0
}

# Remote gateway bridge (optional channel — generic, same shape as the discord/
# telegram/slack blocks in startup.sh). Config + token live in the channel
# .env, resolved via the same claude-home-path helper; the bridge itself ships
# in src/ (provider-neutral, like the others). Relay protocol:
# docs/remote-gateway-protocol.md. Deliberately silent when unconfigured — a
# Sutando-only user never sees it. Back-compat: also detect/honor a legacy
# AG2_REMOTE_* token written to the repo .env by older onboarding, so existing
# agents keep reconnecting after this lands (until they re-onboard onto
# channels/ag2space/.env).
#
# Extracted from startup.sh (was inline) so it can be invoked WITHOUT the rest
# of startup.sh's boot sequence — in particular without reap_stale_task_watcher
# above, which unconditionally kills any live watcher on the assumption that
# startup.sh runs once, at the very start of a session, before anything else
# starts a watcher. Reconnecting a dropped gateway lane mid-session (the named-
# instance lanes are NOT durable across a plain supervisor restart — see the
# named-gateway loop below) used to mean re-running the WHOLE of startup.sh to
# reach this block, which reaped a live, mid-session watcher as a side effect.
# scripts/restart-gateway-lanes.sh calls only this function.
#
# Requires REPO, PY (resolved via scripts/python-binary.sh) and LOGS_DIR set
# by the caller — same contract as every other block in startup.sh.
start_gateway_lanes() {
  local _RELAY_ENV
  if _RELAY_ENV="$(bash "$REPO/scripts/sutando-config.sh" claude-home-path channels/ag2space/.env)"; \
     { [ -f "$_RELAY_ENV" ] && grep -qE "^(REMOTE_TASK_TOKEN|AG2_REMOTE_TOKEN)=" "$_RELAY_ENV" 2>/dev/null; } \
     || [ -n "${REMOTE_TASK_TOKEN:-}${AG2_REMOTE_TOKEN:-}" ]; then
    [ -f "$_RELAY_ENV" ] && { set -a; . "$_RELAY_ENV"; set +a; }
    # Tell the bridge where the durable token lives so auth-rejection recovery
    # (revoked/expired key) can re-read it after the connect flow rewrites it —
    # hot-swap instead of a supervisor crash-loop. Only when the file exists:
    # env-only onboarding has no file to watch and keeps the FATAL-exit path.
    [ -f "$_RELAY_ENV" ] && export REMOTE_TASK_TOKEN_FILE="$_RELAY_ENV"
    # Map legacy AG2_REMOTE_* → REMOTE_TASK_* (the names the bridge reads). The
    # legacy token may be the combined "url|secret" form, which the bridge splits.
    REMOTE_TASK_TOKEN="${REMOTE_TASK_TOKEN:-${AG2_REMOTE_TOKEN:-}}"
    # Default tier is "owner" for the personal-agent model (2026-07-08): a user's
    # own gateway authenticates with their own owner bearer and the broker
    # owner-scopes every pull, so its tasks are the owner's own (e.g. voice
    # delegations). Must match the bridge's own default — otherwise startup.sh
    # would export a value and the bridge's default never fires. A shared /
    # multi-user gateway sets REMOTE_TASK_TIER=team explicitly.
    REMOTE_TASK_TIER="${REMOTE_TASK_TIER:-${AG2_REMOTE_TIER:-owner}}"
    # AG2 Space's gateway tags inbound image/file markers `ag2space-media` (its
    # media-proxy at {gateway}/v1/media/...). The provider-neutral bridge defaults
    # its marker tag to `remote-media`, so without this the marker never matches and
    # the media URL lands in the task body unresolved — the core can't see the image
    # (owner-reported 2026-07-25). Default it to the AG2 tag here, in the AG2-specific
    # launch block, so the generic package carries no provider string. Explicit
    # REMOTE_MEDIA_MARKER (e.g. from the channel .env) still wins. The launchd
    # wrapper defaults it too — launchd jobs never see this shell's exports.
    REMOTE_MEDIA_MARKER="${REMOTE_MEDIA_MARKER:-ag2space-media}"
    export REMOTE_TASK_TOKEN REMOTE_TASK_TIER REMOTE_MEDIA_MARKER
    # Prefer the launchd-supervised job (RunAtLoad + KeepAlive) so the bridge
    # AUTO-RESTARTS on crash/kill instead of dying silently. Falls back to the
    # bare launch below on checkouts lacking the template or if install fails.
    local _gw_supervised=0
    local _GW_LABEL="com.sutando.gateway-bridge"
    local _GW_INSTALLER="$REPO/src/install-gateway-bridge-launchd.sh"
    # Ask launchd about its OWN job, never argv: every named-instance bridge
    # shares one argv and instance identity lives only in env, so a pgrep here
    # reports a dead job as healthy whenever any other bridge is alive.
    _gw_job_pid() {
      launchctl list 2>/dev/null | awk -v l="$_GW_LABEL" '$3 == l && $1 ~ /^[0-9]+$/ { print $1; exit }'
    }
    if [ -f "$_GW_INSTALLER" ] && [ -f "$REPO/src/launchd/$_GW_LABEL.plist" ]; then
      if launchctl print "gui/$(id -u)/$_GW_LABEL" > /dev/null 2>&1; then
        if [ -n "$(_gw_job_pid)" ]; then
          echo "  ✓ gateway bridge (launchd-supervised)"
          _gw_supervised=1
        else
          # Loaded is not the same as running: the wrapper exits 0 when the token
          # is removed, leaving an idle job. Bring it back when credentials return.
          launchctl kickstart -k "gui/$(id -u)/$_GW_LABEL" > /dev/null 2>&1 || true
          for _ in $(seq 1 12); do
            [ -n "$(_gw_job_pid)" ] && { _gw_supervised=1; break; }
            sleep 1
          done
          if [ "$_gw_supervised" = "1" ]; then
            echo "  ✓ gateway bridge (launchd-supervised, recovered idle job)"
          else
            echo "  ⚠ gateway-bridge launchd job is loaded but idle — falling back to bare launch"
          fi
        fi
      else
        echo "  Installing launchd-supervised gateway bridge..."
        if bash "$_GW_INSTALLER" install > /dev/null 2>&1; then
          echo "  ✓ gateway bridge (launchd-supervised) — auto-restarts on death"
          _gw_supervised=1
        else
          echo "  ⚠ gateway-bridge launchd install failed — falling back to bare launch"
        fi
      fi
    fi
    if [ "$_gw_supervised" = "0" ]; then
    # Always spawn; the bridge's own unsuffixed singleton lock self-defers a
    # duplicate. The previous `pgrep -f remote-gateway-bridge` guard was a P1
    # (john, PR review 2026-08-02): every named-instance bridge shares the SAME
    # argv, so a live secondary satisfied the pgrep and suppressed restart of a
    # DEAD primary indefinitely. Instance identity lives only in env, which
    # pgrep cannot see — the lock (role `gateway-bridge`, per-instance suffixed)
    # is the only process-identity source that can arbitrate this.
    # SUTANDO_SUPERVISED=1 marks the launch as supervised (stdout persisted by
    # the redirect below); the bridge stamps launched_via into gateway-status
    # and skips its own bare-launch file log. See remote_gateway_bridge._log.
    if [ -n "$PY" ]; then
      SUTANDO_SUPERVISED=1 "$PY" "$REPO/src/remote-gateway-bridge.py" >> "$LOGS_DIR/remote-gateway-bridge.log" 2>&1 &
      echo "  ✓ gateway bridge (self-defers if already running)"
    else
      echo "  ⊘ gateway bridge skipped — no runnable python3"
    fi
    fi

    # Named secondary gateways (multi-gateway): every AG2_REMOTE_TOKEN_<INST> in
    # the environment launches one extra bridge for that gateway (e.g.
    # AG2_REMOTE_TOKEN_DEV → instance "dev" against the dev homeserver). Each
    # instance gets its own lock role + state files via GATEWAY_INSTANCE, and
    # deliberately inherits NO REMOTE_PROACTIVE_ROOM — proactive nudges stay with
    # the primary (owner-DM) gateway only.
    # No pgrep dedupe here (env vars are invisible to pgrep -f): the bridge's own
    # per-instance singleton lock (role gateway-bridge.<inst>) makes a duplicate
    # launch self-defer and exit, so always-spawn is safe and simpler.
    for _gw_var in $(env | grep -o '^AG2_REMOTE_TOKEN_[A-Za-z0-9_][A-Za-z0-9_]*' || true); do
      _gw_inst="$(printf '%s' "${_gw_var#AG2_REMOTE_TOKEN_}" | tr '[:upper:]' '[:lower:]')"
      # The guard must wrap the WHOLE command. `VAR=1 [ -n "$PY" ] && cmd` applies
      # the assignments to `[` and runs cmd with NONE of them — so the named
      # gateway launched without GATEWAY_INSTANCE / its own REMOTE_TASK_TOKEN /
      # the REMOTE_PROACTIVE_ROOM= scoping, collapsing onto the primary gateway's
      # credentials (CR #2599, @qingyun-wu).
      # The ✓ belongs INSIDE the branch too. Fixing the guard shape here while
      # leaving the success line outside it still reported a launch that never
      # happened — per named instance, on a configured remote-control surface.
      if [ -n "$PY" ]; then
        # REMOTE_TASK_CHANNEL_DIR isolates this instance's channel config
        # (.env fallback + access.json). Without it the named instance defaults
        # to channels/ag2space/ and inherits PROD's credentials and tier map —
        # the exact failure #2701 exists to prevent (review P1, bassil).
        # Convention: instance "dev" → channels/dev-ag2space/; anything else →
        # channels/<inst>-ag2space/ unless the operator overrides via
        # REMOTE_TASK_CHANNEL_DIR_<INST>.
        _gw_chdir_var="REMOTE_TASK_CHANNEL_DIR_${_gw_var#AG2_REMOTE_TOKEN_}"
        _gw_chdir="${!_gw_chdir_var:-${_gw_inst}-ag2space}"
        _gw_token_file_var="REMOTE_TASK_TOKEN_FILE_${_gw_var#AG2_REMOTE_TOKEN_}"
        _gw_token_file="${!_gw_token_file_var:-$(bash "$REPO/scripts/sutando-config.sh" claude-home-path channels "$_gw_chdir" .env)}"
        [ -f "$_gw_token_file" ] || _gw_token_file=""
        SUTANDO_SUPERVISED=1 GATEWAY_INSTANCE="$_gw_inst" REMOTE_TASK_TOKEN="${!_gw_var}" \
          REMOTE_TASK_URL= REMOTE_TASK_TOKEN_FILE="$_gw_token_file" \
          REMOTE_TASK_CHANNEL_DIR="$_gw_chdir" \
          REMOTE_PROACTIVE_ROOM= \
          "$PY" "$REPO/src/remote-gateway-bridge.py" >> "$LOGS_DIR/remote-gateway-bridge.$_gw_inst.log" 2>&1 &
        echo "  ✓ gateway bridge ($_gw_inst — self-defers if already running)"
      else
        echo "  ⊘ gateway bridge ($_gw_inst) skipped — no runnable python3"
      fi
    done
  fi
}

# Same shape as channel_bridge_supervised(): kickstart a loaded service, install
# the job when absent, non-zero so the caller can fall back unsupervised.
#
# A lead already running from this checkout counts as supervised — the launchd
# job stands down while it lives and takes over the moment it exits.
# Requires REPO set by the caller (same contract as start_gateway_lanes).
pool_lead_supervised() {
  local label="com.sutando.pool-lead"
  local service="gui/$(id -u)/$label"
  local daemon="$REPO/scripts/pool-lead-daemon.py"
  local installer="$REPO/scripts/install-worker-pool.sh"
  local template="$REPO/src/launchd/$label.plist"

  [ -f "$installer" ] && [ -f "$template" ] && [ -f "$daemon" ] || return 1
  if launchctl print "$service" > /dev/null 2>&1; then
    pgrep -f "$daemon" > /dev/null 2>&1 && return 0
    launchctl kickstart -k "$service" > /dev/null 2>&1 || return 1
  else
    bash "$installer" --lead-only > /dev/null 2>&1 || return 1
  fi
  for _ in $(seq 1 "${SUTANDO_POOL_LEAD_WAIT_S:-12}"); do
    pgrep -f "$daemon" > /dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}
