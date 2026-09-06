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
 "prompt": "Which design should ship?",
 "fallback": {"mode": "single_select",
              "prompt": "Which design should ship?",
              "options": [{"id": "a", "label": "Design A"},
                          {"id": "b", "label": "Design B"}]}}
```

A fallback is a complete request in its own right and must validate on its
own: `single_select` requires at least one option (the runtime dispatcher
rejects an empty list, `tests/runtime-api-dispatcher.test.py` "single_select
requires options"), so an extension whose fallback is `options: []` is
rejected before it reaches any surface.

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

Call an interaction — one of the five convenience aliases in the table
above, or the runtime method it resolves to — only when ALL hold: a finite, real user choice
exists; the choice changes subsequent execution; it cannot be reliably
inferred from expressed preferences; the options suit structured display.

Do NOT call for: rhetorical questions; decisions the agent can safely make;
questions the user already answered; work that can proceed now with review
later; micro implementation details. A choice card is easy to abuse into
stopping at every step — the policy is part of the contract.

There is no umbrella verb. `request_user_input` is not in the vocabulary;
an agent picks the alias whose response contract it needs, and the
compiler resolves that alias to exactly one wire `mode`.

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

## Relationship to the shipped V1 wires (a boundary, not a competing path)

Two interaction wires already ship. This contract sits between them; it does
not replace either and it adds no second lifecycle.

**Input: the runtime API is where an agent's request enters the pipeline.**
`src/runtime-api/protocol.py` accepts `approval.request` and
`elicitation.request` with `type` in `("free_text", "single_select",
"multi_select", "confirmation")`; `src/runtime-api/dispatcher.py` validates
the request and issues a durable request record. Those are runtime spellings
of the same closed set, and the mapping to the wire `mode` is fixed:

| Runtime API request | Wire `mode` | Status today |
|---|---|---|
| `approval.request` | `approval` | shipped |
| `elicitation.request`, `type: single_select` | `single_select` | shipped |
| `elicitation.request`, `type: multi_select` | `multi_select` | shipped |
| `elicitation.request`, `type: confirmation` | `confirm` | shipped |
| `elicitation.request`, `type: free_text` | `form` (one text field) | rejected by the dispatcher in v0 (`dispatcher.py` "free_text elicitation is not supported"); `form` has no runtime producer yet |
| `human_action.request` | *(none — outside the closed set)* | shipped; `protocol.py:39,84` and `dispatcher.py:204` issue it durably, and `ha_adapter.py` maps it to `external_action` (Done / Decline), NOT to a semantic mode |

The compiler described above consumes the validated runtime request. It is
downstream of the dispatcher, never a parallel entry point.

**`human_action.request` is deliberately outside the compiler's closed scope.**
It asks a human to act in the world and report back, so its reply is a
completion signal (`external_action`: Done / Decline), not a value the agent
consumes as input. It keeps the HITL lifecycle and the stale-response gate
like any other requirement; it simply has no semantic `mode`, and a compiler
must not invent one for it.

**Lifecycle and delivery: `space.ag2.hitl` is the durable envelope, and it stays.**
`src/hitl/schema.py` (`RuntimeEvent -> HumanRequirement -> space.ag2.hitl ->
RequirementCard`) owns interaction state, and the two tokens do NOT
move together. `transition()` bumps `revision` on every legal move but sets
`guard` only when one is supplied; `refresh_guard()` is the separate operation
that rotates the guard (new guard, new revision, same status) when the
underlying runtime interaction changes. Validation compares them
independently: a card projected from an older `revision` is stale, and a reply
whose `guard` differs from the requirement's is refused
(`StaleRequirementError`). Implementing a stale-response gate that rotates
`guard` per transition would rotate the wrong token.
`src/hitl/projector.py` posts each revision as one Matrix event with the
wire under `space.ag2.hitl` and a text `fallback_body`. In this contract the
compiler's A2UI payload is a *projection inside that envelope* — the
presentation of one revision — so the stale-response gate applies to every
semantic mode unchanged, and "pending state survives restarts" is inherited
from the HITL store rather than re-implemented.

**Migration boundary.** Until the compiler exists, the shipped wires *are* the
transport: runtime approvals and elicitations render through
`packages/ag2-sparrow/ag2_sparrow/human_action.py` (a markdown card with the
decision grammar in the text). Runtime requirements (`auth`, `permission`,
`billing`, ...) are *modelled* on the HITL requirement card, but as the status
table below records, that path is not yet driven in production. The compiler replaces how a
revision is *rendered*; it never replaces how a request is *issued* (runtime
API) or how its state is *kept* (HITL). A change to either of those is a
change to this document.

## Current implementation status (this instance, 2026-09-06)

Two columns, because they differ:

| | Exists as code | Deployed and renderable today |
|---|---|---|
| Client A2UI kit (`A2UIButtonsCard` = confirm / single-choice / approval; `choice-group`, `form`) | yes | **no** — the deployed web client does not render `space.ag2.a2ui`: it shows an unclickable "Room App" chip and hides the text fallback (observed live 2026-07-24) |
| `human_action.py` `CardPoster` A2UI block | yes, opt-in (`SPARROW_HA_A2UI`) | **off by default**; `packages/ag2-sparrow/tests/test_human_action.py` pins "plain text card, NO a2ui block" |
| `say.py` raw card seam (`SUTANDO_WORKER_A2UI`) | yes, opt-in | off; `skills/agent-room-ops/SKILL.md` forbids attaching an `a2ui` block until the client renderer ships; `tests/room-ops-say.test.py` pins that an empty option list attaches nothing |
| `space.ag2.hitl` requirement card | yes — schema, manager, projector, supervisor | **no** — nothing drives it in production: `supervise_once()` has no caller outside `tests/hitl-supervisor.test.py`, the manager keeps requirements under `state/hitl/requirements` (`manager.py:51`) while the default composition points the legacy dir at `<state>/human-actions` (`server.py:510`), and that poster only scans `ha_*` names (`human_action.py:118`) — the adapter mints `hitl_` ids (`ha_adapter.py:56`) |
| Markdown text (decision grammar in the body) | yes | **yes** — the path that reaches a human today |

So the only path that reaches a human today is markdown text. The HITL
envelope is the *intended* lifecycle and is fully written, but it is not yet
wired end to end, so this document describes it as the contract's destination,
not as current behaviour. A tap on a rendered button, where a client renders one, sends the
option's action text as an ordinary message — buttons are pre-typed replies.
Next slice: the `interactions` module exposing the V1 verbs with schema
validation behind the runtime API, then capability discovery on the
workers/surface snapshot; the A2UI column flips only when the client renderer
ships, and this table's date moves with it.
