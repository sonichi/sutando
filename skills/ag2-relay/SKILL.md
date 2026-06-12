# ag2-relay — optional hosted-relay channel

Connects this Sutando to a hosted AG2 relay: tasks arrive in `tasks/`,
results post back, same bridge contract as the other channels. Fully
optional — core boots unchanged without it.

## Connect (once)

```bash
bash skills/ag2-relay/onboard.sh
```

One prompt; what you paste picks the journey:
- `https://<base>|<code>` — new user: redeems the invite (creates your
  platform account + agent + token; you choose a password for the platform).
- `https://<base>` — existing user: log in with your platform credentials,
  then name this instance (Enter reconnects to your existing agent).

On success it writes `AG2_REMOTE_TOKEN` + `AG2_AGENT_NAME` (bare localpart)
to the repo `.env` (quoted) and saves a private summary — including your own
single-use invite codes — to `ag2-onboarding.txt` (gitignored).

Where to get an invite/address: ask your relay operator.

## Run

`bash src/startup.sh` auto-starts the client whenever `AG2_REMOTE_TOKEN` is
set (silent pgrep-guarded block; re-running startup is also the restart
path). Manual run: `python3 skills/ag2-relay/remote-task-client.py`.

## Disconnect

Remove the `AG2_REMOTE_TOKEN` line from `.env` and restart — the agent goes
offline. Tokens are identity credentials: never commit or share them.
