# Health snapshot: `GET /health`

One read-only answer to "how is each agent doing?" for the core and every worker. Code:
`src/health_snapshot.py`; served by agent-api (`src/agent-api.py`, port 7843); tests:
`tests/health-snapshot.test.py`.

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
  "overall": "ok",
  "agents": [
    {"id": "core", "role": "core", "label": null, "alive": true,
     "motion": "idle", "condition": "healthy", "reason": null, "since": null},
    {"id": "40659240fd884f63bcd19fa684b451f1", "role": "worker", "label": null, "alive": true,
     "motion": "idle", "condition": "healthy", "reason": null, "since": null}
  ]
}
```

| Field | Meaning |
|---|---|
| `overall` | `attention` if any agent is abnormal; `ok` if every agent is healthy; otherwise `unknown` |
| `id`, `role` | `core`, or a worker id with role `worker` |
| `label` | the worker's roster label; `null` when it is only the id |
| `alive` | `true` beat fresh, `false` beat stale, `null` no beat file (see [Liveness](#liveness)) |
| `motion` | `idle`, `moving` or `unknown` |
| `condition` | `healthy`, `abnormal` or `unknown` |
| `reason` | why it is abnormal (see [Reasons](#reasons)); `null` otherwise |
| `since` | epoch seconds the abnormal state was first seen, when the source knows it |

`view=full` adds `sources` to each agent: every input with its workspace-relative `path`,
`age_s`, the `value` it read, and the `opinion` it gave. Use it to see why a verdict was reached.

## Sources

Listed in the order they are consulted; the order matters when two sources disagree on the reason.

**Core**

| Source | File | Freshness |
|---|---|---|
| `supervisor` | `state/core-supervisor.json` (written by `core-input-watch.py` on each state change) | none: written on change only, so its age is not staleness |
| `cli_wedge` | `state/cli-wedge/window.jsonl`, classified with `cli_wedge.classify_window` | 180 s for health, 30 s for motion |
| `heartbeat` | `state/cores/<host>.alive` mtime | 90 s |
| `activity` | tail (256 KB) of `state/agent-activity.jsonl`, plus result files | 120 s since the task's last row |
| `self_report` | `state/core-status.json` `status` + `ts` | 90 s |

**Worker** (every roster worker whose state is not `retired`)

| Source | File | Freshness |
|---|---|---|
| `supervisor` | `state/core-supervisor.<session>.json` whose `session` is `<name>-<worker id>` (exact id match) | ignored if written before the worker's current incarnation started (`state/workers/<id>/current.json` + `incarnations.json`) |
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
   file left at `idle-ready`), and those say nothing about now.
2. **Abnormal beats healthy.** Condition is `abnormal` if any source says so, else `healthy` if
   any says so, else `unknown`.
3. **The first abnormal source names the reason**, in the source order above. So the
   supervisor's `needs-login` outranks a `cli_wedge` `retry-loop`.
4. **Moving beats idle.** Motion is `moving` if any source says so, else `idle` if any says so,
   else `unknown`.
5. `alive` comes from the beat alone and is reported beside the verdict, not folded into it.

## Reasons

| Reason | Source | Meaning |
|---|---|---|
| `offline` | beat | no beat for 90 s: the session is gone or its beat writer stopped |
| `needs-login` | supervisor, cli_wedge | the CLI is at a sign-in prompt or refused a turn for lack of a login |
| `login`, `permission`, `selection`, `turn-rejected`, `session-limit`, … | supervisor | a prompt that needs a person (the gate kind) |
| `awaiting-input` | supervisor, cli_wedge | waiting for a person, kind unrecognised |
| `hung` | supervisor | the session is there but the self-report stopped advancing |
| `crashed` | supervisor | the supervisor found no session |
| `gateway-down` | supervisor | the core is up but its gateway bridge is not |
| `retry-loop` | cli_wedge | the pane keeps moving while the CLI retries |
| `quota-limit`, `out-of-credits` | cli_wedge | a provider limit stopped the CLI |
| `compacting`, `api-error`, `network-error` | cli_wedge | parked on that text |
| `not-answering`, `wedged`, `watcher-down` | pool | the pool supervisor escalated the worker |
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

## Privacy

`summary` carries no paths, pane text, host names or task text. It is the shape meant to leave the
Mac. `full` carries workspace-relative paths, the machine's host label in the heartbeat path, and
source values. Keep it local.
