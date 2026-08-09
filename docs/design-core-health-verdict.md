# Core health: one authoritative verdict + severity-gated action

_Status: design (owner-approved direction 2026-08-02 — "do 1 and 2" of the watchdog
systematic-fix analysis). Implements items #1 (single verdict) and #2 (severity
gate). Consolidates the point-detector sprawl catalogued across ~50 merged
watchdog/health PRs whose dominant failure class was **false-positive detection**
(healthy-but-idle/slow/foreign cores flagged unhealthy, sometimes restarted) and
**consumers disagreeing** because each re-derived liveness from different raw
signals._

## The problem this fixes

Liveness has been read ~6 different ways (pgrep, tmux session, `state/cores/*.alive`
mtime, `core-status.json`, the gateway serving signal, pane classification), and
different consumers — `health-check.py`, the core supervisor
(`core-input-watch.py` / `core-supervisor-relay.py`), Sutando.app, the dashboard —
each pick a different subset. Any one signal can be independently wrong, so the
consumers disagree, and every disagreement has shipped as its own PR (e.g. #2114
idle→"hung", #2466 login-marker erasing the wedged verdict, #2253/#2345 gateway
identity, #2174 ancestor-core blindness, #2072 PATH mis-read "crashed"). Separately,
the restart trigger has been ad-hoc per consumer, so "report vs kill" was decided in
several places (#2248 pulled `--recover-core` from the launchd default; #2404 added a
compound-signal gate for one path only).

## #1 — One authoritative, severity-tagged verdict

`src/runtime-health.py:derive()` already computes the canonical state
(`working | idle | needs_login | offline | wedged`). Promote it to the **sole**
producer and emit a durable artifact every cycle:

    state/core-verdict.json
    {
      "state":    "working|idle|degraded|needs_login|wedged|offline",
      "severity": "ok|warn|escalate|critical",
      "authed":   true|false|null,
      "detail":   "<human string>",
      "signals":  { "process": true, "gateway": true, "status_fresh": true,
                    "pane_login": false, "heartbeat_fresh": true },  # raw inputs, for audit
      # heartbeat_fresh reads state/cores/<host>.alive, written every ~30s by a
      # SEPARATE process (core_heartbeat.py). It is the process-INDEPENDENT
      # corroborator: a genuinely offline core stops beating (process=False AND
      # heartbeat_fresh=False → 2 down-votes → act), while a lone mis-probe on a
      # live core still beats (1 vote → report). Without it, a lingering gateway
      # could make a dead core unrecoverable (qingyun CR on #2527).
      "ts":       <epoch>,
      "confirm":  <consecutive cycles this (state,severity) has held>
    }

Every consumer READS this file instead of re-deriving. No consumer touches raw
signals directly. A new signal source is added in ONE place (derive), and the
`signals` block keeps the decision auditable (the thing missing when #2114/#2466
shipped as separate fixes).

### Severity mapping (state → severity)

| state       | severity  | meaning                                             | action    |
|-------------|-----------|-----------------------------------------------------|-----------|
| working/idle| `ok`      | alive and (acting \| idle-at-prompt)                | none      |
| degraded    | `warn`    | alive but a soft issue (stale quota, memory-index over limit, checkout drift, gateway-down-with-config) | REPORT |
| needs_login | `escalate`| human-only blocker (login / unrecognized prompt)    | REPORT→human, **never restart** |
| wedged      | `critical`| alive but stalled: status stale AND no progress AND no recognized prompt | ACT (gated) |
| offline     | `critical`| process/session gone (nothing alive to kill)        | ACT (gated) |

`degraded` is new: today soft health-check warnings live outside the verdict and
each consumer guesses their weight. Folding them in as `warn` makes "report, don't
act" explicit and uniform (the owner's exact ask, 2026-08-02).

## #2 — Severity gates action

One gate, applied to every restart path (the launchd core-watchdog, Sutando.app
KeepAlive, `health-check.py --recover-core`, and `start-cli` recovery), replacing
the per-consumer ad-hoc logic:

    REPORT  when severity >= warn        (notify only; the ESCALATE surface)
    ACT     only when ALL hold:
              severity == critical
              AND confirm >= N cycles                 (persists, not a blip)
              AND >= 2 independent signals agree       (compound gate, generalizes #2404)
              AND the core is not freshly-booted       (the #2418/#2246 guard)

Consequences, by design:
- A merely-`degraded` (alive) core is **never** restarted — only reported. This is
  the behavior the owner asked for.
- `needs_login` **never** auto-restarts — it escalates to the human (already true in
  `core-supervisor-relay.py`; the gate makes it structural, not incidental).
- `wedged`/`offline` restart only after confirmation + corroboration, killing the
  false-positive-restart class (idle-core-looks-hung, bad-PATH-looks-crashed).
- Optional per the owner's earlier question: a `wedged`→escalate-only toggle
  (`state/recover-wedged-disabled.sentinel`) so even a confirmed wedge waits for a
  one-tap OK before restart, leaving only `offline` (hard-dead) as fully autonomous.

## Rollout (keeps blast radius small)

1. **This PR (first slice):** `derive()` emits `state/core-verdict.json` with the
   `severity` + `signals` + `confirm` fields, plus a `severity_gate(verdict)` helper
   and a fixture suite of the known "healthy-but-looks-dead" cases (idle,
   foreign-checkout, bad-PATH, stale-but-alive) so the false-positive class can't
   regress. No consumer behavior changes yet — additive, safe.
2. Point `health-check.py` core-recovery + the supervisor at `severity_gate()`
   (one consumer at a time, each its own PR).
3. Retire the now-redundant per-consumer liveness derivations.

## Non-goals

Not touching bridge/service health (those are separate `services-status.json`
concerns). Not changing what counts as `working`/`offline` in `derive()` — only
adding the severity layer on top and centralizing who reads it.
