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

## 5. Every setting and action has a clean definition

A setting or action is defined once. The agent, the card and the Settings page all use that one
definition. It states:

- **What it belongs to**: the user (intrinsic to the person), the app (the desktop or web
  client), a room, or an agent.
- **What it does**, in one plain sentence.
- **Who may change it**. The rule is the same for a person and for an agent acting for them.
- **Its risk**: it just happens, it asks first, or it stays in Settings.

A new setting without these four is not ready to merge.

## 6. What a card (or the reminder bar) must do

- Say what is being asked, in plain words.
- Put the evidence one tap away, for example linked pull request rows with their state.
- Let the person answer in one step.
- Show the outcome on the same card: the status changes to Approved, Expired or Done, updated in
  place, with no follow-up message.
- Never show internals (ids, session names, raw provider errors, key fragments) outside Details.
- Confirm a destructive action in place; the acting side re-checks before it acts.
- Carry a plain-text fallback for clients that do not know the card.

## 7. Where things appear

- **Chat cards** are the record in the timeline.
- **The bar above the message box** is the live reminder that the person needs to do something
  (sign in, open the terminal, out of budget). It clears when the cause clears.
- **Settings** is the reference.

## 8. Trust rules for controls

- A control acts only on a card from the viewer's own agent.
- A click is data, not authority. The acting side verifies that the click replies to a card it
  issued, carries a payload that card offered, comes from the owner, and is in the same room.
- A click that fails the check is refused. It is never treated as chat.
- There is one shared implementation of this check, not one per feature.

## 9. Checklist for contributors and reviewers

Answer each line yes or no. A "no" is fixed in the same pull request, or the pull request says why
not.

1. If this adds something a person can do, can an agent do it through an Action under the same
   authority? If not, is the reason written down?
2. Is every high-impact Action behind an approval hold?
3. Does any user-facing text tell the person to do a known action ("reply yes", "open this link",
   "go to Settings", "run this command")? If so, it becomes a control.
4. Does the person finish in one step on the first card, with related items under one approval
   and the current value pre-filled?
5. Is each new setting or action defined in the one place: what it belongs to, what it does, who
   may change it, and its risk?
6. Does the card say what is asked in plain words, with the evidence one tap away?
7. Does the card show its outcome in place, with no follow-up message?
8. Is user-facing text free of internals and secrets (ids, session names, raw provider errors,
   key fragments) outside Details?
9. Do destructive actions confirm in place, and does the acting side re-check?
10. Is the click check the shared one, and does the card carry a plain-text fallback?

## What this means here (agent engine)

The engine writes the card content and the text the owner reads, so rules 3, 6 and 8 land here.

- Owner-facing text never tells the owner to do a known action in prose. Send a card with the
  agent card helper (sutando#6385) or a local card. For a missing app connection, use the
  [`connect-apps`](../skills/connect-apps/SKILL.md) skill's Connect card.
- A question to the owner carries structured references (for example the pull request rows it is
  about, with their state) and says what each option does. Ask it with
  [`scripts/ask-owner.py`](../scripts/ask-owner.py) and its `--option` / `--default` flags so it
  is a decision card, not a sentence; see the
  [`pending-questions`](../skills/pending-questions/SKILL.md) skill.
- No raw ids, session names or provider errors in owner-facing text. Say what happened and what
  unblocks it in plain words; keep the raw detail in the card's Details or the log.
- A click on a card reaches the engine as a task. It is data: check it with the one shared check
  before acting, act only on a click that passes, and never answer a failed click as if the owner
  had typed it.
- When the outcome is known, update the card that asked. Do not post a follow-up message.
- Open-ended questions stay chat. This rule is about actions the agent already knows.
