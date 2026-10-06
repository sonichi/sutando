# Health snapshot: `GET /health`

One read-only answer to "how is each agent doing?" for the core and every worker. Code:
`src/health_snapshot.py`; served by agent-api (`src/agent-api.py`, port 7843); tests:
`tests/health-snapshot.test.py`, `tests/gateway-health-push.test.py`; observation records:
`src/runtime_observation.py`, `tests/runtime-observation.test.py`.

It reads files the existing watchers already write. It probes no process or pane and writes
nothing, so calling it often is safe and it answers even when the core is down.

## Request

```
GET /health?agent=<which>&view=<view>
python3 src/health_snapshot.py [--agent <which>] [--view <view>] [--workspace <dir>]
```

| Parameter | Values | Default |
|---|---|---|
| `agent` | `all`, `core`, `workers`, or one worker id | `all` |
| `view` | `summary`, `full` | `summary` |

| Response | When |
|---|---|
| `200` | always, whatever the agents' state; an unknown worker id gives an empty `agents` list |
| `400` | `view` is not `summary` or `full` |
| `401` | an API token is configured and not sent |
| `403` | `view=full` from a browser (`Origin` header) or a non-loopback client without the token |

A `200` answer carries no CORS headers, so a web page on another origin cannot read either view.

## Response

```json
{
  "checked_at": 1790566151.7,
  "instance": "@mark-desktop.agent:ag2.space",
  "overall": "ok",
  "suspended": null,
  "agents": [
    {"id": "core", "role": "core", "label": null, "session": "sutando-core", "alive": true,
     "motion": "idle", "condition": "healthy", "reason": null, "since": null},
    {"id": "40659240fd884f63bcd19fa684b451f1", "role": "worker", "label": null,
     "session": "sutando-worker-40659240fd884f63bcd19fa684b451f1", "alive": true,
     "motion": "idle", "condition": "healthy", "reason": null, "since": null}
  ]
}
```

| Field | Meaning |
|---|---|
| `instance` | the agent id the serving gateway lane signed in as (the `agent_id` it writes to `state/gateway-status[.<lane>].json` while connected). `null` when no lane is serving within 180 s, or two serving lanes name different ids: a wrong id would show this Mac twice |
| `overall` | `attention` if any agent is abnormal; `ok` if every agent is healthy; otherwise `unknown` |
| `suspended` | `{"reason", "at"}` while `state/pool-suspended` exists (the app quit and paused the pool); `null` otherwise |
| `id`, `role` | `core`, or a worker id with role `worker` |
| `label` | the worker's roster label; `null` when it is only the id |
| `session` | the tmux session to open for this agent, as its beat file (core) or supervisor file names it; `null` when no file names one. Never guessed |
| `alive` | `true` beat fresh, `false` beat stale or the supervisor saw the session crash, `null` no beat file |
| `motion` | `idle`, `moving` or `unknown` |
| `condition` | `healthy`, `abnormal` or `unknown` |
| `reason` | why it is abnormal (see [Reasons](#reasons)), or `suspended` for a worker the pool's suspension took down; `null` otherwise |
| `since` | epoch seconds the abnormal state was first seen, when the source knows it |

`view=full` adds `sources` to each agent: every input with its workspace-relative `path`,
`age_s`, the `value` it read, and the `opinion` it gave. Use it to see why a verdict was reached.

## Sources

Listed in the order they are consulted; the order matters when two sources disagree on the reason.

**Core**

| Source | File | Freshness |
|---|---|---|
| `supervisor` | `state/core-supervisor.json` (written by `core-input-watch.py` on each state change) | none: written on change only, so its age is not staleness. A `crashed` verdict is ignored when a fresh beat written after it recorded a live core pane (its `pid` is the core's, not the beat writer's `heartbeat_pid`) |
| `observation` | `state/runtime-observations/core.json` (see [Runtime observation](#runtime-observation)) | 45 s lease on the record's own heartbeat |
| `cli_wedge` | `state/cli-wedge/window.jsonl`, classified with `cli_wedge.classify_window` | 180 s for health, 30 s for motion |
| `heartbeat` | `state/cores/<host>.alive` mtime | 90 s |
| `activity` | tail (256 KB) of `state/agent-activity.jsonl`, plus result files | 120 s since the task's last row |
| `self_report` | `state/core-status.json` `status` + `ts` | 90 s |

**Worker** (every roster worker whose state is not `retired`)

| Source | File | Freshness |
|---|---|---|
| `supervisor` | `state/core-supervisor.<session>.json` whose `session` is `<name>-<worker id>` (exact id match) | ignored if written before the worker's current incarnation started (`state/workers/<id>/current.json` + `incarnations.json`) |
| `observation` | `state/runtime-observations/<id>.json` | 45 s lease; ignored if the observer started before the worker's current incarnation |
| `watcher_beat` | `state/watchers/<id>.alive` mtime | 90 s |
| `pool` | the worker's entry in `state/pool-supervision.json` | 900 s since `last_sample_at` (3 missed 300 s samples) |
| `roster` | the worker's `state` in `state/roster.json` | none |
| `activity` | as for the core, for tasks delivered to `deliveries/<id>/` | 120 s |

## What each source says

A source gives an **opinion** (`motion`, `condition`, `reason`, `since`) or none. Any field of an
opinion may be empty.

**Supervisor** (core and worker):

| Supervisor state | motion | condition | reason |
|---|---|---|---|
| `running` | moving | healthy | |
| `idle-ready` | idle | healthy | |
| `blocked-known` (auto-answered gate) | idle | healthy | |
| `blocked-human` | idle | abnormal | the gate `kind` (e.g. `permission`, `turn-rejected`), else `awaiting-input` |
| `logged-out` | idle | abnormal | `needs-login` |
| `hung` | idle | abnormal | `hung` |
| `crashed` | | abnormal | `crashed` |
| `gateway-down` | | abnormal | `gateway-down` |
| `unobserved` | no opinion | | |

`since` for an abnormal supervisor state is the file's mtime.

**cli_wedge** (core only), when its last sample is at most 180 s old:

| cli_wedge kind | motion | condition | reason |
|---|---|---|---|
| `working` | moving if the sample is at most 30 s old, else none | healthy | |
| `idle` | idle | healthy | |
| `retry-loop` | moving | abnormal | `retry-loop` |
| `provider-limit` | idle if the pane is static, else moving | abnormal | `quota-limit` or `out-of-credits` |
| `abnormal` | idle if the pane is static, else moving | abnormal | the first matched pattern (`needs-login`, `awaiting-input`, `compacting`, `api-error`, `network-error`) |
| `unknown`, `cadence-too-sparse` | no opinion | | |

`since` is the start of the current observation run.

**Observation** (core and worker): see [Runtime observation](#runtime-observation).

**Beats** (`heartbeat`, `watcher_beat`): missing → no opinion (a desktop core has no heartbeat
for about 2 minutes after boot); older than 90 s, or more than 5 s in the future → abnormal,
`offline`; otherwise no opinion (a beat proves the session exists, not that it is healthy).

**Self-report**: `running` with a `ts` at most 90 s old → moving. Anything else → no opinion.

**Activity**: a task is live while it has rows, no `done` row, no result file
(`results/<id>.txt`, or `results/archive/[<YYYY-MM>/]<id>[-<ts>].txt`), and its newest row is at most 120 s old.
A live task → moving for the agent it was delivered to (a worker if
`deliveries/<worker id>/<task id>.txt` exists, else the core).

**Pool**: when the sample is fresh, `escalated` → abnormal `not-answering`; `wedge_escalated` →
abnormal `wedged`; `watcher_escalated` → abnormal `watcher-down`. The counters alone give no
opinion; the pool supervisor only escalates after several confirming samples.

**Roster**: a state other than `live` (e.g. `recovering`, `abandoned`) → abnormal with that
state as the reason. `retired` workers are left out of the response.

## How the opinions combine

1. **Offline wins outright.** If any source says `offline`, the agent is `unknown · abnormal ·
   offline`, whatever else it says. A dead agent's files keep their last words (a supervisor
   file left at `idle-ready`), and those say nothing about now. The one exception: when a fresh
   pool sample says it gave up on the worker, the reason is `not-answering`, since that needs a
   person and a plain `offline` does not. Its `since` is the pool's first detection, or the
   beat's when the pool did not record one.
2. **Abnormal beats healthy.** Condition is `abnormal` if any source says so, else `healthy` if
   any says so, else `unknown`.
3. **The first abnormal source names the reason**, in the source order above. So the
   supervisor's `needs-login` outranks a `cli_wedge` `retry-loop`.
4. **Moving beats idle.** Motion is `moving` if any source says so, else `idle` if any says so,
   else `unknown`.
5. `alive` comes from the beat and is reported beside the verdict, not folded into it. The
   one exception is a `crashed` verdict that survives the freshness rules above, which makes
   it `false`: a worker's inbox watcher, and the core's heartbeat writer, run apart from the
   session and outlive it. For a worker this needs its current incarnation to be readable:
   without it the verdict cannot be shown to be this run's, so `alive` stays the beat's while
   the condition still reads `crashed`.
6. **A worker the suspension took down** (listed in `state/pool-suspended`'s `stopped`) reads
   `alive: false · unknown · unknown`, reason `suspended`, since the suspension's `at`, whatever
   its files say. A quit kills the tmux server outright, so no seat records its own end and its
   beat stays fresh for up to 90 s. The pool's resume lifts this.

## Runtime observation

Whatever watches a seat's CLI from inside it can publish one record per seat, validated and
written atomically by `src/runtime_observation.py` (`python3 src/runtime_observation.py write`,
one JSON record on stdin). The record carries `phase`, `motion`, `condition`, `reason`,
`condition_since`, `last_success_at` and its own `heartbeat_at`. The snapshot names no observer and
its wire shape does not change: the record only feeds the same five fields.

The `observation` source gives **no opinion** when the record is:

- missing, invalid, or past its 45 s lease (`heartbeat_at`), or dated more than 5 s ahead;
- from a different tmux session than the one the seat's beat or supervisor file names (when one is);
- for a worker, from an observer that started before the worker's current incarnation (the source
  value reads `previous_run`).

Otherwise its opinion is the record's motion and condition (`unknown` gives none); when abnormal,
the reason is the record's and `since` is `condition_since`. Its `full` value is `phase`,
`observer`, `observer_version`, `seq`, `heartbeat_age_s` and `last_success_age_s`; no session ids.

**Positive recovery.** A completed model request disproves an earlier pane-derived claim. When
the observation is valid and has `last_success_at`, a `supervisor` or `cli_wedge` opinion is
dropped (its value gains `"superseded_by": "observation"`) if it is abnormal, its reason is one of
`needs-login`, `login`, `quota-limit`, `out-of-credits`, `session-limit`, `api-error`,
`network-error`, and its claim time (`since`, else the source's mtime) is older than
`last_success_at`. Nothing else is ever dropped: `crashed`, `hung`, `offline`, `gateway-down`,
`retry-loop`, the pool and roster states, and `suspended` stand regardless, and `alive` is
untouched.

A record is abnormal exactly when it carries a reason; the writer rejects anything else. The core
has no incarnation record, so for it only the session match and the lease apply. An observed
failure does not age: a seat whose last request failed stays abnormal, its lease renewed, until a
request completes or the observer stops.

Seats with no observer (the Codex runtime, an older engine, the observer disabled) have no record:
their verdicts and the `summary` view are exactly what the other sources say, and `full` only gains
an empty `observation` source.

## Reasons

| Reason | Source | Meaning |
|---|---|---|
| `offline` | beat | no beat for 90 s: the session is gone or its beat writer stopped |
| `suspended` | `state/pool-suspended` | the app quit took this worker down; condition stays `unknown` and it never alerts |
| `needs-login` | supervisor, cli_wedge | the CLI is at a sign-in prompt or refused a turn for lack of a login |
| `login`, `permission`, `selection`, `turn-rejected`, `session-limit`, … | supervisor | a prompt that needs a person (the gate kind) |
| `awaiting-input` | supervisor, cli_wedge | waiting for a person, kind unrecognised |
| `hung` | supervisor | the session is there but the self-report stopped advancing, and the pane shows neither the idle footer nor a turn in flight that changed since the last poll |
| `crashed` | supervisor | the supervisor found no session |
| `gateway-down` | supervisor | the core is up but its gateway bridge is not |
| `retry-loop` | cli_wedge | the pane keeps moving while the CLI retries |
| `quota-limit`, `out-of-credits` | cli_wedge | a provider limit stopped the CLI |
| `compacting`, `api-error`, `network-error` | cli_wedge | parked on that text |
| `not-answering`, `wedged`, `watcher-down` | pool | the pool supervisor escalated the worker |
| `needs-login`, `quota-limit`, `out-of-credits`, `api-error`, `permission`, `awaiting-input` | observation | what the observer saw the CLI report |
| `recovering`, `abandoned` | roster | the roster's own state for the worker |

## Worked example

The core, idle, from `view=full` (2026-09-28):

```json
"sources": {
  "supervisor":  {"age_s": 530.0,  "value": {"state": "idle-ready"}, "opinion": {"motion": "idle", "condition": "healthy"}},
  "cli_wedge":   {"age_s": 2933.3, "value": {"kind": "unknown"},      "opinion": null},
  "heartbeat":   {"age_s": 9.2,    "value": "fresh",                  "opinion": null},
  "activity":    {"age_s": null,   "value": "no live task",           "opinion": null},
  "self_report": {"age_s": 533.1,  "value": {"status": "idle"},       "opinion": null}
}
```

Only the supervisor has an opinion. The `cli_wedge` sample is 49 minutes old, so it gives none,
and the self-report is idle. Result: `alive: true` (fresh heartbeat), `idle · healthy`.

## Known blind spots

- **A silent freeze reads healthy.** A CLI that hangs with no text on screen looks like a finished
  turn to every source.
- **`cli_wedge` is rarely fresh.** It only samples when `health-check.py` runs (standalone: every
  5–30 min; desktop: only while the dashboard page is open), so retry loops and limits are often
  unobserved. Sampling it inside the supervisor loop would close this.
- **The saved login is not checked.** After `/logout`, running CLIs keep working on their
  in-memory login, while any restart fails. `claude auth status` would show it.
- **The supervisor file is trusted until it changes.** For the core there is no incarnation check,
  so a dead core monitor's last state stands until the heartbeat goes stale.
- **Moving can lag** by up to 120 s for a task that never writes a result, and by up to 90 s from
  a stale `running` self-report.

## Reaching AG2 Space

The gateway bridge computes this snapshot in-process, so a Mac or cloud Sutando without agent-api
still reports. It sends the core's `alive`/`motion`/`condition`/`reason`/`since` on the heartbeat
and each worker's on the workers report, plus `suspended` (see
[`remote-gateway-protocol.md`](remote-gateway-protocol.md)). `instance`, `session`, `label` and the
`full` view stay on the Mac.

## Privacy

`summary` carries no paths, pane text, host names or task text. It is the shape meant to leave the
Mac. `full` carries workspace-relative paths, the machine's host label in the heartbeat path, and
source values. Keep it local.
