# Project roll-up — haiku subagent prompt

One subagent per project (`model: haiku`), after every session of that project has its `summaries/<slug>/<uuid>.json`. It reads the session JSONs itself and writes the roll-up plus the note text; finalize.py turns the roll-up into `notes/claude-import/<slug>.md` and the memory line.

---

You are writing the project roll-up of the owner's past Claude Code sessions for their Sutando assistant.

Project slug: `<slug>` (working dir `<cwd>`, <N> sessions, <first date> → <last date>)
Session summaries (read every file with the Read tool — only the sessions listed here; the coordinator lists the non-personal ones and leaves out every summary whose `personal` is `true`):
- `<data-dir>/summaries/<slug>/<uuid>.json`  (… one line per non-personal session, oldest first)

Run kind: `<full | incremental>`. On an incremental run only the listed sessions are new; `<data-dir>/projects/<slug>.json` holds the previous roll-up — read it first and carry its facts forward, updating `status`, `top_open_thread` and `open_threads` to the newest state.

Output: write ONE JSON object with the Write tool to `<data-dir>/projects/<slug>.json`. Reply `done` (or `STOP` if an input file was missing). No prose, no code fences.

Rules
- Only facts present in the session summaries. Empty rather than invented.
- A session flagged `personal: true` is not part of this project's story: if one slipped into the list, leave it out of the narrative, threads, decisions, people and companies, and out of `sessions`. `sessions` lists exactly the session uuids this roll-up was written from — finalize.py uses it to tell whether the roll-up must be re-run.
- `name` is the short human name of the project (from the working dir or how the owner refers to it), not the slug.
- `what_it_is` is one line (≤ 90 characters) a stranger can read; `top_open_thread` is the single most important unfinished thing (≤ 90 characters).
- `status` is one of `active`, `paused`, `done`, `unknown` — active if the last session's work was still in flight.
- Lists: at most 20 items each, 200 characters per item, most important first — a roll-up condenses; it does not re-list every session's items.
- `note_markdown` is a readable note for a human, 200–600 words, in this order: what the project is, what was accomplished (with PR/commit refs as written), key decisions and why, open threads, people and companies involved. Plain markdown, no top-level `#` heading (finalize adds it), no secrets.

Schema (`projects/<slug>.json`)
```json
{
  "project": "<slug>",
  "name": "short human name",
  "cwd": "<cwd>",
  "what_it_is": "one line",
  "status": "active | paused | done | unknown",
  "summary": "3–6 sentences across all sessions",
  "top_open_thread": "one line, or empty",
  "open_threads": ["…"],
  "key_decisions": [{"decision": "…", "why": "…"}],
  "accomplished": ["…"],
  "prs_commits": [{"ref": "…", "what": "…"}],
  "people": [{"name": "…", "role_or_relationship": "…"}],
  "companies": ["…"],
  "sessions": ["<uuid>", "…"],
  "note_markdown": "the note body described above"
}
```
