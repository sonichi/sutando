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
2. **Extract.** `python3 skills/session-recap/scripts/extract.py dump --session last --filter dialog --max-chars 0` → chronological `[ts] USER:/ASSISTANT:` stream. Add `--tail-bytes N` to bound the read to the transcript's last N bytes (boot recaps use 8388608; 0 = whole file for on-demand deep dives). `--filter user` for owner messages only (verbatim-quote lookups: just grep this). `--filter all` adds tool-call names + system lines when the recap needs to cover actions, not just conversation. `--max-chars 0` = no cap.
3. **Summarize with a CHEAP model** (owner requirement — transcripts run to tens of MB; never burn core-model quota on this). Spawn an Agent-tool subagent with `model: haiku`, hand it the dump (or the dump file path if huge — have it Read in slices), and ask for: timeline, tasks processed + outcomes, PRs/commits, decisions, errors + fixes, **artifacts (files/notes/memories written — the dump's `Write(path)`/`Edit(path)` tool lines carry the paths; owner requirement 2026-07-13)**, loose ends. Match the detail level the owner asked for. **Drop routine operational noise** (owner rule 2026-07-13): battery-escalation ladders, idle proactive-loop passes, quota checks, watcher restarts, memory syncs, health-check green runs — none of it belongs in a recap unless it materially changed the session's course (e.g. quota exhaustion forced a pivot, a crash lost work).
4. **Deliver** to the asking channel. For a verbatim-quote request, skip the subagent entirely — grep the `--filter user` dump and quote directly.

## Required summary structure (owner requirement 2026-07-13)

Any work-summary the recap produces — boot catchup, human brief, or an on-demand "summarize what I did" — MUST cover these sections (scale depth to the request, but never drop a section that applies):

1. **Executive summary** (lead with it): the **main initiative(s)**, the **high-level goal**, and the **important decisions — especially architecture/design decisions** (the *why*, not just the *what*). One-line status at the end.
2. **Detailed body**: per-item technical detail — PRs with what each did + why + hazards hit, decisions + rationale, errors + their fixes, **artifacts written** (files/notes/memories — from the transcript's `Write`/`Edit` tool lines), loose ends. When the owner asks for "more detail," expand this — specific findings, exact fixes, reproductions, CI/tooling mechanics — not just more headlines.
3. **Roadmap relationship** (when the work maps to `roadmap/ROADMAP.md`): which track/lane it advances and how (e.g. "closes Track-14 gap (a)"), plus a pointer to the relevant plan doc.
4. **Recommended next actions**: prioritized, naming the **owner-gated blockers with exact commands** (what unblocks the biggest thing first), then the follow-on work.

Save durable work-summaries under `<workspace>/notes/work-summaries/YYYY-MM-DD.md` (owner-created folder 2026-07-13), one file per summary, each opening with a `*[workflow, summary] — author | window | requested-by*` line.

## Automatic recap on restart (owner directive 2026-07-13)

**Primary consumer: the next session's agent** (owner 2026-07-13). The boot recap is how the fresh core catches up properly — deeper and less biased than relay notes (which are short, curated, and lost on crash-exits). The human-facing room post is the secondary product.

On each core boot, `/schedule-crons`' final step launches this skill twice-in-one — **as a background subagent, off the boot critical path** (2026-07-31; previous transcripts run 25–58 MB and an inline recap dominated the owner's ~4-minute cold start). The boot turn arms the watcher and ends; the recap lands in `state/last-session-recap.md` a minute or two later, and until then the fresh session catches up from `state/current-track.md` + the latest relay note:
1. **Agent catchup (the point):** generate a structured recap of the previous session tuned for the agent — open loops, in-flight PRs + their exact states, pending owner asks, decisions + their rationale, artifacts written, errors whose fixes are unverified. Write it to `<workspace>/state/last-session-recap.md` so the running session (and anything else) can read it when it lands. **Boot dumps are bounded: `--tail-bytes 8388608`** (8 MiB ≈ the last several hours of a busy session) — open loops live at the transcript's END, and an unbounded read of a 58 MB file is exactly the boot cost this bound removes. On-demand deep recaps may still pass `--tail-bytes 0`.
2. **Human brief:** if `recap_room` is set in `<workspace>/hosts/<hostname>/recap.json` (sibling of crons.json, which stays a bare job list) and a previous session transcript exists, produce a ~10-line recap of the previous session — what shipped, key decisions, owner asks left open, artifacts written; routine ops dropped — and post it to `recap_room` via gateway op:message. **Privacy (owner rule 2026-07-13): `recap_room` MUST be a private, owner-only room** — a recap can contain anything from a session (drafts, credentials context, private conversations). Never point it at a shared/team room; when in doubt, skip the post and leave the recap on disk under `data/session-recaps/`. Deep recaps stay on-demand.

**Single-flight + idempotence (the stamp alone is not enough).** The worker stamps `<workspace>/state/last-recap-session.txt` only at the END of its ~1–2 min background window, so a mid-session `/schedule-crons` re-run inside that window would see no stamp and double-launch. The launcher must therefore reserve the session atomically BEFORE spawning: `python3 skills/session-recap/scripts/recap_claim.py claim <previous-session-uuid>` — atomic-link claim on `state/recap-inflight.json`, exactly one winner among concurrent launchers. Exit 0 → the claim prints `claimed token=<nonce>`; pass the token into the worker's prompt and spawn it. Exit 1 → skip both the spawn and the post (already recapped, or a worker is in flight). The worker refreshes its lease with `recap_claim.py renew <uuid> --token <nonce>` after extraction, after summarization, immediately before writing `last-session-recap.md`, and again immediately before an optional private-room post. Any failed renew means the claim was reclaimed: stop without writing or posting. The worker finishes with `release <uuid> --token <nonce> --stamp` on success (atomic stamp write, then claim dropped) or without `--stamp` on failure so a later boot retries. Renew/release are ownership-checked, so a worker that stalls beyond the 15-minute lease cannot resume into either output side effect or disturb its successor. Crashed workers still become reclaimable instead of wedging future recaps.

## Notes

- Read-only over transcripts; never edit or move them.
- Transcript dir: `<workspace>/.claude-sutando/projects/<repo-slug>/*.jsonl` (the script resolves it).
- A session that spans compaction stays ONE file; a restart starts a new file — so file boundaries ARE session boundaries.
- Subagent (sidechain) transcripts can appear in the same dir as small files; the `list` table's message counts make them easy to spot and skip.
