#!/bin/bash
# Runtime/credential decisions shared by startup and behavior-level tests.

configure_startup_runtime() {
  if [ -f .env ]; then
    set -a; source .env; set +a
  else
    echo "  ~ .env not found — continuing with credential-free services"
  fi

  if [ -n "${GEMINI_VOICE_API_KEY:-${GEMINI_API_KEY:-}}" ]; then
    unset SKIP_VOICE
  elif managed_gemini_credential_present; then
    unset SKIP_VOICE
    echo "  ✓ voice credential: managed (state/auth/managed-credentials.json)"
  else
    export SKIP_VOICE=1
    echo "  ~ voice agent disabled (set GEMINI_VOICE_API_KEY or GEMINI_API_KEY to enable)"
  fi
}

# G8 managed tier (see src/credential-resolver.ts): the desktop app mints
# <workspace>/state/auth/managed-credentials.json instead of exporting BYO env
# keys, and voice-key.ts resolves it at runtime. A minted gemini capability must
# therefore enable voice the same way an env key does — the env-only check above
# left a managed-only install booting with SKIP_VOICE=1 while the voice-agent
# itself would have connected fine (observed live 2026-07-23). Mirrors the
# resolver's presence semantics: gemini-voice falls back to gemini-text, any
# non-empty key counts; expiry/refresh stay the resolver's concern.
managed_gemini_credential_present() {
  local ws file
  ws="$(bash scripts/sutando-config.sh workspace 2>/dev/null)"
  [ -n "$ws" ] || ws="workspace"
  file="$ws/state/auth/managed-credentials.json"
  [ -f "$file" ] || return 1
  python3 -c '
import json, sys
try:
    caps = json.load(open(sys.argv[1])).get("capabilities") or {}
except Exception:
    sys.exit(1)
slots = ("gemini-voice", "gemini-text")
sys.exit(0 if any(isinstance(caps.get(s), dict) and caps[s].get("key") for s in slots) else 1)
' "$file" 2>/dev/null
}

phone_stack_enabled() {
  [ "${SKIP_PHONE:-}" != "1" ] && [ "${SKIP_VOICE:-}" != "1" ]
}
