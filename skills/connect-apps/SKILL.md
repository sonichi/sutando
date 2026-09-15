---
name: connect-apps
description: "Use when the owner asks about their calendar, email, meetings, Linear, Notion, Slack, Google Drive, GitHub or any other third-party app or the data in it ('what's on my calendar', 'any email from Sam', 'my Linear issues', 'find the Drive doc'), asks to connect one, or says 'done' / 'connected' / 'I signed in' after you sent a Connect card. Answers through the Superpower Station connector tools (composio_find, composio_exec). When an app isn't connected yet, posts ONE Connect card to the owner (in their DM as a message; in a room with other people privately, under their message, visible only to them), closes the task, and answers by itself once they sign in, with no engine restart."
---

# Connect apps

The owner's apps (Google Calendar, Gmail, Google Meet, Google Drive, Slack, Linear, Notion, GitHub,
and a thousand more) are reached through the `sutando-station` MCP server. A connection made
mid-session works at once; nothing here ever needs a restart.

When an app isn't connected, the flow looks like this in the owner's chat:

1. You: "Your Google Calendar isn't connected yet, so I can't peek at your schedule. Want to hook it up?"
2. A card: the app's icon and name with a **Connect** button.
3. You: "Once that's done I'll be able to check what's coming up for you." The task ends here.
4. After sign-in you continue by yourself: "Your Google Calendar is connected and ready to go. Here's your Friday: ..."

In a room with other people, lines 1 to 3 are not messages: they are a **private card** under the
owner's message, inside their "Only visible to you" activity card, which no other member receives.
The room sees nothing about connecting, only your reply once the request is done (step 3b).

Every request gets exactly one answer: the steps below check for a wait before answering.

## Tools

- `mcp__sutando-station__composio_find` `{query?, apps?: [names], toolkit?, limit?}` returns JSON
  `{"apps":[{toolkit, name, icon_url, connected, auth_mode, coming_soon}], "actions":[{toolkit, action, description, inputSchema}], "next_step"?}`.
  `apps` matches app names ("google calendar", "linear"); actions are listed for connected apps and
  for an exact `toolkit` slug.
- `mcp__sutando-station__composio_exec` `{toolkit, action, arguments}` runs one action. An error
  whose message contains `connect_required` means the app is not connected.
- `mcp__sutando-station__station_find` / `station_call` reach the owner's cloud tools, including one
  activated a minute ago. See "Paid cloud tools" below.
- The helper script, next to this file:

```bash
C="<this skill's directory>/scripts/connectors.py"
python3 "$C" find "google calendar"            # exact catalog app: {"match": {...} | null, "suggestions": [...]}
python3 "$C" status googlecalendar --room '<room id>'   # connected? plus the room's pending and resumed waits
python3 "$C" await googlecalendar --room '<room id>' --reply-to '<source_message_id>' \
  --task '<task id>' --owner '<owner mxid>' --request-file - <<'SUTANDO_REQUEST'
<the owner request, verbatim>
SUTANDO_REQUEST
python3 "$C" await youtube --room '<shared room id>' --reply-to '<source_message_id>' \
  --task '<task id>' --owner '<owner mxid>' --private \
  --line "YouTube isn't connected yet, so I can't do that. Connect it here and I'll carry on." \
  --line "Once that's done I'll pull up your latest videos." --request-file - <<'SUTANDO_REQUEST'
<the owner request, verbatim>
SUTANDO_REQUEST
python3 "$C" note '<wait id>' "YouTube is connected. On it." [--status connected]   # a private-card line
python3 "$C" claim '<room id>'                 # before answering in a room that may have a wait
python3 "$C" verify-account '<wait id>'        # a resume: is the wait's AG2 Cloud account the one in use now?
python3 "$C" rearm                             # restart dead waiters (startup + proactive loop run it)
```

Pass the owner's words only through the quoted heredoc above, never inside a quoted `--request`
argument: their text (an apostrophe, a quote) must never become shell. `--line` and `note` text is
yours, not the owner's: plain sentences, no quotes from their message.

| exit | meaning |
|---|---|
| 0 | done: a match, all apps connected, a wait recorded, a wait claimed, the account verified |
| 1 | a negative answer: no exact match, an app not connected, nothing claimed, `account_changed`, `account_unknown`, `no_such_wait` |
| 2 | a setup problem to relay, never retry: `not_signed_in`, `connectors_disabled`, `unknown_app`, `coming_soon`, `too_many_apps`, `not_owner_task`, `invalid_arguments`, `cloud_error` |

Every command prints one JSON object. A **resumed** entry (`resumed` from `claim` and `await`,
`resumed_waits` from `status`) is a wait from the last 30 minutes whose request is already handled:
`resume_task` names the task that answers it (`null` when an owner message task claimed and answered
it), and `resume_pending: true` means that task hasn't run yet. Only `claimed_by` `connected` or
`claim` answers the request; with `timeout`, `user_changed` or `unverified` that task posts a note
asking the owner to connect or ask again, so the same request asked again is a fresh one. When a
resume task answers, close your own task with `[no-send]`, never `[deduped: task-connect-...]`: a
resume task's result is itself `[no-send]`, so the gateway would read the dedup as unanswered and
re-ask your task.

## Step 0: who gets a card

A card and a wait only when **all** of these hold for the task:

- `access_tier: owner`, and no `collaborator: true` header;
- `source: ag2space` (a message in AG2 Space; its room is `channel_id` / `source_room_id`).

`await` checks the same on the task file and refuses anything else with `not_owner_task`.
Everything else (cron, a proactive-loop pass, voice, phone, Slack, Discord, Telegram, local chat, a
collaborator or any non-owner) gets text only when an app isn't connected: "Google Calendar isn't
connected yet; connect it in AG2 Space." No card, no wait. A non-owner never gets the owner's
connected-app data either.

## Step 1: are the Station tools loaded?

`composio_find` is loaded when `mcp__sutando-station__composio_find` is in your tool list, or, when
you have ToolSearch, `select:mcp__sutando-station__composio_find` returns it. If it is not:

- Some other `mcp__sutando-station__*` tool is there (`station_find`, a cloud tool; ToolSearch
  `sutando-station`): connected apps aren't enabled on this AG2 Cloud. Say "Connected apps aren't
  available on your AG2 Cloud yet." No restart.
- `python3 "$C" status` exits 2 with `not_signed_in`: ask the owner to sign in to AG2 Cloud in the
  desktop app. No restart.
- Otherwise, no `mcp__sutando-station__*` tool at all: the running engine started without the
  Station. Say this once, and stop:

> I need an engine restart once to use connected apps: Agent settings → Runtime → Restart engine.

Never restart the engine yourself.

## Which kind of room

Everything below depends on whether the task's room is the owner's DM with you. A room counts as
that DM only when you can confirm it:

1. The task's `room_member_count` is exactly `2`; or
2. `room.inspect` on the room returns `safe_metadata.joined_member_count` of exactly `2`.

A missing, unparseable or larger count, or a failed `room.inspect`, means **shared**.

## Where the answer may go

- **Something the owner asked you to do or find where they asked** (post to Slack, create a Linear
  issue, search YouTube, summarize a public page): reply in the room they asked in, like any reply.
- **Private content** (mail, calendar events, files and docs, messages, contacts): only in the
  owner's DM. From a shared room, find your DM with the owner with `room.list` and confirm it with
  `room.inspect` as above; the answer goes there, and the shared room gets only "I sent it to you in
  our DM." No confirmed DM: text only, no data ("I can only share your calendar in our DM.").

Connecting itself (the card, "isn't connected yet", "once that's done", timeouts, account notes) is
never posted in a shared room: it goes on the private card (step 3b).

## Step 2: find the apps

Call `composio_find` with `apps` naming every app the request needs and `query` set to the request.
For each app:

- `connected: true`: first run `python3 "$C" claim '<task room>'` (and the DM's room id too when
  you are answering from a shared room):
  - `claimed` lists a wait: handle it together with this message as in Step 5 ("The owner writes
    first"), which answers its `request` only when `account_ok` is true.
  - `resumed` lists a wait with `claimed_by` `connected` or `claim` whose `task` is this task's id (a
    re-run of the original request), or one with `resume_pending: true` whose `request` is this
    request: it is answered, or about to be. Write the result `[no-send]` and stop. A resumed wait
    with `timeout`, `user_changed` or `unverified` got only a note: answer this message as below.
  - Otherwise: find the action (`composio_find {toolkit, query}`), run `composio_exec`, and answer
    where the answer may go (above). Done.
- `coming_soon: true`: "<App> isn't available yet."
- `connected: false`: step 3 (or the text line from step 0 when the task gets no card).

If an app name doesn't match anything, `python3 "$C" find "<name>"` checks the catalog; exit 1 means
there is no such app, so say so.

## Step 3a: one card for everything missing, in the owner's DM

For a task whose room is the owner's DM (see "Which kind of room"). From a shared room, go to 3b.

1. **Which room.** The task's room.
2. **A card already waiting?** `python3 "$C" status <slugs> --room '<room>'`. When
   `pending_waits` already lists these apps, skip the intro and the card (items 4 and 5): send "The
   Connect card above still works: tap Connect and I'll pick it up as soon as it's connected." and go
   on to step 4, which folds this request into that wait.
3. **Owner id.** Call `ag2.whoami` and use `runtime.owner_id` (never `actor.id`).
4. **Intro.** `room.action.execute` with action `room.message.send`, `operation_id`
   `<task id>:connect-intro`, and `reply_to` the task's `source_message_id` when the action accepts it:
   "Your Google Calendar isn't connected yet, so I can't peek at your schedule. Want to hook it up?"
   (adapt to the app and the request).
5. **Card.** Same action, `operation_id` `<task id>:connect-card`:

   ```json
   {
     "body": "Connect Google Calendar: tap Connect on the card, or open AG2 Space → Marketplace.",
     "extra_content": {
       "space.ag2.connector": {"version": 1, "for": "<owner_id>", "toolkits": [{"slug": "googlecalendar"}]}
     }
   }
   ```

   Several apps: ONE card listing each slug (at most 5), body "Connect Linear and Google Meet: ...".
   If the action is not offered or fails, fall back to the text line from step 0 and skip step 4.

## Step 3b: a private card, in a room with other people

For a task whose room is shared. Nothing about connecting is posted anywhere: no intro, no card
message, no "I sent you a card in our DM", no outro.

1. **No `source_message_id`** on the task: a private card has nowhere to show. Send the owner's DM
   (found and confirmed as in "Where the answer may go") the text line from step 0, post nothing in
   the shared room, write the result `[no-send]`, and stop. No confirmed DM: result `[no-send]`.
2. **A card already waiting?** `python3 "$C" status <slugs> --room '<task room>'`. When
   `private_cards` lists one for these apps with `status: waiting`, run `note <its id> "The Connect
   card on your earlier message still works: tap Connect and I'll carry on."` and go on to step 4:
   `await` folds this request into that wait, and the new card points at it.
3. **Owner id.** Call `ag2.whoami` and use `runtime.owner_id` (never `actor.id`).
4. Go to step 4 with `--private`, `--room` the task's room, `--reply-to` the task's
   `source_message_id`, and two `--line`s in your voice, adapted to the app and the request: the
   intro ("YouTube isn't connected yet, so I can't do that. Connect it here and I'll carry on.") and
   the outro ("Once that's done I'll pull up your latest videos."). The desktop client draws the card
   and the lines under the owner's message.

## Step 4: hand off and close

Run `await` exactly as in the Tools block: the owner's request goes in the quoted heredoc, `--room` is
the task's room, `--owner` is `runtime.owner_id`, `--reply-to` is the task's `source_message_id`,
and from step 3b add `--private` and the two `--line`s.

- **Exit 0 with a `wait_id`:** in the DM (3a), write the task result now: "Once that's done I'll be
  able to check what's coming up for you." (adapted to the app). From 3b the outro is already on the
  private card: write the result `[no-send]`. The task is closed; do not wait inside it. (`superseded` lists earlier waits this one absorbed;
  `"waiter_pid": null` means the waiter could not start, and the next `rearm` starts it.)
- **Exit 0 with `"wait_id": null`:** an earlier wait was handled meanwhile: `resumed` names the task
  answering its request or posting its note. An entry whose `task` is this task's id, or whose `request` is
  this request: result `[no-send]`. Otherwise answer it now (step 2), the apps are connected.
- **Exit 2 with `too_many_apps`:** this task already waits in this room for other apps, and one wait
  holds at most 5. That wait stays armed and answers its request once its apps connect (`status
  --room` lists them). Say: "I'll pick this up once <its apps> are connected; for <the other apps>,
  tell me once they're connected and I'll check." In the DM that is the result; from 3b put it on the
  waiting card with `note` and write the result `[no-send]`.
- **Any other exit 2:** no automatic resume is possible. Say: "Once you've connected Google
  Calendar, tell me and I'll check." In the DM that is the result; from 3b there is no private card
  to carry it, so send it to the owner's confirmed DM and write the result `[no-send]` (no confirmed
  DM: `[no-send]` alone). Never in the shared room.

## Step 5: resume

**A resume task arrives** (`source: connector-resume`, id `task-connect-<wait-id>`). Its text names the
apps, the original request (several requests joined by " / " when the owner asked more than once),
the room and the `reply_to`. It is owner work for that room.

**Its text says `private card`** (a wait from step 3b): do what it says. Every word about
connecting, sign-in or accounts goes on the card with `note`, never in the room or the DM:

- Connected and `verify-account` exits 0: `note '<wait-id>' "YouTube is connected. On it."`, run the
  request with `composio_exec`, and post the result where the answer may go (above), `reply_to` as
  given, `operation_id <wait-id>:answer`. If `composio_exec` still reports `connect_required`,
  `note` "YouTube isn't connected yet: tap Connect again and I'll carry on." and post nothing.
- `verify-account` exits non-zero, timed out, the account changed, or `unverified`: only the `note`
  the task describes. Share no data, post nothing.
- Either way the result is `[no-send]`.

**Otherwise** (a wait from the DM, 3a):

- Connected: first `python3 "$C" verify-account '<wait-id>'`. Any exit but 0 (`account_changed`,
  `account_unknown`, `not_signed_in`, ...) means a different or unconfirmed AG2 Cloud account is
  signed in now: share no data, and post "I didn't check your Google Calendar: I couldn't confirm the
  AG2 Cloud account signed in now is the one you asked from. Ask me again once it is." (`operation_id
  <wait-id>:account`).
  Exit 0: run `composio_find` / `composio_exec`, confirm the room is still the owner's DM
  (`room.inspect`, exactly 2 members; otherwise post only "Google Calendar is connected: ask me again
  in our DM."), then post the answer with `room.message.send` (`operation_id <wait-id>:answer`,
  `reply_to` as given): "Your Google Calendar is connected and ready to go. Here's your Friday: ...".
  If `composio_exec` still reports `connect_required`, post "Google Calendar isn't connected yet: tap
  Connect on the card again and tell me when it's done."
- Timed out, the cloud account changed, or the account the wait was made under could not be confirmed
  (`unverified`): post the note the task describes (its `operation_id`), and share no data. After an
  `unverified` note, the owner's next request is a fresh one: answer it as in step 2.
- Either way the result is `[no-send]`: the answer already went out.

**The owner writes first** in that room ("done", "connected", or the same request again):

```bash
python3 "$C" claim '<room id>'
```

In a shared room, each wait in `claimed`, `pending` or `resumed` with `"private": true` keeps its
connect talk on its card: the "isn't connected yet", "didn't check" and "tap Connect" lines below
become `note '<wait-id>' "..."`, and the room gets only the answer itself, where the answer may go.
When the owner's message was only "done" or "connected", the room gets nothing: `[no-send]`.

- Exit 0: `claimed` lists the waits whose apps are connected; the waiter can no longer fire. With
  `account_ok: true`, answer its `request` in this task's reply, where the answer may go. With
  `account_ok: false` (`account_reason` `account_changed` or `account_unknown`), share no data for it:
  "I didn't check your Google Calendar for your earlier request: I couldn't confirm the AG2 Cloud
  account signed in now is the one you asked from. Ask me again once it is." When this message is
  that request asked again, answer it as a fresh one instead (step 2).
- `resumed` lists a wait with `claimed_by` `connected` or `claim`: its request is answered.
  `resume_pending: true`: the resume task answers it; for "done" or the same request write
  `[no-send]`. Otherwise acknowledge in one short line only if the message needs a reply, and don't
  repeat the answer unless the owner asks again after seeing it.
- `resumed` lists a wait with `claimed_by` `timeout`, `user_changed` or `unverified`: it got a note,
  not an answer. The same request asked again is a fresh one: answer it (step 2), even while
  `resume_pending` is true.
- Exit 1 with `pending` not empty: the apps aren't connected yet. Say so ("Google Calendar isn't
  connected yet: tap Connect on the card and I'll pick it up as soon as it is."). The wait stays armed.
- All three empty: there is no wait; handle the message normally.

## Paid cloud tools

`station_find` marks a metered tool `confirm_before_call: true`. Before the first `station_call` to
such a tool in a conversation, tell the owner the price from its `pricing` ("5 credits per result")
and wait for their OK. A tool activated mid-conversation is usable at once through `station_call`.

## Rules

- Never paste a sign-in or OAuth link. The card (or AG2 Space → Marketplace) is the only way to connect.
- One card per request, listing every app it needs.
- Private content (mail, calendar, files, messages, contacts) only in a room confirmed as the owner's DM.
- In a room with other people, never mention connecting, sign-in, accounts or cards: all of it goes
  on the private card (`--private`, `note`).
- Never restart the engine, and never ask for a restart except the one step 1 case.
- No cards and no waits from cron, proactive passes, voice, phone, or any channel other than AG2 Space.
- Only `connectors.py` writes a resume task. Never write one yourself.
- Report only what an action's result shows. Whose name a message was sent under, who can see it, or
  whether it went out at all: say it only if the `composio_exec` result says so; otherwise say you don't know.
  Never guess: the same connector can post as the owner or as an app, depending on how it was connected.
