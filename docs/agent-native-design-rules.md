# Agent-native design rules

Every contribution that adds or changes something a person or an agent can do in s.space follows
these rules. The same text lives in the web client, the backend and the agent engine; only the
last section, "What this means here", differs.

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
  The authority is the agent's own, delegated by its owner, or approved per use. High-impact
  Actions stay behind an approval hold.
- **Anything an agent needs from a person arrives as a control, not a sentence.**

## 3. The rule of thumb

Any deterministic action we expect the user to take gets a control (a button, a picker or a
field), in place, in one step.

Deterministic means the agent or the app already knows the action and its target: approve this,
sign in to that, pick one of these, open this page. Open-ended input stays chat.

The test: if you are about to write "reply yes to…", "open this link", "go to Settings → …" or
"run this command", stop and add the control.

## 4. Fewest steps, in place

- One tap on the first card. No second card, no detour to Settings.
- One approval for related items, not one per item.
- The current value is pre-filled.

## 5. No dead ends

The person can always finish what they intend, even a delete.

- Safety may add a step. It never removes the path. The step is: see the consequence, press a
  button named after the action, and the acting side re-checks.
- Every refusal names the way through. It never sends the person back to the thing that just
  failed.
- Every "Go" lands on something the person can act on.
- A dismissed item can be brought back.
- The person's own words always work as a fallback.

## 6. Every setting and action has a clean definition

A setting or action is defined once. The agent, the card and the Settings page all use that one
definition. It states:

- **What it belongs to**: the user, the app, a room or an agent (table below).
- **What it does**, in one plain sentence.
- **Who may change it**. The rule is the same for a person and for an agent acting for them.
- **Its risk**: its action class (section 7), and whether an agent needs an approval hold to do
  it.

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
  review, checks), fetched as the viewer.
- Let the person answer in one step, by action class (table below).
- Show the outcome on the same card: the status changes to Approved, Expired or Done, updated in
  place, with no follow-up message.
- Never show internals (ids, hostnames, session names, raw provider errors, key fragments) outside
  Details.
- Carry a plain-text fallback for clients that do not know the card.

| Action class | How the person does it |
| --- | --- |
| Safe | One tap, anywhere. |
| Reversible | One tap, with Undo where one exists. |
| Irreversible | Confirmed in place. The row or card shows the specific consequence and a button named after the action. Never a single tap on a collapsed line. The acting side re-checks before it acts. |
| Needs context | One tap goes to the card. |

Links:

- A link opens in place, in the app's side panel, like a Commons page. A new tab is the fallback,
  never the default. Nothing opens until the person clicks.
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

- **Chat cards** are the record in the timeline.
- **The bar above the message box** shows every card waiting on the person, from any room, whether
  or not that agent is in the current room. It also carries live reminders (sign in, open the
  terminal, out of budget), which clear when the cause clears.
  - It acts in place when the action is safe. Otherwise one tap goes to the card.
  - More than one item becomes a drawer: "N things need you".
  - Each item can be dismissed. Dismiss hides it and does not answer it. It returns only if the
    card changes.
  - It stays discreet in a shared room.
- **Settings** is the reference.

## 10. Agents and discovery

- Agents are named by product name (Dot, Muse, Sutando), never by invented task categories.
- An agent recommends another agent only on an explicit capability: where it runs and what it can
  reach, always on, voice, connected apps, or already in the room. It names the capability. When
  nothing explicit differs, it does not recommend.
- Discovery is a maintained deck of five feature cards. The deck is shown once after an update. An
  agent can pull a card when asked or when it would help, at most once per situation.
- Each card's main action takes the person into the feature in place, landing in the DM with their
  own agent.

## 11. Trust rules for controls

- A control acts only on a card from the viewer's own agent.
- A click is data, not authority. The acting side verifies that the click replies to a card it
  issued, carries a payload that card offered, comes from the owner, and is in the same room.
- A click that fails the check is refused. It is never treated as chat.
- There is one shared implementation of this check, not one per feature.

## 12. Checklist for contributors and reviewers

Answer each line yes or no. A "no" is fixed in the same pull request, or the pull request says why
not.

1. If this adds something a person can do, can an agent do it through an Action under the same
   authority? If not, is the reason written down?
2. Is every high-impact Action behind an approval hold?
3. Is user-facing text free of instructions to do a known action ("reply yes", "open this link",
   "go to Settings", "run this command")? Each one is a control.
4. Does the person finish in one step on the first card, with related items under one approval and
   the current value pre-filled?
5. Can the person always finish what they intend: every refusal names the way through, every "Go"
   lands on something actionable, and a dismissed item can be brought back?
6. Is each new setting or action defined in the one place: what it belongs to (user, app, room or
   agent), what it does, who may change it, and its risk?
7. Does the card say what is asked in plain words, link everything it mentions, and show its
   outcome in place with no follow-up message?
8. Does each action follow its class: one tap for safe and reversible, confirmed in place with the
   consequence and a named button for irreversible, and re-checked by the acting side?
9. Is user-facing text free of internals and secrets (ids, hostnames, session names, raw provider
   errors, key fragments) outside Details?
10. Do links open in place on the person's click, and does a Commons page link show the page on
    the part that changed, with one line on what changed?
11. Does a question to a person use the one model and renderer, with choices as buttons, and no
    instruction to edit a row or a database?
12. Does a card waiting on the person show in the bar from any room, with a dismiss that hides it
    without answering it?
13. Are agents named by product name, and recommended only on a named, explicit capability?
14. Is the click check the shared one, and does the card carry a plain-text fallback?

## What this means here (agent engine)

The engine writes the card content and the text the owner reads, so rules 3, 5, 7, 8, 10 and 11
land here.

- Owner-facing text never tells the owner to do a known action in prose. Send a card with the
  agent card helper (sutando#6385) or a local card. For a missing app connection, use the
  [`connect-apps`](../skills/connect-apps/SKILL.md) skill's Connect card.
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
- A click on a card reaches the engine as a task. It is data: check it with the one shared check
  before acting, act only on a click that passes, and never answer a failed click as if the owner
  had typed it.
- When the outcome is known, update the card that asked. Do not post a follow-up message.
- Open-ended questions stay chat. This rule is about actions the agent already knows.
