---
name: sutando-life-provider
description: Provide bounded, read-only local GitHub and Discord data to Sutando Life through Sutando's runtime capability registry.
---

# Sutando Life provider

This optional provider edge exports `registry_inputs()` from both
`scripts/github_provider.py` and `scripts/discord_provider.py`. Runtime
composition injects their descriptor and reader maps into
`EphemeralCapabilityRegistry`; core code does not locate or import this skill
directly.

The `github.activity` capability provides three read-only operations:

- `identity.get`
- `repositories.list`
- `repository.events.delta`

The `discord.activity` capability provides three read-only operations:

- `identity.get`
- `context.get`
- `channel.messages.delta`

The provider invokes the existing `gh` CLI authentication on the local Sutando
host. It never reads, accepts, prints, or returns a GitHub token. Results contain
at most 100 items and use stable GitHub IDs plus browser evidence URLs.

Capability availability is captured when `registry_inputs()` runs. After
installing `gh` or completing `gh auth login`, restart the runtime daemon so its
ephemeral registry is composed again.

Authorize the local GitHub CLI if needed, then launch the provider-backed runtime:

```bash
gh auth login --hostname github.com
python3 skills/sutando-life-provider/scripts/serve.py
```

The launcher is owned by this optional skill. It injects `registry_inputs` into
the generic runtime startup API for both providers; running
`src/runtime-api/server.py` directly still starts the provider-neutral daemon
with an empty capability registry.

Discord uses the existing local bridge credential resolution order: process
environment, `$CLAUDE_CONFIG_DIR/channels/discord/.env`, then the Sutando vault.
If no token is configured, run `vault set DISCORD_BOT_TOKEN` and restart the
provider runtime. The token remains inside the provider edge and is never
printed, logged, or returned.

Discord context reads also fail closed against the bridge's local
`access.json`: a guild channel must be present under `groups`, while a DM's
non-bot recipients must all be present in `allowFrom`. The capability never
returns either allowlist. `context.get` and `channel.messages.delta` accept
`resource.channelId`; the latter accepts the opaque object cursor returned by
the previous call, overlaps five minutes, and requires consumers to upsert by
message `id`. A full page is marked partial because more messages may exist.

`repository.events.delta` accepts `resource.repository` as `owner/name` and an
opaque cursor object returned by the previous call. The cursor includes the
newest observed event while each subsequent read deliberately overlaps five minutes;
consumers must upsert by event `id`. A full response is marked partial because
older activity may have fallen outside the bounded page.
