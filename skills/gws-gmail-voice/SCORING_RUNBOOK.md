# Inbox importance-scoring runbook

This runbook is the version-controlled source of truth for the LLM-driven inbox-importance scorer. The cron prompt (`score-inbox-llm` in `skills/schedule-crons/crons.json`, gitignored per-machine) is a thin wrapper that says: *"Run the procedure in `skills/gws-gmail-voice/SCORING_RUNBOOK.md`"*.

When a new ACT pass fires this procedure (every 15 min at minute 4/19/34/49), it executes the steps below. The result is `state/external-cache/inbox-important.json` consumed by the `triage_email` voice tool (cache-first 3-tier path).

## Why LLM (not rules)

Hardcoded weights can't see context:
- Active threads (which PR is Chi mid-merge on?)
- Calendar (is the meeting in the email already past?)
- Owner intent (did Chi just ask "find me a flight" 10 minutes ago?)
- Cross-source corroboration (a "deadline" subject from a known-collaborator outranks the same subject from a stranger)

The LLM judges importance with full grounding. Rules retired in PR #705 (per Chi 2026-05-14).

## Per-pass procedure (incremental)

1. **Read existing cache** at `state/external-cache/inbox-important.json`. If absent, treat `scored_emails` as `{}`.

2. **Fetch current unread** via `gws gmail +triage --format json --max 200`. (The full unread queue is ~200 messages; the runbook's incremental algorithm — see step 5 — means each one gets scored ONCE then reused via `scored_at` TTL, so wider net = same steady-state cost. The prior `--max 30` was caught missing buried-important items like calendar invitations and academic follow-ups several days into the queue.)

3. **Diff** against cached:
   - `new_ids = current_ids - cached_ids` (need scoring)
   - `gone_ids = cached_ids - current_ids` (drop — user read/archived/deleted)
   - `unchanged_ids = current_ids ∩ cached_ids` (reuse if fresh, re-score if stale)

4. **Read grounding context** (only if `new_ids` non-empty OR any unchanged entry is stale per step 5):
   - **Calendar:** `gws calendar +agenda --days 2` — for past-meeting detection and upcoming events
   - **Recent commits:** `git log --oneline -10` — active code threads
   - **Owner intent:** `ls -t tasks/archive/2026-*/* | head -10 | xargs head` — recent owner asks
   - **Discord-bridge log tail:** `tail -50 logs/discord-bridge.log` — what threads are active right now

5. **For each `new_id`** (and each unchanged entry where `scored_at` is older than 24h): use LLM judgment to assign:
   - `importance`: `"high"` | `"medium"` | `"low"`
   - `rationale`: 1-line reason grounded in context (e.g. "CI failure on PR you just merged tonight" / "main collaborator's sync transcript" / "newsletter, not in active threads")
   - `scored_at`: current ISO timestamp

   **For each unchanged entry with `scored_at` within 24h**: REUSE existing score. Skip LLM call.

   **Context-shift override (optional but recommended):** if `tasks/` got fresh owner intent within the last hour that's materially different from the prior `context_snapshot.recent_owner_intent` (e.g. owner switched from gws-gmail-voice work to talk prep), re-score even unchanged entries.

6. **Pick `top_3_ids`** by importance ordering: high > medium > low; tie-break by recency (newer first; gws returns newest-first so iterate the current scan in order).

7. **Atomic-write** to `state/external-cache/inbox-important.json` via tmp+rename. Schema below.

## Cache schema (v2)

```json
{
  "version": 2,
  "ts": "<ISO>",
  "last_scored_run_at": "<ISO>",
  "scorer": "llm-act-pass",
  "context_snapshot": {
    "calendar_today_tomorrow": "<short summary>",
    "recent_owner_intent": "<recent ~1h tasks/ summary>",
    "active_threads": ["<thread1>", "<thread2>"]
  },
  "scored_emails": {
    "<gws_message_id>": {
      "from": "<sender>",
      "subject": "<subject>",
      "importance": "high|medium|low",
      "rationale": "<1-line reason>",
      "scored_at": "<ISO>"
    }
  },
  "top_3_ids": ["<id>", "<id>", "<id>"],
  "top_3_important": [/* full message objects — BACKWARD COMPAT for voice tool */],
  "all_unread_count": <int>,
  "query": "is:unread"
}
```

**Field semantics:**
- `version`: schema version. Bump when changing any required field. Voice tool can gate on this to handle legacy caches.
- `ts`: when this cache was written (per-pass).
- `last_scored_run_at`: when the last ACT pass actually ran scoring (distinct from `ts` if a pass wrote cache without scoring — currently they're equal but separating now makes future "did the cron fire?" diagnostics easy).
- `scored_emails`: per-message persistent state for incremental scoring + eviction lifecycle.
- `top_3_ids`: pointer ordering — voice tool can resolve to full objects via `scored_emails[id]`.
- `top_3_important`: full message objects with `rationale` field appended. **Backward compat — do not remove without coordinating with voice tool changes.**

## Eviction lifecycle

| Event | What happens |
|---|---|
| New email in `is:unread` not seen before | Score + add to `scored_emails`. |
| Same email still unread, `scored_at < 24h` | Reuse (skip LLM call). |
| Same email still unread, `scored_at > 24h` | Re-score (catches context shifts). |
| Email not in current `is:unread` (read/archived/deleted) | Drop from `scored_emails`. |
| Whole cache file `ts > 7d` | Discard cache entirely; full re-score. |

## Voice tool contract

`skills/gws-gmail-voice/tools.ts` reads `top_3_important` array and returns it as the tool result. **Do not change `top_3_important` schema without updating the voice tool in the same PR.** Per-message `rationale` field was added in v2; voice agent (Gemini) can include it in the spoken summary.

## Failure modes

- **Cron didn't fire** (stale cache): voice tool's 3-tier path falls through to live `gws` (no importance ranking, but functional). Same degradation as before this whole system existed. ✅
- **LLM judgment fails mid-pass**: write what scored OK so far; flag remainder as `importance: "low"` + `rationale: "scoring failed mid-pass"`; next pass retries.
- **gws unavailable**: write cache with `top_3_important: []` + `error` field; voice tool falls through to live (which will also fail, then to work-tool fallback per tool description).

## Iteration log

- v1 — 2026-05-14 (PR #705) — initial runbook. Replaces rule-based `refresh-cache.py` from PR #704. Per Mini PR #705 review: cron prompt is gitignored → not version-controlled → drift risk; this runbook is the SoT.
