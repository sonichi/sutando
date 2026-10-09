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

`recent_rejections` is a bounded ledger (newest 20) of upstream responses the proxy forwarded with a 4xx/5xx other than 401 — `{ts, status, path, snippet, model, user_agent, peer_port}` — the proxy serves every seat on the host, so the client fields are what make a rejection attributable; the probe counts only entries for this core's own model (`SUTANDO_CORE_MODEL` or `core-runtime.json`) and counts every client when that is unknown. It survives the per-response header rewrite, and `health-check.py`'s `core-request-rejections` probe warns on one inside 15 minutes and fails on five inside an hour, so a credits/overage rejection that leaves every unified-status header `allowed` still reaches the owner.

## Model fallback

The proxy also decides **which Claude model a request runs on** while quota is high, so the
window stretches to its reset instead of the core going dark at 100%. The policy is
`scripts/quota-fallback-policy.ts` (pure; the proxy only feeds it headers and the request body's
`model`), the defaults are this skill's `manifest.json` `config` block, and the owner adjusts them
with `scripts/fallback-config.py`.

**Levels** (1 is the most expensive):

| Level | Models | Role |
|---|---|---|
| 1 | `claude-fable-*`, `claude-mythos-*` | Only when the user switched to it. Never a target. |
| 2 | `claude-opus-*` | The default model (Opus 5.5) and the rest of the Opus family. |
| 3 | `claude-sonnet-*`, `claude-haiku-*` | The cheap backstop; target model `claude-sonnet-5`. |

**Ladder.** Each window holds its own tier; the effective tier is the worst window, and a request is
rewritten only *downward* to the tier's model (`claude-opus-5-5` for tier 2, `claude-sonnet-5` for
tier 3), keeping a `[1m]` context-length variant. A request already at or below the tier is never
touched; non-Claude and unknown models pass through.

- **7-day window** — thresholds. Usage > **85%** → tier 2 (Fable requests run on Opus 5.5);
  usage > **95%** → tier 3 (Fable and Opus requests run on Sonnet 5).
- **5-hour window** — projection. The proxy fits the burn rate over the last hour of its own
  observations (seeded from `quota-history.jsonl`, which `src/quota_projection.py` owns) and projects
  utilization at the window's reset. If it would reach **98%** before the reset → tier 2, and a
  further level after a 10-minute dwell if it still runs out; if it lasts to the reset, no downgrade
  even above 90%. With thin history (under 5 minutes of samples) the even-pace estimate — usage so
  far over the fraction of the window elapsed — stands in; with no reset header or a window under
  5 minutes old the 7d-style thresholds (**90%** / **97%**) decide. **97%** is a hard line to tier 3
  regardless of the projection.
- **Hysteresis**, per window. A threshold tier is left only below (threshold − 3%); a projection tier
  only after the projection has cleared for 3 consecutive samples *and* 5 minutes. A window reset
  (its `-reset` epoch moves) re-evaluates from scratch.
- **`status: rejected`** — no model is swapped: every Claude model shares the unified quota. The proxy
  records a Codex runtime-switch request instead (`fallback.runtime_switch = {to: "codex", reason:
  "rejected", at}`), writes the reverse request when the window is allowed again, and the
  `quota-model-fallback` health probe **fails** until then. **The switch itself is manual in this
  version**: set `core.runtime` to `codex` in `sutando.config.local.json` and run
  `bash src/agent/start-cli.sh --restart` from outside the core session (`docs/codex-core.md`).
  Wiring the restart is a follow-up — the restart-intent file is owner-triggered only (#2401).
- **Low-priority ladder** (off by default). A request carrying `x-sutando-priority: low` may be
  demoted earlier (60% / 85%, both windows). Claude Code sends the header when its launcher exports
  `ANTHROPIC_CUSTOM_HEADERS="x-sutando-priority: low"`; the proxy strips it before forwarding. No
  Sutando launcher sets it yet (follow-up for the cron / health-check paths).

**Adjusting thresholds** (no restart — the proxy re-reads the per-host override on change):

```bash
python3 "$SKILL_DIR/scripts/fallback-config.py" show
python3 "$SKILL_DIR/scripts/fallback-config.py" set 7d level1 0.90   # 7d: Fable → Opus above 90%
python3 "$SKILL_DIR/scripts/fallback-config.py" set 5h level2 0.98   # 5h hard line to Sonnet
python3 "$SKILL_DIR/scripts/fallback-config.py" set 5h projection-limit 0.99
python3 "$SKILL_DIR/scripts/fallback-config.py" set 5h projection off  # 5h back to thresholds
python3 "$SKILL_DIR/scripts/fallback-config.py" set low-priority on
python3 "$SKILL_DIR/scripts/fallback-config.py" unset 7d level1
```

`set` validates `0 < value < 1` and `level1 < level2` per ladder, writes
`<workspace>/state/quota-fallback-config.json` atomically and prints the effective ladder with each
value's source. Precedence the proxy applies: `SUTANDO_QUOTA_FALLBACK_*` env > that override file >
`manifest.json` `config` > built-in. `set enabled off` turns the rewrite off entirely.

**Visibility.** `quota-state.json` carries `fallback` — `{tier, low_priority_tier, windows,
active_model_map, since, reason, fired, runtime_switch}` — rewritten on every tier change.
`health-check.py`'s `quota-model-fallback` probe reads it: ok on the primary tier, warn while a tier is
active (naming the model map), fail on a pending Codex switch request. Every tier change also writes
one line to `<workspace>/results/proactive-quota-fallback-<ts>.txt` — the owner DM — naming the
window and the line that fired (or the projected run-out time), the model now in use, when it
reverts, and the `fallback-config set …` clause that moves that line. One line per change, never
per request.

**Caveats.** The prompt cache is cold on every switch (caches are model-scoped). Claude Code's
`/model` still shows the model the user picked; `quota-state.json`'s `last_request.model` and
`fallback.active_model_map` show what actually ran. Only traffic routed through the proxy is covered
— the Codex runtime is not. The proxy rewrites `model` only; a request feature the target model
rejects (e.g. forced `tool_choice` on Opus 5.5) still fails upstream as it would have on the
original model.

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
