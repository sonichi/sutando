# Known Issues

## Workspace contract — migration from `~/.sutando/workspace/`

The workspace defaults to `<repo>/workspace/` (in-repo). Configuration goes through [`sutando.config.local.json`](docs/workspace-config.md) — the loader resolves the path with a clear precedence order (config file > legacy env var > baked-in default).

**For existing users** with `SUTANDO_WORKSPACE` set in `.env` or shell init: the env var is **no longer honored for workspace resolution** as of v0.8 / #1440. It is still detected on startup to fire a one-time deprecation warning and trigger one-time auto-migration via per-source sentinels (PR #1478), but the resolver itself ignores its value. Remove the export from your shell init when convenient — see step 2 of the pre-migration operator checklist below — otherwise the deprecation banner keeps firing on every shell.

**For users with state in the old default location** (`~/.sutando/workspace/`): the migration script + skill is available now. Run `bash scripts/sutando-migrate.sh --dry-run` to preview, then `--commit` to relocate. Sources are preserved by default; cleanup via `--delete-source` after a 30-day grace window where readers fall back to the legacy location.

If you hit a path-resolution oddity, check the resolved workspace via `bash scripts/sutando-config.sh workspace` and report the file/line in an issue.

### Pre-migration operator checklist

If you're about to migrate a host with vault sync, walk through this once per host before running `bash scripts/sync-workspace.sh --init`. Each item has shipped guardrails in v0.3.0 (see [`docs/workspace-sync.md`](docs/workspace-sync.md) for the full pre-flight checklist), but the operator-side verification still applies:

1. **Set `vault.remote_url`** in `sutando.config.local.json` before first sync push. PR #1483 will refuse plain runs on an uninitialized workspace with a clear error, but the vault URL itself is config.
2. **Remove `SUTANDO_WORKSPACE`** from `~/.zshrc` / `~/.bash_profile`. PR #1478 stops the re-migrate-every-boot loop via per-source sentinels, but the residual deprecation banner will keep firing on every shell until the export is removed.
3. **Grep-confirm `.git/info/exclude`** blocks `.env`, `tasks/`, `results/`, and `state/cores/*.alive`. PR #1460 sets these defaults; per-host verification is still wise.
4. **Confirm `<workspace>/.sutando-vault/ws-id`** exists before the first push. PR #1459 / #1463 handle the wsId mechanics, but the file is the proof.
5. **Stagger host migrations serially**, not in parallel. Race condition on first-push to vault remains even with the wsId fix.
6. **After each host:** restart bridges + voice-agent + Sutando.app, then verify `health-check.py` reports M0 paths cleanly.
7. **Post-migration sanity check:** `cd <workspace> && git status` should be clean immediately after `core_heartbeat.py` writes a heartbeat (validates that the `.alive` excludes in `.git/info/exclude` are working).

### Identity-collision rule (per-bot Discord token)

Your bot's Discord token IS its identity. **Exactly one host at a time can hold a given token.** Copying `.env` to a new box while the old one still runs results in duplicate DMs; carrying it to neither leaves DMs unanswered. The same rule applies to Twilio creds and any other per-identity API key. When migrating to a new host, kill the bridges on the old host before bringing the new one up.

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

