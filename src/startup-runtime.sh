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
resolve_startup_core_runtime() {
  local _repo
  _repo="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
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
  _repo="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

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
  _repo="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
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
  _repo="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
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
  if [ -f .env ]; then
    set -a; source .env; set +a
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
      --pidfile "$WORKSPACE/.voice-agent.pid" \
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
