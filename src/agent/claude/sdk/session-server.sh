#!/bin/bash
# src/agent/claude/sdk/session-server.sh — launch the SDK session server against the
# provider (core_config), reusing the shared provider-env.sh helper.
#
# Boots src/agent/claude/sdk/session-server.ts with ANTHROPIC_BASE_URL / <token> /
# ANTHROPIC_MODEL exported, so the persistent SDK session runs on the configured
# provider. Open http://localhost:${SUTANDO_SESSION_PORT:-4100} for the UI.
#
# Usage:
#   bash src/agent/claude/sdk/session-server.sh
#   SUTANDO_SESSION_FAKE=1 bash src/agent/claude/sdk/session-server.sh   # offline UI dev
#
# Requires a token (env or vault) for the provider. Set
# SUTANDO_SESSION_ALLOW_SUBSCRIPTION=1 to fall back to the subscription auth
# instead (e.g. local dev without a provider configured).

set -euo pipefail

# This script lives at src/agent/claude/sdk/ — four levels under the repo root.
REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
cd "$REPO"

# Workspace-scoped CLAUDE_CONFIG_DIR (same resolution as start-cli.sh / dashp.sh).
if [ -x "$REPO/scripts/sutando-config.sh" ]; then
  if _ccd="$(bash "$REPO/scripts/sutando-config.sh" claude-sutando-config-dir 2>/dev/null)"; then
    mkdir -p "$_ccd"
    export CLAUDE_CONFIG_DIR="$_ccd"
  fi
fi

# FAKE mode needs no provider/token — it never spawns claude.
if [ "${SUTANDO_SESSION_FAKE:-0}" = "1" ]; then
  echo "session-server: FAKE echo mode (no provider needed)" >&2
  exec npx tsx "$REPO/src/agent/claude/sdk/session-server.ts" "$@"
fi

# Shed any inherited ANTHROPIC_BASE_URL before resolving auth. When launched from
# src/startup.sh this is pre-set to the credential proxy (http://localhost:7846),
# which injects the SUBSCRIPTION OAuth token — routing an API-key/provider request
# through it fails ("API Error: Connection error", the exact hang we hit). We
# restore it ONLY for the subscription path (rc=2), where the proxy is the point.
_inherited_base_url="${ANTHROPIC_BASE_URL:-}"
unset ANTHROPIC_BASE_URL

# Provider connection (endpoint + token + model) via the shared helper.
# shellcheck source=src/agent/claude/provider-env.sh
. "$REPO/src/agent/claude/provider-env.sh"
_prc=0; claude_provider_export_env || _prc=$?
if [ "$_prc" = "0" ]; then
  echo "session-server: provider → ${ANTHROPIC_BASE_URL:-(stock Anthropic endpoint)}${ANTHROPIC_MODEL:+ · model=$ANTHROPIC_MODEL} (auth via ${CLAUDE_PROVIDER_AUTH_ENV})" >&2
elif [ "$_prc" = "2" ]; then
  # ANTHROPIC_SUBSCRIPTION sentinel — run on the subscription. Route back through
  # the credential proxy if startup.sh set one (OAuth injection + quota tracking);
  # otherwise the SDK uses the OAuth creds in CLAUDE_CONFIG_DIR directly.
  [ -n "$_inherited_base_url" ] && export ANTHROPIC_BASE_URL="$_inherited_base_url"
  echo "session-server: subscription auth — running the SDK session on the Claude.ai subscription${ANTHROPIC_BASE_URL:+ via $ANTHROPIC_BASE_URL}." >&2
elif [ "${SUTANDO_SESSION_ALLOW_SUBSCRIPTION:-0}" = "1" ]; then
  [ -n "$_inherited_base_url" ] && export ANTHROPIC_BASE_URL="$_inherited_base_url"
  echo "session-server: no provider token — falling back to subscription auth (SUTANDO_SESSION_ALLOW_SUBSCRIPTION=1)" >&2
else
  echo "session-server: no API token via ${CLAUDE_PROVIDER_AUTH_ENV:-ANTHROPIC_AUTH_TOKEN} (env or vault) — refusing to start." >&2
  echo "  Set core_config.provider + the token ('vault set ${CLAUDE_PROVIDER_AUTH_ENV:-ANTHROPIC_AUTH_TOKEN} <token>' or export it)," >&2
  echo "  or set SUTANDO_SESSION_ALLOW_SUBSCRIPTION=1 to use the subscription. See src/agent/claude/README.md." >&2
  exit 1
fi

exec npx tsx "$REPO/src/agent/claude/sdk/session-server.ts" "$@"
