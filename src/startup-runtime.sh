#!/bin/bash
# Runtime/credential decisions shared by startup and behavior-level tests.

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
  python3 - "$_file" <<'PY'
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
