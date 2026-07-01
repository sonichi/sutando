# `src/agent/claude/` — the Claude core-agent

Sutando's **core agent** is Claude Code (`claude`). This directory owns how that core is
**initialized** and how its auth is **onboarded** — kept out of the general `scripts/` pile so
the different launch flows live in one place.

## Layout

```
src/agent/claude/
  cli/                       # launchers that drive the `claude` BINARY
    start-cli.sh             #   the canonical persistent tmux core (sutando-core)
    sutando-shell-setup.sh   #   the `claude-sutando` config-dir alias onboarding
    dashp.sh                 #   headless one-shot `claude -p`
  provider-env.sh            # SHARED: resolves the provider connection for the launchers
  README.md
```

> The launchers self-locate the repo root **four levels up** (`dirname/../../../..`) because
> they live in `cli/`. `provider-env.sh` stays at the `claude/` root (shared) and is sourced by
> the launchers via an absolute `$REPO/...` path.

## Initialization flows

| Flow | Launcher | How `claude` runs | Auth |
|------|----------|-------------------|------|
| **Subscription core** (default) | `cli/start-cli.sh` | interactive, tmux `sutando-core` | Claude.ai OAuth |
| **Provider core** | `cli/start-cli.sh` (when `core_config.provider` is set) | same interactive core, pointed at a custom endpoint | API token + `provider` |
| **Headless** | `cli/dashp.sh` | one-shot `claude -p` | API token + `provider` |

`src/startup.sh` boots the persistent core via `cli/start-cli.sh`. With no provider set that's
the subscription; set `core_config.provider` and the **same** persistent core boots against your
provider instead — a drop-in that survives restarts, health-check `--recover-core`, and
Sutando.app's Restart Core. `dashp.sh` is a separate, opt-in one-shot path that shares the same
provider-connection resolution (`provider-env.sh`).

> A fourth flow — an Agent-SDK **session core** (`core_config.core_type: claude_sdk`) with a live
> browser UI + the full runtime — builds on this and lands in a follow-up PR.

## Configuration — the `core_config` block

The high-level "which core, and how it connects" contract lives in the **`core_config`** block of
`sutando.config(.local).json` (schema + defaults: `src/sutando_config.py::resolve_core_config`,
read via `scripts/sutando-config.sh core-config`):

```json
"core_config": {
  "core_type": "claude_cli",
  "provider": "https://your-gateway.example.com",
  "auth_env": "ANTHROPIC_AUTH_TOKEN",
  "model": "your-opus-class-model-id",
  "models": {
    "sonnet": "your-sonnet-model-id",
    "haiku": "your-haiku-model-id"
  }
}
```

- **`core_type`** — `claude_cli` (the interactive tmux core, `cli/start-cli.sh`) or `claude_sdk`
  (the Agent-SDK session core — follow-up PR).
- **`provider`** — custom Anthropic-compatible endpoint URL. **Empty = stock Anthropic /
  subscription** (no provider needed). Set it to run on a gateway.
- **`auth_env`** — **defaults to the sentinel `ANTHROPIC_SUBSCRIPTION`** = use the Claude.ai
  subscription OAuth, no provider token. For a provider, set the env var (and vault key) holding
  the token: `ANTHROPIC_AUTH_TOKEN` → `Authorization: Bearer`, or `ANTHROPIC_API_KEY` → `x-api-key`.
- **`model`** — the **primary** session model (the core's own, opus-class) → `ANTHROPIC_MODEL`.
- **`models`** — per-**class** model IDs for **subtasks/subagents** (so lighter work runs cheaper
  models), each mapping to a Claude Code alias (empty = inherit): `models.opus` →
  `ANTHROPIC_DEFAULT_OPUS_MODEL`, `models.sonnet` → `ANTHROPIC_DEFAULT_SONNET_MODEL` (subagents
  default here), `models.haiku` → `ANTHROPIC_DEFAULT_HAIKU_MODEL` (quick/background). An
  already-set `ANTHROPIC_DEFAULT_*_MODEL` env var wins.

The token named by `auth_env` is read from the env, else the **vault** (`vault set <auth_env>
<token>` — never in the config file). `dashp.sh`'s launch knobs (permission mode, `--bare`, output
format) are env/CLI overrides only, keeping the config high-level. Precedence everywhere:
**CLI flag > env var > config > default**.

### Run the persistent core on the provider (drop-in replacement)

```jsonc
// sutando.config.local.json
"core_config": {
  "core_type": "claude_cli",
  "provider": "https://your-gateway.example.com",
  "auth_env": "ANTHROPIC_AUTH_TOKEN",
  "model": "your-model-id"
}
```

1. Store the token: `vault set ANTHROPIC_AUTH_TOKEN <token>` (via Slack/Discord), or export it.
2. Restart the core: `bash src/agent/claude/cli/start-cli.sh --restart` — or `bash src/startup.sh`.

When `core_config.provider` is set, `start-cli.sh` sheds any inherited credential-proxy
`ANTHROPIC_BASE_URL`, sources `provider-env.sh` to export `ANTHROPIC_BASE_URL` / the token /
`ANTHROPIC_MODEL`, and launches the same interactive core (env token overrides any OAuth
`.credentials.json` — Claude Code precedence: env > keychain). **If a provider is set but no token
resolves, `start-cli.sh` refuses to start** rather than silently using the subscription. Clear
`core_config.provider` to return to the subscription core.

## Auth / onboarding that lives *elsewhere* (by design)

- **Credential seeding at boot** — the auth-carry / env-token-persist block in `src/startup.sh`.
  Stays inline because it must run in startup's own environment and export `CLAUDE_CONFIG_DIR` for
  the bridges that launch after.
- **Credential proxy + quota** — `skills/quota-tracker/` (local proxy that injects/refreshes the
  OAuth token and routes the subscription core via `ANTHROPIC_BASE_URL=http://localhost:7846`). An
  optional, self-contained **skill** per `CLAUDE.md`; the core boots fine without it. (Provider
  mode overrides this `ANTHROPIC_BASE_URL`.)
