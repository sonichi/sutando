# Session summary — haiku subagent prompt

Fill in the `<...>` fields, hand this to an Agent-tool subagent with `model: haiku`, and let it read and write the files itself. Do not paste dump text into the prompt.

---

You are summarising ONE past Claude Code session for the owner's Sutando assistant.

Input: the chunk file(s) below — a chronological `[timestamp] USER:` / `[timestamp] ASSISTANT:` stream containing conversation text only. Tool output, attachments and thinking were dropped before you got it; secrets are already redacted as `[STORED-IN-KEYCHAIN-…]`. Keep those placeholders verbatim and never guess what they were.

Project slug: `<slug>` (working dir `<cwd>`)
Session: `<uuid>` — title from the index: "<title>"
Chunk files (read every one, in order, with the Read tool; slice large files):
- `<data-dir>/dumps/<slug>/<uuid>.1.txt`
- `<data-dir>/dumps/<slug>/<uuid>.2.txt`  (… one line per chunk)

Output: write ONE JSON object with the Write tool to
`<data-dir>/summaries/<slug>/<uuid>.json` (create the directory if needed). Reply to the caller with the single word `done`, plus `STOP` if a chunk file was missing or unreadable. Nothing else — no prose, no code fences.

Rules
- Facts only from the dump. If something is not in the text, leave the field empty — never invent names, numbers, PR ids, dates or outcomes.
- A USER turn that starts "This session is being continued from a previous conversation" is Claude Code's own compaction summary of earlier work in the same session; treat it as reliable history.
- Drop routine operational noise (health checks, quota checks, watcher restarts, memory syncs, idle loop passes) unless it changed the session's course.
- Short strings: at most 200 characters per item, at most 25 items per list. `summary` is 2–4 sentences.
- Never write anything that looks like a credential, token or password, even if the dump contains one.
- Use the exact schema below; unknown things are `""`, `[]` or `null`, not made-up values.

Schema (`summaries/<slug>/<uuid>.json`)
```json
{
  "session": "<uuid>",
  "project": "<slug>",
  "title": "one line — what this session was about",
  "date_range": ["first timestamp seen", "last timestamp seen"],
  "summary": "2–4 sentences: what the owner was doing, what came out of it, where it ended",
  "timeline": [{"when": "timestamp or empty", "what": "one line"}],
  "tasks": [{"task": "what was attempted", "outcome": "done | partial | abandoned | unknown", "detail": "one line"}],
  "prs_commits": [{"ref": "#123, a sha, or a URL exactly as written", "what": "one line"}],
  "decisions": [{"decision": "what was decided", "why": "the stated reason"}],
  "errors_fixes": [{"error": "what broke", "fix": "what fixed it", "verified": true}],
  "artifacts": ["files, notes, documents or memories the session wrote, by path or name"],
  "loose_ends": ["open questions, unfinished work, promised follow-ups"],
  "people": [{"name": "as written", "role_or_relationship": "e.g. investor, colleague, customer", "context": "one line", "email": "only if written in the text, else empty"}],
  "companies": [{"name": "as written", "context": "one line"}]
}
```

Multi-chunk sessions (map-reduce): when the session has more than one chunk file, first write one partial per chunk to `summaries/<slug>/<uuid>.<n>.json` (same schema, `n` = the chunk number), then merge the partials into `summaries/<slug>/<uuid>.json`: timeline in order, lists de-duplicated, one combined `summary`. Only the merged file is read downstream.
