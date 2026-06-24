# Slack bridge

Receive DMs + channel @mentions in Slack, processed through the same task
pipeline as voice / Discord / Telegram.

## One-time Slack app setup

1. **Create the app**: https://api.slack.com/apps → "Create New App" → "From scratch".
   Pick a name + a workspace.

2. **Socket Mode**: enable on the "Socket Mode" page. Generate an
   **App-Level Token** with scope **`connections:write`** (NOT
   `app_configurations:write` — that's a different scope and the daemon
   will crash on boot with `missing_scope` if you pick the wrong one).
   Copy the token (`xapp-...`).

3. **OAuth & Permissions**: under "Bot Token Scopes" (NOT "User Token
   Scopes" — leave that one empty), add `chat:write`, `im:history`,
   `im:write`, `app_mentions:read`, `channels:history`,
   `groups:history`, `files:read`, `files:write`, `users:read`.

4. **Event Subscriptions**: turn the **Enable Events** toggle ON at the
   top of the page. Leave the Request URL field blank — Socket Mode
   delivers events over the WebSocket, no public webhook needed. Under
   **Subscribe to bot events**, add `message.im` and `app_mention`.
   Click **Save Changes**.

5. **App Home → Messages Tab**: in the left sidebar pick **App Home**,
   scroll to **Show Tabs** → **Messages Tab**, and check
   **"Allow users to send Slash commands and messages from the messages
   tab"**. Without this, the bot's DM screen shows
   *"Sending messages to this app has been turned off"* and your DMs
   silently fail.

6. **Install to workspace**. Copy the Bot User OAuth Token (`xoxb-...`).

## Local config

Create `$CLAUDE_CONFIG_DIR/channels/slack/.env`:

```sh
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
```

Install the Python dep (only once):

```sh
pip3 install slack_bolt
```

## First run + TOFU onboarding

`src/startup.sh` picks up the env file and starts `src/slack-bridge.py`
automatically. On first DM after the bridge starts, the sender is
auto-enrolled as owner — same trust-on-first-use flow Telegram uses (see
`CLAUDE.md` → "Telegram access control"). The access list lives at
`$CLAUDE_CONFIG_DIR/channels/slack/access.json` (same resolution as the `.env` path above).

To allow additional senders later, add their Slack user IDs to `allowFrom`
in that file.

## How messages flow

| Slack event                | Goes to                                          |
|----------------------------|--------------------------------------------------|
| DM to the bot              | `tasks/task-{ts}.txt` with `source: slack`       |
| @mention in a channel      | `tasks/task-{ts}.txt`, replied in-thread         |

Results from `results/task-{ts}.txt` are posted back to the originating
channel. DMs get a top-level reply; @mentions get a threaded reply.

Protocol markers (`[no-send]`, `[REPLIED]`, `[deduped: ...]`) are honored
identically to the Telegram bridge — see `CLAUDE.md` → "Result-body protocol
markers".

## File attachments

Both directions work:

- **Inbound** — any files attached to a DM or @mention are downloaded into
  `<workspace>/slack-inbox/` and the local path is surfaced in the
  task body as `[File attached: /path]`. Slack file URLs require the bot
  token in an Authorization header (they're not public), so the bridge
  handles that internally.
- **Outbound** — result bodies may include `[file: /path]`, `[send: /path]`,
  or `[attach: /path]` markers. Paths are allowlist-gated via
  `_is_path_sendable()` (same `os.path.realpath` + `startswith` sanitizer
  the telegram / discord bridges use — fail-closed by default; allowed
  roots are `<workspace>/{results,notes,docs}` and `<workspace>/slack-inbox/`;
  allowed prefixes are `/tmp/sutando-*`, `/private/tmp/sutando-*`,
  `/tmp/echo-*`, `/private/tmp/echo-*`). Uploads go through `files_upload_v2`
  (the modern endpoint; `files.upload` is deprecated as of 2025).

> `<workspace>` resolves via `bash scripts/sutando-config.sh workspace`
> (M0 helper, PR #1395). Defaults to `<repo>/workspace/`; honors
> `$SUTANDO_WORKSPACE` as a legacy escape hatch internally.

## What's NOT supported

- Slash commands (`/sutando ...`).
- Voice notes (no public Huddle audio API).

See issue #866 for the v0 scope tracker.

## Stop / restart

```sh
pkill -f slack-bridge   # stop
bash src/startup.sh     # restart (and all other bridges)
```

Logs land in `logs/slack-bridge.log`.

## Install gotchas hit during real installs

The Slack API config UI has a few places where a sensible default
silently blocks the bridge. If your DMs aren't reaching the bridge,
check these in order:

1. **App-Level Token scope** — must be `connections:write`, NOT
   `app_configurations:write`. The token-create dialog defaults to the
   wrong one. The daemon crashes on boot with
   `slack_sdk.errors.SlackApiError: missing_scope` if you picked the
   other.

2. **Bot Token Scopes vs User Token Scopes** — OAuth & Permissions has
   two scope lists. Sutando runs entirely as a bot, so User Token
   Scopes stays empty.

3. **Event Subscriptions disabled** — the `Enable Events` toggle defaults
   to OFF. Even with Socket Mode running, no events flow until this is
   on. Bridge log will show only "Socket Mode connecting…" with no
   subsequent activity, and `$CLAUDE_CONFIG_DIR/channels/slack/access.json` will
   never be created.

4. **Save Changes button greyed out** — when Event Subscriptions has
   pending changes but Save is greyed, the form's Socket Mode state is
   stale. Hard-refresh the page (cmd-shift-R) and try again. If still
   greyed, type any placeholder URL into Request URL (e.g.
   `https://example.com/x`); Socket Mode overrides it after save.

5. **Messages Tab disabled in App Home** — see step 5 of setup above. If
   you forget this, DMs to the bot show "Sending messages to this app
   has been turned off" before the message even leaves Slack.

6. **Forgot to reinstall after scope changes** — Slack shows a yellow
   banner "Workspace needs to reinstall app" any time you add scopes.
   Click it and re-authorize, otherwise the new scopes don't take effect.
