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
3. **Summarize with a CHEAP model** (owner requirement — transcripts run to tens of MB; never burn core-model quota on this). Spawn an Agent-tool subagent with `model: haiku`, hand it the dump (or the dump file path if huge — have it Read in slices), and ask for: timeline, tasks processed + outcomes, PRs/commits, decisions, errors + fixes, **artifacts (files/notes/memories written — the dump's `Write(path)`/`Edit(path)` tool lines carry the paths; owner requirement 2026-07-13)**, loose ends. Match the detail level the owner asked for. **Drop routine operational noise** (owner rule 2026-07-13): battery-escalation ladders, idle proactive-loop passes, quota checks, watcher restarts, memory syncs, health-check green runs — none of it belongs in a recap unless it materially changed the session's course (e.g. quota exhaustion forced a pivot, a crash lost work).
4. **Deliver** to the asking channel. For a verbatim-quote request, skip the subagent entirely — grep the `--filter user` dump and quote directly.

## Automatic recap on restart (owner directive 2026-07-13)

**Primary consumer: the next session's agent** (owner 2026-07-13). The boot recap is how the fresh core catches up properly — deeper and less biased than relay notes (which are short, curated, and lost on crash-exits). The human-facing room post is the secondary product.

On each core boot, `/schedule-crons`' final step runs this skill twice-in-one:
1. **Agent catchup (the point):** generate a structured recap of the previous session tuned for the agent — open loops, in-flight PRs + their exact states, pending owner asks, decisions + their rationale, artifacts written, errors whose fixes are unverified. Generating it at boot puts it directly in the new session's context; also write it to `<workspace>/state/last-session-recap.md` so anything else can read it.
2. **Human brief:** if `recap_room` is set in `<workspace>/hosts/<hostname>/recap.json` (sibling of crons.json, which stays a bare job list) and a previous session transcript exists, produce a ~10-line recap of the previous session — what shipped, key decisions, owner asks left open, artifacts written; routine ops dropped — and post it to `recap_room` via gateway op:message. **Privacy (owner rule 2026-07-13): `recap_room` MUST be a private, owner-only room** — a recap can contain anything from a session (drafts, credentials context, private conversations). Never point it at a shared/team room; when in doubt, skip the post and leave the recap on disk under `data/session-recaps/`. Idempotence: stamp `<workspace>/state/last-recap-session.txt` with the recapped session uuid; skip if it already names that session (protects mid-session /schedule-crons re-runs from duplicate posts). Deep recaps stay on-demand.

## Notes

- Read-only over transcripts; never edit or move them.
- Transcript dir: `<workspace>/.claude-sutando/projects/<repo-slug>/*.jsonl` (the script resolves it).
- A session that spans compaction stays ONE file; a restart starts a new file — so file boundaries ARE session boundaries.
- Subagent (sidechain) transcripts can appear in the same dir as small files; the `list` table's message counts make them easy to spot and skip.
