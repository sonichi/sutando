# Slack bridge

Receive DMs + channel @mentions in Slack, processed through the same task
pipeline as voice / Discord / Telegram.

## One-time Slack app setup

1. **Create the app**: https://api.slack.com/apps → "Create New App" → "From scratch".
   Pick a name + a workspace.

2. **Socket Mode**: enable on the "Socket Mode" page. Generate an App-level
   token with scope `connections:write`. Copy it (`xapp-...`).

3. **OAuth & Permissions**: under "Bot Token Scopes", add
   `chat:write`, `im:history`, `im:write`, `app_mentions:read`,
   `channels:history`, `groups:history`.

4. **Event Subscriptions**: enable. Subscribe to bot events `app_mention` and
   `message.im` (DMs).

5. **Install to workspace**. Copy the Bot User OAuth Token (`xoxb-...`).

## Local config

Create `~/.claude/channels/slack/.env`:

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
`~/.claude/channels/slack/access.json`.

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

## What's NOT supported in v0

- File attachments (image / file uploads, in either direction).
- Slash commands.
- Voice notes (no public Huddle audio API).

See issue #866 for the full v0 scope + planned follow-ups.

## Stop / restart

```sh
pkill -f slack-bridge   # stop
bash src/startup.sh     # restart (and all other bridges)
```

Logs land in `logs/slack-bridge.log`.
