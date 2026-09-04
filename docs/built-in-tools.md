# Built-in tools (capability catalog)

Reference for the bash/CLI tools every Sutando session can call directly. Linked from `CLAUDE.md` to keep the per-session context budget small — open this file when you need to know what's available rather than carrying it on every turn.

**Calendar** — read Google Calendar events via `gws calendar`:
```bash
gws calendar +agenda --today            # today's events (table format by default)
gws calendar +agenda --week              # this week
gws calendar +agenda --days 7 --format json   # next 7 days, JSON for parsing
```

**Screen capture** — see what's on the user's screen. The screen-capture server runs on port 7845 (started by `src/startup.sh`). Every capture route requires the startup token in `X-Sutando-Capture-Token` — without it the server answers `403 {"status":"error","error":"forbidden"}`:
```bash
TOKEN="$(cat ~/.config/sutando/screen-capture-token)"
curl -s -H "X-Sutando-Capture-Token: $TOKEN" http://localhost:7845/capture \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["path"])'
# Multi-display: add ?all=true to capture every display, or ?display=N for a specific one.
# /capture-video takes the same header — it is the other capture route, gated identically.
```
Then use the Read tool on the returned path to view the screenshot. Use this for any screen-related question: "what am I looking at", "help me with this", "what's on my screen", etc.

A `forbidden` response means the header was missing or stale, **not** that capture is unavailable — re-read the token file rather than restarting the server.

`/capture` returns the display's native resolution — on a Retina screen that is
megabyte-class per frame. Frames entering a live voice session (the Watch/vision
stream, pull or push) are bounded first: anything over 200 KB is resampled to a
1280 px long edge and re-encoded as JPEG q60, measured at ~2.5 MB → ~236 KB on a
3024×1964 display. Frames share one websocket with realtime audio, so an
unbounded frame delays speech, not just vision. Reading a captured file from disk
is unaffected — the bound applies only on the way into a session.

**Notes** — the user's second brain. Save and retrieve notes:
- Save: write to `notes/{slug}.md` with a descriptive filename
- Retrieve: search notes with `Glob("notes/**/*.md")` or `Grep` for content
- Format: each note has a YAML frontmatter with `title`, `date`, `tags` (list), then the content
- Use for: "remember this", "take a note", "save this for later", research summaries, ideas, bookmarks
- Example:
```markdown
---
title: Project idea — voice-controlled home automation
date: 2026-03-16
tags: [ideas, projects, voice]
---
Content here...
```

**Email (Gmail)** — use the `gws-gmail` skill (OAuth, no app password needed):
```bash
gws gmail +send --to "to@x.com" --subject "subj" --body "body"
gws gmail +triage                               # unread inbox summary
gws gmail +read <messageId>                     # read a message
gws gmail users messages list --params 'q=keyword'  # search
```

**Signatures are never auto-inserted — append one yourself.** Gmail attaches the configured signature in its *composer*, so anything that writes a message some other way (the Gmail API, an IMAP `APPEND`-created draft) produces mail with no signature, and no Gmail setting changes that. When drafting or sending on the owner's behalf, append their signature to the body yourself — plain text plus an HTML alternative, so links render in both parts.

**Finding a specific email** — when the obvious query fails, invoke `/email-find <description>`. Broad-before-narrow playbook (full-inbox scan → partner-domain fanout → thread re-walk) that refuses to give up after one or two failed queries. See `skills/email-find/SKILL.md` for the workflow and rules around subject-mismatch + `get_thread` truncation. Per-user partner-domain mappings live in your own memory (the skill describes the file format).

**Contacts** — look up people by name or email:
```bash
python3 $CLAUDE_CONFIG_DIR/skills/macos-tools/scripts/contacts.py search "Bob"   # find by name
```
Use before sending email to resolve "email Bob" → actual email address. Returns name, emails, phones.

**iMessage** — send and read iMessages:
```bash
imsg send --to "+14155551234" --text "Hello!"    # send message
imsg chats                                        # list recent chats
imsg messages --chat "+14155551234" --limit 10    # read messages
```
Always confirm message content with user before sending.

**WhatsApp** — send messages via WhatsApp (unpaired? use the guided connect flow — `skills/whatsapp/scripts/guided_connect.py`, pairing from chat, no terminal; full reference in `skills/whatsapp/SKILL.md`):
```bash
wacli send text --to "+14155551234" --message "Hello!"
wacli chats list --limit 20
wacli messages search "keyword" --limit 10
```
Optional `.env`: `WACLI_DEVICE_LABEL`, `WACLI_DEVICE_PLATFORM` (label shown in WhatsApp → Linked Devices on the user's phone).

**X (Twitter)** — post, search, read, and monitor:
```bash
python3 skills/x-twitter/x-post.py post "Tweet text"                       # post
python3 skills/x-twitter/x-post.py post "With video" --media /path/to.mp4  # with media
python3 skills/x-twitter/x-post.py search "query"                          # search recent
python3 skills/x-twitter/x-post.py read 123456789                          # read tweet
python3 skills/x-twitter/x-post.py mentions                                # recent @mentions
python3 skills/x-twitter/x-post.py timeline                                # YOUR tweets (OAuth1)
python3 skills/x-twitter/x-post.py user-timeline <handle>                  # ANOTHER account (bearer)
python3 skills/x-twitter/x-post.py engagement 123456789                    # likes/rt/views
```
`post`/`mentions`/`timeline` need X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET.
`search`/`read`/`user-timeline` run on X_BEARER_TOKEN alone. `timeline` resolves `users/me` and is
OAuth1-only; `user-timeline` reads the same endpoint by handle over bearer, so it works where
`timeline` cannot. Its `--limit` is 5..100 (`search`'s is 10..100 — different endpoints).
Always confirm post content with user before publishing.

**Reminders** — read/write macOS Reminders (to-do list):
```bash
python3 $CLAUDE_CONFIG_DIR/skills/macos-tools/scripts/reminders.py list             # incomplete reminders
python3 $CLAUDE_CONFIG_DIR/skills/macos-tools/scripts/reminders.py add "Call Bob"    # add reminder
python3 $CLAUDE_CONFIG_DIR/skills/macos-tools/scripts/reminders.py add "Fix bug" "2026-03-17"  # with due date
python3 $CLAUDE_CONFIG_DIR/skills/macos-tools/scripts/reminders.py complete "Call Bob"  # mark done
```
Use for "add a reminder", "what's on my todo list", "remind me to...", "mark X as done".

**macOS GUI control** — click, type, scroll, press keys in any Mac app via `macos-use` MCP skill. Works in non-interactive mode (which is how the proactive loop runs), unlike Claude's built-in computer-use. Accessibility-tree based — no screenshots leave the machine.

Tools (after `bash skills/macos-use/scripts/build.sh && bash skills/macos-use/scripts/install-mcp.sh`):
- `mcp__macos-use__open_application_and_traverse` — open/activate an app, return its a11y tree
- `mcp__macos-use__click_and_traverse` — click at coordinates in a target PID
- `mcp__macos-use__type_and_traverse` — type text into the focused element
- `mcp__macos-use__press_key_and_traverse` — press a named key (Return, Tab, arrows, ...)
- `mcp__macos-use__scroll_and_traverse` — scroll in a direction
- `mcp__macos-use__refresh_traversal` — re-read the a11y tree without acting

Prefer this for any "open X and do Y" task in a native app (Zoom join, Mail compose, Finder navigation). For web pages, prefer Browser automation (below). Full doc in `skills/macos-use/SKILL.md`.

**Browser automation** — navigate, read, fill forms, screenshot web pages:

In the ChatGPT/Codex desktop app, prefer its Browser or Chrome plugin. Those
desktop browser backends are not available to a plain Codex CLI core, even
when the plugin skill is installed. For the Codex CLI core, use Sutando's
persistent Playwright profile below.

**Default: navigate within the active tab when the next URL has the same origin (scheme + host + port) as the current tab.** Only spawn a new tab for cross-origin navigation, when an existing tab is the only context that holds the relevant state (a logged-in session, a long-running app), or when the user explicitly asks for a new tab. `localhost:7844` and `localhost:8080` are DIFFERENT origins — same hostname, but different ports → different services → don't share a tab. This keeps the browser tab count bounded during multi-step flows — without it, every `mcp__claude-in-chrome__navigate` opens a fresh tab and the user ends up with dozens of half-used tabs after a research session.

Codex CLI and non-interactive use: `src/browser.mjs` uses a dedicated persistent
Chrome profile at `<workspace>/data/browser-profile`. Run setup once, sign in to
the sites Sutando may operate, and close the setup window. Later commands reuse
those sessions without accessing the user's everyday Chrome profile:
```bash
node src/browser.mjs setup                                      # one-time visible sign-in
node src/browser.mjs profile                                    # show profile location
node src/browser.mjs "https://example.com"                    # get page text
node src/browser.mjs "https://example.com" screenshot         # full-page screenshot → path
node src/browser.mjs "https://example.com" "fill:#email:me@x.com" "click:#submit"  # fill + click
node src/browser.mjs "https://example.com" --headed           # watch automation live
node src/browser.mjs "https://example.com" screenshot --timeout=60000  # override the 45s command limit
```
Actions: `text`, `screenshot`, `pdf`, `html`, `click:<selector>`, `fill:<selector>:<value>`, `select:<selector>:<value>`, `wait:<ms>`.
Non-interactive commands are bounded to 45 seconds by default; `--timeout` may
raise that command-level limit to at most 300,000 ms. Navigation uses the
remaining command budget, and declared `wait:` actions must fit the budget or
the command fails before launching with guidance to pass a larger `--timeout`.
Up to five seconds of the limit is reserved for cleanup. Normal completion,
errors, timeouts, `SIGINT`, and `SIGTERM` all close the page, context, and browser
before the command exits. A second signal during cleanup restores Node's normal
immediate termination behavior instead of being swallowed.

**File search (Spotlight)** — find any file on the Mac:
```bash
mdfind "quarterly report"                    # search by content or filename
mdfind -name "resume.pdf"                    # search by filename only
mdfind "kMDItemKind == 'PDF'" -onlyin ~/Documents  # by file type in a folder
```

**Meeting join** — join Zoom or Google Meet with computer audio:
```bash
npx tsx -e "import 'dotenv/config'; import { joinZoomTool } from './skills/zoom/tools.ts'; joinZoomTool.execute({}, null).then(r => console.log(JSON.stringify(r)))"
npx tsx -e "import 'dotenv/config'; import { joinGmeetTool } from './src/inline-tools.ts'; joinGmeetTool.execute({ meetingCode: 'abc-defg-hij' }, null).then(r => console.log(JSON.stringify(r)))"
npx tsx -e "import 'dotenv/config'; import { summonTool } from './skills/zoom/tools.ts'; summonTool.execute({}, null).then(r => console.log(JSON.stringify(r)))"
```
- `joinZoomTool` — Zoom desktop app + computer audio (no screen share)
- `joinGmeetTool` — Chrome browser + computer audio + camera off
- `summonTool` — Zoom + screen share + computer audio

**Conversational phone calls** — use the `/phone-conversation` skill:
- Outbound calls, meeting dial-in (Zoom/Google Meet), concurrent calls
- Auto-summary when calls/meetings end
- Look up contacts and calendar for numbers/PINs before calling
- The voice agent delegates "call X" and "join my meeting" requests to core via `work`

**Local skills** — check `$CLAUDE_CONFIG_DIR/skills/` for user-installed skills (video processing, etc.). Always prefer a local skill over raw commands when one exists for the task.

**Trusted capability catalog** — discover, inspect, install, and update skills
from the allowlisted repositories declared in
`skills/trusted-capabilities/manifest.json`:
```bash
C=skills/trusted-capabilities/scripts/catalog.py
python3 "$C" sources
python3 "$C" search browser
python3 "$C" inspect anthropic-skills skills/skill-creator
python3 "$C" install anthropic-skills skills/skill-creator        # dry run
# Review the dry-run output, then copy its exact commit into the write:
python3 "$C" install anthropic-skills skills/skill-creator --commit <40-char-sha> --yes
python3 "$C" update skill-creator                                 # dry run
python3 "$C" update skill-creator --commit <40-char-sha> --yes
```
Skill installs are pinned to an upstream commit and record provenance for later
updates. Tool repositories can be searched and inspected but are
install-disabled because their setup and permissions are source-specific.

**App launcher** — open any macOS app:
```bash
open -a "Safari"                    # open by name
open -a "Slack"
open "https://github.com"           # open URL in default browser
```

**Context drop + shortcuts** — the Sutando menu bar app (`src/Sutando/`) provides global hotkeys. **Live config**: `~/.config/sutando/hotkeys.json` (per-user override) with defaults registered in `src/Sutando/main.swift:944` (`registerHotKey()` action list). When the user asks "what hotkeys do I have", read those sources — don't quote a static list from this file (it would drift behind the actual registration).

The menu-bar app is optional and is not built or launched by the headless core's `startup.sh`; compile and launch the app separately, including `bash skills/context-drop/build.sh` when enabling context-drop. Check `tasks/` for dropped context.

## Model switch (no CLI needed)

```bash
bash scripts/switch-model.sh claude-opus-5          # alias (opus/sonnet/haiku/fable/default) or a claude-* id, optional [1m]
bash scripts/switch-model.sh fable --dry-run        # prints the two effects, changes nothing
```

Two effects, both visible: pins `model` in `$CLAUDE_CONFIG_DIR/settings.json` (what the next launch
inherits — the launcher passes no `--model`), and sends `/model <name>` to the live `sutando-core`
tmux pane so the running session switches now. Every switch is recorded in
`<workspace>/state/model-switch.json` with `previous` and a timestamp. Exit 2 = name refused, nothing
touched; exit 3 = pinned but no live pane found. An owner message like "switch model to opus" is
handled by running this script; the dashboard's Quota tile shows the model the proxy then sees.
