# Known Issues

## Workspace contract migration in progress

The 3-space workspace contract (Code / Workspace / Memory — see [`docs/workspace-design.md`](docs/workspace-design.md)) is in the middle of a transition. Some existing scripts and skills still write per-user state into the repo (`<repo>/tasks/`, `<repo>/results/`, `<repo>/state/`, `<repo>/logs/`, `<repo>/data/`, etc.) instead of `$SUTANDO_WORKSPACE/`.

**Effect for users**: depending on how a script was invoked, runtime state may end up in either location. On a workspace-pinned install, state written to the repo path is invisible to readers that look in `$SUTANDO_WORKSPACE`. Symptoms include sentinels that never trigger, scans that miss live data, or reply-context that doesn't reach the bot.

**Status**: A migration CLI (`scripts/sutando-migrate.sh`) is queued in PR #1271 to move existing repo-anchored state into the workspace. Per-site fixes for individual scripts are tracked in PRs #1272–#1334 (filed but held in draft until the V1 contract is finalized). The path-resolution audit (`workspace-contract-audit` skill) catches new violations at PR time.

If you hit a path-resolution oddity, check whether your script is reading from the repo vs `$SUTANDO_WORKSPACE` and report the file/line in an issue.

## Task status flickers in web UI after API restart

**Symptom:** Tasks briefly show as "working" then "done" then "working" again in the web client task list after the agent API is restarted.

**Cause:** The agent API stores task history in memory. Restarting it wipes the history, so it rebuilds state from disk on the next poll. If result files were cleaned up before the restart, those tasks lose their "done" status.

**Workaround:** Wait ~5 minutes — the reconciliation logic cleans up stale entries automatically. Or refresh the page after the API stabilizes.

**Status:** By design. Persisting task history to disk would fix this but adds complexity for a rare event.

## Voice agent (Gemini) hallucinates more than Claude Code

The voice/phone agent uses Gemini Live, which hallucinates more than Claude Code — it may say "done" without actually doing the task, or fabricate details instead of looking them up.

## Gemini Live idle timeout (~15 minutes)

**Symptom:** Voice connection drops after ~15 minutes of silence. The web client shows "Connection lost — reconnecting."

**Cause:** Gemini Live sessions have an inactivity timeout. If no audio is sent for ~15 minutes, Google closes the WebSocket.

**Workaround:** The voice agent auto-reconnects when the client reconnects. Click "Start Voice" again or wait for auto-reconnect (3 seconds).

**Status:** Expected behavior from Gemini Live API. The voice agent detects dead sessions and triggers reconnect automatically.

