# Interaction semantics — the AG2 contract (V1)

Owner-settled 2026-08-31 (Client-UI room). Agents do not invent interaction
UI: they call a closed, schema'd vocabulary of semantic requests. The client
chooses components; the runtime owns lifecycle. Same logic as tools — an agent
picks from allowed capabilities, it cannot create one the system doesn't understand.

## Responsibility boundaries

| Party | Decides |
|---|---|
| AG2 protocol | which interaction semantics exist, and their schemas |
| Agent | which semantic mode to use now; the question and options |
| Client | which concrete component, layout, interaction details |
| Broker/runtime | interaction state, permissions, delivery, recovery, idempotency |
| User | the final choice or input |

## V1: four semantic tools (closed set)

- `ask.choice` — single or multi select, distinguished by parameters
  (`{"selection": "single", "min": 1, "max": 1}`), so the underlying schema
  is not duplicated across single/multi variants.
- `ask.input` — one or a few structured fields.
- `ask.confirm` — confirm a decision with NO external side effects.
- `request.approval` — authorize an action WITH side effects.

Agent-facing convenience names may stay finer-grained:
`request_single_choice`, `request_multiple_choices`, `request_input`,
`request_confirmation`, `request_approval`.

### One vocabulary, three spellings — the mapping is normative

The **wire `mode`** is the only spelling the semantic validator accepts and
the only key a surface advertises under `interaction_capabilities.modes`.
The closed-set name is what an agent *means*; the convenience name is what an
agent *calls*. Direction is fixed: agent-facing name → wire `mode` (the
compiler resolves it; nothing resolves the other way).

| Closed-set semantic (agent-facing) | Wire `mode` (validator + capability key) | Convenience alias |
|---|---|---|
| `ask.choice`, `selection: "single"` | `single_select` | `request_single_choice` |
| `ask.choice`, `selection: "multi"` | `multi_select` | `request_multiple_choices` |
| `ask.input` | `form` | `request_input` |
| `ask.confirm` | `confirm` | `request_confirmation` |
| `request.approval` | `approval` | `request_approval` |

Two clarifications this table settles:

- `form` is not a fifth semantic. It is `ask.input`'s wire name; the
  capability entry's `field_types` are the surface's limits for that one
  semantic.
- `ask.choice` keeps ONE request schema (the closed-set point stands), but
  compiles to two wire modes because a surface's limits differ per selection
  kind (`max_options` 7 vs 12 above). The duplication is in the capability
  advertisement, not in the schema an agent fills in.

Every JSON example in this document uses wire `mode` names, and any example
that does not is a defect in the example.

The closed set guarantees: every client knows how to display each mode,
responses validate, accessibility is consistent, agents cannot forge system
UI, interactions deliver cross-platform, pending state survives restarts,
and analytics understand what the user was asked.

## Extension namespace

New semantics ship under a namespaced mode WITH a mandatory fallback an old
client can render:

```json
{"mode": "space.ag2.design_compare",
 "fallback": {"mode": "single_select", "options": []}}
```

## Semantic vs presentation (two layers, never merged)

The semantic decides the RESPONSE CONTRACT. Presentation only affects
display and must never fork the semantics:

```json
{"mode": "single_select", "options": [...], "presentation_hint": "visual_cards"}
```

`visual_cards` is not a peer of `single_select`. A surface that cannot render
the hint degrades to buttons / select menu / numbered list without changing
the contract.

Surface capabilities are the same split: the protocol defines semantics
(`single_select`, `approval`, ...); a surface declares presentation abilities
(`images`, `tables`, `live_preview`, `drag_to_rank`, `secret_input`).

## Capability discovery (not prompt hardcoding)

A surface declares what it supports; the agent/runtime adapts and degrades:

```json
{"interaction_capabilities": {"version": "1", "modes": {
  "confirm": {},
  "approval": {},
  "single_select": {"max_options": 7, "supports_description": true,
                    "supports_images": true, "supports_other": true},
  "multi_select": {"max_options": 12, "supports_min_max": true},
  "form": {"field_types": ["text", "textarea", "number", "date"]}}}}
```

Agents should target the minimal common semantic set, not over-fit the
current client.

## When the agent asks (usage policy)

Call `request_user_input` only when ALL hold: a finite, real user choice
exists; the choice changes subsequent execution; it cannot be reliably
inferred from expressed preferences; the options suit structured display.

Do NOT call for: rhetorical questions; decisions the agent can safely make;
questions the user already answered; work that can proceed now with review
later; micro implementation details. A choice card is easy to abuse into
stopping at every step — the policy is part of the contract.

## Rendering pipeline (owner-settled): compile, don't compose

Agents never generate A2UI component trees. The only path from an agent to
pixels is:

```
Agent — picks a supported interaction semantic
  -> AG2 Interaction Schema (validated request)
  -> deterministic compiler (one shared implementation)
  -> A2UI surface payload
  -> A2UI React renderer
  -> AG2 Space native components
```

We reuse the A2UI catalog/protocol/renderer (roughly 60-70% of the existing
UI stack) but own the semantics above it and the durable interaction
lifecycle in the AG2 runtime. The compiler is the enforcement point: "agents
cannot forge system UI" is structural, not a convention — the semantic
validator + compiler are the only producers of A2UI payloads.

Consequence for today's seams: the raw card pass-through
(`SUTANDO_WORKER_A2UI` on say) is a transition seam only; once the
`interactions` verbs exist it becomes the compiler's internal output and the
agent-facing surface is the verbs alone.

## Current implementation status (this instance)

Rendering today: the client's A2UI kit (`A2UIButtonsCard` = confirm /
single-choice / approval; `choice-group` and `form` components exist but
lack settled submit semantics). Sending today: `skills/agent-room-ops/say.py`
attaches a validated buttons card (`SUTANDO_WORKER_A2UI`). A tap sends the
option's action text as an ordinary message — buttons are pre-typed replies.
Next slice: the `interactions` module exposing the V1 verbs with schema
validation, then capability discovery on the workers/surface snapshot.
