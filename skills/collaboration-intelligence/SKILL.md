---
name: collaboration-intelligence
description: Maintain and use a cross-channel collaboration map of rooms, people, agents, agent owners, relationships, expertise, ownership, roster, priority/VIP handling, and recent context. Trigger when a task or message carries room/channel/sender/member metadata; the user asks who is in a room, what it is for, who owns or knows a component, where to ask, or whom to contact or cc; you must coordinate, delegate, hand off, escalate, or pick who reviews something — including when phrased as an action you already know how to perform, since naming it as a familiar action is how this trigger gets missed; a merge or approval gate reports a PR queued on nobody, short of its required approvals, or carrying a stale approval; a participant is unfamiliar, ambiguous, or designated VIP/priority; identities must be reconciled across bridges; or a roster needs refresh. Do not trigger for generic communication-platform questions, or drafting that does not depend on room, identity, relationship, or collaboration context.
---

# Collaboration Intelligence

Build a living, evidence-backed collaboration map. Treat it as a coordination aid, not an authority or surveillance profile.

## First run

**A freshly installed map is empty, and the Operating contract below does not bootstrap it.** Step 1 there says to load the quick-lookup index first — on a new install there is no index, so an agent queries nothing, reports a miss, learns nothing, and stays empty. It never errors. An unbootstrapped map and a working one are indistinguishable from outside, which is the failure mode this skill exists to fight, turned on itself.

So run this once, before relying on the contract.

**0. Decide whether this pass may bootstrap at all — before seeding and before sweeping.**

The test is per-pass, not per-agent: does *this* pass have a serving `channel_id`?

- **with** one → do not bootstrap. Not the sweep, and **not the seeding either**: soliciting or recording cross-provider identity links is itself collection, and doing it while serving an ordinary room task persists sensitive cross-room associations before any privacy check. Report and stop.
- **without** one (a maintenance pass, owner-tier, serving no channel) → proceed to step 1.

Do not shortcut this by source type. "Cron passes carry no `channel_id`" is **host-specific and was measured false**: one host had 153 cron tasks with zero `channel_id`, another had 3 of 3 carrying one. Read the pass you are in.

And do not read the gate as clearance. Measured: `gate(serving_channel_id=None, …)` returns **ALLOWED**, because the blacklist lookup misses and an empty blacklist permits — it fails **open**. That is an absence of jurisdiction, not a permission; the judgement stays yours.

**1. Seed the owner-stated identity map before sweeping. The sweep enriches it; it never substitutes for it.**

This ordering is measured, not stylistic. On a 7,810-file corpus the sweep produced immediately usable *room* knowledge — traffic ranking plus honest coverage flags — and, for *identities*, a schema-shaped pile: the top participant by traffic and **the owner themselves** both came back as bare unknown ids, because Discord headers carry no display name. Any heuristic of the form "high traffic ⇒ important human" would have misclassified the owner and a peer bot with identical confidence.

Worse for derivation: a known cross-provider identity — one person holding a GitHub handle and a chat id whose display name collides with someone else's — was **underivable from that corpus at any confidence**. It existed in the map only as an owner-stated seed with provenance. That is what makes "owner-stated outranks derived" load-bearing rather than decorative.

So: ask for a handful of mappings (GitHub handle ↔ chat id ↔ person) before sweeping. It is the highest-value minute available, and it is the part the sweep structurally cannot do for you.

**The unresolved list is per-host, and so is the seed list.** The store is host-local by design (`data/` is outside the default vault sync set), so one machine's unknowns are not another's — two agents comparing notes found a Discord id that was authoritative on one host and absent from the other's config entirely. Do not ask someone else to enumerate your unknowns, and do not assume theirs are yours.

**2. Then sweep the task-file stream.** Step 0 has already established that this pass may do so.

> **The bootstrap is not permission-free, and "the bytes are already on disk" is not the test.** The context boundary is *serving-relative*: `src/discord_context_policy.py`'s `gate()` decides whether the channel you are **currently serving** may read some other channel, and it applies to owner-tier tasks too. Its fail-closed behaviour is **conditional**: it refuses an unresolvable guild only when the serving channel has a `contextNotFrom` blacklist at all — with no blacklist it returns ALLOWED before reaching that check (see the measured note above). So the boundary constrains a sweep only where someone configured it; elsewhere the judgement is yours. A sweep run while serving one channel would pull rooms that gate would have refused — and because the sweep **persists** what it reads, those rooms then inform every later answer. That is strictly worse than a single blocked read.
>
> So: run the bootstrap from an explicit owner/operator maintenance context, not as a side effect of handling a task. If you cannot establish that context, either filter every archived observation through the same serving-channel policy, or do not sweep. Record `access_scope` on each observation so a later answer cannot quietly widen it.

**Header fields are source-specific — check, do not assume.** Writers differ, and the richest one is not representative:

| source | provides | consequence |
|---|---|---|
| AG2 Space / Matrix | `channel_id`, `room_name`, `room_members`, `room_member_count`, `user_id` | rooms, names and rosters available |
| Discord | `channel_id`, `channel_name`, `guild_name`, `user_id` | **no roster, no member count** — membership is `unknown`, not empty |
| Slack | `channel_id`, `user_id` | id and speaker only; name and roster `unknown` |

Treat a field the source never emits as **`unknown`**, never as absent-therefore-zero — a room with no roster field is not a room with no members. Set `coverage: unknown` for those sources and `coverage: partial` only where a roster was truncated (`+N more`), which is recordable **at sweep time** and unrecoverable afterwards.

What the sweep yields, on the sources that carry it:

- **rooms**, ranked by traffic, with provider-native ids and whatever name the source gave
- **participants**, ranked by messages, classified `human` / `agent` / `service`
- **unknowns worth resolving immediately** — rooms with real traffic but no name and no members observed

Record the result per [references/schema.md](references/schema.md), including `store_freshness` per source. Do not enumerate rooms, inboxes or accounts you were not already given.

**3. Expect the sweep to surface defects, and record them rather than smoothing them.** Run against a real corpus it immediately produced unnamed high-traffic rooms, hundreds of truncated rosters, and a service account misfiled as human by a two-way agent/human split. Each is a real map entry — an unknown to resolve, a partial-coverage flag, a classification gap — not noise to filter out.

**4. Then let the scheduled work maintain it.** Only once the map holds something does the contract's load-first path mean anything.

**Validate the sweep against itself before trusting its counts.** A parsing sweep does not fail with an error; it returns a plausible number. One real attempt reported a tidy count of unresolved identities that was pure artifact — a fixed-size read had cut header lines mid-value, so a single identity appeared three times truncated to different lengths. A second attempt broke the opposite way and under-counted. Both looked normal.

So run two controls on your own output, and treat the sweep as unusable until both hold:

- **negative** — a known-bad artifact must be ABSENT (no truncated or prefix-duplicated identifiers).
- **positive** — a known-present, high-frequency participant (the owner, typically) must appear with a plausible magnitude. A parser that silently drops most records still returns a small, tidy, wrong set.

A count with neither control is an assertion, not a measurement.

**When the bootstrap is unreachable — say so and route it, do not quietly skip or quietly run it.**

The per-pass test lives at **step 0** above and gates seeding as well as sweeping. This section covers the case where the bootstrap cannot run at all.

- **A bundled install.** No `.git`, so the skill cannot be pulled or refreshed — a property of the packaging, not a choice available to the agent.

**You will not perceive this block. You have to go looking for it.** `skills/install.sh` iterates `$SKILLS_DIR/*/` — only directories that exist — so a skill absent from the bundle is never in the loop, `set -e` never trips, and the script ends by printing an affirmative claim:

```
Installed. Skills available in any Claude Code session.
```

Measured on a bundled host: that line printed for 60 skills while this one was in none of them. So the bundled case is not "cannot comply and knows it" — it is **cannot comply and cannot tell**, and an exit code of 0 with a success message is the evidence you would otherwise have trusted.

**So verify the destination, not the installer.** After installing, assert that this skill's own directory exists under the resolved skills root. Only that answer distinguishes the two cases; the installer's output cannot.

Having established a block that way, **report it to the owner/operator and stop**, rather than approximating compliance. An agent that judges its own context is the only thing currently enforcing this boundary, which is a weak place to put a privacy rule: prose does not fail because someone ignores it, it fails because *an instruction to cross it can look exactly like ordinary diligence*. That happened here — a request to "actually run it, not just read it" was well-intentioned and would have crossed the boundary within minutes of its landing.

If this ever needs a mechanical guard rather than a rule, the repo already has the shape: `discord-read.py` distinguishes a task-serving invocation from an operator one with an explicit `--operator` flag, and refuses rather than inferring.

**The payoff to check for**: after step 1 you should be able to answer "who is in this room, and which of them are agents" and "where does this kind of work usually get discussed" without asking anyone. If you cannot, either the sweep did not run or its store did not persist — see **Where the map is stored**.

## Trigger behavior

When triggered implicitly:

1. Identify the trigger: room observation, identity resolution, roster maintenance, unfamiliar participant, or collaboration routing.
2. Load only the existing records and bridge notes needed for that trigger.
3. Update the map when new evidence is present; otherwise use the map without manufacturing a change.
4. Keep routine bookkeeping silent. Surface only unknown identities, conflicts, stale facts, scope risks, or collaboration recommendations that affect the task.

Do not scan every connected service merely because the skill triggered. Expand to another room or provider only when the current task requires it.

## Firing without being asked

A skill description is matched against how you *framed* the task, and that is exactly what fails. Measured: the description already named coordinating, delegating and escalating work to a person or agent when an agent went to recruit PR reviewers — the case it covers — and it did not fire, because the agent had named the task "chase reviewers" (an action it already knew how to perform) rather than "choose collaborators" (a decision needing a map). Adding trigger phrases does not fix that; a re-framed task evades any phrase list, and the front-matter budget is finite anyway — 1024 characters, which a phrase list burns fast.

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

0. **If the map has never been populated, do the bootstrap in [First run](#first-run) first.** The steps below assume a map that already holds something.
1. Load the compact **quick-lookup index** first (see below) — it answers the common case in a small, bounded payload. Consult the full Collaboration Intelligence record only on a miss, or when the task needs deeper history. If no store is available, build a task-local view and say that persistence is unavailable.
   **Carry freshness with every answer, including the empty one.** A hit reports its `observed_at`; a miss reports when that source was last swept and whether coverage was full — otherwise "not in the map" and "the map has not looked recently" are the same sentence. See `store_freshness` in [references/schema.md](references/schema.md).
2. Observe only sources available for the current task. Do not enumerate private rooms, inboxes, or accounts merely to enrich the map.
3. Normalize observations into rooms, identities, agents, people, relationships, responsibilities, and context. Follow [references/schema.md](references/schema.md).
   For Sutando-supported bridges, also load the matching section of [references/sutando-bridges.md](references/sutando-bridges.md) before interpreting identity, membership, room visibility, agent-versus-human status, or unfamiliar participants.
4. Merge only identifiers supported by strong evidence. Keep uncertain identity matches separate and record a candidate link.
5. Distinguish observation from inference. Attach source, observed time, confidence, and freshness to every nontrivial fact.
6. Update the record idempotently. Preserve contradictory evidence; do not silently overwrite it.
7. Use the map to choose the smallest useful collaboration set. Prefer the responsible agent; cc its owner or relevant human when accountability, approval, ambiguity, or risk requires it.
8. Report material changes: unfamiliar participants, ownership changes, conflicting identity claims, stale room purpose, or newly inferred sensitive relationships.
9. **PR notification contract (owner rule 2026-08-23): every PR create or update ends with reviewers NOTIFIED, addressed to each reviewer's Sutando Stand.** A GitHub review request alone is not notification — the review-request queue is where PRs stall. "Addressed to" means an action that reaches the Stand and triggers it: an **explicit @-mention** in a channel that supports it (Matrix: the literal `@<agent-mxid>` string, e.g. `@sutando-rui:ag2.space` — resolver handles are unreliable, use the mxid; Discord: `<@numeric-id>`), or a **reply-to** on a message from that Stand. Plain-text names are not addressing (measured 2026-08-23: a plain "rui / Chi:" Triage post drew nothing; the agent-mxid mention produced two formal reviews within the hour). **Mentioning the HUMAN is also not addressing the Stand** — correct mention syntax with the person's id notifies the person and triggers nothing (second failure shape, owner-corrected 2026-08-23: `<@Chi> <@kewei>` in #game had to be re-sent as `<@Sutando-Mini> <@kewei-agent>`). And a mention that REACHES a Stand still only *triggers* it if the sender is on that Stand's allowlist (Sutando-Mini bounced an off-allowlist mention with an automated notice) — for action-triggering, confirm allowlist standing first or route through the owner. Route via the map: find each reviewer's Stand/agent identity and its supported channels there, not from recall; record who actually responded back into the map. **Use `scripts/notify_reviewers.py` for the send** — it resolves each reviewer through the roster (`<workspace>/data/collaboration-intelligence/reviewer-stands.json`) and refuses unknown names, human-only targets, and known-off-allowlist Stands, so the rule holds even when acted from momentum.

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

## Work with customers

Customer collaboration needs a clear owner, a shared working surface, and a durable record. Track,
per customer:

- the customer organization and its key contacts
- each contact's role: decision-maker, champion, technical owner, user, or procurement
- the internal owner and the responsible agent
- active scope, commitments, blockers, deadlines, and the next action
- preferred channels, response expectations, and VIP/priority status

### Use each venue for the right purpose

| Venue | Purpose |
|---|---|
| Slack Connect, AG2 Space, WhatsApp | day-to-day coordination |
| GitHub or issue tracker | technical work and delivery status |
| Email, documents, contracts, CRM | formal decisions and commitments |
| Meetings | synchronous discussion — record the outcome in a durable venue afterward |

**Always identify the source of truth, and do not let an important decision exist only in chat.** A
decision that lives in a message thread has no owner, no version, and no reader after scrollback.

### Keep the work moving

1. Acknowledge customer requests promptly.
2. Clarify the desired outcome, the urgency, and who the decision-maker is.
3. Assign one internal owner and one clear next action.
4. Give progress updates **when the state changes**, and before an agreed update deadline **even if
   the work is not finished** — an update whose content is "still working" is still a state report.
5. Surface blockers with options and a recommendation, not only a problem statement.
6. Close the loop: record the outcome and confirm completion with the customer.

⇒ **A customer request must never remain "waiting on nobody."** If ownership is unclear, establish an
internal owner *before* promising a result. An unowned internal item stalls quietly; an unowned
customer item stalls while someone outside is waiting on an answer they were led to expect.

### Protect the boundary

- Treat customer-facing rooms as **external or mixed-audience** spaces.
- Do not carry internal discussion, blame, private incidents, credentials, or personnel context into
  them.
- **Distinguish a customer request from an accepted commitment.** The two look alike in a chat log and
  differ entirely in what they oblige.
- Do not promise scope, dates, pricing, or policy exceptions without the appropriate authority.
- Keep an internal backchannel for private coordination, **and keep the customer-facing status
  accurate and consistent with it** — a backchannel that diverges from what the customer has been told
  is worse than no backchannel.


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
3. **Choose the person first, then choose where to reach them — they are separate decisions.** An identity existing is not the same as it being live: check `identities[].activity` for where that person is actually seen, and honour `exclusive: true` (some people are reachable on exactly one surface, and a message anywhere else reaches nobody). Defaulting to whichever surface you happen to be on is how a reachable person gets treated as unreachable. Prefer the room where the work already has context, subject to that.
   When customers or external collaborators are present, share only context appropriate to that relationship and work scope.
4. Contact the responsible agent directly when it can act. Include its owner or a human when approval, escalation, or shared accountability is needed.
5. **Resolve the recipient before sending anything that carries a payload**, and never guess
   among duplicate names or uncertain identity links to deliver one. When the identity is
   imperfectly resolved, the message is not simply blocked — what you may send is governed by
   *what it carries*, under **"When resolution is imperfect"** below: a bare solicitation may
   go, anything the *least-privileged candidate* could not already access may not. That rule is
   the only exception
   to this step, and it does not license guessing for a payload-bearing message.
6. State why each recipient is included and provide the minimum context, desired action, and expected handoff.

**A request made on the work platform is not solicitation.** A GitHub review
request, a ticket assignment, a "requested changes" — these change a field in a
system the recipient may not be watching. They create a *record* that you asked;
they do not create *knowledge* that you asked. **Filing one and stopping is
indistinguishable, from the recipient's side, from never having asked.**

So for every reviewer/assignee you name on a platform, also **message their agent
where that person is actually reachable** — resolve the identity from this map
first, then reply-to or @-mention. Two properties make this non-optional:

- **Nothing chases it.** A platform request sits indefinitely and emits no
  reminder. Measured: a PR sat one approval short of its ruleset with **no
  reviewer queued at all** — not blocked on anything, simply unattended, and
  invisible to every "is it blocked?" check because unattended is not blocked.
  One @-mention produced the missing review within minutes.
- **Resolve the identity from this map before sending — never derive an agent id
  from a display name or by transforming a user id**, and treat colliding display
  names as unresolved until evidence separates them.

  **When resolution is imperfect, what you may send is decided by what the message
  contains — not by how confident you feel.** The asymmetry that justifies sending
  anyway is real and it is bounded: it holds for the *ask*, never for the payload.

  - **Send it: a bare solicitation.** "Will you review PR #N?", plus a pointer to
    something the recipient could already reach on their own. A wrong recipient
    costs them one message they can ignore; reaching nobody leaves the item
    unattended, and unattended is invisible to every "is it blocked?" check.
    **Owner directive (2026-08-22): a false positive here is the more tolerable
    error.** Say plainly why you think it is them, so a wrong recipient can correct
    you in one line instead of silently absorbing the ask.
  - **Withhold it until identity is established: anything the *least-privileged
    candidate* could not already access.** Not "the recipient" — when the identity is
    unresolved that phrase has no determinate subject and quietly resolves to *the
    person you think it is*, which is the exact case this rule exists for. Score it
    against the least-privileged identity still consistent with the evidence.
    Concretely: private repository contents, incident detail, personnel matters,
    credentials and anything adjacent to them, and context carried from a narrower
    room. Here a wrong recipient is not one ignorable message — it is a
    disclosure, and no correction takes it back. The asymmetry inverts, so the
    default inverts with it.
  - **When it is ambiguous, ask in the open instead of guessing in private.**
    Address the candidates by name in a room they are both in, carrying the request
    and none of the payload. That reaches the right person without betting private
    context on a guess, and a wrong guess costs nothing.

  **Evidence that establishes identity for the second case:** an owner-stated
  mapping, a provider-native id resolved from this map and marked `verified`, or a
  self-identification the person made in a channel you can read. **A display-name
  match is never sufficient**, and neither is an id you derived by transforming
  another id.

Carry what the platform page cannot show: why them specifically, the real cost
(size, conflicts, prerequisites) so they can decline cheaply, and any known
blocker on their side. One message per person covering all their items, not one
per item.

**Soliciting is the start of the obligation, not the discharge of it.** An item
you asked someone to move — a review, a decision, an approval — needs stewardship
until it reaches a terminal state, and the platform will not do that for you.

**The trigger to message is a state change the other party would want to know
about — most often "I addressed your finding."** A push updates a branch and a
comment updates a page; **neither reaches a person.** The reviewer who blocked you
is not watching your branch, so from where they sit an addressed finding and an
ignored one look the same until they happen to look again. That gap is measured in
however long it takes them to re-scan, and it is the single commonest reason a
resolved item sits.

Other state changes worth a message: a blocker of theirs is now cleared; the thing
they were waiting on landed; you have changed direction on something they reviewed;
you are handing the item to someone else.

**And the discipline that keeps this from becoming noise: never send a contentless
nudge.** "Any update?" with nothing new on your side is what gets a channel muted, and
a muted channel costs you every future nudge that mattered.

**But what elapsed time disqualifies is repeating yourself — not asking whether the
item still has a live owner.** Those are different messages and only the first is
noise. So there are two tests before sending, and they key on different variables:

- **Do I have something new to tell them?** New information, an addressed finding, a
  changed direction. This is the trigger that matters most and the one most often
  skipped.
- **Does this item still have a live owner?** Ownership is not established once and
  then trusted forever. A holder's attention can lapse, and **that is invisible from
  your side by construction** — nothing of yours changes when it happens, so elapsed
  time is the *only* signal that can surface it. Pick that horizon deliberately and
  write it down with the ask — an unnamed "eventually" is how eleven days happen — and
  when it arrives send a reassignment question, never an "any update?".

If neither test fires, the item is correctly in their queue — leave it.

Measured on this repo: a PR sat **eleven days** on a blocking review its author had
answered within the hour, and **approvals kept arriving the entire time** — reviewer
after reviewer read it, approved it, and changed nothing, because the block was never
theirs to clear. Nothing had changed on the author's side after day one, and the item
was unambiguously owned, so "has anything changed on my side?" returned *no* every
single day, correctly, while every one of those approvals went unused.

(**This sentence used to headline the approval count. Don't.** The item was still open,
so the figure moved four → six → five → six *while this paragraph was under review* —
and only the first of those was a counting error; the rest were the item accumulating
underneath the measurement. A count of a live item goes stale faster than the document
quoting it. Freeze it with an explicit "as of &lt;timestamp&gt;" or, better, state the
invariant that cannot move: **approvals accumulated and the item did not.** The
first wrong figure was the row count of a truncated terminal display, which is its own
lesson — state which unit a count is in, or the next reader will invent one.)

**And the ownership test has a floor beneath it: an item nobody was EVER asked to
move.** There no holder's attention lapsed — none existed. Nothing of yours changes, by
construction, and there is no one whose silence elapsed time could measure, so both
tests stay quiet indefinitely. That item is not waiting, it is dropped, and the action
is to solicit — not to ask a reassignment question about a holder who was never
assigned. **Never-owned and owned-then-quiet are indistinguishable from outside and
have different fixes: the first needs a name on it, the second needs the name changed.**

Track, per party you are waiting on: what they hold, and what has changed since you
last contacted them. **That pair has no home today** — the record schema in
`references/schema.md` carries entities, rooms, relationships and evidenced facts, but
no outstanding-ask record, and an issue tracker does not store it either. Keep it with
whatever working state you already have for the item, and do not assume a lookup can
answer "what do they hold".

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
- **Repeating something from a smaller room into a larger one requires an explicit signal, and the default is no.** Before carrying a fact across rooms, compare their visibility, audience and membership: narrower → wider is a disclosure decision, not a formatting one. Knowing something because you were present is not permission to restate it. This skill's daily action *is* cross-room routing, so it will meet this case constantly; when in doubt, say that context exists and let the person who owns it decide whether to share it.
- Honor deletion/correction requests and retain superseded facts only when audit needs require it.
- Ask before performing external outreach unless the user already authorized sending or coordination.

## Output pattern

For a material update, return only what changed:

- **Map update:** rooms/entities/relationships added or changed
- **Unknowns:** unfamiliar or ambiguous identities needing review
- **Coordination:** recommended room and recipients, with reasons
- **Stale/conflicting facts:** items that should not be trusted yet

## Runtimes

Nothing in the body assumes a specific host — but *reachability* is not runtime-neutral, and the two supported runtimes differ today:

- **Claude Code — installed.** `skills/install.sh` links every repo directory containing a `SKILL.md` into the Claude skills directory (`sutando-config.sh claude-home-path skills`), so this skill is discovered there and its `name` + `description` drive implicit invocation.
- **Codex — discoverable in principle, not installed in practice.** Codex reads a skills root at `$CODEX_HOME/skills/` and uses the *same* contract: a directory holding `SKILL.md` with `name` + `description` front-matter, alongside optional `agents/`, `assets/`, `references/` and `scripts/`. Its six built-in skills all take exactly that shape. What is missing is only the install step — nothing in this repo links repo skills into that root; the launcher sets `CODEX_HOME` and otherwise invokes individual skill *scripts* by absolute path, which is execution, not discovery.

So this skill already has the right shape for both runtimes, including the `agents/openai.yaml` interface manifest that every Codex built-in carries. Closing the remaining gap is one installer change — teach `skills/install.sh` to link into the Codex skills root as well as Claude's, resolving both through config rather than hardcoding either — and it belongs in its own PR because it changes what a runtime auto-loads for *every* repo skill, not just this one.

Until that lands, do not assume a Codex-runtime agent has this skill loaded — put the invocation in a procedure that runtime already executes, exactly as **Firing without being asked** above requires for the same underlying reason.
