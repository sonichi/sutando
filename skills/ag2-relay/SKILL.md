# ag2-relay — optional hosted-relay channel

**Trigger (for the agent):** when the user asks to "connect to AG2", "join
the relay", mentions an AG2 invite/onboarding string (`https://…|…`), or
pastes a platform address and asks to bring this Sutando online there — run
`onboard.sh`. Collect the inputs conversationally, then call it
non-interactively:

```bash
# new user (string contains "|"); password = their NEW platform login
bash skills/ag2-relay/onboard.sh "https://<base>|<code>" "<instance-name>" "<password>"
# existing user (bare address); arg2 = their platform USERNAME, arg3 = password
# (instance label via AG2_ONBOARD_LABEL env; unset = reconnect existing agent)
bash skills/ag2-relay/onboard.sh "https://<base>" "<platform-username>" "<password>"
```

Never echo the password or token back into chat. Interactive humans can just
run `bash skills/ag2-relay/onboard.sh` with no args.

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
- `https://<base>` + no account yet — request access (email/name/reason);
  once the operator approves you'll receive an invite to finish with.

On success it writes `AG2_REMOTE_TOKEN` + `AG2_AGENT_NAME` (bare localpart)
to the repo `.env` (quoted) and saves a private summary — including your own
single-use invite codes — to `ag2-onboarding.txt` (gitignored).

Where to get an invite/address: ask your relay operator.

## Run

`bash src/startup.sh` auto-starts the client whenever `AG2_REMOTE_TOKEN` is
set (silent pgrep-guarded block; re-running startup is also the restart
path). Manual run: `python3 skills/ag2-relay/remote-task-client.py`.

## Trust tier

Tasks from the relay are processed at the tier in `AG2_REMOTE_TIER`
(default `team` — sandboxed). The relay's own tier claims are ignored: it
sits outside this machine's trust boundary. Only set `AG2_REMOTE_TIER=owner`
if you operate the relay yourself and accept that it can submit
full-capability tasks to this machine.

## Disconnect

Remove the `AG2_REMOTE_TOKEN` line from `.env` and restart — the agent goes
offline. Tokens are identity credentials: never commit or share them.
