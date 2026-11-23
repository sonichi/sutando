---
name: quota-tracker
description: "Track Claude Code quota usage via Anthropic API rate limit headers. Shows 5h and 7d utilization, reset times, and quota status. Works with both subscription and API key auth."
---

# Quota Tracker

Monitor your Claude Code quota in real time by intercepting Anthropic API rate limit headers.

## When to Use

- "How much quota do I have left?"
- "Am I close to the rate limit?"
- "When does my quota reset?"
- Before starting expensive tasks

## How It Works

A credential proxy sits between Claude Code and the Anthropic API. It reads `anthropic-ratelimit-unified-*` headers from every API response and writes quota state to a JSON file.

Without the proxy, the status line recorder writes the same file from Claude Code's own reports (next section). Both sources write one record; the newest write wins.

## Without the proxy: the status line recorder

On by default. At every Claude session launch (the core and each pool worker), the skill's `on_session_config` hook (`scripts/install-statusline.py`) writes two entries into that session's config-dir `settings.json`:

- `statusLine`: `scripts/statusline-record.py`, refreshed every 60 seconds. It reads the status line input's `rate_limits.five_hour` / `seven_day` (`used_percentage`, `resets_at`) and records them through `src/quota_record.py`, with `source: "statusline"`, the proxy's field shape and synthesized `anthropic-ratelimit-unified-5h/7d-utilization` and `-reset` headers. An observation older than the record on disk is dropped, so an idle session cannot overwrite a newer one; the observation time is the session transcript's last change. Each write is keyed by the writing session's login, a digest of the non-secret `oauthAccount` identity in its config dir's `.claude.json`. The top level is the core's login only, and every login's latest entry is under `accounts`. An entry is continued only from the same login's earlier entry: after a login change in one config dir, the old login's numbers and rejections are not carried over. A session with no `oauthAccount` is recorded under a digest of its config dir, as advisory only: it never becomes the top level, never seeds or clears anything, and no reader uses it. Readers trust a status line record only when its `account` matches the account the core's config dir is signed in to now. A worker signed in to another account never changes the core's numbers, and its rate-limit stop never marks the core rejected; a worker on the core's account updates them.
- A `StopFailure` hook (`matcher: rate_limit`, `scripts/stopfailure-record.py`): a turn that ends on a rate limit appends a 429 to `recent_rejections` (the newest 20 are kept) and marks the record rejected. The rejection holds until the same login observes, after the rejection's timestamp, a reply whose numbers moved with every window under its limit; an older observation, a re-render of unchanged numbers, or a record not bound to a login never clears it, and readers refuse a status line record with a 429 at or after its own observation. Turning the recorder off restores the saved status line, reads the settings back, and only then removes the saved copy.

If the config dir already had a status line, the installer saves it beside that settings file as `quota-statusline-passthrough.json` and the recorder pipes the same input to it and prints its output unchanged; otherwise the recorder prints a short line (model, 5h and 7d percentages). Turning the recorder off puts the saved status line back.

Config (`manifest.json` `config`; env overrides):

- `QUOTA_STATUSLINE_ENABLED` (default `true`): `false` removes the recorder and hook at the next launch, and routes sessions through the proxy again.
- `QUOTA_STATUSLINE_PASSTHROUGH`: a status line command to pass through to instead of the saved one.

With `source: "statusline"`, the readers (`src/quota_availability.py`, health-check's `credential-proxy` and `quota-telemetry` rows) treat a missing proxy as expected, not as a failure. `quota-telemetry` warns when the record's `last_checked` is more than 10 minutes (the gate's freshness window) older than the core's last recorded work.

What it gives up compared with the proxy:

- no pre-send `rejected` or overage status: the headline is `allowed` until a StopFailure says otherwise, and no `overage-status` is recorded;
- only 429s, from StopFailure; other upstream rejections are not recorded;
- percentages only to the precision Claude Code reports (often whole numbers);
- nothing for API-key, Bedrock or Vertex logins (Claude Code sends no `rate_limits` there);
- a refresh only when a session on that login gets a reply; between replies the record ages and goes stale as usual.

## Sessions call Anthropic directly

This is the default after upgrading, for new and existing installs alike, and there is no fallback to the proxy. A session that worked through the proxy may have no login of its own, because the proxy injected the credential. If so, you are asked to sign in: health-check's `claude-sign-in` row fails, naming the config dir, and your DM gets one message a day until it is signed in. The message carries the exact command to run in a terminal on this computer, for example:

```bash
CLAUDE_CONFIG_DIR='<config dir>' claude auth login
```

Follow the link it prints and sign in; the next session launch picks the login up. The check (`src/claude_signed_in.py`) runs `claude auth status --json` with the environment the direct session gets and counts only exit 0 with `"loggedIn": true`; a probe that times out (8 seconds) or a missing `claude` is unknown and sends no message. Config dirs are probed four at a time, and the daily message markers are removed after 7 days. It never changes routing and never reads a credential.

With the recorder on, Sutando's Claude sessions (the core and every pool worker) no longer go through the credential proxy. The core and worker launchers (`src/agent/claude/cli/session-launch.sh`) and `src/startup.sh` drop an `ANTHROPIC_BASE_URL` that points at the local proxy (`http://localhost:7846`, `127.0.0.1` or `[::1]`), in the launcher and in the session's own environment, and no longer set it. A base URL the owner set to anything else is left alone.

- The proxy's shared-credential role no longer applies to these sessions: each one uses its own config dir's login. On a host where workers run in the core's config dir, that is the core's login for all of them.
- Nothing stops or uninstalls the proxy: it keeps running, unused by these sessions. Health-check does not fail when the proxy is down or unused and the quota record comes from the status line. With the recorder on (the launchers' own `claude_direct_routing` decides) and nothing still using the proxy (no established connection on port 7846 and no live Sutando session carrying the proxy URL), a running proxy that is out of date or reports an exhausted credential is shown as `ok`, unused, with that state as information. If either is unknown, or with the recorder off, it is reported as before.
- To route through the proxy again, set `QUOTA_STATUSLINE_ENABLED` to `false` (env, or the skill's `manifest.json` `config`) and restart the sessions: the next launch decides its routing with the switch off and points the session at the proxy, which was never stopped. This also removes the recorder and puts back the saved status line.

## The quota in the s.space agent sidebar

On by default. The gateway bridge sends the core's quota to s.space as `health.quota` on its existing heartbeat (`POST /v1/heartbeat`, capability `quota_status.v1`), next to `health.recovery`, so the agent sidebar can show it. The block is built by `src/quota_status.py` from the trusted record (`trusted_record()`: a status line record only for the core's current login), and carries only:

```json
{"v": 1,
 "five_hour": {"used_pct": 42.0, "resets_at": 1795190400},
 "seven_day": {"used_pct": 7.0, "resets_at": 1795600000},
 "limit_state": "allowed",
 "last_checked": 1795176000}
```

and, when limited:

```json
{"v": 1,
 "five_hour": {"used_pct": 100.0, "resets_at": 1795190400},
 "seven_day": {"used_pct": 7.0, "resets_at": null},
 "limit_state": "rejected",
 "resumes_at": 1795190400,
 "last_checked": 1795176000}
```

Times are epoch seconds. `limit_state` is `allowed`, `overage` or `rejected`. `resumes_at` is omitted when the state is `allowed`; otherwise it is present, and null when no reset time is known. A window the record does not name is left out. A window's `resets_at` and the block's `last_checked` are null when the record does not carry them. `used_pct` is not clamped: in `overage` it can exceed 100, and a consumer may clamp it for display. No account digest, other login's numbers, headers, rejection ledger or model is sent. The full heartbeat examples are `contracts/quota_status.v1/heartbeat.json` (allowed) and `heartbeat-limited.json` (limited).

- The block changes when a used percentage moves by at least 1 point or the limit state flips, and is otherwise refreshed at most every 5 minutes; a change sends a heartbeat early, nothing else does. The status line never sends anything.
- A failed read keeps the last block; a broker that refuses the block (400/422) gets beats without it for an hour.
- Not sent from a pool worker's lane, a Codex core, or a standalone Sparrow.
- `QUOTA_STATUS_PUBLISH` (`manifest.json` `config`, default `true`; env overrides): `false` stops it at the gateway's next start.

`python3 src/quota_status.py show` prints the block as it would be sent now.

## Quick Check

```bash
# Read current quota state
cat quota-state.json
```

Output includes:
- `anthropic-ratelimit-unified-5h-utilization` — % of 5-hour window used
- `anthropic-ratelimit-unified-7d-utilization` — % of 7-day window used
- `anthropic-ratelimit-unified-5h-reset` — when the 5h window resets (epoch)
- `anthropic-ratelimit-unified-7d-reset` — when the 7d window resets (epoch)
- `anthropic-ratelimit-unified-status` — "allowed" or "rejected"

`limit_state` is `allowed`, `overage` or `rejected`. `overage` means a limit window is rejected but `anthropic-ratelimit-unified-overage-status` is `allowed`/`allowed_warning` and no 429 has landed since: the core is serving on extra usage, so `available` stays `true`. Clients read this field rather than re-deriving it from the headers; the rule is `classify_limit_state` in `src/quota_availability.py`.

`recent_rejections` is a bounded ledger (newest 20) of upstream responses the proxy forwarded with a 4xx/5xx other than 401 — `{ts, status, path, snippet, model, user_agent, peer_port}` — the proxy serves every seat on the host, so the client fields are what make a rejection attributable; the probe counts only entries for this core's own model (`SUTANDO_CORE_MODEL` or `core-runtime.json`) and counts every client when that is unknown. It survives the per-response header rewrite, and `health-check.py`'s `core-request-rejections` probe warns on one inside 15 minutes and fails on five inside an hour, so a credits/overage rejection that leaves every unified-status header `allowed` still reaches the owner.

## Setup

### 1. Start the credential proxy

```bash
npx tsx "$SKILL_DIR/scripts/credential-proxy.ts"
```

This starts on port 7846 and reads OAuth credentials from macOS keychain.

### 2. Route Claude Code through the proxy

```bash
ANTHROPIC_BASE_URL=http://localhost:7846 claude ...
```

Or add to your voice agent's launchd plist:
```xml
<key>ANTHROPIC_BASE_URL</key>
<string>http://localhost:7846</string>
```

### 3. Read quota state

```bash
python3 "$SKILL_DIR/scripts/read-quota.py"           # human readable
python3 "$SKILL_DIR/scripts/read-quota.py" --json     # machine readable
python3 "$SKILL_DIR/scripts/read-quota.py" --gate     # exit 1 if exhausted, not routed, OR stale
```

## Requirements

- macOS (reads OAuth from keychain)
- Claude Code logged in (subscription or API key)
- Node.js with tsx
