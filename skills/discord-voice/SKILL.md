---
name: discord-voice
description: "Drop Sutando into a Discord voice channel — captures user audio, runs Gemini Live conversation, plays the AI reply back through the bot, with tool-call support."
---

# Discord Voice

Lucy (or any Sutando bot) joins a Discord voice channel and has a real multi-turn spoken conversation, powered by Gemini Live — same UX as `phone-conversation`, but on Discord instead of Twilio.

## When to Use

- "Hop into the #voice channel and chat"
- "Join voice and walk me through this"
- Co-presenting / pairing where you want spoken interaction over Discord rather than a phone call

## Option A (pure Python) — `discord-voice-server-py.py`

Single-process Python server. No bodhi, no Node bridge.

```bash
python3 skills/discord-voice/scripts/discord-voice-server-py.py \
  --guild   1485653766404444352 \
  --channel <voice_channel_id>
```

Env requirements (loaded from `.env` and `~/.claude/channels/discord/.env`):
- `GEMINI_API_KEY` (or `GEMINI_VOICE_API_KEY`)
- `DISCORD_BOT_TOKEN`
- `OPUS_PATH` (default `/opt/homebrew/lib/libopus.dylib`)

Optional overrides:
- `VOICE_NATIVE_AUDIO_MODEL` — Gemini Live model (default `gemini-2.5-flash-native-audio-preview-09-2025`)
- `GEMINI_VOICE_NAME` — voice profile (default `Aoede`)

Python dependencies (install once via `pip3 install --break-system-packages`):
- `discord.py >= 2.7`
- `discord-ext-voice-recv`
- `google-genai`
- `python-dotenv`
- `PyNaCl`
- libopus + ffmpeg on the host

### Architecture

```
                          ┌─────────────────────────┐
   Discord voice gateway  │ discord-ext-voice-recv  │
   (Opus, 48k stereo) ───▶│  BasicSink(decode=True) │
                          └────────────┬────────────┘
                                       ▼
                          ┌─────────────────────────┐
                          │ discord_pcm_to_         │
                          │ gemini_input            │  (audioop: stereo→mono, 48k→16k)
                          └────────────┬────────────┘
                                       ▼
                          ┌─────────────────────────┐
                          │ asyncio.Queue           │
                          │ session.inbound_q       │
                          └────────────┬────────────┘
                                       ▼
                          ┌─────────────────────────┐
                          │ Gemini Live             │
                          │ send_realtime_input     │
                          └────────────┬────────────┘
                                       ▼
                          ┌─────────────────────────┐
                          │ Gemini Live receive()   │  (audio 24k mono, tool_calls)
                          └────┬────────────────┬───┘
                               │                │
                               ▼                ▼
                  ┌────────────────────┐  ┌────────────────────┐
                  │ tool dispatch:     │  │ gemini_output_to_  │  (audioop: 24k→48k, mono→stereo)
                  │ get_time, hang_up  │  │ discord_pcm        │
                  └────────────────────┘  └─────────┬──────────┘
                                                    ▼
                                       ┌────────────────────────┐
                                       │ asyncio.Queue           │
                                       │ session.outbound_q      │
                                       └────────────┬───────────┘
                                                    ▼
                                       ┌────────────────────────┐
                                       │ GeminiAudioSource.read │  (20ms / 3840 B frames)
                                       └────────────┬───────────┘
                                                    ▼
                                          Discord voice gateway
```

### Conversation log

JSONL log per session: `<workspace>/data/discord-voice-<timestamp>.jsonl`. One line per event (`session_start`, `user_text`, `assistant_text`, `tool_call`, `tool_result`, `session_end`).

### Tools

Minimal tool surface to prove the round-trip end-to-end:

| name      | what it does                                                  |
|-----------|---------------------------------------------------------------|
| `get_time`| Runs `date` on the host and returns the result for narration. |
| `hang_up` | Disconnects from the voice channel after a clear goodbye.     |

Wider tool wiring (the dozens of inline / browser / vision tools) stays in `src/inline-tools.ts` / `voice-agent.ts`. This server is intentionally narrow so the audio path is the part you trust on day one.

### Graceful shutdown

`SIGTERM` / `SIGINT` triggers an ordered shutdown: stop the Gemini bridge, drain the audio source, disconnect the voice client, close the discord.py client, write `session_end`.

## Option B (Python bridge + TS bodhi sidecar)

Two-process design that reuses bodhi `VoiceSession` end-to-end. Same tool wiring
(`work`, `inlineTools`, `ownerOnlyTools`, `coreDocumentedSkills`, vision attach)
as `skills/phone-conversation/scripts/conversation-server.ts` — without
re-implementing it in Python.

### Process 1 — TS sidecar (`scripts/discord-voice-server.ts`)

Owns the bodhi `VoiceSession` + Gemini Live transport. Mirrors the
conversation-server structure: same env config, same agent prompt scaffolding,
same `work` / inline / owner-only / configurable tool layering, same
`get_task_status`, same conversation-log + per-session metrics pattern
(`data/discord-voice-{ts}.jsonl`, `data/discord-voice-metrics.jsonl`), same
auto-reconnect on Gemini transport close, lazy vision attach.

Skipped (Twilio-only): mu-law conversion, ngrok, STIR/SHAKEN, DTMF, IVR,
concurrent-call, meeting approval. Discord voice is a clean PCM pipe.

```bash
npx tsx skills/discord-voice/scripts/discord-voice-server.ts
# env: DISCORD_VOICE_PORT (default 3200), GEMINI_API_KEY, VOICE_MODEL,
#      VOICE_NATIVE_AUDIO_MODEL, SUTANDO_WORKSPACE,
#      DISCORD_VOICE_OWNER (default true — gates the work tool)
```

Health check: `curl http://localhost:3200/health`.

### Process 2 — Python bridge (`scripts/discord-voice-bridge.py`)

Joins a Discord voice channel via `discord.py` + `discord-ext-voice-recv`,
captures decoded PCM (48 kHz stereo), downmixes + downsamples to 16 kHz mono,
forwards to the sidecar over WebSocket. Receives Gemini PCM (24 kHz mono),
upsamples to 48 kHz stereo, plays back via `discord.AudioSource`. Reconnects
to the sidecar on disconnect with exponential backoff.

```bash
python3 skills/discord-voice/scripts/discord-voice-bridge.py \
    --guild <GUILD_ID> \
    --channel <VOICE_CHANNEL_ID> \
    [--sidecar ws://localhost:3200/voice]
# env: DISCORD_BOT_TOKEN, DISCORD_VOICE_SIDECAR
```

Kept separate from any "silent-presence-only" bot script — Option B is for
full Gemini conversation mode.

### Option B — WebSocket protocol

JSON text frames over `ws://localhost:3200/voice`. Single-tenant: a new
`hello` closes the previous session.

| Direction | Message |
| --- | --- |
| Bridge → Server | `{"type":"hello","guild":"...","channel":"..."}` |
| Bridge → Server | `{"type":"audio","pcm":"<base64 PCM s16le 16 kHz mono>"}` |
| Bridge → Server | `{"type":"bye"}` |
| Server → Bridge | `{"type":"ready","sessionId":"..."}` |
| Server → Bridge | `{"type":"audio","pcm":"<base64 PCM s16le 24 kHz mono>"}` |
| Server → Bridge | `{"type":"transcript","role":"user|assistant","text":"..."}` |

### Option A vs Option B — when to pick which

- **Option A** is self-contained (single Python process, narrow tool surface).
  Use for quick demos and audio-pipeline experiments.
- **Option B** reuses the phone-agent's full tool wiring. Use when you want
  the bot to actually *do things* (work-delegation, screen control, vision,
  meeting join, etc.) from the Discord voice channel.

