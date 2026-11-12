# Agent-native design rules

Every contribution that adds or changes something a person or an agent can do in s.space follows
these rules. The same text lives in the web client, the backend and the agent engine; only the
section "What this means here" differs.

## 1. Why

s.space is a workspace where people and agents work together.

Arranging a user interface so that every person can find every control by navigation is hard, and
it depends on a designer getting every page right. Example: the gear Settings page mixes things
that belong to the person (avatar, bio) with things that belong to the app (theme).

If the logic of each capability is defined cleanly, an agent can bring the one control the person
needs to where they already are. Far less then depends on perfect navigation. Pages remain as the
complete reference.

## 2. Two halves

- **Anything a person can do, their agent can do through an Action, under the same authority.**
  That authority is the owner's, as delegated: the agent's own, delegated by its owner, or
  approved per use. It is never the authority of whoever is asking in a room. High-impact Actions
  stay behind an approval hold.
- **Anything an agent needs from a person arrives as a control, not a sentence.**

High-impact means, at least, irreversible actions and outward-facing ones: sending to people or
places outside the conversation the request came from (email, a post, another room), publishing,
purchasing, deleting, and changing permissions or credentials. A reply in the conversation the
request came from, or a message to the agent's own owner in their DM, is not high-impact. A
standing approval the owner gave for a named kind of action satisfies the hold, and only a person
can give it.

An Action's risk and its approval requirement come from the Action's own metadata, set by the
platform. They never come from the caller or the card, so an agent cannot downgrade them.

An authorization refusal does not reveal whether the target exists. A caller without access gets
the same answer for "not found" and "not allowed". The refusal names what the caller can do (ask
the owner, request access), not the target.

Some things are person-only, by design. An agent never:

- approves its own approval hold;
- widens its own authority;
- changes sign-in factors (password, 2FA, recovery, sessions, devices);
- mints or reveals credentials;
- deletes the person's account.

These are not coverage gaps. They stay with the person, and the agent's part is to ask, with a
card.

## 3. The rule of thumb

Any deterministic action we expect the user to take gets a control (a button, a picker or a
field), in place, in one step.

Deterministic means the agent or the app already knows the action and its target: approve this,
sign in to that, pick one of these, open this page. Open-ended input stays chat.

The test: if you are about to write "reply yes to…", "open this link", "go to Settings → …" or
"run this command", stop and add the control.

Where no control exists yet, one plain instruction is allowed, and it is tracked as a gap. Known
gaps today:

- Sending a secret is a typed `vault set` command.
- Re-linking an agent is a typed approval code.
- A click on a room card that reaches an agent as a task, and the owner's click on an owner card
  in a shared room, are not accepted yet (section 11).
- A card's outcome is not yet edited in place: the updated card is posted as a reply to the old
  one, which stays as it was (section 7).

## 4. Fewest steps, in place

- One tap on the first card. No second card, no detour to Settings.
- One approval for closely related items, not one per item. The card lists each thing it covers.
- The current value is pre-filled.
- A value that becomes permanent on approval (for example an agent's ID) is editable on the card
  before approving, because it cannot be changed afterwards.

## 5. No dead ends

The person can always finish what they intend, even a delete.

- Safety may add a step. It never removes the path. The step is: see the consequence, press a
  button named after the action, and the acting side re-checks.
- Every refusal names the way through. It never sends the person back to the thing that just
  failed. An authorization refusal still does not reveal the target (section 2).
- Every "Go" lands on something the person can act on.
- Dismissing hides an item from the bar. It comes back by itself only if the card changes. The
  person can bring it back any time from "N hidden · Show" in the bar or the drawer, and the card
  itself stays in its room.
- The person's own words always work as a fallback.

## 6. Every setting and action has a clean definition

A setting or action is defined once. The agent, the card and the Settings page all use that one
definition. It states:

- **What it belongs to**: the user, the app, a room or an agent (table below).
- **What it does**, in one plain sentence.
- **Who may change it**. The rule is the same for a person and for an agent acting for them.
- **Its risk**: its action class (section 7) and its approval requirement. Both come from the
  Action's own metadata (section 2).

A new setting without these four is not ready to merge.

Settings are grouped by what they belong to:

| Belongs to | Meaning | Examples |
| --- | --- | --- |
| User | Intrinsic to the person | Name, avatar, bio, account, devices |
| App | The client on this device | Theme, language, notification delivery, shortcuts, updates |
| Room | The room itself | Name, members, permissions |
| Agent | One agent | Its name, its model, where it runs |

Two cases that are easy to get wrong:

- A room's notification level belongs to the person, not the room.
- Who an agent answers in a room: the room sets the ceiling, and the agent's owner chooses within
  it.

## 7. What a card must do

- Say what is being asked, in plain words.
- Put the evidence one tap away. Anything the card mentions that lives elsewhere (a pull request,
  a page, a room, a file, a person) is a link to it. A pull request link shows live status (state,
  review, checks), fetched as the viewer, when the viewer has a connected account that can read
  it. Otherwise it is a plain link with no status. Status is never fetched with someone else's
  credential, and a private title is never shown to someone without access.
- Let the person answer in one step, by action class (table below).
- Show the card's own outcome on the same card: the status changes to Approved, Expired or Done,
  updated in place. No second chat message repeats it. In-place update is a tracked gap today
  (section 3).
- Show internals (ids, hostnames, session names, raw provider errors) only in Details. Secrets and
  key fragments appear nowhere a person can read them.
- State an install-wide or hard-to-undo effect plainly, before the tap. Keep it undoable where
  possible.
- Carry a plain-text fallback for clients that do not know the card.

| Action class | How the person does it |
| --- | --- |
| Safe | One tap, anywhere. |
| Reversible | One tap, with Undo where one exists. |
| Irreversible | Confirmed in place. The row or card shows the specific consequence and a button named after the action. Never a single tap on a collapsed line. The acting side re-checks before it acts. |
| Needs context | One tap goes to the card. |

Links:

- In-app targets (rooms, messages, Commons pages, settings) open in place, in the side panel.
- An external site opens in place only where the client can host it (the desktop app's side
  panel). Where a site refuses to be embedded (most external sites in the browser client), a
  preview is shown for known kinds, otherwise a new tab.
- A new tab is the fallback, not the default. Nothing opens until the person clicks.
- A link to a Commons page shows the page itself in the message, editable in place, opened on the
  part the agent changed.
- When an agent shares a page, it says which section changed and one line on what changed.

## 8. One way to ask a person

- Questions from an agent to a person have one model and one renderer. The model is: a headline,
  why it is asked, the options with what each does, and references. Internal details stay out of
  the visible text.
- A question with choices is always buttons.
- No hostnames or ids.
- A question never tells the person to edit a row or a database. The agent keeps its own
  bookkeeping.

## 9. Where things appear

- **Chat cards** are the record in the timeline. A card carrying data from the person's own
  accounts or device goes to their DM only, never a shared room.
- **The bar above the message box** shows every card waiting on the person, from any room, whether
  or not that agent is in the current room. It also carries live reminders (sign in, open the
  terminal, out of budget), which clear when the cause clears.
  - It acts in place when the action is safe. Otherwise one tap goes to the card.
  - More than one item becomes a drawer: "N things need you".
  - Order: a stuck agent first (signed out, out of budget, blocked), then approvals, then
    questions, then informational items. Within a class, the current room's items come first.
  - The bar and the agent's working-status line share one slot above the message box. An action
    item takes priority over working status.
  - Each item can be dismissed. Dismissing does not answer it. Dismissing hides an item from the
    bar. It comes back by itself only if the card changes. The person can bring it back any time
    from "N hidden · Show" in the bar or the drawer, and the card itself stays in its room.
  - It stays discreet in a shared room. For an item that comes from another room, the closed bar
    shows only that something needs the person and from which agent, or just a count, with no card
    text. The content appears only when the person opens the drawer.
  - When a stuck-agent item is hidden from the bar, the agent keeps a small warning mark.
- **Settings** is the reference.

## 10. Agents and discovery

- Other agents and agent types are named by product name (Dot, Muse, Sutando), never by invented
  task categories. An agent still introduces itself by its own display name.
- An agent recommends another agent only on an explicit capability: where it runs and what it can
  reach, always on, voice, connected apps, or already in the room. It names the capability. When
  nothing explicit differs, it does not recommend.
- Discovery is a small maintained deck of feature cards (size and contents are set in the spec).
  The deck is shown once after an update. An agent can pull a card when asked or when it would
  help, at most once per situation.
- Each card's main action takes the person into the feature in place, landing in the DM with their
  own agent.

## 11. Trust rules for controls

There are two kinds of card:

| Kind | Examples | Who may act | What the acting side checks |
| --- | --- | --- | --- |
| Owner card | An agent asking its own owner: approvals, sign-ins, the agent's own state | Only the owner | It issued the card, the card offered the payload, the sender is the owner, and the click is in the room the card was posted in (today, the owner's DM only) |
| Room card | Room questions, room-app buttons, invites, polls | Any member the room's permission allows for that action | It issued the card, the card offered the payload, and the room's permission for that action allows the sender, in place of ownership |

For both kinds:

- A click is data, not authority.
- A click that fails the check is refused. It is never treated as chat.
- There is one shared implementation of this check, not one per feature. Nobody writes a second.

Today the shared check covers owner cards in the owner's DM only. The owner's own click in a
shared room is refused. A room card whose clicks reach an agent as a task has no check yet, and
the acting side does not act on such a click until one is added to the same function. Both are
tracked gaps (section 3). Until then, the buttons of that kind of room card must not be relied on:
use a link or open button, or a question.

Other room interactions work today and are checked elsewhere: a room question asked through
`room.question.ask` is checked by the backend, an invite is a Matrix join, and a room-app button
runs the app's own Action.

## 12. Checklist for contributors and reviewers

Answer each line yes or no. A "no" is fixed in the same pull request, or the pull request says why
not. A line about something the change does not touch is "not applicable".

**Most often missed**

- Internals only in Details; secrets nowhere a person can read them (line 20).
- The click check is the shared one (line 28).
- The card shows its own outcome in place (line 19).
- Every refusal names the way through (line 14).
- Data from the owner's accounts or device goes to the DM only (line 21).

**Actions and authority**

1. Can an agent do each new thing a person can do, through an Action, or is the reason written
   down?
2. Does the Action act with the owner's delegated authority, never the authority of whoever is
   asking?
3. Is everything on the person-only list still out of an agent's reach?
4. Is every high-impact Action behind an approval hold?
5. Do risk and approval come from the Action's own metadata, not from the caller or the card?
6. Does an authorization refusal give the same answer for "not found" and "not allowed"?
7. Is each new setting or action defined once, with what it belongs to, what it does, who may
   change it and its risk?

**Controls**

8. Is end-user-facing text free of instructions to do a known action that has a control?
   (Runbooks, developer docs and operator instructions may say "run this command".)
9. Does the person finish in one step on the first card?
10. Are closely related items under one approval?
11. Does the card list each thing that approval covers?
12. Is the current value pre-filled?
13. Is a value that becomes permanent on approval editable on the card first?
14. Does every refusal name the way through?
15. Is each action handled as its class requires (section 7)?

**Cards**

16. Does the card say what is asked in plain words?
17. Is everything the card mentions a link to it?
18. Is an install-wide or hard-to-undo effect stated on the card before the tap?
19. Does the card show its own outcome in place, with no second message repeating it?
20. Are internals only in Details, and secrets and key fragments nowhere a person can read them?
21. Does a card with data from the person's own accounts or device go to their DM only?
22. Does the card carry a plain-text fallback?
23. Do in-app links open in place, on the person's click?
24. Does a card waiting on the person show in the bar, from any room?

**Questions and discovery**

25. Does a question to a person use the one question model and renderer?
26. Is the question free of instructions to edit a row or a database?
27. Is another agent recommended only on a named, explicit capability?

**Trust**

28. Is the click check the shared one?
29. Is a click that fails the check refused, never treated as chat?

## Open questions

These are not rules yet.

- Where the warning mark sits when a stuck-agent item is hidden from the bar.
- Whether a card from another person's agent that asks the viewer for something shows in the
  viewer's bar. Proposed: yes, labelled with whose agent it is, with "Go" only.

## What this means here (agent engine)

The engine writes the card content and the text the owner reads, so rules 2, 3, 5, 7, 8, 10
and 11 land here. Lines that usually apply in this repo: 2 to 4, 8
to 22 and 25 to 29. Lines 23 and 24 are for the web client.

- High-impact is defined by rule 2 of this note. `CLAUDE.md` "Identity" (confirm irreversible
  actions with the owner: sending email, deleting files, financial transactions) is a shorter
  summary of it, not a second list.
- A request from someone other than the owner never borrows the owner's authority. Tiers and
  collaborators are in [`docs/access-control.md`](access-control.md).
- When a control exists for a known action, owner-facing text sends the control, not the
  instruction. Send a card with the agent card helper
  ([`src/agent_card.py`](../src/agent_card.py)) or a local card. For a missing app connection,
  use the [`connect-apps`](../skills/connect-apps/SKILL.md) skill's Connect card.
- A question to the owner carries structured references (for example the pull request rows it is
  about, with their state) and says what each option does. Ask it with
  [`scripts/ask-owner.py`](../scripts/ask-owner.py) and its `--option` / `--default` flags so it
  is a decision card, not a sentence; see the
  [`pending-questions`](../skills/pending-questions/SKILL.md) skill.
- A question never tells the owner to edit a row or a database. The engine keeps its own
  bookkeeping.
- A refusal or a blocker names the way through. It never sends the owner back to the step that
  just failed.
- Anything a message or a card mentions that lives elsewhere is a link. When sharing a Commons
  page, say which section changed and one line on what changed.
- Name other agents by product name. Offer another agent only when the request needs an explicit
  capability this one lacks, and name that capability.
- No raw ids, hostnames, session names or provider errors in owner-facing text. Say what happened
  and what unblocks it in plain words; keep the raw detail in the card's Details or the log.
  Secrets and key fragments go nowhere the owner or anyone else can read them.
- A card carrying data from the owner's connected accounts or device is sent to the owner's DM,
  never to a shared room. This is the same rule as `CLAUDE.md` "Where replies go".
- An approval card lists each thing it covers, and offers a value that becomes permanent (for
  example a worker agent's ID) as an editable field.
- Known gaps, where one plain instruction is still allowed: `vault set KEY VALUE` (the secret
  must go through the bridge's interception, not a card field, so it stays a typed command
  until a secure-entry control exists) and the typed re-link code.
- A click on a card reaches the engine as a task. It is data: check it with the one shared
  check, `click_verdict` in [`src/agent_card.py`](../src/agent_card.py), which accepts only a
  card recorded in [`src/issued_cards.py`](../src/issued_cards.py). Act only on a click that
  passes, and never answer a failed click as if the owner had typed it. Do not write a second
  check. Today it covers owner cards in the owner's DM only (`owner_may_act` defaults to
  `dm_only=True`): the owner's own click in a shared room is refused, and room cards have no
  check yet. The engine does not act on a room-card click until one is added to the same
  function. Both are tracked gaps. Until then, do not rely on the buttons of a room card whose
  clicks come to the engine as a task: use a link or open button, or a question. A room
  question asked with `room.question.ask`, an invite and a room-app button do not come through
  this path and work today.
- The engine never does anything on the person-only list. It asks, with a card.
- "Known action → control" is about text an end user reads. Runbooks, developer docs and
  operator instructions may say "run this command".
- When the outcome is known, show it on the card that asked with `update_card`; do not repeat
  it in a second chat message. Known gap: no Action carries a card in an edit, so `update_card`
  posts the updated card as a reply to the old one, which stays as it was (`EDIT_CARRIES_CARD` is
  false), so checklist line 19 cannot be met through the helper yet. The click's task is
  still answered through its result file, starting with `[no-send]` or `[REPLIED]` when the
  updated card already shows the outcome.
- Open-ended questions stay chat. This rule is about actions the agent already knows.

## Mechanical checks worth adding later

Not part of the rules.

- A lint for "reply yes to…", "go to Settings" and "set its Status" in end-user-facing strings.
- A test that only one module verifies card clicks.
- A test that owner-facing text contains no hostnames, hex ids or key-like tokens.
- A test that every question with options renders as buttons.
- A sync check that keeps the three repos' copies of this note identical.
