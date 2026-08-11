# Task bridge — extended details

Extended reference for `CLAUDE.md` § Task bridge, § Chat-path task tracking,
and § Task progress notifications. The rules inline in `CLAUDE.md` are
authoritative; this file carries the mechanics, schemas, and incident history
behind them.

## Per-channel pull namespace — why the scoped shape is invisible to legacy consumers

Existing consumers (`discord-bridge.py`, `telegram-bridge.py`,
`slack-bridge.py`, `task-bridge.ts`, `agent-api.py`) all key off the legacy
`task-{id}.txt` shape — specific tracked task_id or `task-*` glob — so a
`<key>.task-{id}.txt` filename slides past them. The matching scan inside
`skills/phone-conversation/scripts/conversation-server.ts` reads-and-deletes
the file, then injects its body into the live Gemini session via the same
`transport.sendContent` path the work-tool result drain uses. Helper:
`src/result-channel-key.ts` (TS) / `src/result_channel_key.py` (Python).

The per-consumer prefix is code-enforced (single helper, single source of
truth) so cross-consumer namespace collisions are impossible regardless of
what ID format a future consumer adopts — which is why both the writer and
the scanning consumer must go through the typed key constructor.

## Voice session context — JSON schema

`state/voice-session-context.json` (see `CLAUDE.md` § Task bridge → "Voice
session context" for when to write it):

```json
{
  "updated_at": "<ISO ts>",
  "active_drafts": [{"name": "...", "summary": "...", "path": "..."}],
  "pending_action": {"kind": "paste|review|other", "what": "...", "where": "..."} | null,
  "last_results": [{"task_id": "...", "subject": "...", "ts": "..."}]
}
```

Keep `active_drafts` and `last_results` to ~3 entries each (drop oldest).
Voice forgets specifics like "the post" or "Mini Draft A" that landed earlier
in the session; it calls the `recent_context` tool to read this file when it
senses confusion ("what was the post?" / "what's pending?"). Per Chi
2026-05-13.

## Guest-core incident (2026-07-11)

Why guest automations must never write task/result/liveness files: a Codex
automation with `cwds=[this repo]` auto-loaded AGENTS.md and self-wrote a
`task-chat` every 10 min; the core swallowed each one as a real owner request.
Fix: run such automations in an isolated `/private/tmp` worktree with no repo
cwd, per the safe pattern.

## Watcher history

The stream watcher (`src/watch-tasks-stream.sh`, driven via the `Monitor`
tool) replaces the older one-shot `watch-tasks.sh` (retired 2026-05-14) — no
more restart-on-event cycles. It emits `TASK_FILE: <name>` per new task as a
per-event notification.

## Assorted notes

- Chat-path task files keep the dashboard, result-watcher, and timeout logic
  working the same regardless of entry path — that is why chat commitments
  get a task file at all.
- Cancel confirm results: e.g. `"Cancelled task-X (was in progress)"` or
  `"task-X already completed, nothing to cancel"`. The CANCEL_INSTRUCTION
  task uses the regular task pipeline as its signal channel.
- Task-progress checkpoint updates: e.g. "Done with the research — writing
  up now." The point of notify-first is that the user should never see
  silence followed by a result minutes later.
