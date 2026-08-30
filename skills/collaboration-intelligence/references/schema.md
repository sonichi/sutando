# Collaboration Intelligence record schema

Use this conceptual schema with any available durable store. Preserve stable IDs and timestamps. JSON/YAML examples are illustrative; adapt to the provider without changing semantics.

## Contents

- [Entity](#entity)
- [Room](#room)
- [Relationship](#relationship)
- [Evidenced fact](#evidenced-fact)
- [Candidate identity link](#candidate-identity-link)
- [Reviewer identity map (`reviewer-identity/2`)](#reviewer-identity-map-reviewer-identity2)
- [Alert](#alert)
- [Merge rules](#merge-rules)
- [Suggested freshness windows](#suggested-freshness-windows)

## Entity

```yaml
entity_id: stable-local-id
kind: human | agent | service | organization | unknown
canonical_name: string | null
aliases: [string]
identities:
  - provider: matrix | slack | discord | teams | email | github | other
    user_id: stable-provider-native-user-id
    display_name: string | null
    bridge_origin: string | null
    verified: false
    activity:                       # WHERE this identity is live, not merely that it exists
      last_seen_at: timestamp | null
      rooms: [room-id]              # where it was actually observed
      relative: primary | secondary | dormant | unknown
      exclusive: true | false | unknown   # true = this person is reachable ONLY here
owner_links:
  - owner_entity_id: stable-local-id
    relation: owner | operator | manager | sponsor | unknown
    status: explicit | inferred | historical | disputed
    fact: {source_ref: string, observed_at: timestamp, confidence: 0.0-1.0}
affiliations:
  - organization_entity_id: stable-local-organization-id | null
    collaboration_role: internal | customer | external_collaborator | partner | vendor | unknown
    scope_ref: organization-project-feature-contract-or-room-id | null
    status: active | historical | disputed
    fact: {source_ref: string, observed_at: timestamp, confidence: 0.0-1.0}
attention_profile:
  tier: standard | priority
  label: VIP | string | null
  designated_by_entity_id: stable-local-id | null
  reason: string | null
  scope_ref: organization-account-project-room-or-global-id | null
  valid_from: timestamp | null
  valid_until: timestamp | null
  handling_preferences: [string]
  source_ref: authoritative-reference | null
roles: [evidenced_fact]
expertise: [evidenced_fact]
responsibilities: [evidenced_fact]
behavior_patterns: [evidenced_fact]
first_seen_at: timestamp
last_seen_at: timestamp
```

## Room

```yaml
room_id: stable-local-room-id
provider: string
provider_room_id: stable-provider-native-room-or-channel-id
provider_container_id: workspace-guild-server-community-id | null
name: string | null
topic: string | null              # the room's SELF-DECLARED purpose, read from provider state
kind: dm | group_dm | channel | room | issue | pr | email_thread | other
bridge_origin: string | null
visibility: private | internal | public | unknown
audience: internal_only | mixed_external | external_only | public | unknown
size_band: small_under_100 | large_100_or_more | unknown
attention_priority: high | normal | low
attention_reasons: [string]
purpose: [evidenced_fact]
workstreams: [evidenced_fact]
membership:
  completeness: full | partial | unknown
  sync_mode: sweep | incremental | task_snapshot | observed_only
  observed_at: timestamp
  last_sweep_at: timestamp | null
  last_incremental_at: timestamp | null
  reported_member_count: integer | null
  listed_member_count: integer | null
  truncated: boolean | null
  source_ref: string
  members:
    - entity_id: stable-local-id
      provider_user_id: stable-provider-native-user-id
      collaboration_role: internal | customer | external_collaborator | partner | vendor | unknown
      membership: joined | invited | left | observed | unknown
latest_context:
  summary: string
  decisions: [string]
  blockers: [string]
  handoffs: [string]
  unresolved: [string]
  through_at: timestamp
  source_refs: [string]
last_seen_at: timestamp
```

## Relationship

Store durable organizational relationships separately from scoped work-item collaboration.

```yaml
relationship_id: stable-local-id
from_entity_id: stable-local-id
to_entity_id: stable-local-id
relation: owner | operator | manager | teammate | customer_contact | external_collaborator | collaborator | reviewer | assignee | expert | other
timescale: durable | scoped
scope_type: team | organization | project | feature | pr | issue | incident | room | other
scope_ref: stable-project-feature-pr-issue-or-room-id | null
state: active | dormant | completed | historical | disputed
valid_from: timestamp | null
valid_until: timestamp | null
last_observed_at: timestamp
last_confirmed_at: timestamp | null
facts: [evidenced_fact]
```

- Use `durable` for team, reporting, ownership, and recurring functional relationships that usually persist for weeks or months.
- Use `scoped` for project, feature, PR, issue, or incident collaboration that commonly persists for days or weeks.
- Tie every scoped relationship to `scope_ref` when available. On merge, close, resolution, cancellation, or another terminal event, mark it `completed` or `historical`; do not delete it or let it imply current responsibility.
- Do not infer the end of a durable relationship from short-term inactivity. Require explicit change, contradictory evidence, or a much longer review interval.
- Interpret `customer` and `external_collaborator` relative to an organization or owner and, when applicable, a project/contract scope. Do not encode them as human/agent entity kinds.
- Keep priority/VIP status orthogonal to entity kind, affiliation, trust, and authority. Accept it only from explicit designation; never infer it from behavior or public status.

## Evidenced fact

```yaml
value: string
status: explicit | observed | inferred | disputed | superseded
source_ref: provider-specific-stable-reference
observed_at: timestamp
valid_from: timestamp | null
valid_until: timestamp | null
confidence: 0.0-1.0
access_scope: room-id | private | organization | public
```

## Store freshness

Per-record `observed_at` says how old a fact you *have* is. It cannot say anything about a fact you do **not** have — and an empty result is the most common thing a map returns. Without store-level freshness, "this person is not in the map" and "the map has not looked since Tuesday" are the same answer, and the second one silently reads as the first.

Record it per source, not once for the whole store: sources go stale independently, and a store that swept AG2 Space an hour ago and GitHub last week is fresh and stale at the same time.

```yaml
store_freshness:
  - source: provider-or-feed-id        # e.g. "matrix:ag2.space", "github:owner/repo"
    last_swept_at: timestamp
    cursor: opaque-provider-cursor | null   # resume point; null = full sweep only
    coverage: full | partial | unknown      # partial when the provider truncated
    coverage_note: string | null            # e.g. "roster capped at 10 members"
    last_error_at: timestamp | null         # a failed sweep must not look like a quiet one
    unknown_kind: unreachable_here | unsupported_by_provider | null
```

**`unknown_kind` answers the only question a miss actually raises: is it worth asking
again?** Without it, a provider that structurally cannot enumerate members and one that
merely lacks a token on this host return the same empty result, and the skill gives both
the same advice — go look. For the structural case that sends the caller into a wall that
produces no error, just another empty result indistinguishable from "not asked yet".

- `unsupported_by_provider` — retrying is pointless anywhere. The gap is in the provider.
- `unreachable_here` — retrying is pointless *on this host*, and may succeed on another
  or after configuration. **It is host-local, so it must not be synced across hosts as a
  fact**; one machine's "cannot reach" is another's ordinary success. Only
  `unsupported_by_provider` is safely shareable.

Record it per source at sweep time, not per lookup — it is a property of the source's
capability, not of any one sweep, and re-deriving it per lookup means deriving it
unverified every time.

**Report freshness with every miss.** When a lookup returns nothing, the answer is "not in the map; this source last swept at `<t>`, coverage `<c>`", never a bare "not found". A miss against a stale or partial source is usually a *reason to go look*, not a fact about the world — and the caller cannot make that distinction unless you hand it over.

The exception is `unknown_kind: unsupported_by_provider`, where going to look is the wall described above: report the miss as a property of the source, and do not advise a retry that cannot succeed. Hand over `unknown_kind` alongside `last_swept_at` and `coverage` so the caller can tell the two apart.

`coverage: partial` is not a lesser `full`. A provider that caps a roster returns a complete-looking list, so partial coverage must be recorded at write time by comparing what was returned against the count the provider reported — it cannot be recovered afterwards by inspecting the stored data, which looks consistent either way.

`last_error_at` exists because a sweep that failed and a sweep that found nothing new both leave the store unchanged. Only the error field separates them.

## Candidate identity link

```yaml
left_identity: provider:id
right_identity: provider:id
confidence: 0.0-1.0
evidence: [source_ref]
conflicts: [string]
state: proposed | confirmed | rejected
```

## Alert

```yaml
alert_key: unfamiliar:provider:id:room-id
type: unfamiliar_identity | identity_conflict | ownership_change | stale_context | scope_risk
severity: info | attention | urgent
entity_or_room_id: string
reason: string
first_seen_at: timestamp
last_seen_at: timestamp
acknowledged: false
```

## Quick lookup index

A compact, bounded cache loaded first (see SKILL.md "Quick lookup index"). Never
authoritative — a miss means consult the full store, not that the entity is absent.

```yaml
quick_lookup:
  updated_at: timestamp
  recent_entities:            # capped, most-recent first; VIP/priority pinned
    - entity_id: stable-local-id
      kind: human | agent | service
      one_line: "role/expertise, e.g. reviews backend delivery changes"
      owner_entity_id: stable-local-id | null   # for agents
      active_rooms: [room_id]
      priority: none | priority | vip
      last_seen_at: timestamp
  active_rooms:               # capped, most-recent first
    - room_id: provider:stable-channel-id
      name: string
      purpose: string
      latest_context_line: string
      size_band: small | large
  active_scopes:              # open collaborations
    - scope_ref: pr|issue|incident|feature:id
      participants: [entity_id]
  open_unknowns: [alert_key]
```

## Merge rules

- Upsert identities by `(provider, user_id)` and rooms by `(provider, provider_room_id)` plus the provider container when required for uniqueness.
- Never key an identity or room by display name, handle, nickname, or room title.
- Merge cross-provider identities only with explicit confirmation or multiple independent strong signals totaling at least `0.9` confidence.
- Never use display-name equality alone. Two people can render the same name on one homeserver, so a display-name join does not merely lose precision — it returns *a* stable ID with full confidence, and the wrong one is indistinguishable from the right one downstream.
- **An owner-stated mapping outranks any derived one.** Record it as an evidenced fact with `source: owner_stated` and the time it was stated, and do not let a later derivation silently supersede it. A derivation that disagrees with an owner statement is a conflict to surface, not a correction to apply.
- Keep aliases after a verified rename; do not treat a rename as a new entity.
- `name` and `topic` are mutable aliases, never keys. **`topic` is the room's self-declared
  purpose (provider state); `purpose` is what the map inferred from traffic.** Store both, and
  treat a divergence between them as a signal in its own right — usually a room whose real use
  drifted while nobody updated the topic. Do not silently overwrite either with the other.
- Supersede time-varying facts rather than deleting them silently.
- Decay inferred operational facts when not reconfirmed: room context quickly, active responsibility moderately, identity and explicit ownership slowly.
- Deduplicate alerts by `alert_key`; update `last_seen_at` instead of repeatedly notifying.

## Suggested freshness windows

Treat these as defaults, not truth:

| Fact | Review after |
|---|---:|
| Latest room context | 7 days or after a major decision |
| Active workstream / blocker | 14 days |
| PR/issue/incident collaboration | 3–7 days or at terminal state |
| Project/feature collaboration | 7–30 days or at terminal state |
| Durable team collaboration | 30–90 days |
| Priority/VIP designation | At explicit expiry or every 90 days |
| Feature responsibility | 30 days unless explicitly durable |
| Behavior pattern | 60 days |
| Explicit identity / ownership | 180 days |

Explicit end dates and newer contradictory evidence override these windows.

## Reviewer identity map (`reviewer-identity/2`)

`<workspace>/data/collaboration-intelligence/reviewer-stands.json` — the single
map of a reviewer's Discord identities. **Keyed by the roster's own local
key**, with the GitHub login in the `github` FIELD — a key-equality lookup
on a login queries the wrong axis and reads a mapped reviewer as absent
(measured twice, 2026-08-27 and 08-28). GitHub logins are matched
case-insensitively wherever they are joined.

```json
{
  "_schema": {"name": "reviewer-identity", "version": 2, "generated_at": "...",
              "migrated_from": "...", "contract": "..."},
  "<roster-local-key>": {
    "human_discord_id": "<the PERSON's id>  | null",
    "stand_discord_id": "<the AGENT's id>   | null",
    "other_stand_discord_ids": [{"id": "...", "basis": ["..."]}],
    "unresolved_discord_ids": [{"id": "...", "reason": "..."}],
    "home_channel": "<channel id> | null",
    "id_basis": {"human_discord_id": ["..."], "stand_discord_id": ["..."]},
    "id_shape_failures": [{"path": "…|null", "kind": "…", "reason": "…",
                           "arbitrated_ids": ["…"], "arbitrated_states": ["human|stand"]}],
    "...": "every v1 provenance field (verification, verified_at, source, observed_at, stand, evidence) is preserved verbatim"
  }
}
```

**Why the field names are long.** v1 carried one `discord_id` whose referent was
unstated, and the pr-triage config carried `{discord, bots[]}` for the same
people. Measured 2026-08-28: for `qingyun-wu` the roster's `discord_id` was the
AGENT and pr-triage's `discord` was the HUMAN — both spelled "discord". Merging
on the shared name makes a person and their agent the same value, and every
downstream ping then reaches the wrong party while reporting success.

**Rules for writers.**

- Fill a slot only from a source that STATES the referent: a field whose own
  name says which (`discord_human_id`, `stand_status`, `secondary_agent`),
  pr-triage `people.<login>.discord` vs `.bots[]`, the Discord `peers.json`
  (peer bot ids), or `discord-config.json` `owner` (the human owner).
- A display name is not evidence. "Sutando-Mini" reads as a bot to a person and
  states nothing to a program.
- Sources that disagree, or two ids claiming one slot, go to
  `unresolved_discord_ids` — never arbitrate.
- Keys beginning with `_` are document metadata, not people.

**Rules for readers.** Check `_schema` first and refuse anything below version 2
rather than reading `discord_id`; use `scripts/roster_identity.py`'s accessors
(`human_discord_id`, `stand_discord_id`, `stand_discord_ids`). An id in
`unresolved_discord_ids` answers no lookup.

`id_shape_failures` is RESERVED and migration-owned: findings the migration
could not re-derive from its own output, carried so a refusal survives a
re-migration. `roster_identity.py` owns its record shape, its canonicalisation
and its bound (`SHAPE_MAX`); a present-but-unusable value is a refusal, never
an absence, and is never silently erased. A finding on a writer-owned field
clears only by repairing the SOURCE roster and re-migrating — our own rewrite
of that field is not a repair. Do not use this name for anything else: a v1
roster carrying a same-named provenance field would be read as a refusal.

Two record kinds are reserved and pathless by design, so both survive the bound
and the pathless-evidence drop. `kind: "invalid"` is a carried refusal that
could not be represented — a present-but-unusable container is a refusal, never
an absence. `kind: "arbitration-overflow"` stands in for arbitration records
that exceed `SHAPE_MAX`, carrying the union of their `arbitrated_ids` and
referents so the contested ids stay refused; identity facts are aggregated
rather than dropped, while diagnostic history is truncated.

**Migrating.** `scripts/migrate_roster_identity.py --roster <v1> --triage-config
<pr-triage/config.json> --peers <peers.json> --discord-config <discord-config.json>
--out <v2> --table` — it never writes its input and prints the per-entry
before/after.
