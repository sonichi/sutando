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

Skill adoption arrives through `feature_used` as `skill:<name>`, emitted by
`hooks/skill-usage-telemetry.py` for every skill invoked **through the `Skill`
tool**, on installs where that hook is registered. A skill run some other way,
or an install without the hook, emits nothing. There is no separate skill event.

**Verified at `528e67a6`: no `capture()` call site emits an event outside this
table.** That is a measurement at a commit, not a guarantee — the two emit paths
are bounded differently:

* The **CLI path cannot** add one: `_cli_main` allowlists exactly
  `task_processed` and `feature_used` and exits non-zero on anything else.
* **In-process `capture()` calls are bounded by nothing but review** — the
  function does not inspect the event name. So adding a `capture()` means adding
  a row here in the same change.

### Documented but NOT wired

These were drafted ahead of implementation and **emit nothing today**. Verify
with a call-site grep before citing them anywhere:

| Event | Intended properties | Status |
|-------|--------------------|--------|
| `voice_session` | `duration_bucket` (`<30s` / `30-120s` / `>120s`) | no call site |
| `error` | `type` | no call site |

*(The first table is the source of truth for what is emitted. Move a row up only
in the same change that adds its call site.)*

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

> **Skill-usage coverage:** a `PostToolUse[Skill]` hook
> (`hooks/skill-usage-telemetry.py`) emits `feature_used{feature: "skill:<name>"}`,
> registered by `src/observability/claude/hooks/build-hook-settings.mjs`. It widens
> coverage beyond the hand-instrumented scripts without touching each skill, but only
> for skills invoked **through the `Skill` tool**, and only where the hook is
> registered — see the scoped contract above. Same anonymity and opt-out as any other
> `feature_used`.
