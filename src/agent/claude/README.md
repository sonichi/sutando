# `src/agent/claude/` — the Claude core-agent

Sutando's **core agent** is Claude Code (`claude`). This directory is the home for the code that
**initializes** that core agent and **onboards** its auth — pulled out of the general `scripts/`
pile so the launch/onboarding logic lives in one place, and so a second initialization flow
(e.g. a headless / alternate-provider launcher) can grow alongside the current one without
scattering launch code across the repo.

## Layout

```
src/agent/claude/
  cli/                       # launchers that drive the `claude` BINARY
    start-cli.sh             #   the canonical persistent tmux core (sutando-core)
    sutando-shell-setup.sh   #   the `claude-sutando` config-dir alias onboarding
  README.md
```

> The launchers self-locate the repo root **four levels up** (`dirname/../../../..`) because they
> live in `cli/`. Everything else inside them is repo-root-relative (`$REPO/scripts/...`). Keep
> this in mind if you move them again.

## Contents

- **`cli/start-cli.sh`** — the single canonical launcher/restarter for the `sutando-core` tmux
  session. Every core launch funnels through it: `src/startup.sh` execs it at the end of boot,
  `src/health-check.py` (`_default_core_restart`) runs it with `--restart` for wedge recovery, and
  Sutando.app's "Restart Core CLI" menu invokes it. It assembles the `claude` command line
  (model/settings/cwd/obs args), resolves the workspace-scoped `CLAUDE_CONFIG_DIR`, and seeds
  onboarding/trust state so a detached, no-TTY core doesn't dead-end at the welcome or
  folder-trust prompt.
- **`cli/sutando-shell-setup.sh`** — configures the interactive `claude-sutando` shell function
  (the per-invocation analogue of start-cli.sh's config-dir resolution: it runs `claude` with
  `CLAUDE_CONFIG_DIR` pointed at `<workspace>/.claude-sutando`). Also provides `--import` /
  `--repair-paths` used by `scripts/sutando-migrate.sh`. Invoked once-per-host (`--auto`) from
  `src/startup.sh`.

## Auth / onboarding that lives *elsewhere* (by design)

- **Credential seeding at boot** — the auth-carry / env-token-persist block in `src/startup.sh`
  (copies `.credentials.json` + `.claude.json` from `~/.claude/`, or writes a token from
  `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_AUTH_TOKEN`). Stays inline because it must run in
  startup's own environment and export `CLAUDE_CONFIG_DIR` for the bridges that launch after — it
  is not a separately-execed step.
- **Credential proxy + quota** — `skills/quota-tracker/` (local proxy that injects/refreshes the
  OAuth token and routes the core via `ANTHROPIC_BASE_URL=http://localhost:7846`, plus
  `read-quota.py`). An optional, self-contained **skill** per `CLAUDE.md`'s architecture rules; the
  core boots fine without it.
