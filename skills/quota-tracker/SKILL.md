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

`limit_state` is `allowed`, `overage` or `rejected`. `overage` means a limit window is rejected but `anthropic-ratelimit-unified-overage-status` is `allowed`/`allowed_warning` and no 429 has landed since: the core is serving on extra usage, so `available` stays `true`. Clients read this field rather than re-deriving it from the headers; the rule is `classify_limit_state` in `src/quota_availability.py`.

`recent_rejections` is a bounded ledger (newest 20) of upstream responses the proxy forwarded with a 4xx/5xx other than 401 — `{ts, status, path, snippet, model, user_agent, peer_port}` — the proxy serves every seat on the host, so the client fields are what make a rejection attributable; the probe counts only entries for this core's own model (`SUTANDO_CORE_MODEL` or `core-runtime.json`) and counts every client when that is unknown. It survives the per-response header rewrite, and `health-check.py`'s `core-request-rejections` probe warns on one inside 15 minutes and fails on five inside an hour, so a credits/overage rejection that leaves every unified-status header `allowed` still reaches the owner.

Each request also writes one timing line to the proxy log when it ends: `[Proxy] id=<n> timing method=… path=… attempt=… body_ms=… cred_ms=… dispatch_ms=… first_attempt_ms=… socket_ms=… reused=… connect_ms=… tls_ms=… inflight=… sent_ms=… ttfb_ms=… last_byte_ms=… bytes=… total_ms=… status=… outcome=…`. Durations are milliseconds since the request reached the proxy (`-` when that step never happened), except `cred_ms`, the keychain read and any refresh measured from `body_ms` (the request body fully read), and `first_attempt_ms`, how long the attempt a 401 retry replaced ran; `sent_ms` is the request fully written upstream, `last_byte_ms` the last upstream byte and `bytes` the upstream body bytes seen; `reused` says whether the upstream keep-alive socket was reused, and on a new socket `connect_ms` and `tls_ms` mark its TCP connect and TLS handshake (`-` on a reused one); `inflight` is how many other proxied requests were open at dispatch; `outcome` is `ok`, `proxy_reject` (a 502 or 401 the proxy answered itself), `upstream_error`, `upstream_aborted` (the upstream stopped partway; the client's response is ended too), `idle_abort`, `client_closed_before_response` or `client_closed_during_response`. The path carries no query string and any character outside a plain URL path is masked as `_`, and no headers, bodies or credentials are logged. The same `id=<n>` prefixes that request's `Upstream error`, `Upstream idle` and `rejected HTTP` lines, so a client hang-up can be matched to the retry that follows it. A separate `[Proxy] loop_lag_ms=<n>` line appears only when the proxy's event loop runs more than 200 ms late (checked every 500 ms).

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
