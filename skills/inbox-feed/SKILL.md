---
name: inbox-feed
description: "Poll the owner's connected GitHub, Gmail and Slack (composio_exec) and write what needs them into their s.space Inbox room, as space.ag2.inbox.item events, so their Beeper-style Inbox page shows it. Use on a schedule (poll) or on request (digest, or 'what's in my inbox'). Priority rules (needs_you vs fyi) live in an editable workspace file, not in code — edit <workspace>/state/inbox-feed-rules.json directly, no redeploy needed."
user-invocable: true
---

# Inbox feed

Writes one `space.ag2.inbox.item` event per new piece of activity from the owner's
connected GitHub, Gmail and Slack into their private s.space Inbox room, so the
Inbox page (`src/app/features/inbox/` in `ag2-space/cinny-webclient`) shows it. The
item schema and the room's own state marker are the client's, not this skill's — see
"Where this fits" below before changing either.

Two commands: `/inbox-feed poll` (find new activity, map it, send it) and
`/inbox-feed digest [--hours N]` (summarize what was sent recently into one message).
Both default to a dry run — nothing is sent to the Inbox room unless you say `--live`.

## Where this fits

- The owner's Inbox room is marked with Matrix room state `space.ag2.inbox`, content
  `{owner: "<their mxid>", version: 1}`, and a topic containing "Your private Inbox".
- Each item is a timeline event of type `space.ag2.inbox.item`, content matching
  `InboxItem` in `inboxItems.ts` on the client: `schema_version` (1), `id`,
  `external_id`, `source {app, account?}`, `actor {id, display_name, avatar_url?}`,
  `what {kind, summary}`, `link` (a URL string, or `{room_id, event_id}`),
  `priority` (`needs_you`|`fyi`), `thread_key`, `created_at`, `updated_at`, `state`
  (`open`|`closed`|`read`|`resolved`).
- Writing that event uses the `room.event.send` s-space Action (`{type, content}`),
  which only accepts `type` values under `space.ag2.*` — confirmed live, not assumed
  (`pool-handoff/inbox-feed.md` has the probe). `room.message.send` cannot be used for
  this: it has no `type`/`msgtype` argument, only `body`.

## `/inbox-feed poll [--live]`

1. **Find the Inbox room.** Call `room.list` for yourself, then `room.state.read`
   (`event_types: ["space.ag2.inbox"]`) in each room you're already joined to, plus
   whatever topic/name you can see for rooms you're not joined to (if your tools
   surface that — otherwise skip those and only consider joined rooms). Build one
   JSON array of `{room_id, joined, state_events, topic, name}` and run:

   ```bash
   python3 skills/inbox-feed/scripts/inbox_feed.py find-room --file rooms.json --owner <owner mxid>
   ```

   - `{"status": "found", "room_id": "..."}` → that's the Inbox room, continue.
   - `{"status": "not_member", ...}` → **reply "invite me to your Inbox room" and
     stop.** Do not try `room.invite.*` yourself — the client doesn't invite the
     agent yet (confirmed in research), so there is nothing to call that would work.
   - `{"status": "not_found"}` → the owner has no Inbox room yet (the client makes one
     on first open of the Inbox page); say so and stop.

2. **For each source, read the last cursor, poll, map.**

   ```bash
   python3 skills/inbox-feed/scripts/inbox_feed.py cursors get --source github
   ```

   Then call `composio_exec` for that source using the read actions from the
   research (`pool-handoff/inbox-sources.md` has the full table): GitHub
   `GITHUB_SEARCH_ISSUES_AND_PULL_REQUESTS` (`review-requested:@me`, `mentions:@me`,
   `involves:@me`, each `updated:>last-cursor` once you have one), Gmail
   `GMAIL_FETCH_EMAILS` (seeded from `GMAIL_GET_PROFILE`'s `historyId` once you have a
   cursor, `is:unread` the first run), Slack `SLACK_GET_UNREAD_MESSAGES_FROM_USER`
   per known DM peer and `SLACK_LIST_UNREAD_CHANNEL_MESSAGES` per channel the owner
   cares about. One call per *kind* (review-requested, mentions, …), since the kind
   isn't in the payload — you supply it.

   Reduce each raw hit to the shape `map` expects (see the mapper docstrings in
   `scripts/inbox_feed.py` for the exact fields per source — GitHub hits need no
   reduction, Gmail/Slack do), write it to a file, then:

   ```bash
   python3 skills/inbox-feed/scripts/inbox_feed.py map --source github --kind review_requested --file hits.json
   ```

   Collect every mapped item from every source/kind into one array.

3. **Dedupe, then decide live vs dry-run.**

   ```bash
   python3 skills/inbox-feed/scripts/inbox_feed.py dedupe --file items.json           # preview
   python3 skills/inbox-feed/scripts/inbox_feed.py dedupe --file items.json --live    # also persists
   ```

   `new_items` is what hasn't been sent before. Without `--live` this previews only —
   nothing is marked sent, so a dry run never causes the next real run to skip
   anything. **Without `--live` anywhere in this flow, stop after printing
   `new_items` — do not call `room.event.send`.**

4. **On `--live`, send each new item**, as yourself, into the Inbox room:

   ```
   room.event.send(room_id=<inbox room>, type="space.ag2.inbox.item", content=<item>)
   ```

   one call per item (`room.event.send` takes one event, not a batch). After sending
   all of them, advance each source's cursor and append the sent items to the local
   log, both also gated on `--live`:

   ```bash
   python3 skills/inbox-feed/scripts/inbox_feed.py cursors set --source github --value <newest updated_at> --live
   python3 skills/inbox-feed/scripts/inbox_feed.py log-sent --file items.json --live
   ```

## `/inbox-feed digest [--hours N] [--live]`

```bash
python3 skills/inbox-feed/scripts/inbox_feed.py digest --hours 12
```

Prints a plain-text summary of what was sent in the window (from the local sent-log,
or pass `--file items.json` to summarize an explicit list). On `--live`, also post it
with `room.message.send` to the Inbox room found in step 1 above; without it, just
show the text.

## Priority rules — agent-native, not code

`<workspace>/state/inbox-feed-rules.json` decides `needs_you` vs `fyi` per
`{app, kind}` pair (first match wins, `"*"` is a wildcard for either field). It does
not exist until `rules init` writes Sutando's starting defaults:

```bash
python3 skills/inbox-feed/scripts/inbox_feed.py rules init    # once, if the file is missing
python3 skills/inbox-feed/scripts/inbox_feed.py rules show    # what's active right now
```

Edit the file directly — e.g. the owner asks "github mentions should be needs_you,
not fyi" → add `{"app": "github", "kind": "mentioned", "priority": "needs_you"}`
ahead of the catch-all rule. No code change, no redeploy, no PR.

## Safety

- `poll` and `digest` are **dry-run by default**. `--live` is required to write
  anything — into the Inbox room or into this skill's own state files.
- `room.event.send` and `room.message.send` run under this agent's own authority,
  into a room only its owner's Inbox can be (confirmed by `space.ag2.inbox.owner`,
  never a room_id you pick) — no new UI, no change to sign-in, credentials or
  approval holds; nothing here needs an approval card.
- The rules file and the cursor/sent-id state are per-workspace data, not secrets —
  they hold no account content, only ids, timestamps and a priority label.
