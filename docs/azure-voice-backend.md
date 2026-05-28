# Azure GPT Realtime voice backend (opt-in)

Sutando's voice agent defaults to **Gemini Live**. As an alternative, it can run
the realtime voice session through **Azure-hosted GPT Realtime**. This is opt-in
and does not change the default path — if `VOICE_BACKEND` is unset (or `gemini`),
nothing about the existing behavior changes.

## Enabling it

Set the backend and provide Azure OpenAI credentials in `.env`:

```bash
VOICE_BACKEND=gpt-realtime
AZURE_OPENAI_API_KEY=your-azure-openai-key
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
# Optional overrides:
AZURE_REALTIME_DEPLOYMENT=gpt-realtime          # your deployment name
AZURE_REALTIME_API_VERSION=2025-04-01-preview   # first version to accept session.type
AZURE_REALTIME_VOICE=alloy
```

`bash src/startup.sh` then launches the voice agent on `:9900` against Azure
instead of Gemini. The browser UI is unchanged.

## How it works

The provider lives in `src/voice-backends/azure-realtime.ts` and is imported by
`src/voice-agent.ts` only when `VOICE_BACKEND=gpt-realtime`. It reuses bodhi's
`OpenAIRealtimeTransport` (for its event handling and reconnect logic) but swaps
the underlying client to `AzureOpenAI` and routes the WebSocket through
`OpenAIRealtimeWS.azure(client, { deploymentName })` — the OpenAI SDK's supported
entry point for Azure realtime. When the backend is unset, none of this is
loaded and `VoiceSession` runs Gemini Live as before.

## Known limitations / open items

These are why this ships as experimental:

1. **bodhi legacy protocol required.** Azure's realtime *preview* rejects bodhi's
   GA session shape, so the provider requests bodhi's `protocolVersion: 'legacy'`
   path (drops `type='realtime'`, flattens audio config, aliases
   `response.audio.delta`). That option must be present in the pinned
   `bodhi-realtime-agent`. It is passed via a type cast so this repo compiles
   against bodhi releases that don't expose the field — but at runtime the Azure
   path only works once legacy-protocol support lands in the pinned bodhi commit.
   See the PR description for the bodhi-side follow-up.

2. **30-minute session cap.** Azure GPT Realtime sessions are capped at ~30 min
   (`close code=1001 reason=session_expired`). Robust in-place reconnection (fully
   re-establishing the Azure transport rather than only re-handshaking the client)
   is follow-up work; until then a long session may require a restart.
