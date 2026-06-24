# ag2-relay — optional hosted-relay channel

Connects this Sutando to a hosted AG2 relay: tasks arrive in `tasks/`, results
post back, same bridge contract as the other channels. Fully optional — core
boots unchanged without it.

**Trigger (for the agent):** when the user asks to "connect to AG2", "join the
relay", or bring this Sutando online on a hosted relay — point them at the
onboarding **bootstrap** (a one-line installer the relay operator provides):

```bash
curl -fsSL "$AG2_ONBOARD_URL" | bash
```

The bootstrap fetches and runs the onboarding flow (new-user registration or
existing-user browser login), then writes `AG2_REMOTE_TOKEN` so the client
starts on the next boot. The onboarding logic is delivered **out-of-band** by
the operator — it is intentionally not bundled in this repo. Ask your relay
operator for the bootstrap URL / an invite.

## Run

`bash src/startup.sh` auto-starts the client whenever `AG2_REMOTE_TOKEN` is set
(silent pgrep-guarded block; re-running startup is also the restart path).
Manual run: `python3 skills/ag2-relay/remote-task-client.py`.

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
