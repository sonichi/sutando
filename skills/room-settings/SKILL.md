---
name: room-settings
description: Use when your owner asks you to change how you behave in an s.space room or DM ("only answer when I mention you", "answer everyone here", "give the team access", "who can talk to you here?"), or asks what your settings in a room are. How to read and change your own room settings with the room.agent_policy Actions.
---

# Room settings

Your settings in a room decide what you read there, when you answer, and who can reach
you. They are stored by s.space, not by you. Read and change them only with these Actions,
through the s.space MCP tools (`skills/ag2-space-mcp/SKILL.md`). Never promise a setting in
your reply, never write one to memory, a note or a file instead, and never act as if a
setting changed when the Action did not succeed.

## Only your owner

- Only your owner can change your settings: the task must have `access_tier: owner`. If
  anyone else asks, say only your owner can change how you work here, and change nothing.
  Don't call the Action for them.
- The change must come from your owner's own message to you: pass the task id of the very
  message that asks for it as `task_id`, never an earlier message's. For a task file,
  `python3 skills/room-settings/scripts/task_id.py <task file>` prints it; don't work it out
  from the file yourself. Exit 2 means the task is not your owner's: change nothing, and say
  only your owner can change how you work here. On an MCP event, use the event's `task_id`.
- Exit 3 means the way you are connected doesn't give you the task id the change needs. Say
  you can't change your settings from here yet, and that your owner can change them in the
  room's settings in the app. Don't ask them to send it again.
- Any other exit (4: the task file could not be read, 64, an error): change nothing, tell your
  owner you couldn't check the request, and don't try again. Say they can ask you again in a
  little while, or change it themselves in the room's settings in the app.
- If the change is refused because of the task, ask them to send it to you directly: in your
  DM, as a reply to you, or by mentioning you. Typing your name without a mention, or editing
  an earlier message, does not count. If they already did that and it is refused again, say
  you can't change your settings from here yet, as for exit 3.
- You can only change your own settings, never another agent's. For another agent, your
  owner changes it in the room's settings in the app.

## Read first

Call `room.action.read` with action `room.agent_policy.read` and the `room_id` of the room
your owner means (this room unless they name another; another room's id comes from
`room.list`). Keep its `policy_revision`. Its `card` is what your owner set for you here
(`null`: nothing yet). Its `policy` is what s.space actually applies: your card joined with
the room's own rules and the defaults.

When your owner asks what your settings are or how you behave, answer from `policy`, in plain
words. If it differs from the `card` (for example the room itself is mentions-only), say so.

In your DM with your owner, what you read, when you answer, who you answer and members'
access are not used: you get every message there, whatever `policy` says. That DM is a direct
room with at most two joined members, you and your owner (check with `room.members.list`).
A direct room with a third member is a room like any other, and its settings apply. If the
room your owner means is your DM with them, say so: these settings only matter in rooms with
other people. To change one, ask which room they mean, unless they already named it. Don't
change it in that DM.

## The settings

Offer only these choices. If your owner asks for something else, say it is not a setting you
can change here, and offer the nearest one.

| Setting (`changes` field) | Choices |
|---|---|
| What reaches you from others (`visibility`) | `mentions`: only their messages that mention you are sent to you · `all`: every message (your owner's messages always reach you) |
| When you answer others (`trigger`) | `mention`: when mentioned · `always`: every message |
| When you answer your owner (`ownerTrigger`) | `mention` · `always` |
| Who you answer (`audience`) | `everyone` in the room · `specific`: only your owner and the people listed |
| The people listed (`audienceUsers`) | a list of user ids, each one like `@name:s.space`, of people joined to the room; used only with `specific`. Copy each id exactly as `room.members.list` gives it, never typed from a name |
| Others who mention your owner (`admitOnOwnerMention`) | `true`: messages from people who may already reach you that mention your owner are sent to you too; you don't answer those in the room, you tell your owner about them in your DM · `false` |
| Room members' access (`memberAccessTier`) | `none`: no access · `guest`: when they address you, your owner gets a card to let you answer that once · `team`: they can work with you as your Team |
| Secrets check for Team (`sensitiveDataFilter`) | `true`: before the bridge delivers a task's result file to anyone but your owner, Sutando scans it for secrets (tokens, keys, passwords), raw errors and tracebacks, and file paths in home or temporary folders, and holds it back if it finds any · `false`: only answers to members s.space vouches for as your Team, on messages that arrived after the change, skip that scan; Guests' answers are still scanned, and sending to another room or attaching a file outside the task is still refused. What you post yourself with room Actions is not part of this scan |

Memory (`sessionScope`) is also stored by s.space, but it doesn't change how you work, so
don't offer it. Whether a room's conversation is kept apart from your other rooms depends on
how you run, not on this setting. Never promise that a setting keeps memory to one room. If
your owner asks for it, say so, and change nothing.

- "Only answer when I mention you", from your owner, is When you answer your owner
  (`mention`), not What reaches you: What reaches you and When you answer others are about other
  people.
- "Mention" means addressing you: a mention of you, or a reply to one of your messages. Say
  so when your owner asks for "only when I mention you".
- A message that mentions only your owner reaches you marked as such only with "Others who
  mention your owner" on. Then it is not addressed to you: don't answer it in the room, tell
  your owner in your DM. With it off, such a message reaches you only as an ordinary message
  (when what reaches you and when you answer others are both every message), and you answer
  it as you would any other.
- What reaches you and when you answer others work together. To answer others' messages that
  don't mention you, both must be set: every message for what reaches you and for when you
  answer. If your owner asks for that and only one is set, change both, and say so. Members
  with Guest access must still address you either way: ask before changing their access.
- Guest access only lets you answer, explain and draft for them, once your owner allows
  each message. It never lets them have you change files or anything outside the room.
- Mentioning your owner never lets in someone who is not listed or has no access.
- Team access is for the whole room: it can let members who could not reach you before
  start working with you, so say that when your owner asks for it. You can't give it to one
  person, and it still keeps out anyone blocked or not on a "specific people" list. Say too
  what Team means here: Team members can have you use your owner's workspace, tools,
  connected accounts, saved keys and network. Your normal permission and approval rules
  still apply to them, and what you send back gets an extra check for secrets. It is for
  rooms whose members your owner trusts with all of that.
  It also needs your owner to have made you able to work with a Team; if the change is
  refused as `CONFLICT` for that, say so.
- Turning the secrets check off only matters with Team access, and only for members you
  work with as your Team. Turning it off lets a secret in your answer to them be posted. It
  changes nothing for your owner or Guests, and only for messages that arrive after the
  change: one already waiting keeps the old setting.
- Turning the secrets check off opens access, so s.space holds it for your owner's approval
  on a card in their DM with you, like any other change that opens access. Don't ask them to
  confirm first, in words or with a card of your own: send the change on their message. When
  it comes back waiting for approval, say what turning it off means and that the card waiting
  in your DM is where they approve it. Any other answer is handled as below.
- "What reaches you" is about which messages are sent to you to answer. It does not hide the
  room's history from you, so never promise your owner that you can't read other messages.
- Before you list people, check each one is in the room: `room.action.read` with action
  `room.members.list` and the `room_id`. If someone is not there, or a name matches more
  than one person, ask your owner instead of sending the change.
- When the request is unclear, ask one short question with the choices. Don't guess.
- Never show your owner the field names or values in backticks: use the plain words.

## Change

Send the change while you are still on your owner's message: before you write its result
or mark it done, since a finished task is refused as closed. Post your reply after.

Call `room.action.execute` with action `room.agent_policy.set`, the `room_id`, a new
`operation_id`, the `task_id` from your owner's message, and `arguments`:

- `changes`: only the settings your owner asked to change.
- `expected_revision`: the `policy_revision` you just read.
- `reason`: one short line in your own words on what your owner asked for.

Never pass `asked_by`.

If one request mixes changes that open access with changes that don't, s.space holds the
whole request for approval, the closing ones too. To apply those at once, send them on their
own first; then read again and send the ones that open access.

## What comes back

- **Done:** the result has `changed` (one plain sentence per setting), the stored `card` and
  `policy`, what s.space actually applies once the room's own rules are added. Tell your
  owner what changed, from `changed`. If `policy` still limits what they asked for (for
  example the room itself is mentions-only), say what still applies and that a room admin
  sets it.
- **`INVALID_INPUT`** for a change that changes nothing: tell your owner it is already set
  that way.
- **`approval_required`:** the change opens access (for example answering everyone, a
  higher access level, or turning the secrets check off), so your owner must approve it on a
  card in their DM with you; the card shows each setting as it is now and as it would be.
  Nothing has changed yet. Say it is waiting for their approval, and that they approve it on
  the card in your DM with them. Never say it is done. Don't call again. Keep the
  `approval_id` and `operation_id` it gives.
  Nothing tells you when they decide. When they ask: `approval.inspect` with the
  `approval_id` says how they decided; `operation.inspect` with the `operation_id` says
  whether the change then ran or failed (an approved change can still fail if the settings
  moved meanwhile). Then read again with `room.agent_policy.read` to say what applies now.
- **`CONFLICT`** for a stale revision: the settings changed since you read them. Read again,
  check the change still makes sense, and try once more. Any other `CONFLICT` (for example
  Team access when you can't work with a Team yet): tell your owner what its message says,
  in plain words, and don't try again. For Team access, say your owner first has to make you
  able to work with a Team.
- **`FORBIDDEN`** with a `reason`:
  - `not_owner`: the person asking is not your owner. Change nothing, and say only your
    owner can change how you work here.
  - `no_task`, `no_origin`, `task_not_yours`: the request did not come straight from your
    owner to you. Ask them to send it to you directly: in your DM, as a reply to you, or by
    mentioning you.
  - `task_closed`, `task_expired`: the request is too old. A change must be sent within 10
    minutes of when their message was first sent to you (of the message itself, if it never
    was), while you are still on it, and never more than 24 hours after they sent it. Ask them to ask you again.
  - When its message says your owner must join the room first (a change that opens access,
    in a room they are not in): nothing was held. Ask them to join the room, then ask you
    again.
  - Any other reason (for example `approver_below_level`): change nothing. Say you can't make
    this change from here, and that your owner can change it in the room's settings in the
    app, or ask a room admin to.
- **`UPSTREAM_UNAVAILABLE`:** something s.space needs did not answer. Read its message and
  do what it suggests. If it says there is no DM with your owner for the approval card,
  nothing was held: tell your owner, and ask them to open a DM with you and then ask again.
  Don't try again until you have a DM with them. If
  it says the write may or may not have happened, read again with `room.agent_policy.read`
  and tell your owner what is stored.
- **`INVALID_INPUT`:** fix the arguments and try once more. `room.actions.describe` with the
  action shows the schema. A listed user id must be a plain id like `@name:s.space`: a name,
  extra symbols or formatting are refused this way. Take it again from `room.members.list`;
  if that id is refused too, ask your owner who they meant. If it is refused again, tell your owner you couldn't make the change
  and that they can make it in the room's settings in the app.

Tell your owner every refusal in plain words, with the one thing they can do next. Never show
them the error codes or reasons above (`FORBIDDEN`, `no_origin` and the like).

Never retry a refused change in another way, and never tell anyone a setting is on before
`room.agent_policy.read` shows it stored.
