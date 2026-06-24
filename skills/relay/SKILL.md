---
name: relay
description: "Write a handoff/continuity note for the NEXT Sutando session. Captures what was just in flight, what to check first, what might go wrong, and implicit context the structured snapshot doesn't carry. Read first by /catchup-after-startup."
user-invocable: true
---

# Relay

Pass the baton to the next Sutando session. Where `session-handoff.sh` writes structured facts (recent commits, open PRs, pending Qs), `/relay` writes the **narrative continuity** — what you were just working on, what should be checked first, what might break and how to detect it.

**Usage**: `/relay` (writes a new relay file) or `/relay --append` (appends to the most recent unprocessed relay file rather than creating a new one).

## Why this exists

Catchup-after-startup pulls together 10 categories of structured state for the next session. But "I was about to land PR #X and Mini's review said Y matters most" isn't captured by `git log`, `gh pr list`, or `pending-questions.md` tail. The next session reads structured facts but has to RE-INFER the continuity, which costs context and frequently misses the load-bearing decision.

The relay note encodes intent + judgment — the thing only the LLM that lived through the session can write.

## Folder layout

Mirrors Sutando's existing `tasks/` and `results/` convention:

```
workspace/relay/
├── relay-{epoch_seconds}.md     # pending relay notes (read by next catchup, then archived)
└── processed/
    └── relay-{epoch_seconds}.md # already consumed by catchup; kept for audit
```

- **File naming:** `relay-{epoch}.md` — sortable + greppable, matches the `task-{epoch}.txt` shape.
- **Multiple files allowed:** `/relay` always creates a NEW file by default. `--append` appends to the LATEST unprocessed `relay-*.md` instead of creating a new one.
- **Consumption:** catchup-after-startup reads ALL unprocessed `relay-*.md` files in mtime order (oldest first), prints them as section 0 of its briefing, then `mv`s each one to `processed/` (mirroring the result-watcher drain pattern).
- **Cleanup:** kept indefinitely on local disk. Tiny files (~200-500 bytes each); a year of relay notes is < 1 MB. Sync via the workspace-sync engine (`scripts/sync-workspace.sh`) for fleet visibility — the legacy `sync-memory.sh` flow is deprecated in v0.3.0 and removed in v0.4.0.

## What to write

Write a narrative note (~150–300 words typical, no fixed schema) covering:

1. **What I was just working on** — the active PR / thread / decision in flight. Be specific. ("PR #1429 just merged; was about to start the relay skill PR.")
2. **What to check FIRST in the next session** — concrete first action. ("Pull origin; verify the relay skill SKILL.md lints clean; ping owner on quality-gate decision.")
3. **What might go wrong + recovery** — known failure modes + how to detect them. ("If catchup says 'no relay note found', the relay/ dir is probably orphaned; check `ls workspace/relay/`.")
4. **Implicit context** — the why-behind-the-what, decisions that haven't been committed yet, things you'd want a colleague to know if they walked in cold.

Don't write things that are already in the structured snapshot (recent commits, open PRs, pending-questions tail). Catchup will print those anyway. Relay's value is the things the structured snapshot can't reach.

## Steps

1. Resolve `WORKSPACE="$(bash scripts/sutando-config.sh workspace)"`.
2. `mkdir -p "$WORKSPACE/relay" "$WORKSPACE/relay/processed"` (idempotent).
3. **If `--append`:** find the latest unprocessed file via `ls -t "$WORKSPACE/relay/"relay-*.md 2>/dev/null | head -1`. If none, fall through to new-file mode. If found, append to it (with a `---` separator + timestamp header).
4. **Otherwise (new file):** generate filename `relay-$(date +%s).md` under `$WORKSPACE/relay/`.
5. Write a narrative note (markdown formatting) as described above.
6. Save to the resolved path.
7. Print "Relay note written to <path>" with the absolute path. Mention briefly what's in it.

## Quality bar

A good relay note is the difference between the next session starting at full speed vs. spending its first 5–10 minutes re-inferring state. Write something useful, not "I did stuff."

**Bad:** "Worked on some PRs. Ended session."

**Good:** "PR #1429 (import-UX) merged at 06:21Z; owner asked to start relay-skill PR next. Lucy's nit on stderr-parity landed pre-merge (commit 0ca0c89). Open thread: catchup PID-stamp variant — local edits applied to repo/skills/, NOT committed; owner is doing E2E test. If they greenlight, PR off staging-workspace-revamp with ~25-line diff in 2 SKILL.md files."

If the session was genuinely uneventful (read-only, no decisions, no in-flight work), say so explicitly: "No new work this session; previous relay note still valid." (Still write the file so catchup has a current heartbeat to read.)

## Phase 1 scope

- **Manual-invocation only.** Auto-refresh (writing/updating from `/proactive-loop`) deferred to Phase 2 once we see how owners actually use the manual path.
- **No quality-gate (refuse-on-thin-note).** Always write whatever the LLM produces. Quality-gate deferred to Phase 2 pending observation.
- **Read-side** lives in `/catchup-after-startup` — see that skill for the read-then-archive flow.

## Phase 2 ideas (NOT in scope)

- Auto-refresh from `/proactive-loop`: if `workspace/relay/relay-*.md` is mtime-stale (> 30 min) AND substantive work has happened since, write a fresh relay note as part of the loop.
- Quality-gate: refuse to write if the LLM can't list any of [active work / first action / known risk]. Could add `--skip` flag for genuine no-op sessions.
- Hook integration: PreCompact hook could prompt the LLM to write a relay note before compaction discards context.

## Where it lives

`workspace/relay/relay-{epoch}.md` (pending) + `workspace/relay/processed/relay-{epoch}.md` (consumed by catchup). Workspace-root parallel to `build_log.md` + `pending-questions.md` + `tasks/`. Owner-readable; both LLM and human can `cat` it cleanly.
