# Telemetry

Sutando collects **anonymous, opt-out product telemetry** so the maintainers can
see how many people run it and which features get used. It is designed so events
can't be attributed to you, and it's easy to turn off.

## What is collected

Only bucketed / categorical **product events**:

| Event | Properties | Why |
|-------|-----------|-----|
| `core_started` | `interval_s` | Count active installs (OSS + desktop) |
| `feature_used` | `feature` (snake_case, e.g. `morning_briefing`, `skill:<name>`) | Which features matter |
| `task_processed` | `source` (`discord`/`telegram`/`slack`; more surfaces as wired) | Activation — whether installs process any tasks after launch, and via which surface |
| `skill_invoked` | `skill` | Skill adoption |
| `voice_session` | `duration_bucket` (`<30s` / `30-120s` / `>120s`) | Voice usage |
| `error` | `type` | Reliability (type only) |

*(Later events are added as they are wired in; this table is the source of truth.)*

## What is NEVER collected

- Task content, message text, prompts, or model output
- Logs, file paths, hostnames, or environment
- Email, name, or any PII
- No autocapture, no session replay, no screen contents

Identity is a **random per-install UUID** stored at
`<workspace>/state/telemetry-id` — not a device fingerprint, not tied to any
account. PostHog creates a "person" for this UUID so installs can be counted as
active users, but that person carries **no PII** — it is just the anonymous
install id.

**On IP addresses:** every event sets `$ip=""` and `$geoip_disable`, so PostHog
does not store or geolocate your IP. Note the network-level source IP is
inherent to any HTTPS request (the same as visiting any website); we simply
instruct the vendor not to record or attribute it.

## How to opt out

Set **any** of these (checked live on every event — takes effect immediately):

```sh
export DO_NOT_TRACK=1        # the cross-project standard (Astro, Bun, Prisma…)
export SUTANDO_TELEMETRY=0
```

…or create the file `<workspace>/state/telemetry-disabled`. In the desktop app,
use the Privacy toggle in Settings.

## Transparency

- All telemetry lives in one file: [`src/telemetry.py`](src/telemetry.py).
- Run with `SUTANDO_DEBUG_TELEMETRY=1` to print every event to stderr **before**
  it is sent, so you can see exactly what would leave the machine.
- Events are a best-effort background POST to PostHog
  (`POSTHOG_HOST`, default US cloud `https://us.i.posthog.com`) over the Python
  standard library — no third-party dependency, never blocking, errors swallowed.
- The PostHog project key (`POSTHOG_API_KEY` / embedded `phc_...`) is **public
  and write-only**; it cannot read data back.

> **Skill-usage breadth (feat/skill-usage-telemetry-hook):** a PostToolUse[Skill] hook (`hooks/skill-usage-telemetry.py`) emits `feature_used{feature: "skill:<name>"}` for EVERY skill the core invokes — auto-registered by `src/observability/claude/hooks/build-hook-settings.mjs`. This broadens feature coverage from hand-instrumented scripts (morning_briefing) to the whole skill surface without touching each skill. Same anonymity + opt-out as `feature_used`.
