# Sutando — Feature Reference

Sutando is a personal AI agent that runs on your Mac. You talk to it (voice or
text), it does things — research, email, scheduling, code, browsing, file work,
content creation — the way you would. This document catalogs everything it can
do as of the `sonichi/sutando` upstream sync (checkpoints CP-0–CP-12).

---

## 1. How you interact with Sutando

Sutando has one brain (the core agent — a Claude Code session) reachable through
many surfaces. Every surface drops tasks into the same file-based **task bridge**;
results flow back to wherever the request came from.

| Surface | What it is | Notes |
|---|---|---|
| **Voice** | Real-time spoken conversation (Gemini Live) on `:9900` | Hands-free; the orb/menu-bar avatar shows state |
| **Web client** | React app at `http://localhost:8080` | Conversation, tasks, notes, settings, dashboard links |
| **macOS app** | `Sutando.app` menu-bar app | Native Settings, global hotkeys, context-drop, unified window |
| **Telegram bridge** | DM the Telegram bot | Text, photos, files, voice notes; first DM auto-onboards (TOFU) |
| **Discord bridge** | DM or @mention the Discord bot | Tiered access (owner/team/other); file attachments |
| **Discord voice** | Sutando joins a Discord voice channel | 2-way Gemini Live conversation + screen share |
| **Slack bridge** | DM or @mention in Slack (Socket Mode) | Tiered access; file attachments; tokens set in Settings |
| **Phone** | Inbound/outbound phone calls (Twilio) | Conversational; can dial into Zoom/Meet |
| **Context-drop hotkey** | Global shortcut captures the current selection | Drops screen/selection context straight into a task |

**Task priority:** tasks carry an `urgent` / `normal` / `low` header — voice/phone
are urgent, chat/owner-DM normal, cron/health-check low. The consumer processes
highest priority first (mtime FIFO tie-break).

---

## 2. Built-in capabilities (always-on tools)

These are inline tools — instant, no round-trip. Available in voice and phone.

**Productivity & information**
- **Calendar** — read Google Calendar (`gws calendar`): today, week, N-day agenda.
- **Email (Gmail)** — send, triage unread, read, search via the `gws-gmail` OAuth skill.
- **Contacts** — look up people by name/email (resolves "email Bob" → address).
- **Reminders** — read/write macOS Reminders (add, list, complete, due dates).
- **Notes** — a second brain: save/retrieve markdown notes with tags.
- **Web search / research** — delegated to the core agent.

**Messaging**
- **iMessage** — send and read iMessages.
- **WhatsApp** — send messages, list chats, search history (`wacli`).
- **X / Twitter** — post, search, read, mentions, timeline, engagement.

**Screen & device**
- **Screen capture** — see what's on screen (`describe_screen`, multi-display).
- **macOS GUI control** — click/type/scroll/keypress in any app via the
  accessibility tree (`macos-use`), works headless.
- **Browser automation** — navigate, read, fill forms, screenshot; `open_url`
  reuses the active tab on same-origin; `switch_tab`, `scroll` (small/med/large).
- **App launcher** — open any macOS app or URL.
- **File search** — Spotlight (`mdfind`) by content, name, or type.
- **Keyboard / system** — `press_key` (with arrow aliases), `type_text`
  (clipboard-paste for emoji/non-ASCII, `append` mode), `volume`, `brightness`.

**Meetings & calls**
- **Meeting join** — join Zoom or Google Meet with computer audio; `summon`
  (Zoom + screen share); look up meeting IDs/PINs from calendar + contacts.
- **Conversational phone calls** — outbound calls, meeting dial-in, concurrent
  calls, auto-summary on hang-up (`/phone-conversation`).

**Task control**
- `cancel_task` — cancels by writing a `CANCEL_INSTRUCTION` task into the pipeline.
- `recent_context` — re-reads `state/voice-session-context.json` so voice can pick
  up a thread that predates its rolling context window.
- `get_core_status` / `toggle_tasks` / `get_current_time`.

---

## 3. Vision — Sutando watches your screen

A push-mode vision pipeline streams screen frames into the live Gemini session.

- **Vision tools** — `start_vision` / `stop_vision` / `send_vision_frame`; a local
  control server (`:7848`) lets the web client's 👁 Watch button drive it.
- **screen-companion skill** — pre-configured "watch with me" modes: guided-setup,
  paper-reading, pair-debug, pair-review-code. Sutando watches and helps in real
  time without you narrating intent each session.
- **screenshot-explain** — capture the screen and answer a question about it
  ("what does this error mean", "what's this chart showing").
- **Discord screen-share** — the discord-voice bot can share its screen in-channel.

---

## 4. In-session artifact cache

Load a document once, then ask about it repeatedly with no task-bridge round-trip
(`set_active_artifact` / `query_active_artifact` / `clear_active_artifact`). Cuts
multi-turn document Q&A from minutes to seconds. Cleared on session end.

---

## 5. Skills

Skills are self-contained, optional feature modules under `skills/`. Core runs
without any of them.

**Briefings & daily flow**
- `morning-briefing` / `morning-briefing-pro` — email + calendar + reminders +
  weather + overnight messages + a daily insight, delivered by voice or DM.
- `calendar-prep` / `meeting-prep` — pre-meeting prep: attendees, last email
  thread, prior notes, talking points, agenda.
- `commute-and-weather` — "should I bike today" decisions (calendar × weather).
- `linear-or-github-triage` — daily issue triage across Linear + GitHub; standup draft.

**Communication**
- `gws-gmail-voice` — inline Gmail triage/read/search for voice/phone.
- `email-triage` — triage inbox by urgency, draft and send replies.
- `x-twitter` — full X/Twitter posting and monitoring.
- `whatsapp` — WhatsApp messaging via `wacli`.
- `bot2bot-post` — coordination messages between Sutando nodes.

**Voice & phone**
- `phone-conversation` — conversational phone calls and meeting dial-in.
- `discord-voice` — live voice presence in a Discord channel + screen share.

**Vision & screen**
- `screen-companion` — real-time screen-watching assistant (see §3).
- `screen-record` — screen recording with narration/subtitles.
- `screenshot-explain` — Q&A about the current screen.

**Media generation**
- `image-generation` — images (Gemini Flash Image) and video (Veo).
- `make-viral-video` — short news-explainer videos, self-healing, pluggable TTS.
- `gemini-tts` / `openai-tts` — text→speech for narration and audio notes.

**Developer tools**
- `code-reviewer` — senior-engineer review of a GitHub PR (voice-callable).
- `claude-codex` / `claude-gemini` — drive the local Codex / Gemini CLIs.
- `claude-router` — auto-pick the best local delegate for a task.
- `regression-search` — find when a phone-call feature regressed.
- `call-diagnostics` — call diagnostics & repair.

**Monitoring & finance**
- `info-radar` — monitor arXiv, GitHub, Hacker News, tech news for chosen topics.
- `deal-finder` — scan marketplaces for used-item listings matching saved criteria.
- `subscription-scanner` — monthly recurring-charge scan; `/paidsubscriptions` panel.
- `receipt-to-expense` — receipt photo → categorized expense line → CSV/Sheet.

**System & automation**
- `macos-tools` — screen capture, calendar, reminders, contacts, Mail, Spotlight.
- `macos-use` — accessibility-tree GUI control of any macOS app.
- `context-drop` — global-hotkey capture of the current selection into a task.
- `schedule-crons` — recurring scheduled tasks.
- `proactive-loop` — Sutando's autonomous loop: watches tasks, runs health checks,
  builds missing capability on a recurring schedule.

**Self-maintenance & introspection**
- `self-diagnose` — narrative of what the agent has been doing, what's broken,
  what to prioritize; cross-node gather over SSH.
- `quota-tracker` — Claude Code quota usage (5h / 7d windows, reset times).
- `report-feedback` — structured feedback reporting.

**Memory & fleet sync**
- `cross-node-sync` — rsync-over-SSH sync of memory + notes between Sutando
  machines; regenerates `MEMORY.md` after each sync.

**Skill management**
- `skill-installer` — install agent skills.
- `superpower-station` — browse / install / publish skills and cloud tools.

---

## 6. Memory & cross-node sync

- **File-based memory** — durable facts about you, feedback, project context, and
  external-system references, indexed in `MEMORY.md`.
- **Conversation store** — every turn is mirrored into a time-indexed SQLite
  database (`data/conversation.sqlite`) alongside the text `conversation.log`,
  making "what did we say around 9pm on the 4th" a single query. Best-effort —
  the text log stays primary truth. (Requires Node ≥ 22.13 to populate; degrades
  silently otherwise.)
- **Cross-node sync** — memory + notes rsync between fleet machines; each host
  writes a liveness heartbeat so cores can see who's alive.

---

## 7. Observability & reliability

- **Dashboard** (`:7844`) — capability matrix, service health, activity feed,
  system stats, quota.
- **Health-check** — detects stuck task loops, macOS permission denials, down
  services; won't double-queue work when a core is already running.
- **Outbox audit log** — every outbound bridge message is logged.
- **Core liveness** — each core writes `state/cores/<host>.alive` every 30s.
- **Pidfile lock** — the voice agent refuses to start a duplicate that would
  strand its ports.
- **Resilience** — bridges meter usage and throttle on cap-hit; the task watcher
  is exception-contained; voice results retry then fall back to a Discord DM.

---

## 8. Architecture notes

- **Three spaces** — Code (the git checkout), State (`$SUTANDO_WORKSPACE`,
  default `~/.sutando/workspace/`), Memory (`$SUTANDO_MEMORY_DIR`). See
  [`workspace-design.md`](workspace-design.md).
- **Task bridge** — every surface writes `tasks/task-*.txt`; the core agent
  processes them and writes `results/`; bridges poll and reply.
- **Skills are optional** — core boots and runs with any or all skills removed.
- **Inline tools vs skills** — inline tools are thin, instant wrappers; anything
  with real logic is a skill.

For setup and operation see `README.md`; for the built-in tool surface see
[`built-in-tools.md`](built-in-tools.md).
