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
- `https://<base>` — existing user, **interactive human**: opens a browser
  to log in (device-authorization flow — no password typed in the terminal),
  polls until you finish, writes the token. Falls back to CLI username/password
  if no browser/tty.
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

## Relay Protocol

The local client intentionally speaks a tiny provider-agnostic protocol. The
hosted relay owns platform details such as Matrix rooms, Discord channels,
Telegram chats, attachments, rate limits, and reply routing.

```text
GET  /v1/tasks?wait=<seconds>        # long-poll for standard Sutando tasks
POST /v1/tasks/<task-id>/ack         # task is safely queued locally
POST /v1/results                     # result body for a task id
POST /v1/heartbeat                   # online/tier/in-flight status
```

`ack` and `heartbeat` are best-effort extensions: if an older relay returns
404/405, the client keeps using the original pull/result protocol.

## Trust tier

Tasks from the relay are processed at the tier in `AG2_REMOTE_TIER`
(default `team` — sandboxed). The relay's own tier claims are ignored: it
sits outside this machine's trust boundary. Only set `AG2_REMOTE_TIER=owner`
if you operate the relay yourself and accept that it can submit
full-capability tasks to this machine.

## Disconnect

Remove the `AG2_REMOTE_TOKEN` line from `.env` and restart — the agent goes
offline. Tokens are identity credentials: never commit or share them.

## Server contract (device flow)

The browser device-flow needs the relay to expose `POST /connect/start`
(mint device_code + verify_url), `POST /connect/complete` (the login page
binds the agent after auth), `POST /connect/poll` (this client retrieves the
token), and serve the `/connect-page` login page. If those aren't deployed,
`onboard.sh` falls back to the credential prompt automatically.
