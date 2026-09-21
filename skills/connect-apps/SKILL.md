---
name: connect-apps
description: "Use when the owner asks about their calendar, email, meetings, Linear, Notion, Slack, Google Drive, GitHub or any other third-party app or the data in it ('what's on my calendar', 'any email from Sam', 'my Linear issues', 'find the Drive doc'), asks to connect one, asks to switch or change their <app> account, reconnect <app> or use a different <app> account, asks which apps they are connected to, asks to disconnect <app>, or says 'done' / 'connected' / 'I signed in' / 'switched' after you sent a Connect or Switch account card. Answers through the Superpower Station connector tools (composio_find, composio_exec). When an app isn't connected yet, posts ONE Connect card to the owner (in their DM as a message; in a room with other people privately, under their message, visible only to them), closes the task, and answers by itself once they sign in, with no engine restart."
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
python3 "$C" status                            # every connection: id, toolkit, name, status, accountLabel
python3 "$C" card googlecalendar --room '<room id>' --reply-to '<source_message_id>' \
  --task '<task id>' --owner-from-task --request-file - <<'SUTANDO_REQUEST'
<the owner request, verbatim>
SUTANDO_REQUEST
# the owner's DM: checks the catalog and the connections, arms the wait, prints "message" to post
python3 "$C" card youtube --room '<shared room id>' --reply-to '<source_message_id>' \
  --task '<task id>' --owner-from-task --private \
  --line "YouTube isn't connected yet, so I can't do that. Connect it here and I'll carry on." \
  --line "Once that's done I'll pull up your latest videos." --request-file - <<'SUTANDO_REQUEST'
<the owner request, verbatim>
SUTANDO_REQUEST
# a room with other people: writes the private card under the owner's message; nothing to post
python3 "$C" card linear --room '<room id>' --reply-to '<source_message_id>' \
  --task '<task id>' --owner-from-task --switch --request-file - <<'SUTANDO_REQUEST'
<the owner request, verbatim>
SUTANDO_REQUEST
# a Switch account card (add --private in a room with other people)
python3 "$C" note '<wait id>' "YouTube is connected. On it." [--status connected]   # a private-card line
python3 "$C" claim '<room id>'                 # before answering in a room that may have a wait
python3 "$C" verify-account '<wait id>'        # a resume: is the wait's AG2 Cloud account the one in use now?
python3 "$C" rearm                             # restart dead waiters (startup + proactive loop run it)
```

`card` is `status` + `await` in one process: the catalog check for each slug, one connections read,
one account read, then the wait (merged with the room's pending waits for the same apps and account,
exactly as `await`). `--owner-from-task` takes the owner from the task file's `user_id`, so you never
call `ag2.whoami` for it. `--line` is optional: the first line is the intro (the text above the card
in the DM, the first line of a private card); without it a plain default is used. `await` (same
arguments, `--owner '<owner mxid>'` required, no catalog or connections check, no message) still
exists for the same wait; `card` is the one to use.

Pass the owner's words only through the quoted heredoc above, never inside a quoted `--request`
argument: their text (an apostrophe, a quote) must never become shell. `--line` and `note` text is
yours, not the owner's: plain sentences, no quotes from their message.

| exit | meaning |
|---|---|
| 0 | done: a match, all apps connected, a wait recorded (or `card` found every app connected), a wait claimed, the account verified |
| 1 | a negative answer: no exact match, an app not connected, nothing claimed, `account_changed`, `account_unknown`, `no_such_wait` |
| 2 | a setup problem to relay, never retry: `not_signed_in`, `connectors_disabled`, `unknown_app`, `coming_soon`, `too_many_apps`, `not_owner_task`, `invalid_arguments`, `cloud_error` |

`card` prints everything `await` prints plus `mode` (`dm` or `private`), `apps` (each with
`connected`), `all_connected`, and for `mode: dm` a `message` object (`body`, `extra_content`,
`reply_to`, `operation_id`) to pass verbatim to `room.action.execute` / `room.message.send`. It is
`null` for a private card and when no wait was made. `find`, `status` and `card` read the
connections and the catalog through a 30-second cache; `claim`, `verify-account`, the switch
baseline and the waiter always read the cloud.

`--switch` (step 3c) records which connections of the apps are active right now; the wait is ready
only once each app has an active connection that wasn't, so the old account never answers. A switch
wait and a plain wait are never merged. Every wait summary carries `switch: true|false`.

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

`card` and `await` check the same on the task file and refuse anything else with `not_owner_task`.
Everything else (cron, a proactive-loop pass, voice, phone, Slack, Discord, Telegram, local chat, a
collaborator or any non-owner) gets text only when an app isn't connected: "Google Calendar isn't
connected yet; connect it in AG2 Space." No card, no wait. A non-owner never gets the owner's
connected-app data either.

## Step 1: are the Station tools loaded?

`mcp__sutando-station__composio_find` is normally in your tool list: call it directly, without a
ToolSearch first. Only when it is not listed, and you have ToolSearch, try
`select:mcp__sutando-station__composio_find` once (the fallback, not the first step). If neither
finds it:

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

Everything below depends on whether the task's room is the owner's DM with you. Read it from the
task file, in this order, and stop at the first that answers:

1. The task's `channel_kind` header: `dm` means the DM, `room` means **shared**.
2. No `channel_kind`: `room_member_count` of exactly `2` means the DM.
3. Neither: `room.inspect` on the room; `safe_metadata.joined_member_count` of exactly `2` means the DM.

A missing, unparseable or larger count, or a failed `room.inspect`, means **shared**. The precheck
line (Step 2) repeats the verdict as `room_kind=dm|room|unknown`; `unknown` means go to item 3.

## Where the answer may go

Reply where you were asked. The precheck line (Step 2) says it as `reply_to=room|dm`.

- **Anything you did, found or wrote on the web or in a public app** (post to Slack, create a Linear
  issue, search YouTube, summarize a public page, web research, listings, shopping, summaries, code):
  reply in the room they asked in, like any reply, when that room is the audience: the room asked
  for it, or the owner asked for it to be posted there. An apartment search asked by a shared room
  is answered in that room; one the owner asked for themselves goes to the owner's DM, with nothing
  in the room unless the room was waiting for it (CLAUDE.md "Where replies go").
- **The DM exception is a closed list: data read from the owner's connected accounts or device**
  (mail, calendar events, contacts, message history, files from Drive/Dropbox/Notion, credentials,
  health or financial records): only in the owner's DM. From a shared room, find your DM with the
  owner with `room.list` and confirm it with `room.inspect` as above; the answer goes there, and the
  shared room gets exactly one line: "I sent it to you in our DM." Never move silently. No confirmed
  DM: text only, no data ("I can only share your calendar in our DM.").

Connecting itself (the card, "isn't connected yet", "once that's done", timeouts, account notes) is
never posted in a shared room: it goes on the private card (step 3b).

## Step 2: find the apps

When you first touched the task file, the connect-apps precheck hook may have added a line to your
context: `connect-apps precheck: needs_connect=<slugs>; connected=<slugs>; room_kind=…; reply_to=…; run: …`.
It is read from the task's words and a 30-second cache of the owner's connections, so:

- `needs_connect=` names every app the request needs (and `connected=` the rest): skip
  `composio_find` and go straight to step 3 with those slugs (the `run:` hint is the `card` call,
  with the room, `--reply-to` and `--task` filled in; you add the request heredoc).
- Anything else (`mentions=` with `connected=unknown`, an app the request needs that the line does
  not name, a `needs_connect=none`, no line at all): call `composio_find` **once**, with `apps`
  naming every app the request needs and `query` set to the request.

For each app the request needs:

- `connected: true` and the owner wants a different account for it ("switch my Linear account", "use
  my other Gmail", "reconnect Notion"): step 3c.
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

1. **One call.** Run `card` exactly as in the Tools block: every missing slug (at most 5), `--room`
   the task's room, `--reply-to` the task's `source_message_id`, `--task` the task's id,
   `--owner-from-task`, the owner's request in the quoted heredoc, and optionally one `--line` with
   your intro, adapted to the app and the request ("Your Google Calendar isn't connected yet, so I
   can't peek at your schedule. Want to hook it up?"). No `status`, no `ag2.whoami`, no separate
   intro message: `card` checks for a wait already in the room and folds this request into it.
2. **Post the message.** On exit 0 with a `wait_id` and a `message`: ONE `room.action.execute` with
   action `room.message.send` and the printed `message` object verbatim (`body`, `extra_content`,
   `reply_to`, `operation_id`). The body is your intro above the card; `extra_content` is the card
   itself, listing each app. `"reused": true` means this task already posted that card: the same
   `operation_id` makes the post a no-op, so posting again is harmless, or say "The Connect card
   above still works: tap Connect and I'll pick it up as soon as it's connected." A non-empty
   `superseded` means an earlier card for these apps is already in the room; the new message is
   still right (it lists everything the merged wait waits for).
   If the action is not offered or fails, fall back to the text line from step 0; the wait stays
   armed either way.
3. Go to step 4.

## Step 3b: a private card, in a room with other people

For a task whose room is shared. Nothing about connecting is posted anywhere: no intro, no card
message, no "I sent you a card in our DM", no outro.

1. **No `source_message_id`** on the task: a private card has nowhere to show. Send the owner's DM
   (found and confirmed as in "Where the answer may go") the text line from step 0, post nothing in
   the shared room, write the result `[no-send]`, and stop. No confirmed DM: result `[no-send]`.
2. **One call.** Run `card --private` exactly as in the Tools block: `--room` the task's room,
   `--reply-to` the task's `source_message_id`, `--task`, `--owner-from-task`, the request in the
   heredoc, and two `--line`s in your voice, adapted to the app and the request: the intro
   ("YouTube isn't connected yet, so I can't do that. Connect it here and I'll carry on.") and the
   outro ("Once that's done I'll pull up your latest videos."). The desktop client draws the card
   and the lines under the owner's message. A card already waiting for these apps in the room is
   folded in by `card` itself: the old card points at the new one, so no `status` or `note` first.
3. Go to step 4 (`mode: private`, `message: null`).

## Step 3c: switch an app to another account

For a request to sign an app that is already connected in with a different account. Step 0 applies:
anything but the owner's own AG2 Space task gets text only, "You can switch your Linear account in
AG2 Space: Settings → Integrations."

1. **The request.** When the owner asked something that needs the other account ("check my work Gmail
   instead"), that is the request to redo. When they only asked to switch, the request is their
   switch message itself; the resume then only says "<App> is now signed in as <label>".
2. **In the owner's DM** (see "Which kind of room"): run `card <slugs> --switch` as in the Tools
   block (the quoted heredoc, `--room`, `--reply-to`, `--task`, `--owner-from-task`, optionally
   `--line "<intro>"`). It records the account in use before any card exists, so a sign-in that lands
   right after the card is still seen as the switch, and only then prints the `message`. On exit 0
   with a `wait_id`, post that `message` with ONE `room.action.execute` / `room.message.send` (its
   `operation_id` is `<task id>:switch-card`, its `extra_content` carries `"mode": "switch"`):

   ```json
   {
     "body": "<your intro>\n\nSwitch your Linear account: tap Switch account on the card, or open Settings → Integrations.",
     "extra_content": {
       "space.ag2.connector": {"version": 1, "for": "<owner_id>", "toolkits": [{"slug": "linear"}], "mode": "switch"}
     },
     "reply_to": "<source_message_id>", "operation_id": "<task id>:switch-card"
   }
   ```

   Then write the result: "Once you've signed in with the other account I'll carry on." (adapted).
   `"reused": true` means that switch card is already waiting: say "The Switch account card above still
   works." instead of sending another. Any exit 2: no card; say "You can switch your Linear account in
   Settings → Integrations; tell me once it's done."
3. **In a room with other people:** no messages about it in the room. With a `source_message_id`, run
   `card <slugs> --private --switch --line "<intro>" --line "<outro>"` (for example "Tap Switch account
   to sign Linear in with your other account." and "Once that's done I'll carry on."), then write the
   result `[no-send]`. Without one, handle it as step 3b item 1.

The resume task of a switch wait says "signed in with the new account" and is handled as in step 5:
`verify-account` first, then redo the request, or for a switch-only request read
`python3 "$C" status <slug>` and say "<App> is now signed in as <accountLabel>" (no label: "<App> is
now signed in with the new account."). The label is personal: in a shared room it goes on the private
card with `note`, never in the room.

## Which apps am I connected to? Disconnect <app>

- **"Which apps am I connected to?"** Run `python3 "$C" status`. List the apps whose `connections`
  status is `active`, each with its `accountLabel` when there is one, and end with "Manage them in
  Settings → Integrations." Account labels are personal: answer only where private content may go
  (the owner's confirmed DM, see "Where the answer may go"); from a shared room, send it to the DM and
  say only "I sent it to you in our DM."
- **"Disconnect <app>"**: no action and no card. Say "You can disconnect <App> in Settings →
  Integrations." You never disconnect an app yourself.

## Step 4: the result

The `card` call from step 3 already armed the wait; read its output:

- **Exit 0 with a `wait_id`:** in the DM (3a), after posting the `message`, write the task result
  now: "Once that's done I'll be able to check what's coming up for you." (adapted to the app). From
  3b the outro is already on the private card: write the result `[no-send]`. The task is closed; do
  not wait inside it. (`superseded` lists earlier waits this one absorbed; `"waiter_pid": null`
  means the waiter could not start, and the next `rearm` starts it.)
- **Exit 0 with `"all_connected": true`:** every app is connected after all (the precheck or
  `composio_find` was stale): no card, no wait; answer the request now as in step 2.
- **Exit 0 with `"wait_id": null`** and `resumed`: an earlier wait was handled meanwhile: `resumed`
  names the task answering its request or posting its note. An entry whose `task` is this task's id,
  or whose `request` is this request: result `[no-send]`. Otherwise answer it now (step 2), the apps
  are connected.
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
  connected yet: tap Connect on the card and I'll pick it up as soon as it is."). For a pending wait
  with `switch: true` the app is not switched yet: "Linear isn't switched yet: tap Switch account on
  the card and I'll carry on once you've signed in with the other account." The wait stays armed.
- All three empty: there is no wait; handle the message normally.

## Paid cloud tools

`station_find` marks a metered tool `confirm_before_call: true`. Before the first `station_call` to
such a tool in a conversation, tell the owner the price from its `pricing` ("5 credits per result")
and wait for their OK. A tool activated mid-conversation is usable at once through `station_call`.

## Rules

- Never paste a sign-in or OAuth link. The card (or AG2 Space → Marketplace) is the only way to connect.
- The place to see, switch or disconnect the owner's connected apps is **Settings → Integrations**.
  Never name a "Superpower Station page" or a dashboard for that. Marketplace is only for browsing and
  connecting new apps.
- Never disconnect an app, and never switch one without the owner's card tap.
- One card per request, listing every app it needs.
- Data read from the owner's connected accounts or device (mail, calendar events, contacts, message
  history, files from Drive/Dropbox/Notion, credentials, health or financial records) only in a room
  confirmed as the owner's DM; that list is closed. Web research, listings, shopping, summaries and
  code go where they were asked when that room is the audience; what the owner asked for themselves
  goes to the DM. A moved answer the room was waiting for leaves one line in the room: "I sent it to
  you in our DM."
- In a room with other people, never mention connecting, sign-in, accounts or cards: all of it goes
  on the private card (`--private`, `note`).
- Never restart the engine, and never ask for a restart except the one step 1 case.
- No cards and no waits from cron, proactive passes, voice, phone, or any channel other than AG2 Space.
- Only `connectors.py` writes a resume task. Never write one yourself.
- Report only what an action's result shows. Whose name a message was sent under, who can see it, or
  whether it went out at all: say it only if the `composio_exec` result says so; otherwise say you don't know.
  Never guess: the same connector can post as the owner or as an app, depending on how it was connected.
