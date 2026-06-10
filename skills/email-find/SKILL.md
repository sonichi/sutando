---
name: email-find
description: "Locate a specific email when the obvious searches fail. Use when the user is confident an email exists but a targeted query returned nothing."
user-invocable: true
---

# Email Find

A playbook for finding a specific email through the Gmail MCP (`claude.ai Gmail`) when the obvious search query returns nothing. Optimized for the case where the user describes an email and the agent must *not* give up easily.

**Usage**: `/email-find <description>`

ARGUMENTS: $ARGUMENTS

## Behavioral rules

1. **If the user is confident an email exists, the email exists.** Do not respond with "I can't find it" after one or two failed queries. The default failure mode is the agent's query, not the user's memory.

2. **Broad before narrow.** Always run at least one query that scans the full inbox by recency before narrowing on subject or sender keywords. A reply about Topic-X can land on a thread whose subject names a different topic, with zero topic-X tokens in the subject — a keyword filter throws those threads away.

3. **Expand sender to partners, not just the named entity.** When the user mentions a customer / vendor / collaborator by name, also search for known associated email domains. Operational replies often come from data-ops partners, contractors, or assistants — not the named principal contact.

4. **Re-fetch threads in full.** `get_thread` with `MINIMAL` format returns metadata + snippets for every message but omits the message **bodies**. If you've identified the candidate thread and need to read what was actually written, fetch it again with `FULL_CONTENT`. The search-result preview in the UI may also truncate long threads; `FULL_CONTENT` exposes everything.

5. **Show the search trail.** End every "found it" or "still hunting" reply with the list of queries you tried, so the user can see what worked and what didn't.

## Workflow

In the queries below, `me` is Gmail's reserved keyword for the authenticated user's primary address — works for everyone regardless of which account is connected.

### Phase 1 — Broad scan

Run **one** broad query first to anchor on what's actually in the inbox in the relevant time window:

```
search_threads query="(to:me OR from:me) newer_than:Nd" pageSize=15
```

Where `N` covers the window the user cited (default 2; cite-driven). The `(to:me OR from:me)` form covers both received and sent mail — stubborn lookups are sometimes for a message the user *sent* and can't refind. Look at the actual returned threads — note senders, subjects, dates. Often the email is already in the top 10 results, just with a subject you wouldn't have guessed.

### Phase 2 — Expand sender domain

If Phase 1 didn't surface it, run **one query per partner domain** the user may have meant. Look up known partner domains for the named entity in `## Per-user partner-domain memory` below. For each, format:

```
search_threads query="from:DOMAIN OR from:NAMED-ADDRESS" pageSize=10
```

`DOMAIN` here is the **bare domain** (e.g. `acmecorp.com`), not a wildcard like `*@acmecorp.com` — Gmail's `from:` operator matches any address at the bare domain but does not support `*@` wildcards on the user portion. If the memory file stores domains in `*@domain` form for readability, strip the `*@` prefix when building the query, otherwise Phase 2 silently no-ops.

If no partner-domain file exists yet, skip this phase and proceed to Phase 3. When Phases 3–4 later surface an email from an unexpected domain, auto-record the mapping per `## Per-user partner-domain memory` below.

### Phase 3 — Walk threads by participant

If Phase 2 didn't surface it, list threads where the named contact appears anywhere on the message (To/CC/BCC), not just as sender:

```
search_threads query="DOMAIN OR NAMED-ADDRESS"
```

Gmail q-syntax treats a bare email as an any-position match — covers `from:`, `to:`, and `cc:`.

### Phase 4 — Re-walk identified threads

If you've spotted a candidate thread (e.g. one whose subject matches a related topic, or whose participants overlap), fetch it again with **`messageFormat: "FULL_CONTENT"`** and scan ALL messages, not just the snippet on the search hit. The actual answer is often inside a thread you've already seen but truncated.

### Phase 5 — Ask only after Phases 1–4

If all four phases came up empty, *then* ask the user for one of:
- Exact sender email
- Exact subject line or a short snippet
- Approximate timestamp (UTC or local — both work)

When you ask, include the actual list of senders + subjects you saw, so the user can spot the email and tell you which one.

## Per-user partner-domain memory

Named-entity → partner-domain mappings live in the user's memory directory, not in this skill.

### File location

Resolve `SUTANDO_MEMORY_DIR` (default: `~/.claude/projects/<project-id>/memory/`). Look for `reference_partner_domains.md`, `partner_domains*.md`, or a `## Partner domains` section in `user_profile.md`. If none exist, the skill remains fully functional — Phase 2 just no-ops.

### Auto-learn

When Phases 3–4 surface an email from a domain the user didn't name for that entity, append the mapping to the partner-domains file silently. Do not ask for confirmation; do not narrate the save in the reply. If the file doesn't exist, create it on first discovery. The cost of an unhelpful row is one extra query in a future fanout; the cost of asking is friction every time.

### File format

```markdown
---
name: partner-domains
description: Named entities → associated email domains. Auto-maintained by /email-find.
metadata:
  type: reference
---

| Named entity | Associated email domains |
|---|---|
| Acme Corp | `acmecorp.com`, `acme-data-ops.com` |
| Foo Foundation | `foo.org`, `programs@foo.org` |
```

Store **bare domains** (e.g. `acmecorp.com`) — Phase 2 uses them verbatim in `from:DOMAIN`. Specific addresses (e.g. `programs@foo.org`) are fine alongside bare domains. No timestamp bookkeeping: a stale row only costs one extra Phase-2 query in fanout, and hand-maintained `first_seen` / `last_useful` / `pruned_at` would be applied inconsistently turn-to-turn — heavy protocol for low payoff. Match whatever frontmatter convention the user already uses elsewhere in their memory dir.

## Subject-mismatch heuristic (no subject filtering in Phases 1–3)

A reply about Topic-X frequently rides on an existing operational thread whose subject is about something entirely different. The most common cases:

- A topic that started as a complaint/incident keeps using the incident's subject for all subsequent replies, even months later.
- A customer's data-ops team replies to whatever was the *first* email in the relationship, ignoring topic shifts.
- A forwarded thread (`Fwd: Fwd: ...`) carries the original subject forever.

**Implication: never subject-filter on the named entity in Phases 1–3.** Subject keywords go in Phase 5 only, after the user provides them. Trust sender / recipient / date scoping; let the subjects be whatever Gmail kept on the thread.

## Reporting

After running the workflow, reply with:

1. The candidate email's sender, subject, timestamp, and attachment list
2. Which phase found it (1–4)
3. What queries were run in each phase (one line each)
4. If Phase 4 was used, which thread was re-walked

If nothing was found after Phase 4, reply with:

1. The 10 most recent emails in the cited time window (sender + subject + timestamp)
2. All queries tried
3. A direct request for clarification on one of: sender, subject, timestamp

## Don't

- Conclude "not found" before completing Phases 1–4.
- Show clarification questions before showing the broad-scan list.
- Hardcode partner mappings in this skill — they live in user memory.
