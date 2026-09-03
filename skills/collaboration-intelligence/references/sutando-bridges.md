# Sutando bridge notes

Load the section for the active bridge before resolving identities or room membership. Preserve the provider's stable IDs in addition to display names.

## Contents

- [Cross-bridge hard pitfalls](#cross-bridge-hard-pitfalls)
- [Roster refresh policy](#roster-refresh-policy)
- [AG2 Space (Matrix)](#ag2-space-matrix)
- [Discord](#discord)
- [Telegram](#telegram)
- [Slack](#slack)
- [WhatsApp](#whatsapp)

## Cross-bridge hard pitfalls

1. **A bridged identity is the same entity, not a second person.** Preserve both the native identity and bridge alias, then link them. For example, a Slack user represented as `@_slack_*` remains the same Slack entity.
2. **When multiple agent accounts share one GitHub login** (uncommon, but it happens): do not attribute a commit from the login alone. **Commit-author email is not a reliable discriminator by itself** — it can be inherited across machines, or overridden/forged (`git -c user.email=…`; API-created commits use the token account's email), so it can be present, unambiguous, *and wrong*. Prefer a **verified signature** (e.g. an mxid signature in the PR body) when one exists; treat commit-author email as distinguishing only when a repo-local email override is known to be set; otherwise leave authorship unresolved.
3. **An empty resolver result does not mean the entity is absent.** Run a known-good positive control against the same resolver and scope first. Treat an empty result as `unknown` when the control also fails or the data surface is incomplete.

## Roster refresh policy

Use `100` members as the default operational threshold; allow provider configuration to override it.

- **Room with fewer than 100 members:** when the provider exposes a roster endpoint, sweep the complete roster and reconcile joins, leaves, identity changes, and entity links. A sweep is appropriate on first encounter and whenever the stored snapshot becomes stale.
- **Room with 100 or more members:** maintain the roster incrementally from join/leave events, task headers, observed senders, replies, mentions, and provider deltas. Avoid frequent full sweeps.
- **Sweep a large room when necessary:** sweep before a sensitive send or permission decision, after a membership-count mismatch, when important identities remain unresolved, when incremental state has a gap, after reconnect/recovery, or when the last reliable snapshot is stale.
- Record `sync_mode`, `last_sweep_at`, `last_incremental_at`, source, member count, and completeness with the roster.
- A successful API call is not automatically a full sweep. Mark membership `full` only when the provider contract guarantees exhaustive results and every page completed; otherwise keep it `partial` or `unknown`.
- Respect rate limits and pagination. If a sweep fails partway, preserve the last known-good snapshot, record the failed attempt, and apply verified deltas without deleting unseen members.
- Do not sweep merely to enrich a profile when the current task does not justify enumerating the room.

Bridge-specific implications:

- AG2 Space task headers provide the true total in `room_member_count`, but `room_members` is capped at 10 names. Treat a header as full only when the parsed list length equals the reported total.
- Large Discord servers commonly remain partial even after available enumeration.
- Telegram through this bridge has no Matrix-style exhaustive member table; do not label an observed-member scan as full.
- Slack and WhatsApp may support full channel/group reconciliation when the configured bridge can page the entire roster.

## AG2 Space (Matrix)

AG2 Space is an emerging Sutando-native, agent-native workspace.

### Identity

- Matrix IDs use `@localpart:homeserver`. For example, `@alice:example.org` is username `alice` on the `example.org` homeserver.
- Sutando agents conventionally use `@x.agent:homeserver` or `@sutando-x:homeserver`.
- Treat those agent patterns as strong agent evidence. Preserve the complete Matrix ID; the localpart alone is not globally unique.

### Room and roster visibility

- Each task header carries `room_members` plus the true room size in `room_member_count`. The server caps `room_members` at 10 names.
- Parse the member list and compare counts:
  - If `len(room_members) == room_member_count`, mark the task-time snapshot `full`.
  - If `len(room_members) < room_member_count`, mark it `partial` and `truncated: true`. Use `room_member_count` as the real room size; never use the listed-name count as the total.
  - If `len(room_members) > room_member_count` or parsing is invalid, mark completeness `unknown` and record a header-integrity anomaly.
- In the current server behavior, rooms with at most 10 members can be full; rooms above 10 are represented by exactly 10 listed names and a larger true count. Use the comparison rule rather than hard-coding room size so the consumer remains correct if the cap changes.
- A `full` header is complete only at the task timestamp. It is not a permanently live roster.
- When an agent is in an AG2 Space room, its owner is expected in that room. Check this invariant only from a `full` snapshot or an independent exhaustive membership query. The owner's absence from a truncated header is inconclusive and must not trigger an ownership anomaly.

### Observation source: the task-file stream

- The **task files this agent receives** (`tasks/` + `tasks/archive/`) are the primary passive observation source. Each one already carries room/sender/member metadata (`room_id`/`channel_id`, `room_name`, `room_members`, `room_member_count`, `sender_name`, `user_id`, `access_tier`). **Building the map from that stream is observation, not room enumeration** — so it is allowed where actively enumerating rooms is not.
- **Sweep incrementally with a cursor, never from scratch.** Persist the last-processed task arrival time; the next sweep reads only task files newer than the cursor. Take arrival time from the immutable source — the `id`'s epoch-ms (`task-<epoch-ms>`) or the `timestamp:` header — **never file mtime**, which resets on workspace sync / `git checkout` / `touch` and would silently reprocess or skip.
- A task-derived membership snapshot is still capped at 10 (above), so apply the same `len(room_members) == room_member_count` → `full`/`partial` rule per task.

### Trust and purpose

- `access_tier` is the system-wide trust base. Do not reinterpret it as a room-local reputation score or infer it from membership.
- Room names commonly express room purpose. Use the name as a strong initial hint, then refine it with current room context.

## Discord

### Identity

- Users, channels, guilds, messages, and roles use numeric snowflakes. Mentions commonly use `<@id>`.
- Prefer the numeric ID over display name or nickname.
- Use the Discord bot flag as strong bot evidence, but do not assume every bot is a Sutando agent. Confirm agent linkage from bridge configuration or observed function.

### Room and roster visibility

- Track collaborators per channel or thread; guild membership alone does not imply participation in every channel.
- Discord frequently contains very large servers and channels. A room is "large" at **100 or more members** (the roster-refresh threshold above); treat large-room activity as lower-signal by default and maintain it incrementally.
- Small Discord rooms and threads (fewer than 100 members) are usually more deliberate collaboration spaces. Give them higher attention for purpose, participant changes, decisions, handoffs, and unfamiliar identities.
- Room size affects attention, not trust or authority. In a large room, direct mentions, explicit assignments, sensitive context, owner/agent participation, or an unexpected privileged identity still require immediate attention.
- Large-server rosters are commonly partial. Mark them `partial` unless the bridge explicitly supplies a complete member snapshot.
- Channel permissions, thread membership, and recent posters are different observations; record which surface produced the roster.

### Known pitfalls

- A webhook identity, bot identity, and represented remote sender can differ. Preserve each role instead of collapsing them blindly.
- Community servers can contain many unfamiliar humans; alert only when identity affects sensitive access, trust, or requested collaboration.

## Telegram

### Identity

- Use stable numeric `chat_id` and `user_id` values.
- A Telegram `@handle` can change. Keep it as an alias, never the identity key.
- Classify bot versus human only from provider metadata or verified bridge metadata, not the handle or display name.

### Room and roster visibility

- Telegram does not provide a Matrix-style full member table through this bridge. Membership is normally partial or unknown.
- Distinguish direct chats, groups, supergroups, broadcast channels, and topics when metadata permits.

### Known pitfalls

- A reply may contain only a `[Replying to…]` quote. Treat the quote as the available reply context; do not assume access to the original message, surrounding history, author membership, or full thread.
- Forwarded content does not prove that its original author participates in the current chat.

## Slack

### Identity

- Slack user IDs commonly look like `U0…`. Channel IDs match `[CDG][A-Z0-9]+` for channel/DM/group-DM surfaces.
- Preserve `thread_ts` as the thread identifier beneath the parent channel.
- A Slack identity bridged into Matrix as `@_slack_*` is the same entity. Link the bridge alias to the Slack user ID; do not create a second person.
- Use Slack user/app/bot metadata to distinguish humans, apps, and bots.

### Room and roster visibility

- Channel membership may be visible, but workspace membership is not equivalent to channel membership. Record the observed scope.
- Slack Connect channels may include external collaborators; normal workspace channels commonly contain internal company members.
- Channel names commonly express purpose. Confirm with channel topic, description, canvas/bookmarks, and recent durable context.

### Known pitfalls

- Do not move internal context into a Slack Connect channel without checking the external audience.
- Activity level does not establish responsibility or feature ownership.

## WhatsApp

### Identity

- Use the normalized E.164 phone number as the stable participant identity when the bridge exposes it.
- Display names and contact-book labels are weak, local evidence and can differ between observers.
- A phone-shaped identity is normally human, but automation or business endpoints can exist; use bridge metadata when classification matters.

### Room and roster visibility

- Distinguish direct chats, groups, and communities. A group can mix coworkers, customers, vendors, friends, or family.
- Treat roster observations as time-bound because participants can join or leave and numbers can be reassigned.

### Known pitfalls

- Phone numbers are sensitive. Attach the narrowest applicable `access_scope` and never leak them into a larger room merely to identify or mention someone.
- Quoted and forwarded content does not prove that the original author belongs to the current group.
