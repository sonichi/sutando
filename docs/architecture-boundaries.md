# Sutando architecture boundaries

**Status:** Proposed boundary for incremental adoption. This document classifies
the current repository; it does not imply that every existing dependency already
follows the target rules.

Sutando's open-source core is intended to be the common execution foundation for
the community product and future distributions, including Sutando Enterprise.
Product surfaces and optional capabilities may differ, but they should consume
the same versioned core rather than fork it.

## Definitions

### Core

Core is the smallest surface-independent runtime that can accept an authorized
task, execute registered capabilities, and route a result while maintaining its
workspace and lifecycle.

Core owns:

- task and result envelopes, validation, queueing, and routing;
- identity, access-tier, capability, and policy decisions;
- workspace/config resolution and shared state-path contracts;
- agent lifecycle, task-watcher, health, and liveness primitives;
- public tool/plugin registration interfaces;
- provider-neutral conversation/session contracts;
- provider-neutral audit and observability event contracts.

Core must not depend on:

- a messaging or telephony provider;
- a model vendor;
- a UI application;
- a particular skill or business workflow;
- enterprise-only policy, identity, or deployment code.

Removing every adapter, app, and skill should still leave an independently
buildable and testable core.

### Adapter

An adapter translates an external system into or out of a core contract. Examples
include Discord, Slack, Telegram, Twilio, remote gateways, model providers, and
OS-specific integrations.

Adapters may depend on the public core API. Core must not import an adapter.

### App

An app is a user-facing process or UI assembled from core plus adapters. The
desktop app, web client, browser voice surface, and phone service are apps or app
components. Apps choose configuration and presentation; they do not define core
policy.

### Skill

A skill is an optional capability or workflow under `skills/<name>/`. Installing
or removing a skill must not change whether core can boot. Skills may use the
public SDK and declared capability interfaces. They should not import arbitrary
core internals or another skill's implementation.

### Tooling

Build, migration, release, test, and repository-maintenance scripts are tooling.
Tooling may inspect multiple layers but is not part of the runtime dependency
graph.

### Workspace

The resolved `workspace/` is mutable user state, not product code. Its layout and
confidentiality rules are defined by the
[Workspace Contract](workspace-contract.md).

## Dependency direction

The intended runtime dependency flow is:

```text
schemas / public contracts
            ↓
           core
            ↓
    adapters and skills
            ↓
           apps
```

Allowed:

- core → schemas/public contracts;
- adapters → public core API;
- skills → public SDK and declared capability interfaces;
- apps → core, adapters, and skills;
- tooling/tests → any layer needed to build or verify it.

Disallowed:

- core → adapters, apps, or skills;
- adapter → another adapter's private implementation;
- skill → another skill's private implementation;
- skill/adapter → private core modules when a public contract exists;
- enterprise code → unpublished core internals.

Shared behavior needed by multiple adapters belongs in core only when it is
provider-neutral. Otherwise it belongs in an adapter library.

### Optional adapter capabilities

Optional capability discovery also remains at the adapter edge. A generic
core helper may run an injected script path and standardize timeout/failure
semantics, but it must not name, locate, or import a concrete skill. This keeps
the dependency direction adapter → helper while preserving the rule that core
does not depend on installed skills. Add direct contract tests for the runner
and wiring tests for every adapter that delegates to it.

### Shared adapter policy

Provider-neutral workspace-state policy must have one dependency-light core
implementation. Adapters pass the resolved workspace into that implementation
and retain only provider-specific receive, send, threading, and formatting
mechanics. For example, `src/presenter_mode.py` owns the presenter sentinel path
and expiry semantics; Discord, Slack, Telegram, and notification jobs decide
what delivery to suppress when that policy reports active.

When several adapters publish the same mutable workspace record, the shared
module also owns the record's schema, field bounds, atomic publication, and
fail-open/fail-closed contract. Adapters inject the resolved path and their log
sink; they do not reproduce the write recipe. Tests exercise the production
writer directly under the relevant concurrency model, then separately pin each
adapter's delegation. `src/owner_activity.py` is the reference pattern.

Centralize only writers with the same policy. AG2 Sparrow's
`remote_gateway_bridge._write_owner_activity` intentionally remains separate:
before publishing the shared record it applies the sender-tier gate, excludes
known fleet agents, strips gateway attribution, and redacts secrets. Those are
gateway trust-boundary rules, not provider-neutral record-publication mechanics.
Do not delegate that writer to `src/owner_activity.py` unless those controls and
their tests move with it. Any writer excepted from centralization must have its
exception documented, as this one is.

### Shared result-file lifecycle

The task/result filesystem protocol is core infrastructure, including its
claim, crash-recovery, collision, and retry rules. A dependency-light core
helper owns each shared state transition; adapters supply their resolved paths
and retain only provider-specific delivery. For example,
`src/proactive_recovery.py` restores proactive delivery claims stranded by a
crash, while Discord, Slack, and Telegram decide how the recovered result is
sent. Copying the filesystem state machine into each adapter is not permitted.

### Outbound delivery ownership

Outbound delivery of an already-published result has one implementation, and it
is the outbox. `src/outbox.py` owns delivery claims (acquire/release/reclaim,
per-item locking, crash recovery) and `src/outbox_adapter.py` owns the
three-state delivery outcome (CONFIRMED / NOT_DELIVERED / OUTCOME_UNKNOWN);
both are vendored verbatim into `packages/ag2-sparrow/`. Scope is the outbound
leg only — an existing `OutboundItem` from claim through terminal disposition;
other task/result lifecycle transitions keep their documented owners (e.g.
`src/proactive_recovery.py` above). Consumers delivering outbound results bind
these; do not re-implement claim, delivered-sentinel, or retry machinery in a
bridge. Adapters bind their resolved directories and retain provider-specific
delivery only. Pin both the shared contract and every adapter's delegation in
tests. Pre-outbox private copies (e.g. discord-bridge's
archive/delivered-sentinel/pending-replies machinery) are migration debt, not
precedent.

### HTTP transport handlers

HTTP handlers are transport adapters, not the owner of feature policy. Repeated
authentication gates, status/header emission, and JSON serialization belong in
small handler helpers so every route uses one wire contract. Route branches retain
only dispatch; named endpoint methods own orchestration and payload construction.
Refactors must preserve delegation, status codes, headers, and payload shapes with
direct contract tests.

### HTTP route boundaries

HTTP route methods should remain dispatch layers: parse and authorize the request,
call a named operation, then emit its result. Filesystem reconciliation and
response assembly belong in module-level operations that can be tested without a
socket. Protect both the operation contract and one route-wiring path.

The same rule governs result-body markers, with an explicit one-way dependency
direction:

    result_markers.parse_markers()   # protocol interpretation
              |
              v
    send_allowlist.is_path_sendable()  # delivery authorization
              |
              v
    provider-specific upload mechanism

`src/result_markers.py` owns marker syntax, precedence, stripping, and action
extraction. `src/send_allowlist.py` owns attachment-path authorization. Delivery
consumers (Discord, Slack, Telegram, gateway, and the `dm-result.py` REST
fallback) own transport routing and upload calls only — they must not define
marker regexes or path-policy copies.

**All four Python consumers now conform, and the guard enforces it.**
`discord-bridge.py`, `dm-result.py`, `telegram-bridge.py`, and `slack-bridge.py`
obtain marker grammar solely from `parse_markers()`.
`tests/bridge-marker-no-leak.test.py` fails if any of them declares the grammar
itself, matching the grammar in any regex literal so a renamed private parser
cannot slip past. Telegram's `send_reply()` used to compile its own
`file|send|attach` regex and Slack declared the same regex dead at module scope;
both are gone. The rule above forbids *new* private parsers — add any new
consumer to that guard when it starts handling markers.

Parsing never authorizes and authorization never parses: `parse_markers()`
extracts any marker value and leaves the decision about whether a path may be
opened to the allowlist. A consumer that filters values during parsing
re-couples the two and drifts — which is precisely how `dm-result.py` came to
deliver literal `[file: ...]` text that every other consumer stripped.

## Current repository classification

This is the ownership intent for today's paths. Several rows contain known
boundary debt; the physical moves and dependency cleanup will happen separately.
For a generated file-by-file inventory of `src/`, see the
[source module map](src-map.md).

| Current path | Classification | Notes |
|---|---|---|
| `schemas/` | Contracts | Canonical wire/config schemas; should remain dependency-light. |
| `src/agent/`, task watcher/bridge, local task protocol, result routing | Core | Execution lifecycle and provider-neutral task/result plumbing. |
| Workspace/config/path, access-tier, health, heartbeat primitives in `src/` | Core | Shared infrastructure used across surfaces. |
| `src/discord-bridge.py`, `src/slack-bridge.py`, `src/telegram-bridge.py`, remote-gateway bridges | Adapters | Currently stored under `src/`; target ownership is adapter, not core. |
| `src/voice-agent.ts` | App/runtime composition | Wires a voice provider and browser session to core contracts. |
| `src/web-client.ts`, `src/Sutando/` | Apps | Web and macOS presentation/application code. |
| `src/inline-tools.ts` | Mixed boundary debt | Registration framework is core-facing; concrete feature/provider tools should live in skills/adapters. |
| `src/recording-tools.ts` and other feature-specific tool implementations | Skills/adapters | Existing core-to-feature coupling to remove through registration interfaces. |
| `skills/phone-conversation/` | Phone app/adapter | Deliberate historical exception: call transport/lifecycle is physically skill-shaped but is not generic core. |
| Other `skills/<name>/` | Skills | Optional capabilities and workflows. Coupled skills are migration candidates for the public SDK. |
| `packages/ag2-sparrow/` | Independent package/adapter | Separately packaged gateway support, not core. |
| `scripts/`, `hooks/`, `.github/` | Tooling/governance | Build, migration, repository policy, and CI. |
| `tests/` | Verification | Tests should be tagged or grouped by the layer they protect. |
| resolved `workspace/` | User state | Never product source and never part of a distributable core artifact. |

## Known boundary debt

The current tree has two forms of coupling that must be removed before a physical
core extraction:

1. Some `src/` modules locate or import concrete skill implementations.
2. Some skills import internal modules directly from `src/`.

Both should move toward dependency inversion:

- core exposes a narrow contract or registration hook;
- adapters/skills implement and register it;
- apps select which implementations to assemble;
- compatibility shims preserve current prompts, tool names, and behavior during
  migration.

Moving files without first removing these dependencies would make the boundary
look cleaner without making it real.

## Schema migration vs live writer

A module that owns live write APIs must not also own destructive one-time schema
transformations. Migration code is high-consequence, runs once at startup, and is
almost impossible to test through the live surface — embedded in a writer it ends
up exercised only by driving a real session.

Worked example, `src/conversation-store.ts` → `src/conversation-store-migrations.ts`:

> `conversation-store.ts` owns current schema initialization and live write APIs.
> Destructive or legacy SQLite transformations belong in
> `conversation-store-migrations.ts`, are idempotent, transaction-tested and
> invoked before views/statements are prepared. Do not place migration SQL in a
> live record function.

The ordering is part of the contract: current-table DDL → migrations → view
rebuild → prepared statements. The migration module never creates current-schema
DDL (that stays the caller's job) and never propagates failure — the store must
still initialize after a handled, rolled-back migration.

Enforced by `tests/conversation-store-migration-delegation.test.ts`, which checks
the delegation and the ordering, and scans the store for the legacy table names
and transaction verbs while deliberately still permitting current-schema
`CREATE TABLE`.

## Transport vs request domain

A transport (socket server, HTTP handler, message consumer) owns framing, connection
lifecycle and daemon composition. It must not own authorization, policy or durable
state transitions — when it does, the security rules can only be exercised by
driving the transport, and any second transport is free to reimplement them
differently.

Worked example, `src/runtime-api/server.py` → `src/runtime-api/dispatcher.py`:

> `server.py` owns Unix-socket transport and daemon composition. JSON-RPC method
> dispatch, approval/elicitation validation, governed-capability authorization,
> idempotency and durable request transitions belong in `dispatcher.py`. Actor
> identity is resolved daemon-side and passed in explicitly; a client parameter
> must never override it.

The dependencies are ordinary constructor arguments (store, human-action adapter,
actor, executor map) — no injection framework. Note the executor map must be *read*
from the instance, not from the module global, or the argument is decorative and a
caller injecting fakes silently gets the real executors.

## Skill-internal boundaries

The core/adapter/skill split is not the only boundary that matters. A large skill
can carry the same layering problem internally: analysis policy welded to data
loading, CLI parsing and presentation, so the rules can only be exercised by
running the whole tool.

Worked example, `skills/call-diagnostics/scripts/diagnose.py` →
`scripts/analysis.py`:

> Complex skill diagnostics must separate pure analysis policy from data
> loading, CLI and presentation. Call-diagnostics detection, categorization and
> repair policy lives in its analysis module; loaders and renderers consume it
> and must not carry copied detection rules.

The analysis module is import-safe by contract — it resolves no workspace, reads
no `sys.argv`, opens no database or file, prints nothing and generates no HTML.
That contract is asserted directly, by scanning the module source (docstring
excluded, since it legitimately names what it avoids) and by importing it under a
polluted `sys.argv` and asserting silence.

This boundary is skill-internal: the policy stays inside `skills/call-diagnostics/`
and is not promoted into `src/`. The delegation check is narrow — it forbids the
renderer redefining moved symbols while leaving presentation labels and styles
alone.

## Presentation adapters vs domain/storage

A presentation module (an HTTP server, a renderer, a CLI front end) adapts and
displays. It must not also own domain parsing, validation or storage
transactions — when it does, the policy is unreachable from any other consumer
and untestable except through the presentation surface.

Worked example, `src/dashboard.py` → `src/dashboard_schedules.py`:

> Dashboard HTTP handlers and rendering code must delegate schedule parsing,
> validation and atomic `crons.json` mutation to `src/dashboard_schedules.py`.
> Schedule mutations must remain locked read-modify-write operations; do not
> rebuild cron validation or persistence inside a route.

The split point that matters: **the adapter resolves the path, the domain module
receives it.** `dashboard.py` keeps `_crons_path()` (workspace + host-label
resolution is deployment knowledge); `dashboard_schedules.py` takes a `Path` and
owns the locked read→merge→write. That keeps the domain module free of workspace
resolution while leaving the adapter with no persistence logic of its own.

Enforced by `tests/dashboard-schedule-delegation.test.py`, which asserts the
delegation is real and scans `dashboard.py` for the atomic-write primitives
(`os.replace`, `.tmp` construction, a local `threading.Lock()`) that would mean
a route had rebuilt its own transaction.

## Decision guide for new code

This section explains the architectural categories. The repository-specific
placement checklist in `AGENTS.md` / `CLAUDE.md` remains authoritative for
current file locations; update both documents together if the placement rules
change.

Walk this list top to bottom:

1. Is it a provider-neutral task, policy, workspace, lifecycle, or plugin
   contract required with all optional components removed? **Core.**
2. Does it translate a third-party API, model provider, OS API, or transport?
   **Adapter.**
3. Is it a user-facing process that assembles core and adapters? **App.**
4. Is it an optional capability or business workflow? **Skill.**
5. Is it used only to build, migrate, test, release, or maintain the repository?
   **Tooling.**
6. Is it mutable per-user data? **Workspace, not code.**

If a module appears to fit two layers, split the provider-neutral contract from
the provider/feature implementation. Prefer the more specific outer layer for
the implementation.

## Core change governance

Until the core is independently packaged, a "core change" means a change to the
paths classified as core or contracts above, or any change that alters their
public behavior.

Core changes should eventually require:

- dedicated core code owners;
- two current-head approvals from core maintainers;
- stale approval dismissal after new commits;
- approval from someone other than the last pusher;
- no direct pushes or routine admin bypass to the protected branch;
- full unit, contract, integration, compatibility, and security checks;
- an ADR and threat-model update for public API or security-boundary changes.

Repository rules and CI enforcement are follow-up work. This document defines
the boundary they will protect.

## Enterprise relationship

Sutando Enterprise should consume released, immutable OSS core artifacts and run
the same core contract suite against the exact artifact digest it ships.
Enterprise-only SSO/SCIM, tenant administration, audit exporters, deployment
controls, proprietary connectors, and policy packs stay outside the OSS core.

The enterprise product may extend public interfaces; it must not patch private
core internals or maintain a long-lived core fork.

## Migration principles

- Classify and test before moving files.
- One concern per PR; no big-bang directory rewrite.
- Preserve prompts, tool names, and behavior exactly while changing boundaries.
- Add dependency-direction checks with a temporary allowlist for known debt.
- Reduce the allowlist monotonically.
- Extract independently buildable core packages only after forbidden imports are
  gone.
- Consider a separate public core repository only after the package boundary is
  proven inside this repository.

## Events plane: resident transport vs on-demand capability (2026-08-05)

Two components consume gateway room events. Ownership, ratified by the owner
in the client-stack review (Feature Haul, 2026-08-05):

**ag2-sparrow owns (resident plane):**
- the resident SSE connection and its reconnect loop
- durable cursor ownership
- the SQLite inbox (`event_inbox.EventInbox`)
- dedupe / exactly-once consumption
- event → ambient task promotion (taskification)

**agent-room-ops owns (on-demand plane):**
- subscription configuration (`subscribe`/`unsubscribe`)
- subscription inspection (`subscriptions`)
- bounded ad-hoc `pull`
- bounded, foreground, non-durable diagnostic streaming (debug-only; must be
  bounded via `--max-events`/`--duration`-class limits, never a second
  resident event client)
- the event-acceptance policy the Agent applies

**Grandfathered debt register** (discovered/confirmed by running the guard
against main; frozen — may only shrink, never grow):
- `skills/agent-room-ops/events.py:stream_with_resume` + its durable
  `save_cursor` file: resident-lifecycle behavior (forever-reconnect with
  durable cursor ownership) in the on-demand skill. Already over the
  boundary; migrates to sparrow's event pump, after which the bounded
  debug-only stream is what remains. (`room_ops.py`'s `events stream
  --cursor-file` is this debt's CLI wiring; its `--once`/`--max-events`
  bounded modes are the shape the surviving debug stream keeps.)
- `skills/agent-room-ops/events_acceptance.py` imports
  `ag2_sparrow.event_consumer` and performs taskification from the skill
  side. Promotion of the taskify client to the sparrow plane is required
  work; the import is frozen as-is until then.

Sparrow-side debt (same register, same rules):
- `human_action.py` posts question cards through the `/v1/room` envelope;
  `remote_gateway_bridge.py` uploads media through the `/v1/rooms/{room}/media`
  facade. Both are frozen; no NEW sparrow file may touch the room-verb
  endpoint surface.

Do not add to the skill: SQLite inboxes, forever reconnect loops, durable
cursor ownership, background daemon lifecycles, or event taskification. Do
not add on-demand room verbs to sparrow — enforced at the ENDPOINT surface
(`/v1/room` envelope + `/v1/rooms/` facade, frozen to the two files above).
Ad-hoc event pull shares sparrow's legitimate `/v1/events` consumption
endpoint and is governed by review, not grep. All of this is pinned by
`tests/events-plane-boundary.test.py` — allowlists frozen, shrink-only, and
the collector excludes only real test artifacts (`tests/` dirs, `test_*.py`,
`*.test.py`), never production filenames that merely contain "test".
