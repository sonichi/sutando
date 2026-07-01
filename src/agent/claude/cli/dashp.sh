#!/bin/bash
# src/agent/claude/cli/dashp.sh — headless (`claude -p`) launcher for the claude
# core agent, against a custom Anthropic-compatible provider + API token.
#
# Sibling of start-cli.sh. Where start-cli.sh runs an INTERACTIVE `claude` in a
# tmux `sutando-core` session, dashp.sh runs a ONE-SHOT, NON-INTERACTIVE
# `claude -p "<prompt>"`. The assistant's final message is printed to stdout;
# exit 0 on success, non-zero on error.
#
# (To run the PERSISTENT core on the provider instead — a drop-in for the
# subscription core — set `core_config.provider` in sutando config; start-cli.sh
# then boots the interactive core against it. dashp.sh is the one-shot path.)
#
# Usage:
#   bash src/agent/claude/cli/dashp.sh "Summarize today's open PRs"
#   echo "Summarize today's open PRs" | bash src/agent/claude/cli/dashp.sh
#   bash src/agent/claude/cli/dashp.sh --json --model my-model "Find bugs in app.js"
#
# Options (consumed from the front; the rest is the prompt):
#   --json / --text   → claude --output-format json|text
#   --bare            → claude --bare  (skip hooks/skills/MCP/CLAUDE.md)
#   --model NAME      → provider model (same as ANTHROPIC_MODEL / core_config.model)
#   --                → end option parsing; everything after is the prompt
# If no prompt args are given, the prompt is read from stdin.
#
# ── Configuration ────────────────────────────────────────────────────────────
# The provider connection (provider + auth_env + model) comes from the
# `core_config` block of sutando.config(.local).json, resolved by the shared
# helper provider-env.sh (schema: src/sutando_config.py::resolve_core_config):
#
#   "core_config": {
#     "provider": "https://your-gateway.example.com",
#     "auth_env": "ANTHROPIC_AUTH_TOKEN",   // or ANTHROPIC_API_KEY (x-api-key)
#     "model": "your-model-id"
#   }
#
# dashp.sh's own launch knobs are env/CLI only (NOT in core_config), keeping the
# config high-level. Precedence: CLI flag > env var > default.
#   SUTANDO_DASHP_OUTPUT_FORMAT   text|json   (or --json/--text)
#   SUTANDO_DASHP_BARE            1            (or --bare)
#   SUTANDO_DASHP_PERMISSION_MODE <mode>       (empty = --dangerously-skip-permissions)
# Provider-connection env overrides (shared, honored by provider-env.sh):
#   SUTANDO_PROVIDER_URL / SUTANDO_PROVIDER_MODEL / SUTANDO_PROVIDER_AUTH_ENV
#
# ── Auth ─────────────────────────────────────────────────────────────────────
# The token is read from the env var named by `auth_env` if set, else from the
# vault key of the same name (skills/secret-vault). Never logged. It takes
# precedence over any OAuth .credentials.json in CLAUDE_CONFIG_DIR (Claude Code:
# env > settings > keychain), so the subscription token is never used here.
#
# ── Permissions ──────────────────────────────────────────────────────────────
# `claude -p` denies all tools unless told otherwise. Empty permission_mode →
# --dangerously-skip-permissions (parity with the subscription core). Tool
# execution is always LOCAL regardless of provider — set a restrictive
# permission_mode (e.g. dontAsk) when the provider isn't trusted.

set -euo pipefail

# This script lives at src/agent/claude/cli/ — four levels under the repo root.
REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
cd "$REPO"

# ---- CLI options (highest precedence) --------------------------------------
cli_output_format=""   # "json" | "text" when a CLI flag set it
cli_bare=0
PROMPT_ARGS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --json)  cli_output_format="json"; shift ;;
    --text)  cli_output_format="text"; shift ;;
    --bare)  cli_bare=1; shift ;;
    # Inject --model into the env var the provider helper reads, so CLI beats
    # both env and config for the model (CLI > env > config).
    --model|-m) export SUTANDO_PROVIDER_MODEL="${2:-}"; shift 2 ;;
    --)      shift; while [ "$#" -gt 0 ]; do PROMPT_ARGS+=("$1"); shift; done ;;
    *)       PROMPT_ARGS+=("$1"); shift ;;
  esac
done

# ---- dashp-only launch knobs (env/CLI only — NOT in core_config) ------------
# These are dashp.sh-specific and intentionally kept out of core_config to keep
# it high-level. Precedence: CLI flag > env var > default.

# output format: CLI > env > default(text)
if [ -n "$cli_output_format" ]; then
  OUTPUT_FORMAT="$cli_output_format"
elif [ -n "${SUTANDO_DASHP_OUTPUT_FORMAT:-}" ]; then
  OUTPUT_FORMAT="$SUTANDO_DASHP_OUTPUT_FORMAT"
else
  OUTPUT_FORMAT="text"
fi

# bare: CLI > env > default(off)
BARE=0
if [ "$cli_bare" -eq 1 ]; then
  BARE=1
elif [ -n "${SUTANDO_DASHP_BARE:-}" ]; then
  case "$SUTANDO_DASHP_BARE" in 1|true|TRUE|yes) BARE=1 ;; esac
fi

# permission posture: env > default(--dangerously-skip-permissions)
PERMISSION_MODE="${SUTANDO_DASHP_PERMISSION_MODE:-}"

# ---- prompt: args win; else stdin ------------------------------------------
# `claude -p -` reads the prompt from stdin. Refuse the ambiguous
# "no args AND a TTY stdin" case rather than hang on an interactive terminal.
PROMPT=""
READ_STDIN=0
if [ "${#PROMPT_ARGS[@]}" -gt 0 ]; then
  PROMPT="${PROMPT_ARGS[*]}"
elif [ ! -t 0 ]; then
  READ_STDIN=1
else
  echo "dashp: no prompt given (pass it as args or pipe it on stdin)" >&2
  echo "  e.g. bash src/agent/claude/cli/dashp.sh \"your prompt\"" >&2
  exit 2
fi

# ---- workspace-scoped CLAUDE_CONFIG_DIR (mirror start-cli.sh) ---------------
# Resolve the same per-runtime config dir so settings/skills/CLAUDE.md and
# channel state resolve to the workspace tree (not global ~/.claude/). The env
# token resolved below takes precedence over any OAuth .credentials.json here.
if [ -x "$REPO/scripts/sutando-config.sh" ]; then
  if _ccd="$(bash "$REPO/scripts/sutando-config.sh" claude-sutando-config-dir 2>/dev/null)"; then
    mkdir -p "$_ccd"
    export CLAUDE_CONFIG_DIR="$_ccd"
  fi
  # Helper-missing / invalid → silent fallback (claude resolves its own dir).
fi

# ---- provider connection (shared with start-cli.sh) ------------------------
# shellcheck source=src/agent/claude/provider-env.sh
. "$REPO/src/agent/claude/provider-env.sh"
_prc=0; claude_provider_export_env || _prc=$?
if [ "$_prc" = "0" ]; then
  echo "dashp: provider → ${ANTHROPIC_BASE_URL:-(stock Anthropic endpoint)}${ANTHROPIC_MODEL:+ · model=$ANTHROPIC_MODEL} (auth via ${CLAUDE_PROVIDER_AUTH_ENV})" >&2
elif [ "$_prc" = "2" ]; then
  # ANTHROPIC_SUBSCRIPTION sentinel — run `claude -p` on the Claude.ai subscription
  # (no provider token). Set core_config.provider + a token auth_env for a provider.
  echo "dashp: subscription auth (auth_env=ANTHROPIC_SUBSCRIPTION) — running claude -p on the Claude.ai subscription." >&2
else
  echo "dashp: no API token found via ${CLAUDE_PROVIDER_AUTH_ENV:-ANTHROPIC_AUTH_TOKEN}. Set it in the env," >&2
  echo "  or store it in the vault ('vault set ${CLAUDE_PROVIDER_AUTH_ENV:-ANTHROPIC_AUTH_TOKEN} <token>' via Slack/Discord)," >&2
  echo "  or set core_config.auth_env=ANTHROPIC_API_KEY for x-api-key style. See src/agent/claude/README.md." >&2
  exit 3
fi
[ -n "${ANTHROPIC_BASE_URL:-}" ] || echo "dashp: no provider set — using the stock Anthropic endpoint (set core_config.provider for a custom provider)." >&2

# ---- assemble claude args ---------------------------------------------------
# Model comes from ANTHROPIC_MODEL (exported by the helper), so no --model here.
OUTPUT_ARGS=(--output-format "$OUTPUT_FORMAT")
BARE_ARGS=()
[ "$BARE" -eq 1 ] && BARE_ARGS=(--bare)
PERM_ARGS=(--dangerously-skip-permissions)
[ -n "$PERMISSION_MODE" ] && PERM_ARGS=(--permission-mode "$PERMISSION_MODE")

# ---- launch -----------------------------------------------------------------
# --add-dir "$HOME" mirrors start-cli.sh so the headless core has the same file
# reach as the interactive one. The ${arr[@]+...} guards keep empty arrays safe
# on bash 3.2 under `set -u` (same pattern as start-cli.sh).
if [ "$READ_STDIN" -eq 1 ]; then
  exec claude -p - \
    ${OUTPUT_ARGS[@]+"${OUTPUT_ARGS[@]}"} \
    ${BARE_ARGS[@]+"${BARE_ARGS[@]}"} \
    ${PERM_ARGS[@]+"${PERM_ARGS[@]}"} \
    --add-dir "$HOME"
else
  exec claude -p "$PROMPT" \
    ${OUTPUT_ARGS[@]+"${OUTPUT_ARGS[@]}"} \
    ${BARE_ARGS[@]+"${BARE_ARGS[@]}"} \
    ${PERM_ARGS[@]+"${PERM_ARGS[@]}"} \
    --add-dir "$HOME"
fi
