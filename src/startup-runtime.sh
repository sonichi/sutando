#!/bin/bash
# Runtime/credential decisions shared by startup and behavior-level tests.

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
_managed_voice_credential_present() {
  local _repo _ws _file
  _repo="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  _ws="$(bash "$_repo/scripts/sutando-config.sh" workspace 2>/dev/null)" || return 1
  [ -n "$_ws" ] || return 1
  _file="$_ws/state/auth/managed-credentials.json"
  [ -f "$_file" ] || return 1

  # Mirrors CAPABILITY_FALLBACKS['gemini-voice'] = ['gemini-voice','gemini-text'].
  # Malformed/unreadable files skip the tier rather than throwing, matching
  # readManaged()'s try/catch contract.
  # Resolve an interpreter that actually RUNS before reading. A bare `python3`
  # on a fresh Mac resolves to the Xcode Command Line Tools stub, which exits
  # non-zero — and this function returns 1 on any failure, so a stub would be
  # indistinguishable from "no managed credential configured". That is a silent
  # wrong answer, not a crash: startup would proceed BYO-only while a managed
  # credential sat on disk.
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
  # exactly the silent managed-user outage this function exists to prevent, and
  # inconsistent with the workspace lookup ABOVE, which resolves fine because
  # sutando-config.sh honours $SUTANDO_PY internally.
  #
  # `python-bin` is consulted FIRST so the precedence has one definition. The
  # explicit tiers after it are a fallback for the case where that script cannot
  # run at all; Homebrew stays last, beyond the canonical order, because it was
  # added for a real host and dropping it would regress that case. brew's
  # location is asked for, never written down (REVIEW.md hardcoded-paths).
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
  if [ -z "$_py" ]; then
    echo "  ~ managed-credential gate: no usable python3 —" \
         "cannot read $_file; treating as UNKNOWN, not as absent" >&2
    return 1
  fi

  "$_py" - "$_file" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as fh:
        caps = (json.load(fh) or {}).get("capabilities") or {}
    if not isinstance(caps, dict):
        raise ValueError("capabilities is not an object")
except Exception:
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

  # Order mirrors resolveCredential(): managed tier first, then BYO env. Only the
  # *reason* differs between the two enabled branches — both start voice.
  if [ -n "${GEMINI_VOICE_API_KEY:-${GEMINI_API_KEY:-}}" ]; then
    unset SKIP_VOICE
  elif _managed_voice_credential_present; then
    unset SKIP_VOICE
    echo "  ✓ voice agent enabled (managed credentials)"
  else
    export SKIP_VOICE=1
    # Say WHY, and name the two ways out — a gate that disables a feature without
    # explaining itself is the screen-capture failure mode repeated.
    echo "  ~ voice agent disabled (no managed credentials in" \
         "<workspace>/state/auth/managed-credentials.json; set GEMINI_VOICE_API_KEY" \
         "or GEMINI_API_KEY for a BYO key)"
  fi
}

phone_stack_enabled() {
  [ "${SKIP_PHONE:-}" != "1" ] && [ "${SKIP_VOICE:-}" != "1" ]
}
