# Session Recap

Reconstruct what happened in a past core session — from a high-level summary down to verbatim owner quotes — by reading the raw session transcripts (complete, crash-proof, unbiased), not the curated relay/handoff notes.

**Usage**: `/session-recap [last | <session-uuid-prefix> | list] [detail hint]`

## Why transcripts, not notes (owner design, 2026-07-13)

1. Relay/handoff notes are short and reflect my curation — bias by construction.
2. Notes need a graceful exit; the transcript JSONL is appended by the harness live, so accidental restarts lose nothing.
3. Only the transcript has *verbatim* detail ("what did the owner say exactly").

Notes remain useful as a fast index into a long session — nothing more.

## How to run it

1. **Bound the session.** `python3 skills/session-recap/scripts/extract.py list` prints recent sessions (newest first: file uuid, start/end ISO, message counts, first user line). Cross-check with `<workspace>/state/session-starts.log` (one JSONL line per core boot; consecutive entries bound a session). "last" = the second-newest transcript (newest = the running session).
2. **Extract.** `python3 skills/session-recap/scripts/extract.py dump --session last --filter dialog --max-chars 0` → chronological `[ts] USER:/ASSISTANT:` stream. `--filter user` for owner messages only (verbatim-quote lookups: just grep this). `--filter all` adds tool-call names + system lines when the recap needs to cover actions, not just conversation. `--max-chars 0` = no cap.
3. **Summarize with a CHEAP model** (owner requirement — transcripts run to tens of MB; never burn core-model quota on this). Spawn an Agent-tool subagent with `model: haiku`, hand it the dump (or the dump file path if huge — have it Read in slices), and ask for: timeline, tasks processed + outcomes, PRs/commits, decisions, errors + fixes, **artifacts (files/notes/memories written — the dump's `Write(path)`/`Edit(path)` tool lines carry the paths; owner requirement 2026-07-13)**, loose ends. Match the detail level the owner asked for.
4. **Deliver** to the asking channel. For a verbatim-quote request, skip the subagent entirely — grep the `--filter user` dump and quote directly.

## Notes

- Read-only over transcripts; never edit or move them.
- Transcript dir: `<workspace>/.claude-sutando/projects/<repo-slug>/*.jsonl` (the script resolves it).
- A session that spans compaction stays ONE file; a restart starts a new file — so file boundaries ARE session boundaries.
- Subagent (sidechain) transcripts can appear in the same dir as small files; the `list` table's message counts make them easy to spot and skip.
