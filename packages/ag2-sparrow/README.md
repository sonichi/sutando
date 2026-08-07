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

## Optional: room-event subscription (0.3.0)

Off by default. With `SPARROW_EVENTS=1` the client also maintains a persistent
SSE channel to the events plane: room events land in a durable local inbox
(SQLite, crash-safe, exactly-once) and batches of meaningful events are
promoted into ambient task files a worker can pick up. Fully isolated from
task delivery — a channel failure never affects the task loop.

| Env | Meaning |
|---|---|
| `SPARROW_EVENTS` | `1` enables the event channel + consumer (default off) |
| `SPARROW_HA_OWNER` | owner mxid; enables human-action decision routing — the owner's typed/reacted answers to pending question cards resolve them |
| `SPARROW_HA_ROOM` | room id where question cards are posted (with `SPARROW_HA_OWNER`) |
| `SPARROW_HA_A2UI` | `1` attaches interactive A2UI blocks to cards (default off; requires a client that renders them) |


## Optional: Bee wearable source (0.3.0)

`sutando-bee-watcher` subscribes to the [Bee](https://bee.computer) developer
surface and turns selected events into tasks. Bee pushes no webhooks; its
surface is an authenticated local proxy (after `bee login`) or the cloud API —
so the watcher runs client-side, where the credentials live, and dials out.

Two subscription modes (exactly one is used; API base wins): the local proxy
(`BEE_PROXY_URL`), or direct-to-cloud with a bearer (`BEE_API_BASE` +
`BEE_API_TOKEN`) for an always-on headless container.

Three delivery sinks (`BEE_SINK`): `broker` posts through the AG2 Space
broker's authenticated inbound hop (`/v1/ingest`; results route to the Bee
fallback DM room); `local` writes task files onto the same file bridge voice
and Discord use — the fully-OSS, no-broker mode; `inbox` delivers into the
durable EventInbox and drains through the shared taskify consumer.

Every Bee-derived task is stamped `access_tier: ambient`, never `owner`:
device-captured speech is an observation the owner never consciously issued
as a command, so privileged actions must surface for approval rather than
execute. Tier is the authorization boundary; body-injection defang is
separate and also applied.

| Env | Meaning |
|---|---|
| `BEE_PROXY_URL` | Bee local proxy base (required unless `BEE_API_BASE` set; empty → exit 2) |
| `BEE_EVENTS_PATH` | SSE path on the proxy (default `/v1/stream`) |
| `BEE_EVENT_TYPES` | comma-list of SSE types to forward (default `todo-created,todo-updated`; the per-utterance stream would flood the queue) |
| `BEE_API_BASE` / `BEE_API_TOKEN` | cloud-direct mode (vault-preferred token) |
| `BEE_BROKER_URL` / `BEE_BROKER_TOKEN` | broker sink target + bearer (agent record needs `"ingest": true`) |
| `BEE_AGENT_ID` | relay agent whose queue receives Bee tasks |
| `BEE_CURSOR_FILE` | resume-cursor path (required headless; defaults under the local workspace) |
| `BEE_INBOX_FILE` | inbox sink's OWN sqlite (never share the gateway channel's inbox — its `MAX(cursor)` is that channel's resume anchor) |
| `BEE_SINK` | `broker` (default) \| `local` \| `inbox` |

Resume: the last delivered SSE event id persists and replays as
`Last-Event-ID` on reconnect; a failed delivery halts the stream rather than
skipping ahead, and enqueue is idempotent by task id.

## Directories & single source

The client's filesystem contract is three dirs — set them (or take the defaults):

| Env | Default |
|---|---|
| `AGENT_CONNECT_TASK_DIR` | `~/.ag2-sparrow/task_dir` |
| `AGENT_CONNECT_RESULT_DIR` | `~/.ag2-sparrow/result_dir` |
| `AGENT_CONNECT_STATE_DIR` | `~/.ag2-sparrow/state` |

Point these at the same queue your worker (e.g. agent-connect) watches. Zero third-party runtime dependencies.

The transport modules (`remote_gateway_bridge`, `event_channel`, `event_inbox`, `event_consumer`, `human_action`, `_dirs`, `send_allowlist`) are canonical here; the pure shared utilities (`task_archive`, `local_task_protocol`, `result_markers`, `workspace_lock`) are bundled verbatim from [`sonichi/sutando`](https://github.com/sonichi/sutando) `src/` via `tools/sync_from_src.py`.

## License

MIT
