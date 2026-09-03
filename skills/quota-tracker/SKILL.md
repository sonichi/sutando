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

`recent_rejections` is a bounded ledger (newest 20) of upstream responses the proxy forwarded with a 4xx/5xx other than 401 — `{ts, status, path, snippet}`. It survives the per-response header rewrite, and `health-check.py`'s `core-request-rejections` probe warns on one inside 15 minutes and fails on five inside an hour, so a credits/overage rejection that leaves every unified-status header `allowed` still reaches the owner.

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
