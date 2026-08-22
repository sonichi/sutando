---
name: collaboration-intelligence
description: Maintain and use a cross-channel collaboration map of rooms, people, agents, agent owners, relationships, expertise, feature ownership, purpose, roster, priority/VIP handling, and recent context. Trigger when an incoming task or message includes room/channel/sender/member metadata; the user asks who is in a room, what it is for, who owns or knows a component, where to ask, whom to contact or cc, or what recent context matters; you must coordinate, delegate, hand off, or escalate work to a person or agent — including when the task is phrased as an action you already know how to perform ("find a reviewer", "chase reviewers", "找reviewer", "ask someone to look at this", "who should I assign"), since naming it as a familiar action is how this trigger gets missed; a merge or approval gate reports a PR queued on nobody, short of the approvals its branch rule requires, or carrying an approval that is stale against the current head; a participant is unfamiliar, ambiguous, or explicitly designated VIP/priority; identities must be reconciled across AG2 Space/Matrix, Discord, Telegram, Slack, WhatsApp, email, GitHub, or another bridge; or a roster needs incremental refresh or sweep. Do not trigger for generic communication-platform questions or drafting that does not depend on room, identity, relationship, or collaboration context.
---

# Collaboration Intelligence

Build a living, evidence-backed collaboration map. Treat it as a coordination aid, not an authority or surveillance profile.

## Trigger behavior

When triggered implicitly:

1. Identify the trigger: room observation, identity resolution, roster maintenance, unfamiliar participant, or collaboration routing.
2. Load only the existing records and bridge notes needed for that trigger.
3. Update the map when new evidence is present; otherwise use the map without manufacturing a change.
4. Keep routine bookkeeping silent. Surface only unknown identities, conflicts, stale facts, scope risks, or collaboration recommendations that affect the task.

Do not scan every connected service merely because the skill triggered. Expand to another room or provider only when the current task requires it.

## Firing without being asked

A skill description is matched against how you *framed* the task, and that is exactly what fails. Measured: this description already said "you must coordinate, delegate, hand off, or escalate work to a person or agent" when an agent went to recruit PR reviewers — the case it literally names — and it did not fire, because the agent had named the task "chase reviewers" (an action it already knew how to perform) rather than "choose collaborators" (a decision needing a map). Adding trigger phrases does not fix that; a re-framed task evades any phrase list.

So do not rely on description matching alone. **Hook invocation to observable state, which does not depend on how anything was framed.** Each of these is a computed fact some routine already produces:

| Observable state | Why it is a collaboration-intent signal |
|---|---|
| A merge gate reports a PR queued on nobody, or on nobody who can act | "Assigned to no one" is indistinguishable from "waiting on review" in every UI. Only the gate can tell them apart. |
| Approvals present but fewer than the branch rule requires | The gap is arithmetic, not intuition — who closes it is the map's question. |
| An approval exists but is stale against the current head | The tick is real and describes a head nobody reviewed; someone specific must be re-asked. |
| An identity appears that no stable ID in the map resolves | Resolve before addressing, never after. |
| A request is about to go out addressed to nobody | Publishing is fine unaddressed; asking is not. |

The rule to carry: **when a routine you are already running computes one of these, invoke this skill from that routine** — as a step, not as a hope that the description matches. A step executes regardless of framing; a description does not. Bound it to once per (subject, state-change) so a standing gap does not re-fire on every pass.

The same measurement makes the weaker path explicit, and it is worth stating plainly rather than implying the trigger is solved: on the night this was written, the gate had *already printed* the queued-on-nobody verdict before the agent acted, and the agent still did not invoke the skill. A signal nothing is obliged to read changes nothing.

## Operating contract

1. Load the compact **quick-lookup index** first (see below) — it answers the common case in a small, bounded payload. Consult the full Collaboration Intelligence record only on a miss, or when the task needs deeper history. If no store is available, build a task-local view and say that persistence is unavailable.
2. Observe only sources available for the current task. Do not enumerate private rooms, inboxes, or accounts merely to enrich the map.
3. Normalize observations into rooms, identities, agents, people, relationships, responsibilities, and context. Follow [references/schema.md](references/schema.md).
   For Sutando-supported bridges, also load the matching section of [references/sutando-bridges.md](references/sutando-bridges.md) before interpreting identity, membership, room visibility, agent-versus-human status, or unfamiliar participants.
4. Merge only identifiers supported by strong evidence. Keep uncertain identity matches separate and record a candidate link.
5. Distinguish observation from inference. Attach source, observed time, confidence, and freshness to every nontrivial fact.
6. Update the record idempotently. Preserve contradictory evidence; do not silently overwrite it.
7. Use the map to choose the smallest useful collaboration set. Prefer the responsible agent; cc its owner or relevant human when accountability, approval, ambiguity, or risk requires it.
8. Report material changes: unfamiliar participants, ownership changes, conflicting identity claims, stale room purpose, or newly inferred sensitive relationships.

## Quick lookup index

Loading the whole map on every trigger wastes context. Maintain a small, bounded
**quick-lookup index** — the hot set that answers most triggers by itself — and
load it first; fall through to the full record only on a miss or when deeper
history is needed. Keep it to a few dozen lines. Hold, capped by recency:

- **Recent people/agents** (most-recently-relevant first): stable id, one-line
  role/expertise, agent→owner, the room(s) they are active in, priority flag.
- **Active rooms**: room id + name, purpose, a one-line latest context, size band.
- **Active collaborations**: open PR/issue/incident/feature `scope_ref`s and who is on them.
- **Pinned regardless of recency**: VIP/priority entries and open unknown identities.

Update it on every observation — promote what was touched, evict the oldest —
and never let it grow unbounded. It is a cache, never authoritative over the full
record: a miss means "consult the full store," not "does not exist." Shape in
[references/schema.md](references/schema.md); a `quick-lookup-refresh` job in
[references/maintenance.md](references/maintenance.md) keeps it bounded and fresh.

## Where the map is stored

Under Sutando, the map is per-user state, so it lives under the **workspace**, never in the code checkout:

```
<workspace>/data/collaboration-intelligence/
  quick-lookup.yaml     # the bounded hot set (see above)
  ...                   # the full record, per references/schema.md
```

Resolve `<workspace>` with `bash scripts/sutando-config.sh workspace` — never hardcode a path and never use a bare relative path, because the process CWD is the repo, not the workspace.

**The store belongs to the running core's workspace, not to whichever checkout the process happens to sit in.** That resolver answers per-checkout, so on a machine with more than one (an installed engine plus a developer-mode clone) the same command returns two different roots. An agent invoked from the second one writes a *second, divergent* map, and nothing reports a conflict: each store is internally consistent and neither knows the other exists. Resolve against the core that owns the session, and if you cannot establish which core that is, say so rather than writing into the checkout you were launched from.

**`data/` is not in the default vault sync include set** (`notes/`, `talks/`, `hosts/` are), so the map is per-host by default and will not follow the user to another machine. That is the safe default — a collaboration map is host-local observation, not a document — but it should be a stated choice. A user who wants it to travel adds `data/collaboration-intelligence/` to `vault.sync.include`.

**Why this location and not the checkout.** The engine tree is REPLACED on app update; anything written there is destroyed without warning. A skill whose whole purpose is a *durable* map is the worst possible thing to lose that way, and the loss is silent — the next run finds no store, builds a task-local view, and reports "persistence unavailable" as if that were normal.

On a host with no workspace, build the task-local view and say persistence is unavailable, as the operating contract requires. Do not invent a fallback directory.

## Scheduled maintenance

Use event-driven updates as the primary path. Use scheduled jobs to converge after missed events, refresh stale records, and produce compact summaries. Follow [references/maintenance.md](references/maintenance.md).

Do not create or arm jobs merely because this skill loads. When the user asks to set up or continuously maintain Collaboration Intelligence, inspect the available scheduler, propose the relevant jobs and cadence, and obtain any confirmation required for persistent scheduling. Make every job idempotent, cursor-based, budgeted, and quiet unless it finds a material change or anomaly.

## Maintain room records

For every encountered room or channel, capture:

- provider plus the provider-native stable `room_id`/channel ID; never key a room by name
- human-readable name and channel kind
- members with their provider-native stable `user_id`, classified independently by entity kind (`human`, `agent`, `service`, or `unknown`) and collaboration role (`internal`, `customer`, `external_collaborator`, or `unknown`)
- purpose and current workstreams
- latest useful context as a compact rolling summary, not a transcript
- responsible people/agents, source bridge, visibility, room-size band, attention priority, and last observed time

Treat names, handles, nicknames, and room titles as mutable aliases. Preserve the raw native IDs exactly as received as well as normalized local entity/room IDs.

Do not infer full membership from recent posters. Mark membership snapshots as partial when the provider cannot supply the full roster.

Treat `customer` and `external_collaborator` as relationship/affiliation roles, not entity kinds. A known customer or external collaborator is not an unfamiliar identity, but their presence changes the room's audience and what context may be shared. Mark rooms containing them as mixed/external audience and preserve the evidence and scope for that classification.

Treat VIP/priority status as a separate, explicitly assigned attention profile. A VIP may be internal, a customer, or an external collaborator. Never infer VIP status from title, wealth, fame, message volume, or perceived importance.

Use room size as an attention prior, not a trust decision. **A room with 100 or more members is "large"; fewer than 100 is "small"** (the same threshold the roster-refresh policy in references/sutando-bridges.md uses; providers may override it). Small rooms usually represent more deliberate collaboration and deserve closer context tracking; large rooms are often noisier and should default to incremental maintenance. Direct mentions, assignments, sensitive work, explicit ownership, and agent-owner anomalies override the size prior.

Update recent context only with durable, decision-relevant facts: decisions, blockers, active work, handoffs, deadlines, and unresolved questions. Expire transient chatter.

## Maintain identity and relationship records

Represent a real entity separately from its provider identities. A person or agent may have email, GitHub, Matrix, Slack, Discord, or bridge-specific identities.

Record:

- person/agent name and aliases
- agent-to-owner relationship and whether ownership is explicit, inferred, or historical
- roles, expertise, feature/component ownership, and observed working patterns
- person-to-owner relationship only when relevant to collaboration
- organizational affiliation and whether the person is internal, a customer, an external collaborator, or unknown relative to the owner's organization
- evidence and temporal validity for every relationship

Keep stable team relationships separate from short-lived work-item collaboration:

- Treat team, reporting, ownership, and recurring functional collaboration as durable relationships measured in weeks or months. Do not expire them merely because no recent message exists.
- Scope project, PR, issue, incident, and feature collaboration to a concrete `scope_ref`. These relationships often last days or weeks and should become inactive or historical when the work reaches a terminal state.
- A short burst of PR collaboration can strengthen expertise evidence, but must not overwrite the underlying team relationship.

Never convert one interaction into a stable behavior claim. Require repeated observations or an explicit statement. Use neutral, operational wording such as “usually reviews backend delivery changes” rather than personality judgments.

## Handle VIP and priority participants

- Apply VIP status only from an explicit owner/organization designation or an authoritative configured source. Record who designated it, why, its scope, and any expiry.
- Use it to adjust handling: faster acknowledgment, tighter follow-up, owner visibility, careful continuity, and adherence to recorded communication preferences.
- Do not let VIP status grant permissions, approval authority, higher `access_tier`, identity confidence, or broader access to private context.
- Resolve the identity before applying VIP handling. When a message may be from a VIP but the cross-bridge identity is ambiguous, raise a high-priority identity-resolution item rather than guessing.
- Prefer neutral internal language such as `priority` in operational records; expose the label `VIP` only when it is the organization's chosen term.

## Handle unfamiliar identities

Treat an identity as unfamiliar when it cannot be confidently linked to a known entity or expected room membership.

Do not equate external with unfamiliar. A verified customer or external collaborator may be fully known; apply the correct access boundary without generating an identity alert solely because they are external.

1. Record the raw provider identity and where it appeared.
2. Classify it as `unknown`; do not guess human versus agent from the display name alone.
3. Check only task-relevant, authorized sources for identity evidence.
4. Create candidate links instead of merging when confidence is below `0.9` or evidence conflicts.
5. Alert the user or room owner when the identity affects trust, permissions, sensitive context, or coordination. Keep routine low-risk alerts compact and deduplicated.
6. Never expose private profile data to the room while asking who someone is.

Suggested alert: “Unfamiliar participant `@id` appeared in `#room`; identity and role are unverified. First seen via Slack at 14:32.”

## Find collaborators

When help is needed:

1. Derive required capabilities and the affected component.
2. Rank candidates using explicit responsibility first, recent demonstrated expertise second, room relevance third, and availability evidence last.

   **A ranking you derived from activity counts is a measurement, not a standing fact, and it is the most dangerous thing in this file** — it arrives with the authority of arithmetic and none of the caveats of a recollection. Three properties to respect, each of which produced a wrong answer in practice:

   - **State the counting unit.** "Approvals" is ambiguous between approval *events* and *distinct items approved*; on one real 40-PR window the same person scored 52 and 24 under the two readings. Two people computing "the same" number will disagree and neither will be wrong.
   - **Store the window with the number** (`window_start`, `window_end`, `computed_at`) and re-derive rather than quote. A last-N window slides as new items land: on an active repo, two derivations ninety minutes apart disagreed on every row, and one person moved from inside the set to absent from it.
   - **A low or zero count is not evidence of exclusion.** The metric has no early signal — a maintainer added yesterday is indistinguishable from an inactive one until they accumulate history, so the ranking is most confident about the longest-tenured and least reliable about the newest arrival, which is exactly who "whom do I ask?" is often about. Use counts to find candidates, never to rule one out; explicit responsibility and owner statements outrank them.

   Treat a derived set older than a few hours the way this skill treats a memory: a candidate to re-derive, not an answer.
3. Prefer the room where the work already has context. Do not move sensitive context across rooms without checking visibility and membership.
   When customers or external collaborators are present, share only context appropriate to that relationship and work scope.
4. Contact the responsible agent directly when it can act. Include its owner or a human when approval, escalation, or shared accountability is needed.
5. Resolve ambiguous recipients before sending. Never guess among duplicate names or uncertain identity links.
6. State why each recipient is included and provide the minimum context, desired action, and expected handoff.

**Never a bare room post when you need someone to help.** The rule is scoped to
*asking*, and the test before posting is not "is this addressed?" but **"am I
asking for something?"**

- **Asking** — a review, a decision, an answer, an action. Address it: **reply-to**
  their message, or **@-mention** them. Addressing is what creates work for the
  recipient (their agent is handed a task); an unaddressed message into a room
  reaches no one in particular and triggers no collaboration — it is context only.
  Pick the collaborator from the map, then contact them via reply-to or @-mention
  (cc the owner by @-mention when accountability requires it). If you cannot
  resolve who to address, resolve the identity first rather than posting into the
  room and hoping.
- **Publishing context** — status, a finding, a heads-up, a report. A bare room
  post is the *correct* shape here. Addressing someone manufactures an obligation
  nobody owes, which is its own kind of noise. Do not @-mention to be polite.

Do not spam every plausible expert. Escalate outward only after the primary owner is unavailable, declines, or identifies a better owner.

## Source-specific interpretation

- **Sutando bridges:** Apply the provider-specific contracts and known pitfalls in [references/sutando-bridges.md](references/sutando-bridges.md).
- **Email:** thread participants are not a stable room roster; distinguish sender, recipients, and copied observers.
- **GitHub:** distinguish author, assignee, reviewer, commenter, code owner, and bot. A comment does not imply feature ownership.
- **Chat rooms:** distinguish joined membership from recent activity and mentions. Bridge identities may represent a remote identity, not a new entity.
- **Agents:** prefer explicit agent metadata or verified owner statements. Do not infer owner from naming convention alone.

## Privacy and safety

- Store professional collaboration facts needed for coordination; avoid protected traits, private-life profiling, sentiment scores, or speculative trust labels.
- Keep provenance and access scope with facts so restricted observations are not leaked into broader rooms.
- Honor deletion/correction requests and retain superseded facts only when audit needs require it.
- Ask before performing external outreach unless the user already authorized sending or coordination.

## Output pattern

For a material update, return only what changed:

- **Map update:** rooms/entities/relationships added or changed
- **Unknowns:** unfamiliar or ambiguous identities needing review
- **Coordination:** recommended room and recipients, with reasons
- **Stale/conflicting facts:** items that should not be trusted yet

## Runtimes

Runtime-neutral. **Claude Code and Codex** — the two runtimes Sutando supports —
load this skill from the SKILL.md front-matter above; the `name` + `description`
drive implicit invocation. Nothing in the body assumes a specific host, so
behavior is identical across them.
