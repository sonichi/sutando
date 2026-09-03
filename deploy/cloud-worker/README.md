# Cloud worker container — one container per user

A **cloud worker** is one more seat of a user's Sutando agent, running on a
cloud host instead of the user's Mac. It shares the agent identity (same
broker token, same MXID); it does not create a second agent. This directory
packages that seat so one container can be provisioned per user:

```
container sutando-worker-<id>
├── gateway client   src/remote-gateway-bridge.py, SUTANDO_WORKER_LOCATION=cloud
│                    pulls GET /v1/tasks?worker=<id> → /workspace/tasks/
│                    watches /workspace/results/ → POST /v1/results (metadata.worker_id=<id>)
└── seat runtime     SUTANDO_WORKER_RUNTIME = stub | claude | ag2-assistant | adapter
                     reads /workspace/tasks/task-*.txt, answers into /workspace/results/
```

The image carries only the client's import graph — the `ag2-sparrow` package,
the loader shim and the seven `src/` modules it imports, the workspace
resolver's shell wrapper, and this directory's scripts. No workspace, no
skills, no secrets are baked in. `/workspace` is a volume; the engine's
`sutando.config.local.json` is written at container start to point at it.

Design: sonichi/sutando issue #3794. Client half (worker id on the wire,
cloud mode): PR #3796. Broker half (per-seat queues, lease failover):
ag2space-backend PR #945. Pool docs: `docs/lead-follower-pool.md` → "Cloud
worker".

## Files

| file | role |
|---|---|
| `Dockerfile` | `base` target (default, ~150 MB): python:3.12-slim, non-root `sutando`, the allowlisted COPYs, HEALTHCHECK, ENTRYPOINT. `claude` target (~380 MB): adds the Claude Code CLI via its native installer, no node. |
| `entrypoint.sh` | validates env, writes the workspace config, starts the client and the seat runtime, exits when either dies (the restart policy relaunches). |
| `seat-stub.py` | `stub` runtime: answers every task with `answered by <worker id>`. |
| `seat-ag2-assistant.py` | `ag2-assistant` runtime: one ACP session per task against the AG2 Assistant sidecar, result signed `— <worker id> (ag2-assistant)`. |
| `assistant-bootstrap.sh` | runs *inside* the sidecar (mounted at `/bootstrap.sh`): first-boot profile, provider-key seed into its secret store, then `acp-serve`. |
| `healthcheck.sh` | healthy while `state/gateway-status.<instance>.json` is younger than 3 minutes (the client rewrites it on every poll). |
| `docker-compose.yml` | one worker service (+ one ag2-assistant sidecar) per user; copy the blocks to scale. |
| `.env.example` | the env contract (keys, no values). |
| `provision.sh` / `deprovision.sh` | one container per user, idempotent, from a per-user env file. |

## Env contract

Required — `entrypoint.sh` exits 2 naming every missing key:

| key | meaning |
|---|---|
| `REMOTE_TASK_URL` | broker base URL the user's agent enrolled with |
| `REMOTE_TASK_TOKEN` | that user's agent token (bare secret, or the combined onboarding string) |
| `SUTANDO_WORKER_ID` | this seat's name on the wire, `[A-Za-z0-9_-]{1,32}` — `provision.sh` sets it from argv |

Set by the entrypoint: `SUTANDO_WORKER_LOCATION=cloud` (always),
`GATEWAY_INSTANCE` (default `cloud`; names the status file),
`SUTANDO_SUPERVISED=1`.

Optional: `SUTANDO_WORKER_RUNTIME` (`stub` default), `REMOTE_TASK_POLL_WAIT`,
`AGENT_MXID`, `CLAUDE_CONFIG_DIR` (claude runtime only), every other
`REMOTE_*` knob the client documents in its docstring. Full list with
comments: `.env.example`.

Runtime `ag2-assistant` adds: `AG2ASSISTANT_ACP_TOKEN` (required — the shared
secret the sidecar's `acp-serve --token` checks on the WebSocket upgrade),
`GEMINI_API_KEY` (sidecar only — the worker never reads it),
`AG2ASSISTANT_ACP_URL` (default `ws://assistant:8802`, the alias `provision.sh`
gives the sidecar), `SUTANDO_ACP_TURN_TIMEOUT_S` (default 300).

## Provisioning one user

```bash
cp deploy/cloud-worker/.env.example /somewhere/private/alice.env   # fill URL + token
bash deploy/cloud-worker/provision.sh cloud-alice /somewhere/private/alice.env --build
docker logs -f sutando-worker-cloud-alice
bash deploy/cloud-worker/deprovision.sh cloud-alice            # keep the volume
bash deploy/cloud-worker/deprovision.sh cloud-alice --purge    # drop it too
```

`provision.sh` builds the image when it is absent (or on `--build`), trying
`docker pull $SUTANDO_CLOUD_WORKER_IMAGE` first; the build context is a
scratch directory holding only the Dockerfile's COPY sources, so the working
tree never reaches the daemon. The target defaults to `base`; `--target
claude` builds the CLI seat. Re-running it on a running container is a no-op (exit 0); on a
stopped one it starts it. Extra `docker run` flags go after `--`.

N users = N calls with N env files (one token + one worker id each), or N
service blocks in `docker-compose.yml`.

## Seat runtimes and their auth

| `SUTANDO_WORKER_RUNTIME` | what runs | auth |
|---|---|---|
| `stub` (default) | `seat-stub.py` — answers `answered by <worker id>` | none; it is the test double |
| `claude` | the Claude Code CLI seat, `claude --dangerously-skip-permissions --add-dir /workspace -- /proactive-loop`, the way the pool's `pool-core-wrapper.sh` launches a follower (no tmux, the container is the session) | Claude subscription, one **OAuth login per container**, stored under `CLAUDE_CONFIG_DIR` on the volume |
| `ag2-assistant` | `seat-ag2-assistant.py` — the **backup seat**: one ACP session per task against an [AG2 Assistant](https://github.com/ag2ai/ag2-assistant) sidecar (`ghcr.io/ag2ai/ag2-assistant`, `acp-serve` on 8802) | the sidecar's model key (`GEMINI_API_KEY`) + the shared `AG2ASSISTANT_ACP_TOKEN`; no login |
| `adapter` | `bash /workspace/runtime.sh` if executable, else exit 4 | whatever the adapter needs — an Agent SDK or codex app-server seat takes an **API key** in env |

Credential order the owner set: the user's Claude **subscription** (`claude`),
then the user's own **API key** (`adapter`), then **our key** as the backup
(`ag2-assistant` on a Gemini key we hold).

### Runtime: claude

Build the `claude` target and log in once, inside the container, with the
config dir on the volume. Nothing is copied from a Mac — no
`.credentials.json`, no `.claude.json`:

```bash
bash deploy/cloud-worker/provision.sh cloud-alice alice.env --build --target claude
docker exec -it sutando-worker-cloud-alice \
  env CLAUDE_CONFIG_DIR=/workspace/claude-config claude auth login
# follow the printed URL / code in a browser; the credential lands on the volume
docker exec sutando-worker-cloud-alice \
  env CLAUDE_CONFIG_DIR=/workspace/claude-config claude auth status
docker restart sutando-worker-cloud-alice    # the seat starts once login exists
```

`claude setup-token` (subscription) or `CLAUDE_CODE_OAUTH_TOKEN` in the env
file is the non-interactive alternative; the token is per user and lives in
that user's env file, never in the image. `--dangerously-skip-permissions`
is why the image runs as a non-root user (the CLI refuses it as root).

The `/proactive-loop` skill shells into `scripts/` and `skills/` that the
minimal image does not carry. If `/workspace/engine` exists (a full engine
checkout on the volume) the seat runs from there; otherwise it runs from the
minimal tree and only the task loop works. Populating the skill set for the
container is not done in this slice.

### Runtime: ag2-assistant (backup seat)

A different brain, on purpose: AG2 Assistant has its own memory, tools and
profile, and none of Sutando's skills. It is the cheap always-on seat that
answers when the user's own seats are down, not a replacement for them.

The official image runs as a **sidecar** — it is not baked into our image
(its dependency set is heavy) — serving ONE headless profile over ACP
WebSocket (`ag2-assistant acp-serve --host 0.0.0.0 --port 8802 --token …`,
experimental). `provision.sh` starts it automatically when the env file says
`SUTANDO_WORKER_RUNTIME=ag2-assistant` (or with `--with-assistant`): a
per-user docker network, the sidecar `sutando-assistant-<id>` on alias
`assistant`, volumes `<id>-data:/data` and `<id>-workspace:/workspace`, and
only its own keys forwarded (`AG2ASSISTANT_ACP_TOKEN`, `GEMINI_API_KEY`,
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `TZ`) — the broker token never reaches
it. The sidecar's entrypoint is our `assistant-bootstrap.sh`, mounted
read-only: it runs `ag2-assistant profiles create backup` when
`/data/profiles.json` is absent (registry-only; the default profile is
Gemini), **seeds the provider key into the sidecar's secret store**
(`SecretStore.set_key`, idempotent), then `exec`s `acp-serve`. The seed is
needed because `acp-serve` gates every session on the store, not on the
process env — its `serve_ws(...)` call passes no `env`, so the `env_var` auth
method it advertises cannot be satisfied by `GEMINI_API_KEY` alone (measured:
`session/new → Authentication required` with the key in env; `authMethods: []`
and a session id after the seed). `docker-compose.yml` carries the same pair
as a template.

The seat (`seat-ag2-assistant.py`) is an adapter loop over the same file
contract as the stub: for each pending `tasks/task-<id>.txt` it dials the
sidecar with the bearer token (the ACP SDK's `acp.ws.client`, the package
ag2-assistant itself imports; installed in our image as
`agent-client-protocol[http]`), sends `initialize` → `session/new` →
`session/prompt` with the `task:` body, concatenates the
`agent_message_chunk` text, and writes `results/task-<id>.txt` ending with
`— <worker id> (ag2-assistant)`. A permission request from the agent is
answered `cancelled` (approvals are owner-side in ag2-assistant). Every turn
is bounded by `SUTANDO_ACP_TURN_TIMEOUT_S` (300 s): the dial retries with
backoff while the sidecar boots, and a timeout or transport failure still
writes a short, signed failure result so the task is never swallowed.

```bash
# alice.env: SUTANDO_WORKER_RUNTIME=ag2-assistant, AG2ASSISTANT_ACP_TOKEN=<random>, GEMINI_API_KEY=<ours>
bash deploy/cloud-worker/provision.sh cloud-alice alice.env
docker logs sutando-assistant-cloud-alice     # "acp-serve (experimental) listening on ws://0.0.0.0:8802"
```

### Runtime: adapter

The slot for the seat runtimes that do not exist yet (Agent SDK, codex
app-server). Put an executable `runtime.sh` on the volume; it is run with the
seat env (`SUTANDO_WORKER_ID`, `SUTANDO_WORKER_LOCATION`, `SUTANDO_CLOUD_WORKSPACE`)
and must read `/workspace/tasks/task-*.txt` and write
`/workspace/results/task-<id>.txt`. Without it the container exits 4 and stays
down.

## At-least-once, failover, dedupe

The broker leases a task to the seat that pulled it. A seat that dies mid-run
stops heartbeating; the lease expires and the broker re-fronts the task, and
with ag2space-backend #945's per-seat dispatch on, the policy re-picks with
the dead seat excluded — that is how a task lands on `cloud-1` when the Mac's
`worker-1` is down, and vice versa. Consequences:

- delivery is **at-least-once**: a task on a worker that dies is re-served;
- results **dedupe by task id** at the sink, and the client never queues an id
  it already holds (pending, `.assigned-*`, `.claimed-*`) — see the client
  docstring "Lease safety";
- the residual hazard is the broker's `RELAY_VISIBILITY_TIMEOUT` being shorter
  than an honest task: set it above the longest one, or two seats answer.

With the #945 flag off the broker keeps one queue per agent and ignores
`worker=`; the container still works — it is simply another puller of the
same queue.

## Health

`HEALTHCHECK` runs `healthcheck.sh` every 60 s (45 s start period, 3
retries): healthy while `/workspace/state/gateway-status.<instance>.json` was
written within 3 minutes. The client rewrites it after every poll
(`REMOTE_TASK_POLL_WAIT`, 25 s), connected or not; a client that hangs or
dies stops rewriting it. `docker inspect -f '{{.State.Health.Status}}'`.

## Not done in this slice

- no image registry publishing (`provision.sh` builds locally; `docker pull`
  is tried only if `SUTANDO_CLOUD_WORKER_IMAGE` names a pushed tag);
- no cloud IaC (no Terraform/Fly/ECS definitions — compose + scripts only);
- no login automation for the `claude` runtime (per-container device login,
  by hand, documented above);
- no skill set in the image for the `claude` runtime beyond the task loop;
- the Agent SDK / codex app-server adapters (`adapter` is the slot);
- the ag2-assistant sidecar is `:latest` (no digest pin) and its `acp-serve`
  is marked experimental upstream; the sidecar's own memory/tools are not
  wired to Sutando's;
- no workspace sync into the container: the seat's memory is the broker's room
  history (#3794 §5a) until the sync lane covers cloud hosts.

## Tests

`tests/cloud-worker-container.test.sh` (hermetic, no docker daemon): the
Dockerfile copies only allowlisted paths, `entrypoint.sh` refuses each missing
required env var, writes the workspace config and marks the seat `cloud`,
`provision.sh` refuses a missing env file and (under a fake `docker`) issues
the right `run`/`start`/`build`/sidecar/network calls from a staged context of
only the allowlisted paths, the ag2-assistant seat completes an ACP turn over
an in-process transport (chunks → signed result; silent agent → signed
timeout result; dead sidecar → signed failure), the compose file parses, and
`healthcheck.sh` distinguishes fresh / stale / absent status files.
