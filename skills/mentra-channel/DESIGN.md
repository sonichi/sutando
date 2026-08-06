# Mentra channel — design (pre-implementation)

Owner directive 2026-08-06: wearables order is Bee → **Mentra** → Even
Realities; she owns the hardware, so live E2E is available. This doc pins the
verified SDK facts and the architecture before any code — sibling in spirit
to the Teams design doc (ag2space-backend#443).

## Verified SDK surface (cloud-docs.mentra.glass, checked 2026-08-06)

- npm `@mentra/sdk` (2.x; the docs page's `@mentraos/sdk` name is stale — verified against the registry + installed types); app = a self-hosted server holding an `AppSession`
  per user session: `new AppSession({ packageName, apiKey, cloudUrl:
  ... })` (the SDK defaults `cloudApiUrl` to `api.mentra.glass`; the
  server bootstrap sets no explicit cloud URL).
- Session lifecycle is **webhook-initiated**: MentraOS Cloud POSTs
  `https://<app-domain>/webhook/session-start` with `sessionId` + `userId`;
  the app then opens the WebSocket for that session.
- Inbound: `session.events.onTranscription()` → `{ text,
  transcribeLanguage, … }` — live speech transcription pushed to us.
- Outbound (native reply legs): `session.layouts.showTextWall()` /
  `showReferenceCard()` / `showDashboardCard()` for the HUD;
  `session.audio.speak()` (TTS) / `session.audio.play()` for speakers.
- Full-duplex, event-push, per-user session binding. Works across MentraOS
  hardware (Mentra Live, Even Realities G1, Vuzix Z100, …).

## Architecture: where each piece lives

```
glasses ── MentraOS Cloud ──(webhook + WS)── mentra app server (ours, public URL)
                                                  │  events → tasks     ▲ results
                                                  ▼                     │
                                    broker /v1/ingest (source=mentra)   │
                                                  │                     │
                              sparrow lane (mentra-lane agent) → core ──┘
```

- **The app server is a client-side adapter** (like the Bee watcher, unlike
  the Teams broker module): MentraOS only talks to a server WE host over its
  own webhook+WS protocol, so the adapter cannot live inside the broker.
  New skill `skills/mentra-channel/` (TypeScript, `@mentra/sdk`).
- **Inbound:** utterance segments from `onTranscription` are debounced into
  message-sized chunks (final-transcript boundaries, not per-word), then
  POSTed to the broker's `/v1/ingest` as `source: "mentra"` tasks
  (`user_id` = MentraOS `userId`, `channel_id` = `sessionId`), exactly the
  Bee pattern. The broker needs zero changes for inbound.
- **Outbound — ONE owner, explicit (review P1 2026-08-06):** backend#444
  sends every `source=mentra` result to `INTEGRATION_FALLBACK_ROOM_MENTRA`
  the moment that env is set (the app server is not a broker deliverer), so
  polling AND setting the room would double-deliver. `MENTRA_DELIVERY`
  picks the owner per deployment: `room` (default — broker fallback room
  owns all replies; glasses show an ack; safe with the room env set) or
  `glasses` (app server long-polls `GET /v1/result/<id>` and renders via
  `showTextWall`; the broker room env MUST stay unset). A future broker
  claim/availability contract can make this dynamic per session.
- **Trigger discipline:** unlike Bee todos, raw transcription is a firehose.
  v1 forwards ONLY wake-phrase-gated utterances ("hey sutando …" — exact
  phrase TBD with owner) + an explicit button/gesture if the hardware
  surfaces one. Everything else is dropped at the app server. A later
  ambient mode can reuse the events-promotion (taskify) pipeline with
  `access_tier: ambient` semantics; NOT in v1.

## Registration / deployment (the non-code prerequisites)

1. Mentra developer console account + app registration (`packageName`,
   webhook URL, `MENTRA_API_KEY`) — needs an identity decision from the
   owner (her account vs an agent account).
2. A public HTTPS endpoint for the webhook + WS egress. AG2 Space runs on
   **EKS** (owner correction 2026-08-06 — no longer EC2): the app server
   deploys as a Deployment/Service on the cluster with an ingress route for
   `chat.ag2.space/mentra`, sibling of the broker's EKS deploy. Same
   git-tracked-only rule (owner hard rule 2026-07-16): manifests + image
   build land in the backend repo first; never hand-edit the cluster.
3. Secrets via vault: `MENTRA_API_KEY`, the lane's ingest bearer. (The
   owner-supplied key was vaulted as `MENTRAOS_API_KEY` on 2026-08-06;
   re-store it under `MENTRA_API_KEY` — the name `requiredConfigMissing`
   checks — or set `MENTRA_API_KEY` from it at deploy.)

## Test plan

- Unit (no cloud): stub `AppSession` seam; pin debounce/wake-gate behavior,
  ingest POST shape (source/user/channel/task-id alphabet — sparrow's
  `[A-Za-z0-9._-]{1,64}` rule), result-poll → layout render, and
  session-gone → no render (fallback room owns it).
- Live E2E (owner wearing the device): wake phrase → task file on the lane →
  core reply → text on glasses; then glasses-off → reply in the Mentra room.

## Open items for the owner

- Developer-account identity (her email vs agent email).
- Wake phrase choice.
- Which glasses to pair first (Mentra Live vs G1-under-MentraOS — the G1
  answer interacts with the Even Hub track).
