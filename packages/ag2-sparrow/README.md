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

## Single source of truth

These modules are the canonical AG2 Space relay client, kept in lockstep with
[`sonichi/sutando`](https://github.com/sonichi/sutando) `src/`. They are copied
verbatim (never forked) via `tools/sync_from_src.py`; a CI drift-check fails if
the package diverges from the source. Zero third-party runtime dependencies.

## License

MIT
