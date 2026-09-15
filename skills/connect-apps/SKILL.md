---
name: connect-apps
description: "Use when the owner asks about their calendar, email, meetings, Linear, Notion, Slack, Google Drive, GitHub or any other third-party app or the data in it ('what's on my calendar', 'any email from Sam', 'my Linear issues', 'find the Drive doc'), asks to connect one, or says 'done' / 'connected' / 'I signed in' after you sent a Connect card. Answers through the Superpower Station connector tools (composio_find, composio_exec). When an app isn't connected yet, posts ONE in-chat Connect card to the owner, closes the task, and answers by itself once they sign in, with no engine restart."
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
python3 "$C" claim '<room id>'                 # before answering in a room that may have a wait
python3 "$C" rearm                             # restart dead waiters (startup + proactive loop run it)
```

Pass the owner's words only through the quoted heredoc above, never inside a quoted `--request`
argument: their text (an apostrophe, a quote) must never become shell.

| exit | meaning |
|---|---|
| 0 | done: a match, all apps connected, a wait recorded, a wait claimed |
| 1 | a negative answer: no exact match, an app not connected, nothing claimed |
| 2 | a setup problem to relay, never retry: `not_signed_in`, `connectors_disabled`, `unknown_app`, `coming_soon`, `not_owner_task`, `invalid_arguments`, `cloud_error` |

Every command prints one JSON object. A **resumed** entry (`resumed` from `claim` and `await`,
`resumed_waits` from `status`) is a wait from the last 30 minutes whose request is already handled:
`resume_task` names the task that answers it (`null` when an owner message task claimed and answered
it), and `resume_pending: true` means that task hasn't run yet. When a resume task answers, close
your own task with `[no-send]`, never `[deduped: task-connect-...]`: a resume task's result is itself
`[no-send]`, so the gateway would read the dedup as unanswered and re-ask your task.

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

## Where personal data may go

Calendar events, mail, files and anything else from a connected app go only to a room where the
owner and you are the only members. A room counts as that DM only when you can confirm it:

1. The task's `room_member_count` is exactly `2`; or
2. `room.inspect` on the room returns `safe_metadata.joined_member_count` of exactly `2`.

A missing, unparseable or larger count, or a failed `room.inspect`, means **shared**. From a shared
room, find your DM with the owner with `room.list` and confirm it with `room.inspect` the same way;
the data, the card and the answer go there, and the shared room gets only "I sent it to you in our
DM." No confirmed DM: text only, no data ("I can only share your calendar in our DM.").

## Step 2: find the apps

Call `composio_find` with `apps` naming every app the request needs and `query` set to the request.
For each app:

- `connected: true`: first run `python3 "$C" claim '<task room>'` (and the DM's room id too when
  you are answering from a shared room):
  - `claimed` lists a wait: answer its `request` together with this message (see Step 5).
  - `resumed` lists a wait whose `task` is this task's id (a re-run of the original request), or
    one with `resume_pending: true` whose `request` is this request: it is answered, or about to be.
    Write the result `[no-send]` and stop.
  - Otherwise: find the action (`composio_find {toolkit, query}`), run `composio_exec`, and answer
    where personal data may go (above). Done.
- `coming_soon: true`: "<App> isn't available yet."
- `connected: false`: step 3 (or the text line from step 0 when the task gets no card).

If an app name doesn't match anything, `python3 "$C" find "<name>"` checks the catalog; exit 1 means
there is no such app, so say so.

## Step 3: one card for everything missing

1. **Which room.** The task's room when it is the owner's DM, else the confirmed DM (see "Where
   personal data may go"). From a shared room the task's result is "I sent you a connect card in our
   DM." No confirmed DM: text only, as in step 0.
2. **A card already waiting?** `python3 "$C" status <slugs> --room '<card room>'`. When
   `pending_waits` already lists these apps, skip the intro and the card (items 4 and 5): send "The
   Connect card above still works: tap Connect and I'll pick it up as soon as it's connected." and go
   on to step 4, which folds this request into that wait.
3. **Owner id.** Call `ag2.whoami` and use `runtime.owner_id` (never `actor.id`).
4. **Intro.** `room.action.execute` with action `room.message.send`, `operation_id`
   `<task id>:connect-intro`, and `reply_to` the task's `source_message_id` when the action accepts it
   (not in the shared-room case: that event lives in another room):
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

## Step 4: hand off and close

Run `await` exactly as in the Tools block: the owner's request goes in the quoted heredoc, `--room` is
the card room, `--owner` is `runtime.owner_id`. `--reply-to` is the task's `source_message_id`; in
the shared-room case use the event id the intro's `room.message.send` returned in the DM.

- **Exit 0 with a `wait_id`:** write the task result now: "Once that's done I'll be able to check
  what's coming up for you." (adapted to the app). In the shared-room case, send that line to the DM
  with `operation_id` `<task id>:connect-outro` and keep the result "I sent you a connect card in our
  DM." The task is closed; do not wait inside it. (`superseded` lists earlier waits this one absorbed;
  `"waiter_pid": null` means the waiter could not start, and the next `rearm` starts it.)
- **Exit 0 with `"wait_id": null`:** the apps were connected meanwhile and `resumed` names the task
  answering the earlier request. Same request: result `[no-send]`. A different request: answer it
  now (step 2), the apps are connected.
- **Exit 2:** no automatic resume is possible. Result: "Once you've connected Google Calendar, tell me
  and I'll check."

## Step 5: resume

**A resume task arrives** (`source: connector-resume`, id `task-connect-<wait-id>`). Its text names the
apps, the original request (several requests joined by " / " when the owner asked more than once),
the room and the `reply_to`. It is owner work for that room.

- Connected: run `composio_find` / `composio_exec`, confirm the room is still the owner's DM
  (`room.inspect`, exactly 2 members; otherwise post only "Google Calendar is connected: ask me again
  in our DM."), then post the answer with `room.message.send` (`operation_id <wait-id>:answer`,
  `reply_to` as given): "Your Google Calendar is connected and ready to go. Here's your Friday: ...".
  If `composio_exec` still reports `connect_required`, post "Google Calendar isn't connected yet: tap
  Connect on the card again and tell me when it's done."
- Timed out, or the cloud account changed: post the note the task describes (its `operation_id`).
- Either way the result is `[no-send]`: the answer already went out.

**The owner writes first** in that room ("done", "connected", or the same request again):

```bash
python3 "$C" claim '<room id>'
```

- Exit 0: `claimed` lists the waits whose apps are connected. Answer each `request` in this task's
  reply, where personal data may go; the waiter can no longer fire.
- `resumed` lists a wait: its request is already handled. `resume_pending: true`: the resume task
  answers it; for "done" or the same request write `[no-send]`. Otherwise it was answered already:
  acknowledge in one short line only if the message needs a reply, and don't repeat the answer unless
  the owner asks again after seeing it.
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
- Connected-app data only in a room confirmed as the owner's DM.
- Never restart the engine, and never ask for a restart except the one step 1 case.
- No cards and no waits from cron, proactive passes, voice, phone, or any channel other than AG2 Space.
- Only `connectors.py` writes a resume task. Never write one yourself.
