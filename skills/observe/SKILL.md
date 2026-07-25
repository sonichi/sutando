---
name: observe
description: "/observe — natural-language entry to the Policy Interaction Layer: compile an NL request into a structured observation/subscription policy, confirm via effect-card (or standing approval), then subscribe on the events plane."
---

# /observe — NL → observation policy (MVP step 1)

The owner (or a scoped user) says what they want watched, in natural language:

    /observe watch doc changes in this room and ping me daily

The core agent COMPILES that into a structured draft; everything deterministic
around the compilation lives in `observe_policy.py`. Design + resolved
decisions: `workspace notes/observe-mvp-slice-design.md`.

## Processing convention (core agent, on an `/observe <text>` owner task)

1. **Compile (LLM-side — you):** produce a draft dict:
   `{room_id, event_types[], mode, cost_cap?, created_by, source_text}`
   - `room_id`: the task's `channel_id` unless the text names another room.
   - `event_types`: map the intent to plane types (`message.created`,
     `reaction.added`, `artifact.updated`, `member.joined`, …). Unknown intent →
     ask, don't guess.
   - `mode`: `observe` (context only) | `record` (journal) | `notify` (ping the
     owner) | `taskify` (promote batches to ambient tasks — never
     standing-approved).
2. **Validate:** `observe_policy.validate_draft(draft)` — errors go back to the
   user verbatim; do not "fix" a draft silently.
3. **Standing approval:** `evaluate_standing_approval(rec, owner_mxid=…,
   owner_rooms=…)`. `owner_rooms` = rooms the owner OWNS — created by the owner or where the
   owner holds PL≥50 (checked via the room state the core already has; when
   ownership is unknown, the room is NOT in scope). Familiarity — "the owner
   sent a task from here" — is NOT ownership: a shared room must never enter
   standing-approval scope (001 review; the server's four-way authz still
   gates the subscribe, but the standing-approval semantic is stricter by
   design). True → save + `transition(id,
   "active")` + subscribe + post the AUTO-ACTIVATED card (visibility is
   mandatory — never activate silently). False → save draft + post the CONFIRM
   card and wait for the decision.
4. **Decision grammar** (same infrastructure as human-action cards):
   `policy <id> activate | edit <new text> | cancel` — typed reply, reaction on
   the card, or (when custom-event fan-out lands) an A2UI button. `edit`
   recompiles with the new text into the SAME policy id.
5. **Subscribe on activate:** events client `subscribe(room_id, event_types)` —
   the events plane's four-way authz is the permission evaluator; a subscribe
   rejection (e.g. PL too low) goes back to the user as the card's failure
   line, not swallowed.
6. **Enforcement:** active `notify`/`record`/`taskify` policies are consumed by
   the sparrow drain handlers (taskify today; notify/record handlers are the
   next slice). `cost_cap` shows the CAP on cards; metering is a later slice.

## Files

- `observe_policy.py` — validation, standing-approval evaluator,
  SubscriptionStore (`<workspace>/state/observe/`), effect-card renderer
  (plain text always; A2UI choice-group in the real renderer contract behind
  `include_a2ui`).
- Store records: `obs_*.json`, states `draft → active → cancelled/expired`,
  terminal states immutable, every transition audited.

Test: `python3 tests/observe-policy.test.py` (99% module coverage).
