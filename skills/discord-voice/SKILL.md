---
name: discord-voice
description: Sutando joins a Discord voice channel and runs a 2-way Gemini Live conversation. Standalone TS process — discord.js + @discordjs/voice + bodhi VoiceSession.
when_to_use: When the user (in a DM or task) asks Sutando to "join voice", "join the lounge", or generally to be present in a Discord voice channel for live conversation.
---

# Discord Voice

Sutando joins a Discord voice channel and runs a real-time 2-way conversation via Gemini Live, reusing the same bodhi `VoiceSession` + tool wiring as `skills/phone-conversation/scripts/conversation-server.ts` (Twilio path).

## When to Use

- User says "join voice", "join the lounge", "join `<voice channel name>`", or any equivalent.
- A task arrives asking Sutando to be present in a Discord voice channel.

NOT for: silent presence (no Gemini), text-only Discord channels (use `discord-bridge.py`), Zoom/Meet/phone (use the respective skills).

## Architecture

One process, all in TypeScript:

```
Discord user voice
    ↓
@discordjs/voice receiver (opus packets per speaking user)
    ↓ prism opus.Decoder → PCM s16le 48k stereo
    ↓ downsample48StereoTo16Mono
    ↓
bodhi VoiceSession.handleAudioFromClient (PCM 16k mono)
    ↓
Gemini Live
    ↓ base64 PCM 24k mono
    ↓ upsample24MonoTo48Stereo
    ↓
@discordjs/voice AudioPlayer → opus-encoded out to voice connection
    ↓
Discord channel audio out
```

`@discordjs/voice` handles Discord's DAVE (E2EE) via `DAVESession` first-party — no extra config.

## Setup

1. **Register a Discord bot account** at the [Discord developer portal](https://discord.com/developers/applications). Give it the `bot` scope with `applications.commands` + the voice perms (`Connect`, `Speak`, `Use Voice Activity`).
2. **Add the bot token** to `~/.claude/channels/discord/.env`:
   ```
   DISCORD_BOT_TOKEN=...
   ```
3. **Invite the bot** to your Discord server with voice channel access.
4. **Set `GEMINI_API_KEY`** in `.env` at the repo root.

## Run

```bash
DISCORD_VOICE_SERVER=1 \
  npx tsx skills/discord-voice/scripts/discord-voice-server.ts \
  --guild <GUILD_ID> \
  --channel <VOICE_CHANNEL_ID>
```

Optional env:
- `VOICE_MODEL` / `VOICE_NATIVE_AUDIO_MODEL` — mirrors `voice-agent.ts`.
- `SUTANDO_WORKSPACE` — workspace root for tasks/results/data/logs.
- `DISCORD_VOICE_OWNER` — `true` (default) treats every speaker in the voice channel as the owner — see **Trust boundary** below.

`DISCORD_VOICE_SERVER=1` flips the polymorphic `dismiss` tool (`src/meeting-tools.ts`) into "SIGTERM self" mode instead of its default Zoom AppleScript path. Without it, asking Sutando to "leave"/"dismiss" in the channel would try to leave a (non-existent) Zoom meeting.

## Trust boundary — read this before inviting the bot anywhere shared

`DISCORD_VOICE_OWNER=true` is the deliberate default for a personal-use bot and it has a sharp edge: **anyone who can speak in the same voice channel inherits owner-tier `work` privileges** — full task delegation, file edits, message sends, anything the proactive loop can do.

This is fine because every Sutando install runs its own bot in its own guild, and the operator controls who they let into the voice channel. But it means:

- Don't invite the bot to a voice channel you don't trust the membership of.
- Don't leave the bot connected to a public/community voice channel unattended.
- If you want a shared / community deployment, set `DISCORD_VOICE_OWNER=false` — that gates `work` and the other owner-only tools to the configured `owner` env. (Non-owner callers still get the safe read-only tools.)

There is no per-user ACL inside the voice channel; the unit of trust is "who's allowed in the channel" (Discord channel permissions own that), not "who's speaking right now".

## DM-triggered join

Anyone running a Sutando proactive loop can DM their bot "join the lounge voice channel in `<server>`" — the loop spawns the run command above as a subprocess. No separate launcher needed; the task-bridge → proactive-loop → Bash pipeline already handles it.

## Tools

Inherits the full `inlineTools` + `ownerOnlyTools` set from `src/inline-tools.ts` (same surface as `voice-agent.ts` and `conversation-server.ts`). Notable Discord-relevant tools:

- `work` — delegate non-trivial tasks to core (writes `tasks/voice-task-{ts}.txt`, blocks on result).
- `dismiss` — leave the current voice presence. Polymorphic via `DISCORD_VOICE_SERVER` env: SIGTERMs self in Discord mode, runs Zoom AppleScript otherwise.
- `share_screen` / `stop_share_screen` — drive Discord's screen-share picker. **Has a hard dependency — see below.**
- `summon` — skill-local override redirecting "share my screen" to `share_screen` (the core `summon` opens Zoom, wrong app when user is in Discord).
- `get_current_time`, `get_core_status`, `join_zoom`, `join_gmeet`, `lookup_meeting_id`, `call_contact` — all standard.

## Screen sharing — extra setup required

`share_screen` / `stop_share_screen` are NOT free — they CGEvent-click the Discord webapp's "Share Your Screen" button and the Chrome native share-picker. That means:

1. **You need a separate Chrome instance running with Discord logged in.** The tool targets the `chrome-devtools-mcp` Chrome profile specifically (at `~/.cache/chrome-devtools-mcp/chrome-profile`), so the share happens as whoever is logged into THAT Chrome — not the bot, not necessarily your main Discord. Recommended: create a secondary ("alt") Discord account and log into the MCP-Chrome as that, so your primary Discord (in regular Chrome / desktop app) stays uninterrupted. The alt and the bot both join the voice channel; the alt's screen is what gets shared. **The alt must be a member of the same Discord server** — voice channels are server-only (no DM voice for bots / no DM screen-share via this tool).
2. **That Chrome window must be open to the Discord voice channel detail view** (not a text channel, not minimized). The script clicks at a hardcoded screen coord that corresponds to the main-view "Share Your Screen" button.
3. **Hardcoded coords assume a maximized Chrome window** (screenX=0, screenY=32 on macOS, 1920×972 outer). Move/resize the window and clicks miss. Re-derive coords via `macos-use refresh_traversal` on the MCP-Chrome main PID, then update `COORDS` in `scripts/share-screen-modal.py`.
4. **macOS Accessibility permission** is required for the controlling process (Claude Code / Terminal) to post CGEvent clicks. Grant in System Settings → Privacy & Security → Accessibility.

If you don't want screen-sharing, the rest of the skill (voice conversation, tool delegation) works without any of this — `share_screen` will fail silently with no impact on voice.

## Graceful shutdown

`SIGTERM`/`SIGINT` triggers `cleanupSession()` which calls `connection.destroy()` (sends Discord voice-gateway disconnect frame) and `voiceSession.close()`. The handler then waits 1.5s before `process.exit(0)` so the disconnect frame actually flushes — without that delay, Discord pins the bot in-channel until its own 60-90s heartbeat timeout.

Metrics + transcripts land in `$SUTANDO_WORKSPACE/data/discord-voice-{sessionId}.jsonl` and `discord-voice-metrics.jsonl`.
