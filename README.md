# Sutando

[![Discord](https://img.shields.io/badge/Discord-Join-5865F2?logo=discord&logoColor=white)](https://discord.gg/uZHWXXmrCS) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Website](https://img.shields.io/badge/Web-sutando.ai-blue)](https://sutando.ai)

**Summon your AI superpower — voice, vision, and autonomous action.**

It shares your screen, joins your meetings, makes phone calls, and builds itself.

It belongs entirely to you.

> **No *Claude Extra usage* required.** Sutando runs on your existing Claude Code subscription ($20, $100, or $200/month) with minimal extra costs — no separate Anthropic API key to top up — unlike agents that route every action through pay-per-token APIs and hosted services.

> *Named after [Stands](https://jojo.fandom.com/wiki/Stand) from JoJo's Bizarre Adventure — a personal spirit that fights on your behalf. Like a Stand, Sutando starts unnamed. As it learns your style and earns real capabilities, it names itself and generates its own avatar — your Stand, unique to you.*

https://github.com/user-attachments/assets/a86ec34e-3b26-4011-824c-d2d124753c25

> 24 tool calls. 6 tasks. 7 minutes. All by voice from a phone. Demo by [@liususan091219](https://github.com/liususan091219). [Watch on YouTube (full quality) →](https://youtu.be/NC0kdpLulUY)

📺 [Watch the vision talk at UC Berkeley →](https://youtu.be/c39fJ2WAj6A?t=7719) — the idea behind Sutando, before it existed.

---

## What can you do with it?

**Talk while you work.** You're looking at a doc. You say "make this paragraph shorter." Sutando sees your screen, rewrites the paragraph, and replaces the original text directly.

**Join meetings for you.** "Join my 2pm call." It reads your calendar and joins — Zoom via the desktop app, Google Meet via the browser — with computer audio. It can also dial in by phone when you ask. It takes screenshots to identify participants, does live research when someone asks a question, and writes you a summary when the call ends. Meeting access is gated — it messages you on Telegram asking for approval before enabling task delegation.

**Make calls for you.** "Call her and leave a message." Sutando looks up the contact, dials the number, has the conversation, and reports back — while you keep working. It can even make concurrent calls while in a meeting.

**Work from your phone.** Call Sutando and say "summon." It opens Zoom with screen sharing — join from your phone to see its screen in real time. "What's on my screen?" — it takes a screenshot and tells you. "Fix the typo in that file" — done. You scroll, switch apps, navigate — all by voice while walking around.

**Get better on its own.** When you're not giving it tasks, Sutando runs an autonomous build loop — it monitors its own health, detects patterns in how you work, discovers new skills, and builds missing capabilities. Most of Sutando's code was written this way. It learns from your corrections and adapts over time.

**Remember everything — and act on it.** You have an idea while walking. Say it out loud. Sutando captures it, tags it, and saves it as a searchable note. If there's something actionable, it starts working on it right away or queues it for the next free cycle.

**Reach you anywhere.** Voice, Zoom, Google Meet, Telegram, Discord, web, phone, or email — same agent, same memory, any channel.

**Scale across machines.** Plug in a second Mac and Sutando sets it up — the original agent opens a Discord channel, sends setup commands, and migrates services. The new machine handles phone calls 24/7 while your laptop stays portable. No migration scripts needed — the two agents coordinate the handoff themselves.

---

## Status: Alpha

This is an early-stage project. Honest status:

| | Count | Details |
|---|---|---|
| **Verified working** | 30 | Voice, screen capture, notes, calendar, reminders, contacts, browser, phone calls, meeting dial-in, task delegation, pattern detection, health check, dashboard, Telegram, Discord, multi-machine migration, onboarding tutorial, and more |
| **Needs external setup** | 3 | Twilio (phone), Telegram bot, Discord bot |

We're looking for contributors to help test and harden these capabilities. If you try something and it breaks, [open an issue](https://github.com/sonichi/sutando/issues).

---

## How it works

```
    You ──voice (browser)──► Voice agent ─────────┐
     │                       (Gemini Live,        │
     │                        WS on :9900)        ├──► inline tools (instant,
     │                                            │    in-process: describe_screen,
     ├──phone (Twilio)─────► Conversation server ─┤    get_current_time, hang_up,
     │                       (Gemini Live,        │    dtmf, ...)
     │                        WS on :3100)        │
     │                                            └──┐
     │                                               │   file bridge       .──────▶────────.
     ├──telegram──────────► Telegram bridge ─────────┼── tasks/ ─────────► |               |
     │                                               │                    |   Core        |
     │                                               │                    |   agent ↻     |
     └──discord───────────► Discord bridge ──────────┘                    |               |
                                                                           `──────◀────────'
                                                                                  │
                                                                                  ▼
                                                                          uses anything:
                                                                          email, calendar,
                                                                          browser, files,
                                                                          phone, reminders...
                                    ◄── results/ ◄────────────────────────────────┘
                                (spoken via voice/phone,
                                 text via Telegram/Discord)

    ↻ = a cron job fires the `/proactive-loop` skill every 5 minutes
        (`*/5 * * * *` in `skills/schedule-crons/crons.json`). The skill
        runs as a 10-minute pass that keeps a persistent watcher on
        `tasks/` via Claude Code's `Monitor` tool — pending tasks are
        processed the moment they arrive, not just on the cron tick.
        Each pass also runs health checks and picks the next build-log
        item autonomously.
```

Four processes work together:
- **Voice agent** (Gemini Live, WebSocket on :9900) — listens and talks in real time for browser voice.
- **Web client** (`com.sutando.web-client.plist`, HTTP on :8080) — separate launchd service that serves the browser UI. The browser then connects directly to the voice agent's WebSocket on :9900 — the web client is not in the WebSocket data path.
- **Conversation server** (Gemini Live, Twilio WebSocket on :3100) — same role as the voice agent for inbound and outbound phone calls.
- **Core agent** (Claude Code CLI) — executes tasks with full system access. We use the CLI because it provides cron scheduling, plugins, and an interactive terminal that the SDK doesn't offer out of the box.

Voice agent and conversation server handle conversation-scope actions with **inline tools** — in-process calls that round-trip instantly (describe the screen, hang up, send DTMF, read the clipboard/current time, capture a screenshot). For anything outside that scope they write to `tasks/`; core reads them, executes, and writes to `results/`, which each channel speaks or messages back. Telegram and Discord bridges only use the `tasks/` path.

---

## Quick start

**Prerequisites:**
- macOS 15+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code/getting-started) (run `claude` once to complete login)
- Node.js 22+ (`brew install node`)
- fswatch (`brew install fswatch`)
- [Gemini API key](https://ai.google.dev) (click "Get API key")
- *(optional, for phone calls)* [Twilio account](https://www.twilio.com/) + [ngrok](https://ngrok.com/) — Sutando can answer inbound calls and make outbound calls; you can run the browser + Telegram + Discord paths without them.
- *(optional, for video/audio)* ffmpeg (`brew install ffmpeg`) — used by subtitle-burn, video-concat, and recording handoff.

```bash
# Clone
git clone https://github.com/sonichi/sutando.git
cd sutando

# Configure (minimum: GEMINI_API_KEY is required)
cp .env.example .env
# Edit .env — add your GEMINI_API_KEY (from Google AI Studio)

# Start everything
bash src/startup.sh
```

This starts all services (voice agent, phone conversation server, web client, dashboard, API, Sutando menu bar app) and opens http://localhost:8080 in your browser. The autonomous loop starts automatically — click **Connect** and start talking. Look for **S** in your menu bar — it provides global hotkeys (see [Keyboard shortcuts](#keyboard-shortcuts)) plus **Open Core** (Claude Code terminal) and **Open Dashboard** (status page).

> **Why Sutando runs with elevated permissions.** Autonomous voice-driven work means `startup.sh` launches Claude Code with `--dangerously-skip-permissions` — the prompts that would otherwise fire on every tool call would break the voice-in / answer-out flow. In exchange:
>
> - **It's local.** Sutando runs entirely on your Mac. No remote control plane, no third party with write access.
> - **You control the audience.** 3-tier access gating means owner / verified / unverified callers get different capability bands on phone, Discord, and Telegram. Set `VERIFIED_CALLERS` in `.env` before going live.
> - **Actions are auditable.** Every Claude Code invocation lands in `build_log.md`, every task in `tasks/` + `results/`, every shell call in the service logs (`logs/*.log`). Use `tail -f build_log.md` while it works to watch in real time.
> - **Hooks are your brake pedal.** `git-rules-guard.sh` (see `~/.claude/hooks`) pops a Discord approval DM for any public write (push / PR / issue comment) regardless of transport. Reject with 👎 to block.
>
> Keep the Claude Code terminal window reachable — quota-exhaustion or an unrecognized CLI prompt can leave the core agent waiting for you to respond.

**Why macOS 15+?** The setup scripts assume the Sequoia System Settings layout for granting TCC permissions (Screen Recording, Accessibility, Input Monitoring). Earlier macOS versions may work for the headless parts (proactive loop, Discord/Telegram bridges) but aren't tested.

**macOS permissions** — on first run, macOS will ask you to grant Screen Recording, Accessibility, and Microphone access. See [Security](#security) for what each permission is used for.

**Try saying:**
- "What's on my screen?" — takes a screenshot and describes it
- "Summon my computer to zoom" — opens Zoom with screen sharing, join from your phone
- "Join my next meeting" — checks your calendar and joins
- "Take a note: my first idea" — saves a searchable note
- "Tutorial" — walks you through all capabilities step by step

**Verify your setup** (optional):
```bash
bash src/verify-setup.sh
```

**Troubleshooting:**
- Browser shows blank page? Services may still be starting — wait 5 seconds and refresh
- Microphone not working? Chrome will ask for permission on first connect — click Allow
- Voice agent not responding? Check `logs/voice-agent.log` for errors. Common causes:
  - `GEMINI_API_KEY` not set or invalid in `.env` — get one at [ai.google.dev](https://ai.google.dev)
  - Port 9900 already in use — run `lsof -i :9900` to check
- `npm install` failed? Make sure Node.js 22+ is installed: `node --version`
- Gemini 429 errors? Your shell may have a stale `GEMINI_API_KEY` overriding `.env` — run `unset GEMINI_API_KEY` then restart
- Screen recording produces 0-second files? `screencapture -v` needs a TTY. Sutando uses `ffmpeg` instead — make sure it's installed: `brew install ffmpeg`
- Something broke? Run `bash src/restart.sh` — this kills all services and restarts fresh
- Sutando acting confused, contradicting itself, or giving stale answers after a long session? Claude hallucinates more as the context window fills up — restart the Claude Code session every now and then to reset.
- Phone call answers with "We are sorry, an error has occurred"? The conversation server (`skills/phone-conversation/scripts/conversation-server.ts`, port 3100) isn't running. Run `bash src/startup.sh` or `bash src/restart.sh` to relaunch all services.

**Shutting down:**
```bash
bash src/restart.sh    # stops all services (voice agent, web client, API, bridges, etc.)
pkill -x Sutando # stop the menu bar app
```
Exiting `startup.sh` alone does NOT stop background services. Always use `restart.sh` (or `kill-all.sh` if available) to cleanly shut everything down.

**Uninstalling:**
1. Stop all services: `bash src/restart.sh && pkill -x Sutando`
2. Remove the repo: `rm -rf ~/Desktop/sutando` (or wherever you cloned it)
3. Remove config: `rm -rf ~/.claude/projects/*sutando*`
4. Remove npm packages (optional): the repo uses local `node_modules/` — deleted with the repo
5. Remove any tools you installed during setup (e.g. `imsg`, `wacli`) via the package manager you used to install them.
6. If you installed the OS-supervised health checks: `bash src/install-health-check-launchd.sh --uninstall` (idempotent — no-op if not installed).

---

## Optional integrations

These unlock more capabilities. Add to `.env` when ready:

| Integration | What it unlocks | Setup |
|-------------|----------------|-------|
| Gmail | Read/send/search email from voice | `gws auth setup --login` (OAuth, no app password) |
| Twilio + ngrok | Phone calls, SMS, meeting dial-in, task delegation via phone | [twilio.com](https://www.twilio.com) + `brew install ngrok` (see [Running costs](#running-costs)) |
| Telegram | Message Sutando from your phone | [Create bot via @BotFather](https://t.me/BotFather), then `/telegram:configure <token>` |
| Discord | Message Sutando from Discord (DM + channel @mentions) | [Developer portal](https://discord.com/developers), then `/discord:configure <token>` |
| Claude for Chrome | Browser automation — navigate, read pages, fill forms, interact with web apps | [Install extension](https://claude.ai/chrome), log in with the same account as Claude Code |
| Sutando app (menu bar) | Global hotkeys (see [Keyboard shortcuts](#keyboard-shortcuts)) | Auto-launches via `startup.sh` |
| OS-supervised health checks | Detect stuck loops, dead watchers, and queue pileups even when core is unresponsive — macOS notifies you when Sutando is broken | `bash src/install-health-check-launchd.sh` (idempotent; uninstall with `--uninstall`) |

---

## Running costs

One table, organized by capability. The only required paid piece is your Claude Code subscription — everything else is optional and mostly free-tier-sufficient.

| Capability | When you need it | Service required | Cost |
|---|---|---|---|
| **Basic** (core agent + screen / notes / calendar / email / reminders / contacts / browser / iMessage) | Always — this is Sutando's baseline | [Claude Code](https://www.anthropic.com/pricing) + [Gemini API key](https://ai.google.dev) + Google OAuth + macOS | Claude Code $20/mon (Pro), $100/mon (Max 5×), or $200/mon (Max 20×). Gemini + OAuth + macOS all free. |
| **Voice agent** (real-time conversation in browser or on phone) | If you want to talk to Sutando | [Gemini voice API](https://ai.google.dev) (same key as Basic) | Free tier covers normal use (~15 req/min). Heavy use: [Gemini paid](https://ai.google.dev/pricing) ~$0.30–$1.30/hr. |
| **Telegram / Discord / WhatsApp** (message Sutando from any of these) | If you want non-voice chat from your phone or desktop | [Telegram BotFather](https://t.me/BotFather), [Discord developer portal](https://discord.com/developers/applications), `wacli` (bundled) | All free for personal use. |
| **Phone calls / summon (remote control)** | If you want Sutando to make inbound/outbound calls, or to share its computer screen via Zoom/Google Meet and be controlled by voice from your phone | [Twilio](https://www.twilio.com/pricing) phone number + [ngrok](https://ngrok.com/download) webhook | Twilio ~$1/mon number + ~$0.0085/min inbound + ~$0.015/min outbound + [Media Streams](https://www.twilio.com/en-us/pricing) ~$0.004/min. ngrok and Zoom free tiers both work for the summon flow. |
| **Agent joining meetings via dial-in** (PSTN join into Zoom / Google Meet) | If you want the phone agent to dial into a meeting as a participant | [Zoom Pro](https://zoom.us/pricing) OR [Google Workspace Business](https://workspace.google.com/pricing.html) *on the host side* (the meeting organizer's account needs toll dial-in enabled) | Zoom Pro ~$15/mon, Google Workspace Business Starter ~$7/mon. Sutando's side is already covered by the Phone row above. |

**Minimal-cost path** (what most users want): Claude Code subscription + free Gemini + free OAuth. Everything voice + browser + messaging works at $0 beyond the Claude Code sub. Phone and meeting dial-in are opt-in.

---

## What's inside

| Capability | Script | Status |
|-----------|--------|--------|
| Voice conversation | `voice-agent.ts` | Verified |
| Task delegation (voice → Claude) | `task-bridge.ts` + `watch-tasks-stream.sh` + `tasks/` dir | Verified |
| Screen capture + analysis | `macos-tools` skill | Verified |
| Notes / second brain | `notes/` directory (YAML-frontmatter markdown) | Verified |
| Context drop + shortcuts | `src/Sutando/` menu bar app | Verified |
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
| Cross-node sync (memory + notes between Macs) | `cross-node-sync` skill | Verified |
| Info-radar (arXiv / GitHub / HN / news monitoring) | `info-radar` skill + daily digest | Verified |
| Menu-bar avatar states (idle/listening/speaking/working) | `src/Sutando/main.swift` + `/sse-status` | Verified |

---

## Services

When running, Sutando exposes these local ports:

| Port | What |
|------|------|
| 8080 | Voice web client — talk to Sutando here |
| 7844 | Dashboard — status, activity, and capability matrix |
| 7843 | Agent API — submit tasks from any device |
| 9900 | Voice agent WebSocket |
| 7845 | Screen capture server |
| 3100 | Phone conversation server (Twilio webhook target) |
| 4040 | ngrok admin UI (when ngrok is running) |

---

## Keyboard shortcuts

The Sutando menu bar app (`src/Sutando/`) provides global keyboard shortcuts. It launches automatically via `startup.sh`.

| Shortcut | Action |
|----------|--------|
| ⌃C | **Context drop** — sends selected text, clipboard image, or Finder file to Sutando |
| ⌃S | **Screenshot drop** — sends a screenshot of the active window/screen to Sutando |
| ⌃V | **Voice toggle** — connects/disconnects voice in the browser |
| ⌃M | **Mute toggle** — mutes/unmutes microphone during voice |

The menu bar also has **Open Core** (brings up the Claude Code terminal) and **Open Dashboard** (opens the status dashboard at localhost:7844).

On first run:
1. Grant **Accessibility** permission to the Sutando app in System Settings → Privacy & Security
2. Enable **Allow JavaScript from Apple Events** in Chrome: View → Developer → Allow JavaScript from Apple Events (required for ⌃V voice toggle)

The binary auto-compiles on `startup.sh` if missing. To compile manually: `cd src/Sutando && swiftc -O -o Sutando main.swift -framework Cocoa -framework Carbon -framework ApplicationServices`

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

## Security

🚨 Sutando has deep access to your computer — file system, screen, keyboard, browser, email, and phone. Understand the risks before deploying.

**Built-in protections:**
- **STIR/SHAKEN verification** — inbound calls are checked for carrier-level caller ID attestation. Spoofed numbers are automatically downgraded and denied owner access.
- **3-tier access control** — owner, verified, and unverified callers get different levels of access on phone, Discord, and Telegram.

**Recommended setup:**
- Keep your Twilio phone number private
- Set `VERIFIED_CALLERS` explicitly in `.env` (don't leave it empty)

**macOS permissions Sutando needs** (System Settings → Privacy & Security):
- **Screen Recording** → add `claude` and `node`. Required for `describe_screen`, `capture_screen`, and the screen-capture server (port 7845) — lets Sutando see what you're looking at when you ask "what's on my screen?". Also used by the screen-record skill for subtitled recordings.
- **Accessibility** → add the Sutando menu-bar app. Required for the global hotkeys (see [Keyboard shortcuts](#keyboard-shortcuts)) and for the `macos-use` skill to click/type into native apps on your behalf.
- **Microphone** → Chrome (and Terminal, for the screen-record skill). Chrome asks on first voice connect — click Allow.
- **Contacts / Calendar / Reminders** → asked on demand by the features that use them (contact lookup before a call, `gws calendar +agenda`, `reminders.py add/list/complete`). You can grant these when first prompted rather than up front.

See **[SECURITY.md](SECURITY.md)** for full details, best practices, and how to test your setup.

---

## Contributing

This is alpha software. The biggest need is **testing** — try a capability, report what breaks.

- [Join the Discord](https://discord.gg/uZHWXXmrCS) for help, discussion, and updates
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
