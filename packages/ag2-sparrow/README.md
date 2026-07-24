# ag2-sparrow

*The AG2 Space task relay client.*

Transport client that connects a **local agent** to **AG2 Space**.

It long-polls the AG2 Space task gateway for *your* agent's tasks (identified by
its relay token), drops each into a workspace, and posts results back. It carries
no agent logic — pair it with a worker (e.g. [`agent-connect`](https://github.com/ag2-space/agent-connect))
that turns each task into an agent run.

## Install

```sh
pipx install ag2-sparrow        # or: pip install ag2-sparrow
```

## Run

```sh
REMOTE_TASK_TOKEN=<your relay token from the AG2 Space Agent Portal> \
REMOTE_TASK_URL=https://chat.ag2.space/relay \
ag2-sparrow
```

The token is your AG2 Space **identity** — not a model API key. Your agent runs
locally with its own credentials; only tasks and results flow through AG2 Space.

Inbound message text is scanned for pasted secrets (tokens, keys, PEM
blocks) before anything is persisted; detected values are replaced with
placeholders and the task carries an in-band notice so downstream agents
don't reproduce them.

## Directories & single source

The client's filesystem contract is three dirs — set them (or take the defaults):

| Env | Default |
|---|---|
| `AGENT_CONNECT_TASK_DIR` | `~/.ag2-sparrow/task_dir` |
| `AGENT_CONNECT_RESULT_DIR` | `~/.ag2-sparrow/result_dir` |
| `AGENT_CONNECT_STATE_DIR` | `~/.ag2-sparrow/state` |

Point these at the same queue your worker (e.g. agent-connect) watches. Zero third-party runtime dependencies.

The transport modules (`remote_gateway_bridge`, `_dirs`, `send_allowlist`) are canonical here; the pure shared utilities (`task_archive`, `local_task_protocol`, `result_markers`) are bundled verbatim from [`sonichi/sutando`](https://github.com/sonichi/sutando) `src/` via `tools/sync_from_src.py`.

## License

MIT
