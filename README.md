# Sutando

**A personal AI you talk to in real time — shares your screen, joins your meetings, makes phone calls, and builds itself.**

It belongs entirely to you.

> *Named after [Stands](https://jojo.fandom.com/wiki/Stand) from JoJo's Bizarre Adventure — a personal spirit that fights on your behalf. Like a Stand, Sutando starts unnamed. As it learns your style and earns real capabilities, it names itself and generates its own avatar — your Stand, unique to you.*

https://github.com/user-attachments/assets/a3f19d83-634b-4b15-a9c4-d3073ef556a4

Unmute to hear the real-time conversation. [Watch on YouTube →](https://youtu.be/PXUErLkgGc8)

---

## What can you do with it?

**Talk while you work.** You're looking at a doc. You say "make this paragraph shorter." Sutando sees your screen, rewrites the paragraph, and replaces the original text directly.

**Join meetings for you.** "Join my 2pm call." It reads your calendar and joins — Zoom via the desktop app, Google Meet via the browser — with computer audio. It can also dial in by phone when you ask. It takes screenshots to identify participants, does live research when someone asks a question, and writes you a summary when the call ends. Meeting access is gated — it messages you on Telegram asking for approval before enabling task delegation.

**Make calls for you.** "Call her and leave a message." Sutando looks up the contact, dials the number, has the conversation, and reports back — while you keep working. It can even make concurrent calls while in a meeting.

**Work from your phone.** Call Sutando and say "summon." It opens Zoom with screen sharing — join from your phone to see its screen in real time. "What's on my screen?" — it takes a screenshot and tells you. "Fix the typo in that file" — done. You scroll, switch apps, navigate — all by voice while walking around.

**Get better on its own.** When you're not giving it tasks, Sutando runs an autonomous build loop — it monitors its own health, detects patterns in how you work, discovers new skills, and builds missing capabilities. Most of Sutando's code was written this way. It learns from your corrections and adapts over time.

**Remember everything — and act on it.** You have an idea while walking. Say it out loud. Sutando captures it, tags it, and saves it as a searchable note. If there's something actionable, it starts working on it right away or queues it for the next free cycle.

**Reach you anywhere.** Voice, Zoom, Google Meet, Telegram, Discord, web, phone, or email — same agent, same memory, any channel.

---

## Status: Alpha

This is an early-stage project. Honest status:

| | Count | Details |
|---|---|---|
| **Verified working** | 29 | Voice, screen capture, notes, calendar, reminders, contacts, browser, phone calls, meeting dial-in, task delegation, pattern detection, health check, dashboard, Telegram, Discord, onboarding tutorial, and more |
| **Needs external setup** | 3 | Twilio (phone), Telegram bot, Discord bot |

We're looking for contributors to help test and harden these capabilities. If you try something and it breaks, [open an issue](https://github.com/sonichi/sutando/issues).

---

## How it works

```
    You ──voice──► Voice agent ──────────┐
     │                                   │ file bridge
     ├──telegram──► Telegram bridge ─────┤ tasks/ ──► Core agent
     │                                   │ ◄── results/     │
     ├──discord───► Discord bridge  ─────┤       │    uses anything:
     │                                   │       ▼    email, calendar, browser,
     └──browser───► Web client ──────────┘  speaks /  files, phone, reminders...
                                            replies
```

Two processes work together:
- **Voice agent** (Gemini Live) — listens and talks in real time, runs as a background daemon
- **Core agent** (Claude Code CLI) — executes tasks with full system access. We use the CLI because it provides cron scheduling, plugins, and an interactive terminal that the SDK doesn't offer out of the box.

They communicate through files: voice agent writes tasks, the core agent executes them, writes results back, voice agent speaks the answer.

---

## Quick start

**Prerequisites:**
- macOS 15+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code/getting-started) (run `claude` once to complete login)
- Node.js 22+ (`brew install node`)
- fswatch (`brew install fswatch`)
- [Google AI Studio API key](https://ai.google.dev) (free — click "Get API key")

```bash
# Clone and install
git clone https://github.com/sonichi/sutando.git
cd sutando
npm install

# Configure (minimum: GEMINI_API_KEY is required)
cp .env.example .env
# Edit .env — add your GEMINI_API_KEY (from Google AI Studio)

# Start everything
bash src/startup.sh
```

This starts all services (voice agent, web client, dashboard, API) and opens http://localhost:8080 in your browser. The autonomous loop starts automatically — click **Connect** and start talking.

**Verify your setup** (optional):
```bash
bash src/verify-setup.sh
```

**Troubleshooting:**
- Browser shows blank page? Services may still be starting — wait 5 seconds and refresh
- Microphone not working? Chrome will ask for permission on first connect — click Allow
- Voice agent not responding? Check `src/voice-agent.log` for errors. Common causes:
  - `GEMINI_API_KEY` not set or invalid in `.env` — get one at [ai.google.dev](https://ai.google.dev)
  - Port 9900 already in use — run `lsof -i :9900` to check
- `npm install` failed? Make sure Node.js 22+ is installed: `node --version`
- Something broke? Run `bash src/restart.sh` — this kills all services and restarts fresh

---

## Optional integrations

These unlock more capabilities. Add to `.env` when ready:

| Integration | What it unlocks | Setup |
|-------------|----------------|-------|
| Gmail | Read/send/search email from voice | `gws auth setup --login` (OAuth, no app password) |
| Twilio + ngrok | Phone calls, SMS, meeting dial-in, task delegation via phone | [twilio.com](https://www.twilio.com) (~$1/mo) + `brew install ngrok` |
| Telegram | Message Sutando from your phone | [Create bot via @BotFather](https://t.me/BotFather), then `/telegram:configure <token>` |
| Discord | Message Sutando from Discord (DM + channel @mentions) | [Developer portal](https://discord.com/developers), then `/discord:configure <token>` |
| Claude for Chrome | Browser automation — navigate, read pages, fill forms, interact with web apps | [Install extension](https://claude.ai/chrome), log in with the same account as Claude Code |
| Context drop | Send selected text to Sutando from any app via hotkey | See [setup below](#context-drop-setup) |

---

## What's inside

| Capability | Script | Status |
|-----------|--------|--------|
| Voice conversation | `voice-agent.ts` | Verified |
| Task delegation (voice → Claude) | `task-bridge.ts` | Verified |
| Screen capture + analysis | `macos-tools` skill | Verified |
| Notes / second brain | via CLAUDE.md | Verified |
| Context drop (hotkey → agent) | `context-drop.sh` | Verified |
| Gmail read/send/search | `gws-gmail` skill | Verified |
| Calendar reading | `google-calendar` skill | Verified |
| Reminders management | `macos-tools` skill | Verified |
| Contacts lookup | `macos-tools` skill | Verified |
| Browser automation | `browser.mjs` + MCP tools | Verified |
| Conversational phone calls | `phone-conversation/` | Verified (needs Twilio + ngrok) |
| Phone → task delegation | `phone-conversation/` | Verified (needs Twilio + VERIFIED_CALLERS) |
| Join Zoom (computer audio) | `inline-tools.ts` | Verified |
| Join Google Meet (browser audio) | `inline-tools.ts` | Verified |
| Meeting dial-in (Meet + Zoom) | `phone-conversation/` | Verified (needs Twilio + ngrok) |
| Meeting approval via Telegram | `phone-conversation/` | Verified (needs Twilio + Telegram) |
| Inbound call handling | `phone-conversation/` | Verified (needs Twilio) |
| Telegram messaging | `telegram-bridge.py` | Verified (text + photos + files + voice) |
| Discord messaging | `discord-bridge.py` | Verified (DMs + channel @mentions + files) |
| Cross-device task submission | `agent-api.py` | Verified |
| Health monitoring | `health-check.py` | Verified |
| Pattern detection + user modeling | Built into Claude Code memory system | Verified |
| System dashboard | `dashboard.py` | Verified |

---

## Services

When running, Sutando exposes these local ports:

| Port | What |
|------|------|
| 8080 | Voice web client — talk to Sutando here |
| 7844 | Dashboard — status, activity, and capability matrix |
| 7843 | Agent API — submit tasks from any device |
| 9900 | Voice agent WebSocket |

---

## macOS permissions

On first run, grant these in System Settings → Privacy & Security:
- **Screen Recording** → add `claude` and `node`
- **Accessibility** → add Shortcuts.app (for context drop)
- **Microphone** → Chrome will ask on first voice connect

---

## Context drop setup

Send any selected text to Sutando with a keyboard shortcut.

1. Open **Automator** → New → **Quick Action**
2. Set "Workflow receives" → **no input** in **any application**
3. Add action: **Run Shell Script** → paste: `bash /path/to/sutando/src/context-drop.sh`
4. Save as "Sutando: Drop Context"
5. Go to **System Settings → Keyboard → Keyboard Shortcuts → Services** → assign a shortcut to "Sutando: Drop Context"
6. Grant **Accessibility** permission to Shortcuts.app in System Settings → Privacy & Security

Now select any text and press your shortcut — Sutando reads it and acts on it.

---

## Proactive mode

`startup.sh` automatically enables proactive mode. Sutando runs an autonomous loop that:

- Processes voice tasks and context drops immediately
- Runs health checks and auto-fixes failed services
- Picks the highest-value improvement work when idle
- Learns from your corrections and adapts over time
- Notifies you on Discord and voice when it completes autonomous work

It consumes API quota proportional to how much work it finds to do.

---

## Contributing

This is alpha software. The biggest need is **testing** — try a capability, report what breaks.

- [Open an issue](https://github.com/sonichi/sutando/issues) for bugs

---

## How it was built

Sutando was largely built by its own autonomous build loop -- a Claude Code session that reads a build log, picks the highest-value missing piece, builds it, and loops. The human provides direction and testing; the agent does the rest.

---

## Acknowledgments

Voice agent built on [bodhi-realtime-agent](https://github.com/sonichi/bodhi_realtime_agent), a Gemini Live voice session library.

---

## License

MIT
