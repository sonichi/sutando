#!/bin/bash
# Guards in src/agent/claude/provider-env.sh::claude_provider_export_env:
#   - auth_env is validated as an env-var identifier (no shell injection via the
#     indirect expansion / export), replacing an earlier `eval`.
#   - a provider token is never sent over a leaky endpoint: non-https to a
#     non-loopback host is refused, and userinfo (user:pass@host) is refused.
#   - precedence is CLI(SUTANDO_PROVIDER_*) > inherited ANTHROPIC_* > config, so
#     an explicit override wins over an inherited ANTHROPIC_MODEL/_BASE_URL.
#
# Each case sources the helper in a subshell and sets SUTANDO_PROVIDER_* so the
# result is independent of the caller's sutando.config (env override > config).

REPO="$(cd "$(dirname "$0")/.." && pwd)"
export REPO
PE="$REPO/src/agent/claude/provider-env.sh"
fail=0

# call <extra-env-eval> → prints "rc=<n> base=<url> model=<m> stderr=<text>"
call() {
  ( set +e
    . "$PE"
    eval "$1"
    local err; err="$(claude_provider_export_env 2>&1 1>/dev/null)"
    # re-run to capture rc + exports on stdout (stderr already captured above)
    claude_provider_export_env >/dev/null 2>&1; local rc=$?
    echo "rc=$rc base=${ANTHROPIC_BASE_URL:-} model=${ANTHROPIC_MODEL:-} stderr=$err"
  )
}

check() { # <name> <expect-substr> <actual>
  if printf '%s' "$3" | grep -qF "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; echo "        want ~ '$2'"; echo "        got    '$3'"; fail=1; fi
}

marker="$REPO/../.pe-guard-injection-marker.$$"
rm -f "$marker" 2>/dev/null

check "auth_env injection is rejected (not executed)" "invalid auth_env" \
  "$(call 'export SUTANDO_PROVIDER_AUTH_ENV="X:-\$(touch '"$marker"')"')"
if [ -e "$marker" ]; then echo "  FAIL injection EXECUTED (marker created)"; fail=1; rm -f "$marker"; else echo "  ok   injection did not execute"; fi

check "non-https to non-loopback refused" "refusing non-https" \
  "$(call 'export SUTANDO_PROVIDER_URL=http://evil.example.com SUTANDO_PROVIDER_AUTH_ENV=ANTHROPIC_AUTH_TOKEN ANTHROPIC_AUTH_TOKEN=t')"

check "embedded userinfo refused" "embedded credentials" \
  "$(call 'export SUTANDO_PROVIDER_URL=https://user:pass@host.example.com SUTANDO_PROVIDER_AUTH_ENV=ANTHROPIC_AUTH_TOKEN ANTHROPIC_AUTH_TOKEN=t')"

check "loopback http allowed (rc=0)" "rc=0" \
  "$(call 'export SUTANDO_PROVIDER_URL=http://localhost:7846 SUTANDO_PROVIDER_AUTH_ENV=ANTHROPIC_AUTH_TOKEN ANTHROPIC_AUTH_TOKEN=t')"

check "https + token ok (rc=0)" "rc=0" \
  "$(call 'export SUTANDO_PROVIDER_URL=https://api.example.com SUTANDO_PROVIDER_AUTH_ENV=ANTHROPIC_AUTH_TOKEN ANTHROPIC_AUTH_TOKEN=t')"

check "SUTANDO_PROVIDER_MODEL beats inherited ANTHROPIC_MODEL" "model=FLAGMODEL" \
  "$(call 'export ANTHROPIC_MODEL=INHERITED SUTANDO_PROVIDER_MODEL=FLAGMODEL SUTANDO_PROVIDER_URL=https://api.example.com SUTANDO_PROVIDER_AUTH_ENV=ANTHROPIC_AUTH_TOKEN ANTHROPIC_AUTH_TOKEN=t')"

check "SUTANDO_PROVIDER_URL beats inherited ANTHROPIC_BASE_URL" "base=https://flagurl.example.com" \
  "$(call 'export ANTHROPIC_BASE_URL=https://inherited.example.com SUTANDO_PROVIDER_URL=https://flagurl.example.com SUTANDO_PROVIDER_AUTH_ENV=ANTHROPIC_AUTH_TOKEN ANTHROPIC_AUTH_TOKEN=t')"

check "subscription sentinel → rc=2 (no token, endpoint exempt from https guard)" "rc=2" \
  "$(call 'export SUTANDO_PROVIDER_AUTH_ENV=ANTHROPIC_SUBSCRIPTION')"

echo "----------------------------------------"
if [ "$fail" -eq 0 ]; then echo "PASS: provider-env guards"; else echo "provider-env guards FAILED"; exit 1; fi
