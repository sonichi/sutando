#!/bin/bash
# src/agent/claude/provider-env.sh — SOURCEABLE helper. Resolves the alternate
# Anthropic-compatible provider connection from the `core_config` block (+ env
# overrides + vault) and EXPORTS the ANTHROPIC_* vars that Claude Code reads:
#
#   ANTHROPIC_BASE_URL   — provider endpoint (from core_config.provider)
#   <auth_env>           — the API token (ANTHROPIC_AUTH_TOKEN → Bearer, or
#                          ANTHROPIC_API_KEY → x-api-key), read from the env or,
#                          failing that, the macOS-keychain vault. Never logged.
#   ANTHROPIC_MODEL      — primary session model (core_config.model)
#   ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL — per-class subtask/subagent model
#                          IDs (core_config.models.{opus,sonnet,haiku})
#
# Single source of truth for "connect the claude agent to the provider", shared
# by every launcher that runs against a provider:
#   - src/agent/claude/cli/dashp.sh          (one-shot `claude -p`)
#   - src/agent/claude/cli/start-cli.sh      (interactive core, when a provider is set)
#   - src/agent/claude/sdk/session-server.sh (Agent-SDK session)
#
# CONTRACT: this file only DEFINES a function — no top-level side effects, no
# `set`, no `exit`, no `exec` — so it is safe to `.`/`source` from a caller that
# is mid-flight under `set -euo pipefail`. The caller must have $REPO set to the
# repo root before sourcing.
#
# Per-setting precedence: env override > core_config > default. CLI flags are
# injected by the caller as the matching SUTANDO_PROVIDER_* env var before
# calling, so "CLI > env > config" holds end-to-end. Env overrides:
#   SUTANDO_PROVIDER_URL       → endpoint      (core_config.provider)
#   SUTANDO_PROVIDER_MODEL     → model         (core_config.model)
#   SUTANDO_PROVIDER_AUTH_ENV  → token env/key (core_config.auth_env)

# The sentinel `auth_env` value meaning "use the Claude.ai SUBSCRIPTION OAuth —
# there is NO provider API token to inject." It is NOT a real env var to read.
CLAUDE_SUBSCRIPTION_AUTH="ANTHROPIC_SUBSCRIPTION"

# Resolve + export the provider env. Returns:
#   0 — a token was found and exported (provider connection is ready)
#   1 — a token env was named but NO token found (caller decides fatal vs skip)
#   2 — auth_env is the ANTHROPIC_SUBSCRIPTION sentinel: subscription OAuth, no
#       token injected (endpoint/model still exported if a provider is configured)
# On return:
#   $CLAUDE_PROVIDER_AUTH_ENV  — the resolved auth_env (messaging)
#   $CLAUDE_PROVIDER_ENDPOINT  — the resolved endpoint ("" if none configured;
#                                callers use this to tell provider vs subscription)
claude_provider_export_env() {
  local cfg_provider="" cfg_model="" cfg_auth_env="" cfg_opus="" cfg_sonnet="" cfg_haiku=""
  if [ -x "$REPO/scripts/sutando-config.sh" ]; then
    # Read core_config LOUD. A malformed sutando.config makes the getter exit
    # nonzero (resolve_core_config → _load_json raises on bad JSON). Capture the
    # rc so we can surface that instead of silently swallowing it and falling
    # back to the subscription default — a provider you configured must not
    # vanish quietly. `|| _cc_rc=$?` keeps a `set -e` caller alive.
    local _k _v _cc_raw="" _cc_rc=0
    _cc_raw="$(bash "$REPO/scripts/sutando-config.sh" core-config 2>&1)" || _cc_rc=$?
    if [ "$_cc_rc" -ne 0 ]; then
      echo "provider-env: WARNING — reading core_config failed (rc=$_cc_rc); using defaults (subscription). Fix sutando.config, then: bash scripts/sutando-config.sh core-config" >&2
      printf '%s\n' "$_cc_raw" | sed 's/^/  /' >&2
    else
      while IFS='=' read -r _k _v; do
        case "$_k" in
          CFG_PROVIDER)     cfg_provider="$_v" ;;
          CFG_MODEL)        cfg_model="$_v" ;;
          CFG_AUTH_ENV)     cfg_auth_env="$_v" ;;
          CFG_MODEL_OPUS)   cfg_opus="$_v" ;;
          CFG_MODEL_SONNET) cfg_sonnet="$_v" ;;
          CFG_MODEL_HAIKU)  cfg_haiku="$_v" ;;
        esac
      done <<< "$_cc_raw"
    fi
  fi

  # Which env var / vault key holds the token (picks Bearer vs x-api-key style),
  # or the ANTHROPIC_SUBSCRIPTION sentinel (default — subscription, no token).
  local auth_env="${SUTANDO_PROVIDER_AUTH_ENV:-}"
  [ -n "$auth_env" ] || auth_env="${cfg_auth_env:-$CLAUDE_SUBSCRIPTION_AUTH}"
  CLAUDE_PROVIDER_AUTH_ENV="$auth_env"

  # $auth_env drives indirect expansion and `export` below, so it MUST be a plain
  # env-var identifier — never anything the shell could interpret as code. It
  # comes from config / SUTANDO_PROVIDER_AUTH_ENV (which may be synced), so reject
  # anything that isn't [A-Za-z_][A-Za-z0-9_]* before we use it. (The
  # ANTHROPIC_SUBSCRIPTION sentinel is itself a valid identifier and passes.)
  case "$auth_env" in
    ""|[0-9]*|*[!A-Za-z0-9_]*)
      echo "provider-env: invalid auth_env '$auth_env' — must be an env-var name (e.g. ANTHROPIC_AUTH_TOKEN, or the ANTHROPIC_SUBSCRIPTION sentinel)." >&2
      return 1 ;;
  esac

  # Endpoint (CLI > env > config): an explicit SUTANDO_PROVIDER_URL (the channel
  # dashp.sh's flags inject through) WINS over an inherited ANTHROPIC_BASE_URL;
  # otherwise a pre-set ANTHROPIC_BASE_URL stands; otherwise fall back to
  # core_config.provider.
  if [ -n "${SUTANDO_PROVIDER_URL:-}" ]; then
    export ANTHROPIC_BASE_URL="$SUTANDO_PROVIDER_URL"
  elif [ -z "${ANTHROPIC_BASE_URL:-}" ] && [ -n "$cfg_provider" ]; then
    export ANTHROPIC_BASE_URL="$cfg_provider"
  fi
  CLAUDE_PROVIDER_ENDPOINT="${ANTHROPIC_BASE_URL:-}"

  # Don't leak the token over this endpoint. When a provider TOKEN will be sent
  # (i.e. NOT the subscription sentinel), refuse credentials embedded in the URL
  # (userinfo leaks via logs/proxies) and refuse a plaintext http:// URL to
  # anywhere but loopback (the token would travel unencrypted). Loopback http is
  # allowed — it's how the subscription credential-proxy is addressed.
  if [ -n "$CLAUDE_PROVIDER_ENDPOINT" ] && [ "$auth_env" != "$CLAUDE_SUBSCRIPTION_AUTH" ]; then
    local _authority="${CLAUDE_PROVIDER_ENDPOINT#*://}"; _authority="${_authority%%/*}"
    case "$_authority" in
      *@*)
        echo "provider-env: refusing provider URL with embedded credentials (user@host) — store the token in auth_env/vault, not the URL." >&2
        return 1 ;;
    esac
    case "$CLAUDE_PROVIDER_ENDPOINT" in
      https://*) : ;;
      http://localhost|http://localhost:*|http://localhost/*) : ;;
      http://127.0.0.1|http://127.0.0.1:*|http://127.0.0.1/*) : ;;
      'http://[::1]'|'http://[::1]:'*|'http://[::1]/'*) : ;;
      *)
        echo "provider-env: refusing non-https provider URL '$CLAUDE_PROVIDER_ENDPOINT' — the $auth_env token would be sent in plaintext. Use https:// (loopback http is allowed)." >&2
        return 1 ;;
    esac
  fi

  # Model (CLI > env > config): an explicit SUTANDO_PROVIDER_MODEL (the channel
  # dashp.sh's --model injects through) WINS over an inherited ANTHROPIC_MODEL;
  # otherwise fall back to core_config.model, but only when ANTHROPIC_MODEL isn't
  # already pinned. (Previously an inherited ANTHROPIC_MODEL silently no-op'd
  # --model, breaking the documented precedence.)
  if [ -n "${SUTANDO_PROVIDER_MODEL:-}" ]; then
    export ANTHROPIC_MODEL="$SUTANDO_PROVIDER_MODEL"
  elif [ -n "$cfg_model" ] && [ -z "${ANTHROPIC_MODEL:-}" ]; then
    export ANTHROPIC_MODEL="$cfg_model"
  fi

  # Per-class model overrides for subtasks/subagents (core_config.models.*). Each
  # maps to a Claude Code alias env var; sonnet is what subagents default to,
  # haiku is used for quick/background work. Only set when configured AND not
  # already pinned in the env (so an explicit ANTHROPIC_DEFAULT_*_MODEL wins).
  [ -n "$cfg_opus" ]   && [ -z "${ANTHROPIC_DEFAULT_OPUS_MODEL:-}" ]   && export ANTHROPIC_DEFAULT_OPUS_MODEL="$cfg_opus"
  [ -n "$cfg_sonnet" ] && [ -z "${ANTHROPIC_DEFAULT_SONNET_MODEL:-}" ] && export ANTHROPIC_DEFAULT_SONNET_MODEL="$cfg_sonnet"
  [ -n "$cfg_haiku" ]  && [ -z "${ANTHROPIC_DEFAULT_HAIKU_MODEL:-}" ]  && export ANTHROPIC_DEFAULT_HAIKU_MODEL="$cfg_haiku"

  # Subscription sentinel → no token to resolve/inject; Claude Code uses its
  # OAuth (.credentials.json). The env token would otherwise OVERRIDE that.
  if [ "$auth_env" = "$CLAUDE_SUBSCRIPTION_AUTH" ]; then
    return 2
  fi

  # Clear the OTHER Anthropic auth var so Claude Code doesn't send two credential
  # headers at once. A provider like z.ai wants ONLY its Bearer token — a stale
  # ANTHROPIC_API_KEY lingering in the shell would conflict and get rejected.
  if [ "$auth_env" = "ANTHROPIC_AUTH_TOKEN" ]; then
    unset ANTHROPIC_API_KEY 2>/dev/null || true
  elif [ "$auth_env" = "ANTHROPIC_API_KEY" ]; then
    unset ANTHROPIC_AUTH_TOKEN 2>/dev/null || true
  fi

  # Token: env value of $auth_env first, else the vault key of the same name.
  # Bash indirect expansion (${!name}) — NOT eval — so a hostile auth_env cannot
  # be executed. (auth_env is also identifier-validated near the top.)
  local have="${!auth_env:-}"
  if [ -z "${have:-}" ]; then
    local vault="$REPO/skills/secret-vault/secret-vault.py"
    if [ -f "$vault" ] && command -v python3 > /dev/null 2>&1; then
      local tok=""
      if tok="$(python3 "$vault" get "$auth_env" 2>/dev/null)" && [ -n "$tok" ]; then
        export "$auth_env=$tok"
        have="$tok"
      fi
    fi
  fi

  [ -n "${have:-}" ] || return 1
  return 0
}
