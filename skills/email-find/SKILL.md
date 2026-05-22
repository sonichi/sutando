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

3. **Expand sender to partners, not just the named entity.** When the user mentions a customer / vendor / collaborator by name, also search for known associated email domains. Operational replies often come from data-ops partners, contractors, or assistants — not the named principal contact. See `## Per-user partner-domain memory` below for how this is stored per user.

4. **Re-fetch threads in full.** `get_thread` may show only a subset of messages when called with `MINIMAL` format on a previously-summarized thread. If you've identified the candidate thread, fetch it again with `FULL_CONTENT` (or scan `MINIMAL` carefully — the message count should match what's in the Gmail web UI). Long threads (29+ messages) are particularly prone to truncation.

5. **Show the search trail.** End every "found it" or "still hunting" reply with the list of queries you tried, so the user can see what worked and what didn't.

## Workflow

In the queries below, `me` is Gmail's reserved keyword for the authenticated user's primary address — works for everyone regardless of which account is connected.

### Phase 1 — Broad scan

Run **one** broad query first to anchor on what's actually in the inbox in the relevant time window:

```
search_threads query="to:me newer_than:Nd" pageSize=15
```

Where `N` covers the window the user cited (default 2; cite-driven). Look at the actual returned threads — note senders, subjects, dates. Often the email is already in the top 10 results, just with a subject you wouldn't have guessed.

### Phase 2 — Expand sender domain

If Phase 1 didn't surface it, run **one query per partner domain** the user may have meant. Look up known partner domains for the named entity in `## Per-user partner-domain memory` below. For each, format:

```
search_threads query="from:DOMAIN OR from:NAMED-ADDRESS" pageSize=10
```

**If no partner-domain file exists yet**, skip this phase — proceed straight to Phase 3. (Phase 2 only adds value when you've accumulated mappings; the skill works fine without it.) Then, if Phases 3–4 surface an email from a domain you didn't expect for the named entity, follow the "first-time setup" flow in `## Per-user partner-domain memory` below to offer to create the file.

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

The mapping of "named entity → partner email domains" is per-user knowledge that lives in the user's memory directory, not in this skill file. The skill works without it (Phase 2 just becomes a no-op), but accumulating these mappings makes future searches much faster.

### Where the file lives

The agent should look in the user's memory directory:

1. **Read** the env var `SUTANDO_MEMORY_DIR`. If unset, fall back to the default `~/.claude/projects/<project-id>/memory/` (the same path the Sutando memory system uses — see Sutando's CLAUDE.md for the exact resolution).
2. **Look** for any file matching the patterns: `reference_partner_domains.md`, `partner_domains*.md`, or check whether `user_profile.md` has a `## Partner domains` section.
3. **If found**, parse the table and use it in Phase 2.
4. **If not found**, skip Phase 2 and proceed with Phases 3–4. Do not error out — the skill is fully functional without this file.

### First-time setup flow (offered, not required)

When the skill runs for the first time and the file doesn't exist, AND either:
- the agent surfaces an email from a domain the user didn't name (e.g. user asked for "K12 email" and the hit came from `soulilution.com`), OR
- the agent accumulates 2+ named-entity → domain mappings during a session,

then **at the end of the reply, offer to create the file** with a concrete one-line command, e.g.:

> Want me to save this mapping for next time? I can create `<resolved-memory-dir>/reference_partner_domains.md` with the {NamedEntity} → {discovered-domains} table already filled in. Reply "yes" or paste any additional entities + domains you want included.

If the user says yes, the agent writes the file using the format below and adds a one-line entry in the user's `MEMORY.md` index pointing at it (per the Sutando memory-system convention). The file is then read on every future `/email-find` invocation.

### File format

```markdown
---
name: partner-domains
description: Map of named entities (customers, vendors, collaborators) to associated email domains.
metadata:
  type: reference
---

| Named entity | Associated email domains |
|---|---|
| Acme Corp | `*@acmecorp.com`, `*@acme-data-ops.com`, `vendor.contact@example.com` |
| Foo Foundation | `*@foo.org`, `programs@foo.org`, `*@foo-partner.com` |
```

The agent should treat this format as a template — match whatever frontmatter / heading convention the user already uses elsewhere in their memory dir.

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

- Don't conclude "not found" before completing Phases 1–4.
- Don't subject-filter on the named entity in Phases 1–3 — the subject often doesn't carry that token.
- Don't trust a partial `get_thread` result. Long threads truncate.
- Don't ask the user for clarification before showing them the broad-scan list — they can usually spot the email in 10 seconds if you put it in front of them.
- Don't hardcode partner mappings in this skill. They live in user memory per `## Per-user partner-domain memory`.
