# Dead-Man's Switch

Off-machine liveness alerting. Every other layer of Sutando's health stack
(health-check.py, the 30s core heartbeat, the launchd health-check fallback)
runs ON the local machine — when the Mac sleeps, loses power, or dies, they
all go silent together, and from the owner's phone a dead Sutando is
indistinguishable from a quiet one. This skill closes that gap with the
standard dead-man's-switch pattern: a launchd job pings an external monitor
every 5 minutes; the monitor alerts the owner when pings **stop**.

## Semantics

| Machine | Core | What the monitor sees | Owner alert |
| --- | --- | --- | --- |
| awake | alive (`.alive` mtime < 90s) | `GET <url>` every 5 min | none (healthy) |
| awake | dead/wedged | `GET <url>/fail` | instant |
| asleep / dead / offline | — | silence | after grace period |

Pings are **empty GETs** — no hostname, no status payload, nothing about the
system leaves the machine. The URL itself is the secret (a private UUID), so
it lives in the vault, never on disk.

## Setup (one-time)

1. Sign up at https://healthchecks.io (free tier, 20 checks) — or any
   compatible self-hosted instance.
2. Create a check: name `sutando-<host>`, period **5 min**, grace **15 min**
   (tolerates short naps; tighten later if you want faster alerts).
3. Add your alert channels on the monitor side (Discord webhook, email,
   phone push).
4. Send the ping URL through the secure path: `vault set
   HEALTHCHECKS_PING_URL <url>` in Slack/Discord DM.
5. Install the launchd job:
   ```bash
   bash skills/dead-mans-switch/install.sh
   ```
   Installing before step 4 is fine — the job is a silent no-op until the
   vault key exists ("installed but unarmed").

## Verify / drill

- `bash skills/dead-mans-switch/install.sh --status` — job state + log path.
- Missed-ping drill: `bash skills/dead-mans-switch/install.sh --uninstall`,
  wait out the grace period, confirm the alert arrives, re-install.
- Core-down drill: stop the core; the next tick pings `/fail` → instant alert.

## Failure modes

- **No vault key** → silent no-op (exit 0). Supported "unarmed" state.
- **Monitor unreachable** (network down, healthchecks outage) → one stderr
  log line, exit 0. Fail-open: the ping path must never break anything.
  Note the monitor-side effect is the same as machine-asleep — you may get
  a false "down" alert during long offline stretches. That is inherent to
  the pattern, not a bug.
- **TCC block** (launchd bash denied Documents access — the #1897 failure
  mode): caught at install time by the post-install self-test, which
  kickstarts one tick and verifies run evidence within 10s.

## Files

- `scripts/ping.sh` — the ping logic (URL: env > vault; liveness:
  `<workspace>/state/cores/<host>.alive` mtime; test hook:
  `$SUTANDO_DEADMAN_ALIVE_FILE`).
- `launchd/com.sutando.deadman-ping.plist` — job template (300s interval).
- `install.sh` — install / `--uninstall` / `--status`.
- `tests/dead-mans-switch-ping.test.sh` (repo tests/) — hermetic suite with
  a local capture server.
