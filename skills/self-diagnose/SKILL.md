---
name: self-diagnose
description: "Sutando introspection — read logs + git + memory + build log for a chosen time window and produce a concise narrative of what the agent has been doing, what's broken, and what to prioritize next."
user-invocable: true
---

# Self-Diagnose

Read Sutando's own observable state (logs, git, memory, build log, pending questions, health check, cold-review log) over a chosen window and produce a structured narrative:

- **What's been happening** — significant events, PRs shipped, tasks processed, user interactions
- **What's broken** — errors in logs, pending questions, health-check failures, 1006/1011/1007 transport events, unresolved bugs surfaced in logs
- **What I'd do next** — concrete prioritized actions grounded in what was observed

**Usage**: `/self-diagnose [--since 24h]`

ARGUMENTS: `$ARGUMENTS`

## Flow

1. **Gather.** Run `bash skills/self-diagnose/scripts/gather.sh [window]` — collects log tails, git log, build_log.md tail, pending-questions, health-check output, cold-review-log, and recent Discord activity into `/tmp/sutando-diagnose-<ts>/`.
2. **Synthesize.** Read each gathered file. Group findings under the three headings above. Be concrete: cite log lines, PR numbers, timestamps. Skip speculation.
3. **Save.** Write the report to `notes/diagnose-YYYY-MM-DD-HHMM.md` with frontmatter (`title`, `date`, `tags: [diagnose, self]`).
4. **Summarize.** Reply to the caller with a 5–10 line executive summary + path to the full report.

## Default window

24 hours if unspecified. Accept: `24h`, `3d`, `1w`. Longer windows = broader scope, higher token cost.

## What a good report looks like

- Cites specific log timestamps (`18:28:16 — transport 1006 after NoteView injection`) rather than "some errors happened"
- Lists PRs by number + status (`#394 mergeable no reviews`, `#354 retention sweep open`)
- Separates recurring from one-off issues
- Prioritizes "what I'd do next" by impact and blocking, not by what's most recent

## What to skip

- Verbose log dumps — the gathered files are on disk already; just reference them
- Speculation without log evidence — "maybe the Gemini server is..." should be backed by a log line showing the close code and preceding event
- Restating CLAUDE.md / build_log.md content — those are already durable; only surface what's NEW in the window

## Related

- `notes/cold-review-capability.md` — sister capability for PR-level review
- `notes/voice-transport-1006-hypothesis.md` — example of the depth of analysis a self-diagnose report should match
- `build_log.md` — the canonical "what has been built" log; complements but doesn't replace
