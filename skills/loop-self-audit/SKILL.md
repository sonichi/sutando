# Loop Self-Audit

Periodic analysis of the proactive-loop's own **decision lines** (the `chose: <action> — category: <CAT> — reason: <text>` format introduced 2026-04-26), surfacing patterns the agent or owner should act on.

**Usage**: `/loop-self-audit` (manual), or invoked by `/proactive-loop` every N passes (default 20).

## Relationship to other skills

This skill is **scoped narrowly** to decision-line patterns. For broader state analysis, use the existing skills first:

- **`self-diagnose`** (`/self-diagnose [--since 24h]`) — reads logs + git + memory + build_log + pending-Qs + health-check + cold-review-log over a time window, produces structured narrative (what's happening / broken / next). Use for "what's the agent's state?" questions.
- **`regression-search`** — phone call history regression hunting. Use for "when did X stop working?".
- **`call-diagnostics`** — phone call observability + repair recommendations.

`loop-self-audit` is distinct: it ONLY parses decision lines from build_log to detect rule-2 (3-same-section) violations, idle-rate, and lazy-reasoning. Cheap (~1s), can run every 20 passes. Does NOT duplicate self-diagnose's broader scan.

The v2-v4 plan below extends what loop-self-audit reads, but stays scoped to **decision-pattern + freshness**. Anything broader (errors, regressions, narratives) belongs to self-diagnose.

## What it analyzes

Multiple log streams, each producing patterns the agent or owner should act on:

### v1 (shipped) — `build_log.md`
Parses `chose: <action> — category: <CAT> — reason: <text>` decision lines (introduced pass 2496):
1. **Category distribution** in window N (default 50 passes). Detect 3-same-section runs (rule 2 of `feedback_work_menu_enforcement.md`).
2. **Idle/wait rate.** Rate >20% is suspicious unless explicitly rationalized.
3. **Reason-string repetition.** Identical reasons across 3+ passes flag lazy reasoning.

### v2 (planned) — production reliability logs
4. **`logs/voice-agent.log`** — bodhi FATAL count per 10k lines, transport-1006/1007 errors, GoAway-after-CLOSED frequency. Voice reliability signal.
5. **`logs/discord-bridge.log` / `logs/telegram-bridge.log`** — message processing rate, error patterns, "[skip]" reasons. Bridge health beyond TCP-alive.
6. **`logs/conversation-server.log`** — phone call session patterns; pair with `results/calls/calls.jsonl` for outcomes.
7. **`data/voice-metrics.jsonl`** — voice session durations, turn counts, hangup reasons. Already used by daily-insight.

### v3 (planned) — meta state
8. **`pending-questions.md`** — open Q age + churn. Items unmoved for >7 days flag stale.
9. **`core-status.json`** — running/idle ratio (sample over time via build-log timestamps).
10. **MEMORY.md vs files-on-disk** — index drift (files unindexed; entries pointing to missing files).

### v4 (planned) — `notes/work-menu-state.md`
Cross-reference work-menu items and surface those whose `last-acted` is older than their `freshness` window.

## Output

Writes report to `notes/loop-self-audit-YYYY-MM-DD.md`. Anomalies (3-same / >20% idle / repeat reasons / FATAL spikes / stale menu items) → one-line summary to `results/proactive-loop-audit-{ts}.txt` for owner DM.

## How to invoke

```bash
bash skills/loop-self-audit/scripts/audit.sh [N]
```

Default `N=50`. Output to stdout AND to `notes/loop-self-audit-{YYYY-MM-DD}.md`.

## Threshold defaults

- `--3-same-section`: 3 passes from the same category in a row → anomaly
- `--idle-rate`: >20% (10/50) of recent passes idle/wait → anomaly
- `--repeat-reason`: same reason text across 3+ passes → anomaly
- `--stale-menu-item`: item with `last-acted` past `freshness` window → anomaly

Override via env vars or CLI flags later if needed; defaults are baseline.

## Why

The loop's pivot enforcement (`feedback_work_menu_enforcement.md`) is rule-based but unobservable in real time. This skill makes the rule's compliance grep-able + dated. Replaces "I noticed I was idling for 5h" (after-the-fact, owner-flagged) with "audit at pass 2520 fired a 3-same anomaly at pass 2515" (in-loop, agent-flagged).
