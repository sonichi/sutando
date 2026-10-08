---
name: ag2space-voice
description: "AG2 Space voice surface — binds the room the AG2 Space client announces to the voice session's origin, so work delegated by voice in a room is answered in that room, and moves the client by voice (navigate_ui)."
when_to_use: "Loaded automatically at voice-agent startup as a manifest skill. Not slash-invoked. Only an AG2 Space client sends the frames it handles; on any other install it stays idle."
---

# AG2 Space voice

Optional plugin for installs that talk to an AG2 Space client (desktop app or webapp).
The host engine carries a product-neutral *voice session origin* (`VoiceSessionOrigin`,
`src/task-bridge.ts`) and offers client frames to skills (`setup(ctx)`, see
[`skills/MANIFEST.md`](../MANIFEST.md)). This skill owns everything AG2 Space specific on
top of that. Remove the directory and the engine boots, types and tests unchanged.

## Wire contract (client side: webapp `features/room/voice/`)

| Direction | Frame |
|---|---|
| client → engine | `{type:'session.context', version:1, room_id, room_name, surface:'room'\|'dm', capabilities?}` — after `session.config` and on every room change; a DM frame has `room_id: null` |
| engine → client | `{type:'session.context.ack', version:1, room_id, bound, surface:'dm'\|'room'\|'refused', reason?}` |
| engine → client | `{type:'ui.navigate', version:1, request_id, target:'dm'\|'room'\|'home', query?}` |
| client → engine | `{type:'ui.navigated', version:1, request_id, ok, room_id?, room_name?, error?:'not_found'\|'ambiguous'\|'unsupported', candidates?}` |

## What it does

- **`session-context.ts`** — parses and bounds the frame (Matrix room id grammar, name
  cap, capability list) and builds the ack.
- **`room-binding.ts`** — the room a client names is a claim. It becomes the session's
  origin only when the gateway bridge answers
  `state/voice-room-checks/<key>.request.json` with a verified `<key>.verdict.json`
  (owner and agent both joined). A different room releases the bound one *before* the
  wait; a newer frame supersedes a pending one; a refusal, a throw or silence leaves the
  session on the owner DM.
- **Origin handed to the core**: channel `ag2space`, target = the room id, header keys
  `channel_kind: room` + `source_room_id`, the `room_context:` body line, the
  `[dm-only]` delivery note, and a `verify` that re-asks the gateway bridge when the
  result is delivered. Results therefore land as
  `results/proactive-result-<task>-<ts>.to-ag2space.txt` with `[channel: <room>]` first.
- **Prompt**: a `ROOM:` context line while docked, and one system notice per actual
  room change.
- **`navigate.ts` / `navigate-protocol.ts`** — the `navigate_ui` tool ("let's talk in my
  DM", "take me to GTM in Investors", "go home"). It is contributed through
  `voiceSurface()`, so it is on the web voice session only, never the phone tool table,
  and only when this install has the gateway channel (`REMOTE_TASK_TOKEN` /
  `AG2_REMOTE_TOKEN` in the environment or in `channels/ag2space/.env` under the Claude
  config dir). Its NAVIGATION prompt rule is present under the same condition. The frame
  goes only to a client that announced the `ui.navigate` capability; any other attached
  client gets an immediate "update the app" answer.

Tests: `tests/ag2space-voice-*.test.ts` (skipped when this directory is absent).

Optionality check: move this directory aside, run `npx tsc --noEmit -p .` and
`npm run test:ts` — both pass, with only the two `ag2space-voice-*` files reporting a skip.

## Packaged desktop builds

A packaged engine runs on plain node and cannot import `tools.ts`. The desktop build
bundles this skill to an `.mjs` the same way it bundles its private plugins and loads
it through `SUTANDO_EXTERNAL_PLUGIN_DIRS`.
