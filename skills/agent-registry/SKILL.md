---
name: agent-registry
description: Local Agent Registry — a standalone, dependency-free service that tracks running Claude Code (and other) agent instances. Agents self-register on startup and heartbeat while alive; the Electron overlay and Sutando dashboard read the live list. Use when you need to know which coding agents are running, where, and since when.
---

# Agent Registry

A thin **local** service that tracks running agent instances. Claude Code
instances register themselves on startup (via a SessionStart hook) and
heartbeat while alive; consumers read the live list over a localhost HTTP API.

This is **not** the AG2 Workforce Hub app. It is a small local service that can
*optionally* mirror its state to an AG2 hub using the hub connection layer
(`ag2_workforce.hub.client`) — see "Optional: AG2 Hub mirroring" below — but the
core service has zero third-party dependencies and works entirely offline.

## Components

```
skills/agent-registry/
├── scripts/registry-service.py   # the HTTP service + SQLite store
├── scripts/registry-client.py    # CLI: register / heartbeat / deregister / list / watch
└── hooks/session-start.sh        # Claude Code SessionStart hook
```

- **DB:** `<workspace>/data/agent-registry.db` (SQLite, auto-created)
- **Discovery file:** `<workspace>/state/agent-registry.json` — written by the
  service with the bound port so clients find it without a hardcoded port.
- **Port:** binds `127.0.0.1`, first free port from `7847` upward.

## Running the service

It is **startable by Sutando** three ways, in order of preference:

1. **Auto-start (default).** Any `registry-client.py` call with `--autostart`
   launches the service detached if it is not already running. The
   SessionStart hook passes `--autostart`, so the first Claude Code session to
   start brings the registry up.
2. **From `startup.sh`.** For an always-on registry, add to `src/startup.sh`:
   `python3 skills/agent-registry/scripts/registry-service.py &`
3. **By hand:** `python3 skills/agent-registry/scripts/registry-service.py`

## Registering Claude Code instances

Add the SessionStart hook to `.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [
        { "type": "command",
          "command": "bash skills/agent-registry/hooks/session-start.sh" }
      ] }
    ]
  }
}
```

On session start the hook backgrounds `registry-client.py watch`, which
registers the instance, heartbeats every 30s, and deregisters when the session
ends. If it is killed ungracefully the entry ages out via heartbeat staleness
(`> 90s` → `stale`; stopped/stale rows are pruned after 1h).

## HTTP API

| Method | Path          | Body                          | Returns                  |
|--------|---------------|-------------------------------|--------------------------|
| POST   | `/register`   | `{name, cwd, pid, host?, meta?}` | `{id}`                |
| POST   | `/heartbeat`  | `{id}`                        | `{ok, status}`           |
| POST   | `/deregister` | `{id}`                        | `{ok}`                   |
| GET    | `/agents`     | —                             | `{agents:[...], count}`  |
| GET    | `/health`     | —                             | `{ok, count, uptime}`    |

Each agent record: `id, name, cwd, pid, host, started_at, last_heartbeat,
heartbeat_age, status, meta`. `status` is `active` / `stale` / `stopped`.

## CLI quick reference

```bash
C=skills/agent-registry/scripts/registry-client.py
python3 $C list                                  # show the registry
python3 $C register --name claude-code --pid $$  # register (prints id)
python3 $C heartbeat  --id <ID>
python3 $C deregister --id <ID>
python3 $C watch --name claude-code --pid <PID> --autostart   # used by the hook
```

## Other agents (Kimi Code, etc.)

The service is agent-agnostic — `name` is free-form, so any agent registers the
same way (`--name kimi-code`). What is agent-specific is the *registration
trigger*: Claude Code uses a SessionStart hook; another agent needs its own
equivalent (a startup hook, plugin, or a launch-command wrapper that runs
`registry-client.py watch`).

## Optional: AG2 Hub mirroring

To announce registry state to an AG2 hub, a future extension can open a
`RemoteHubClient` (`ag2_workforce.hub.client`) and post agent join/leave events.
This is deliberately *not* in the core service — it stays dependency-free and
local-first. Add it as a separate module that subscribes to registry changes.
