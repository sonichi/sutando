# Entities pass — haiku subagent prompt

One subagent (`model: haiku`) over ALL session summaries at the end of the run (on an incremental run: the previous `entities.json` plus the new session summaries). Sutando's People store and Rainmaker are entity-shaped, so this is what feeds them; finalize.py turns `people[]` into `people__upsert_person` payloads (≥ 2 citations, ≤ 25 per run).

---

You are extracting the durable entities from the owner's past Claude Code sessions.

Inputs (read every file with the Read tool):
- `<data-dir>/entities.json` — previous pass, if it exists; merge into it, never drop an existing citation
- `<data-dir>/summaries/<slug>/<uuid>.json` (… one line per non-personal session summary — the coordinator leaves out every summary whose `personal` is `true`; if one is listed anyway, cite nothing from it)

Output: write ONE JSON object with the Write tool to `<data-dir>/entities.json`. Reply `done` (or `STOP` if an input was missing). No prose, no code fences.

Rules
- Every entry carries `citations`: one per session that supports it, with the exact `project` slug and `session` uuid the fact came from and a ≤ 160-character `quote_or_context`. An entry with no citation is dropped.
- Merge duplicates: the same person under two spellings is one entry; keep the fullest name, all emails, all citations.
- People are real humans the owner dealt with or discussed (investors, colleagues, customers, collaborators) — not the assistant, not the owner, not authors of libraries. `relationship` is from the owner's point of view.
- `email` only when it is written in the summaries; never construct one.
- Companies, deals and decisions likewise only as stated; `open_threads` are unfinished items across projects, most important first, at most 30.
- Keep to what is durable: `decisions` are the ones that shaped a project (at most 40, most important first), `companies` only those the owner dealt with or discussed as a counterparty — not every product, library or vendor named in passing.
- Nothing that looks like a credential.

Schema (`entities.json`)
```json
{
  "generated_at": "ISO timestamp",
  "people": [
    {"name": "…", "email": "or empty", "company": "or empty", "role": "or empty",
     "relationship": "one line from the owner's side",
     "citations": [{"project": "<slug>", "session": "<uuid>", "quote_or_context": "…"}]}
  ],
  "companies": [
    {"name": "…", "what": "one line", "relationship": "customer | investor | partner | vendor | other",
     "citations": [{"project": "<slug>", "session": "<uuid>", "quote_or_context": "…"}]}
  ],
  "deals": [
    {"name": "…", "counterparty": "…", "stage": "as stated", "amount": "as stated or empty",
     "citations": [{"project": "<slug>", "session": "<uuid>", "quote_or_context": "…"}]}
  ],
  "decisions": [
    {"decision": "…", "why": "…", "project": "<slug>",
     "citations": [{"project": "<slug>", "session": "<uuid>", "quote_or_context": "…"}]}
  ],
  "open_threads": [
    {"thread": "…", "project": "<slug>", "owner_action": "what the owner still has to do",
     "citations": [{"project": "<slug>", "session": "<uuid>", "quote_or_context": "…"}]}
  ]
}
```
