# `src/agent/claude/` — the Claude core-agent

Sutando's **core agent** is Claude Code (`claude`). This directory owns how that core is
**initialized** (subscription CLI, provider-backed CLI, headless one-shot, or an Agent-SDK
session) and how its auth is **onboarded** — kept out of the general `scripts/` pile so the
different launch flows live in one place.

## Layout

```
src/agent/claude/
  cli/                       # launchers that drive the `claude` BINARY
    start-cli.sh             #   the canonical persistent tmux core (sutando-core)
    sutando-shell-setup.sh   #   the `claude-sutando` config-dir alias onboarding
    dashp.sh                 #   headless one-shot `claude -p`
  sdk/                       # the Agent SDK (@anthropic-ai/claude-agent-sdk)
    session-server.ts        #   persistent, observable session + SSE event stream
    session-server.sh        #   its launcher (provider via provider-env.sh)
    ui.html                  #   minimal live browser UI over the event stream
  provider-env.sh            # SHARED: resolves the provider connection for all of the above
  README.md
```

> The launchers self-locate the repo root **four levels up** (`dirname/../../../..`) because
> they live in `cli/` or `sdk/`. `provider-env.sh` stays at the `claude/` root (shared) and is
> sourced by both via an absolute `$REPO/...` path. Keep this in mind if you move things again.

## Initialization flows

| Flow | Launcher | How `claude` runs | Auth |
|------|----------|-------------------|------|
| **Subscription core** (default) | `cli/start-cli.sh` | interactive, tmux `sutando-core` | Claude.ai OAuth |
| **Provider core** | `cli/start-cli.sh` (when `core_config.provider` is set) | same interactive core, pointed at a custom endpoint | API token + `provider` |
| **Headless** | `cli/dashp.sh` | one-shot `claude -p` | API token + `provider` |
| **SDK session** | `sdk/session-server.sh` | persistent Agent-SDK session (streaming) | API token + `provider` |

`src/startup.sh` boots the persistent core via `cli/start-cli.sh`. With no provider set that's
the subscription; set `core_config.provider` and the **same** persistent core (tmux session,
watcher loop, `/schedule-crons`) boots against your provider instead — a drop-in that survives
restarts, health-check `--recover-core`, and Sutando.app's Restart Core (all re-exec that one
script). `dashp.sh` and the SDK session are separate, opt-in paths that share the same provider
connection resolution (`provider-env.sh`).

## Configuration — the `core_config` block

The high-level "which core, and how it connects" contract lives in the **`core_config`** block
of `sutando.config(.local).json` (schema + defaults: `src/sutando_config.py::resolve_core_config`,
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
  (the Agent-SDK session, `sdk/session-server.sh`).
- **`provider`** — custom Anthropic-compatible endpoint URL. **Empty = stock Anthropic /
  subscription** (no provider needed). Set it to run on a gateway; required for `claude_sdk`.
- **`auth_env`** — **defaults to the sentinel `ANTHROPIC_SUBSCRIPTION`** = use the Claude.ai
  subscription OAuth, no provider token (correct for the default `claude_cli` core). For a
  provider, set the env var (and vault key) holding the token: `ANTHROPIC_AUTH_TOKEN` →
  `Authorization: Bearer`, or `ANTHROPIC_API_KEY` → `x-api-key`.
- **`model`** — the **primary** session model (the core's own, opus-class) →
  `ANTHROPIC_MODEL`. Empty = provider/account default.
- **`models`** — per-**class** model IDs for **subtasks/subagents** (so lighter work
  runs cheaper models), each mapping to a Claude Code alias (empty = inherit):
  `models.opus` → `ANTHROPIC_DEFAULT_OPUS_MODEL`, `models.sonnet` →
  `ANTHROPIC_DEFAULT_SONNET_MODEL` (subagents default here), `models.haiku` →
  `ANTHROPIC_DEFAULT_HAIKU_MODEL` (quick/background). An already-set
  `ANTHROPIC_DEFAULT_*_MODEL` env var takes precedence.

The token is read from the env var named by `auth_env`, else the **vault** (`vault set <auth_env>
<token>` — never in the config file). `dashp.sh`'s launch knobs (permission mode, `--bare`,
output format) are intentionally **not** in `core_config` — they're env/CLI overrides only, so
the config stays high-level. Precedence everywhere: **CLI flag > env var > config > default**.

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
`ANTHROPIC_MODEL` (env token overrides any OAuth `.credentials.json` — Claude Code precedence:
env > keychain), and launches the same interactive core. **If a provider is set but no token
resolves, `start-cli.sh` refuses to start** rather than silently using the subscription. Clear
`core_config.provider` to return to the subscription core.

## Live UI over the session (Agent SDK)

`sdk/session-server.ts` runs a **persistent, fully-observable** Claude Code session and streams
every event to a browser. It uses `@anthropic-ai/claude-agent-sdk`'s `query()` with an
**async-iterable prompt** — one long-lived process that keeps context across turns (the stable,
SDK-wrapped form of the CLI's `--input-format stream-json` / `--output-format stream-json`). No
hooks / OTel / JSONL-tail glue: the SDK message stream *is* the event feed.

```
browser ──GET /events (SSE)──►  session-server.ts  ──query({prompt: stream})──►  Agent SDK ⇄ claude
        ◄─POST /input {text}──                       (assistant · tool_use · tool_result · result)
```

```bash
bash src/agent/claude/sdk/session-server.sh                        # → http://localhost:4100
SUTANDO_SESSION_FAKE=1 bash src/agent/claude/sdk/session-server.sh # offline echo mode (UI dev)
```

Endpoints: `GET /` (UI), `GET /events` (SSE — every SDK message), `POST /input` (`{"text"}` — a
user turn), `GET /health`. Env: `SUTANDO_SESSION_PORT` (4100), `SUTANDO_SESSION_BIND` (127.0.0.1
— no auth; relays full prompts + tool I/O, keep it loopback), `SUTANDO_SESSION_FAKE=1`.

Tools **auto-approve** (`permissionMode: 'bypassPermissions'`). To surface approvals in the UI
later, pass a `canUseTool` callback to `query()` that emits a `permission_request` event and
awaits a `POST /permission` decision.

### It runs the full Sutando runtime (hybrid)

`session-server.ts` is not a bare Q&A session — it's the `claude_sdk` **core**, running the same
runtime as the interactive core. The SDK session is the **brain** (it loads `CLAUDE.md` + skills
via `settingSources: ['user','project','local']` + the `claude_code` `systemPrompt` preset —
without these the SDK runs in *isolation mode* with no runtime), and this Node **host** is the
**nervous system** — it watches for work and injects it as turns:

- **Task ingestion** — watches `<workspace>/tasks/` and injects each task as a turn. On pickup a
  task is atomically moved to `tasks/.sdk-inflight/` *before* injection (so the proactive loop's
  own `tasks/` scan can't double-process it), then archived to `tasks/archive/YYYY-MM/` when its
  result appears; staged leftovers are recovered on restart. Sets **`SUTANDO_HOST_OWNS_WATCHER=1`**
  so the agent's own `Monitor` watcher (`src/watch-tasks-stream.sh`) no-ops (holds its PID so the
  proactive loop's watcher check passes, but emits nothing).
- **Cron + proactive scheduling** — reads the same `hosts/<host>/crons.json`, matches each 5-field
  schedule against the clock, and injects the cron's prompt (`prompt_skill` → `/skill`, e.g.
  `/proactive-loop` on `*/5`; `prompt` → the literal text, which self-gates via `cron-gate.sh`).
  **This is host-driven because `CronCreate`/`CronList` are NOT available in an SDK session**
  (verified live) — so the bootstrap orients the agent to *not* run `/schedule-crons` or manage
  the watcher (override with `SUTANDO_SESSION_BOOTSTRAP` / `SUTANDO_SESSION_NO_BOOTSTRAP=1`).
- **Liveness** — the per-host `state/cores/<host>.alive` heartbeat (30s, atomic, schema-v1, matching
  `src/core_heartbeat.py`) and `state/core-status.json` (running/idle). Skipped with
  `SUTANDO_SESSION_NO_HEARTBEAT=1` when `startup.sh` already runs `core_heartbeat.py`.

Extra SSE server events: `bootstrap`, `task_injected {id}`, `task_done {id}`, `cron_fired {name}`,
`user_turn`. In `SUTANDO_SESSION_FAKE=1` mode the SDK isn't spawned but the **host runtime still
runs** (heartbeat, task lifecycle, cron ticks) — so you can develop/verify ingestion offline.

### Starting it — `src/startup.sh` routes by `core_type`

`bash src/startup.sh` starts every service, then launches the core based on
`core_config.core_type`:

- **`claude_cli`** (default) → the interactive tmux core (`cli/start-cli.sh`), as always.
- **`claude_sdk`** → the SDK core: `startup.sh` starts `sdk/session-server.sh` in the background
  (with `SUTANDO_SESSION_NO_HEARTBEAT=1`, since `core_heartbeat.py` already runs) and opens the UI.

So to run everything on the SDK core, set `"core_type": "claude_sdk"` in `sutando.config.local.json`
and run `bash src/startup.sh`. Run `sdk/session-server.sh` directly for a standalone core (it then
owns its own heartbeat).

## Auth / onboarding that lives *elsewhere* (by design)

- **Credential seeding at boot** — the auth-carry / env-token-persist block in `src/startup.sh`
  (copies `.credentials.json` + `.claude.json` from `~/.claude/`, or writes a token from
  `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_AUTH_TOKEN`). Stays inline because it must run in
  startup's own environment and export `CLAUDE_CONFIG_DIR` for the bridges that launch after.
- **Credential proxy + quota** — `skills/quota-tracker/` (local proxy that injects/refreshes the
  OAuth token and routes the subscription core via `ANTHROPIC_BASE_URL=http://localhost:7846`,
  plus `read-quota.py`). An optional, self-contained **skill** per `CLAUDE.md`'s architecture
  rules; the core boots fine without it. (Provider mode overrides this `ANTHROPIC_BASE_URL`.)
