---
name: email-find
description: "Locate a specific email when the obvious searches fail. Use when the owner is confident an email exists but a targeted query returned nothing."
user-invocable: true
---

# Email Find

A playbook for finding a specific email through the Gmail MCP (`claude.ai Gmail`) when the obvious search query returns nothing. Optimized for the case the owner mentions a description and the agent must *not* give up easily.

**Usage**: `/email-find <description>`

ARGUMENTS: $ARGUMENTS

## Behavioral rules

1. **If the owner is confident an email exists, the email exists.** Do not respond with "I can't find it" after one or two failed queries. The default failure mode is the agent's query, not the user's memory.

2. **Broad before narrow.** Always run at least one query that scans the full inbox by recency before narrowing on subject or sender keywords. A reply about K-12 can land on a thread whose subject is `Re: Fwd: Highland Fleets - Contact Data` with zero K-12 / K12 tokens in it — a subject-keyword filter throws those threads away.

3. **Expand sender to partners, not just the named entity.** When the owner mentions a customer or vendor by name, also search for known partner domains. Most operational replies come from data-ops partners, not the named principal contact. The current map lives in `## Partner domain map` below. Add to it as new partnerships surface.

4. **Re-fetch threads in full.** `get_thread` may show only a subset of messages when called with `MINIMAL` format on a previously-summarized thread. If you've identified the candidate thread, fetch it again with `FULL_CONTENT` (or scan `MINIMAL` carefully — message count should match what you see in the Gmail UI). Long threads (29+ messages) are particularly prone to truncation.

5. **Show the search trail.** End every "found it" or "still hunting" reply with the list of queries you tried, so the owner can see what worked and what didn't.

## Workflow

### Phase 1 — Broad scan

Run **one** broad query first to anchor on what's actually in the inbox in the relevant time window:

```
search_threads query="to:vasiliy@ag2.ai newer_than:Nd" pageSize=15
```

Where `N` covers the window the owner cited (default 2; cite-driven). Look at the actual returned threads — note the senders, subjects, dates. Often the email is already in the top 10 results, just with a subject you wouldn't have guessed.

### Phase 2 — Expand sender domain

If Phase 1 didn't surface it, run **one query per partner domain** the owner may have meant. Use the partner-domain map below. For each, format:

```
search_threads query="from:DOMAIN OR from:NAMED-ADDRESS" pageSize=10
```

### Phase 3 — Walk threads by participant

If Phase 2 didn't surface it, list threads where the named contact is in the recipient list (To/CC/BCC), not just the sender:

```
search_threads query="DOMAIN OR NAMED-ADDRESS"
```

(`to:`, `from:`, `cc:` all match the relationship — Gmail q-syntax treats a bare email as any-position match.)

### Phase 4 — Re-walk identified threads

If you've spotted a candidate thread (e.g. one whose subject matches a related topic, or whose participants overlap), fetch it again with **`messageFormat: "FULL_CONTENT"`** and scan ALL messages, not just the snippet on the search hit. The actual answer is often inside a thread you've already seen but truncated.

### Phase 5 — Ask only after Phases 1–4

If all four phases came up empty, *then* ask the owner for one of:
- Exact sender email
- Exact subject line or a short snippet
- Approximate timestamp (UTC or local — both work)

When you ask, include the actual list of senders + subjects you saw, so the owner can spot the email and tell you which one.

## Partner domain map

This map grows as new partnerships surface. When the owner mentions a relationship by name, expand to all known related domains:

| Named entity / customer | Partner / data-ops domain(s) |
|---|---|
| K12 / K-12 / K12-data | `data.ops@soulilution.com`, `maneet@soulilution.com`, `*@soulilution.com`, `Charlie@k12-data.com`, `*@k12-data.com` |
| Highland Fleets (K12 sub-order) | same as K12 |
| Nokia | `*@nokia.com` |
| DNAnexus | `*@dnanexus.com` |

When adding a row, also append it to `feedback_gmail_search_assumptions.md` so the next session inherits the knowledge.

## Subject-mismatch examples (do NOT subject-filter)

Real cases seen on this account where the obvious subject filter would have hidden the email:

- K12 requirements reply → subject was `Re: Fwd: Highland Fleets - Contact Data` (no K12/K-12 token)
- Civics batch 3 delivery → subject was `Re: Fwd: Highland Fleets - Contact Data` (same long thread)

When the topic has a recent operational history with a customer, **expect the reply to ride on the historical thread, not a new subject.**

## Reporting

After running the workflow, reply with:

1. The candidate email's sender, subject, timestamp, attachment list
2. Which phase found it (1–4)
3. What queries were run in each phase (one line each)
4. If Phase 4 was used, which thread was re-walked

If nothing was found after Phase 4, reply with:

1. The 10 most recent emails in the cited time window (sender + subject + timestamp)
2. All queries tried
3. A direct request for clarification on one of: sender, subject, timestamp

## Don't

- Don't conclude "not found" before completing Phases 1–4.
- Don't subject-filter on the named entity (K12, Nokia, etc.) in Phases 1–3 — the subject often doesn't carry that token.
- Don't trust a partial `get_thread` result. Long threads truncate.
- Don't ask the owner for clarification before showing them the broad-scan list — they can usually spot the email in 10 seconds if you put it in front of them.
