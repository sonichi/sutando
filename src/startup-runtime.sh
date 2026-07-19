#!/bin/bash
# Runtime/credential decisions shared by startup and behavior-level tests.

configure_startup_runtime() {
  if [ -f .env ]; then
    set -a; source .env; set +a
  else
    echo "  ~ .env not found — continuing with credential-free services"
  fi

  if [ -z "${GEMINI_VOICE_API_KEY:-${GEMINI_API_KEY:-}}" ]; then
    export SKIP_VOICE=1
    echo "  ~ voice agent disabled (set GEMINI_VOICE_API_KEY or GEMINI_API_KEY to enable)"
  else
    unset SKIP_VOICE
  fi
}

phone_stack_enabled() {
  [ "${SKIP_PHONE:-}" != "1" ] && [ "${SKIP_VOICE:-}" != "1" ]
}
