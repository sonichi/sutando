# Built-in tools (capability catalog)

Reference for the bash/CLI tools every Sutando session can call directly. Linked from `CLAUDE.md` to keep the per-session context budget small — open this file when you need to know what's available rather than carrying it on every turn.

**Calendar** — read Google Calendar events via `gws calendar`:
```bash
gws calendar +agenda --today            # today's events (table format by default)
gws calendar +agenda --week              # this week
gws calendar +agenda --days 7 --format json   # next 7 days, JSON for parsing
```

**Screen capture** — see what's on the user's screen. The screen-capture server runs on port 7845 (started by `src/startup.sh`):
```bash
curl -s http://localhost:7845/capture | python3 -c 'import json,sys; print(json.load(sys.stdin)["path"])'
# Multi-display: add ?all=true to capture every display, or ?display=N for a specific one.
```
Then use the Read tool on the returned path to view the screenshot. Use this for any screen-related question: "what am I looking at", "help me with this", "what's on my screen", etc.

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

**Contacts** — look up people by name or email:
```bash
python3 ~/.claude/skills/macos-tools/scripts/contacts.py search "Bob"   # find by name
```
Use before sending email to resolve "email Bob" → actual email address. Returns name, emails, phones.

**iMessage** — send and read iMessages:
```bash
imsg send --to "+14155551234" --text "Hello!"    # send message
imsg chats                                        # list recent chats
imsg messages --chat "+14155551234" --limit 10    # read messages
```
Always confirm message content with user before sending.

**WhatsApp** — send messages via WhatsApp (requires `wacli auth` first):
```bash
wacli send text --to "+14155551234" --message "Hello!"
wacli chats list --limit 20
wacli messages search "keyword" --limit 10
```

**X (Twitter)** — post, search, read, and monitor:
```bash
python3 skills/x-twitter/x-post.py post "Tweet text"                       # post
python3 skills/x-twitter/x-post.py post "With video" --media /path/to.mp4  # with media
python3 skills/x-twitter/x-post.py search "query"                          # search recent
python3 skills/x-twitter/x-post.py read 123456789                          # read tweet
python3 skills/x-twitter/x-post.py mentions                                # recent @mentions
python3 skills/x-twitter/x-post.py timeline                                # your tweets
python3 skills/x-twitter/x-post.py engagement 123456789                    # likes/rt/views
```
Requires X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET in .env.
Always confirm post content with user before publishing.

**Reminders** — read/write macOS Reminders (to-do list):
```bash
python3 ~/.claude/skills/macos-tools/scripts/reminders.py list             # incomplete reminders
python3 ~/.claude/skills/macos-tools/scripts/reminders.py add "Call Bob"    # add reminder
python3 ~/.claude/skills/macos-tools/scripts/reminders.py add "Fix bug" "2026-03-17"  # with due date
python3 ~/.claude/skills/macos-tools/scripts/reminders.py complete "Call Bob"  # mark done
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

Preferred (interactive): Use **Playwright MCP tools** (`mcp__playwright__*`) or **Chrome plugin** (`mcp__claude-in-chrome__*`). These provide real browser control with live DOM access, screenshots, and form interaction.

**Default: navigate within the active tab when the next URL has the same origin (scheme + host + port) as the current tab.** Only spawn a new tab for cross-origin navigation, when an existing tab is the only context that holds the relevant state (a logged-in session, a long-running app), or when the user explicitly asks for a new tab. `localhost:7844` and `localhost:8080` are DIFFERENT origins — same hostname, but different ports → different services → don't share a tab. This keeps the browser tab count bounded during multi-step flows — without it, every `mcp__claude-in-chrome__navigate` opens a fresh tab and the user ends up with dozens of half-used tabs after a research session.

Fallback (non-interactive / headless): `src/browser.mjs` for scripted or background use:
```bash
node src/browser.mjs "https://example.com"                    # get page text
node src/browser.mjs "https://example.com" screenshot         # full-page screenshot → path
node src/browser.mjs "https://example.com" "fill:#email:me@x.com" "click:#submit"  # fill + click
```
Actions: `text`, `screenshot`, `pdf`, `html`, `click:<selector>`, `fill:<selector>:<value>`, `select:<selector>:<value>`, `wait:<ms>`.

**File search (Spotlight)** — find any file on the Mac:
```bash
mdfind "quarterly report"                    # search by content or filename
mdfind -name "resume.pdf"                    # search by filename only
mdfind "kMDItemKind == 'PDF'" -onlyin ~/Documents  # by file type in a folder
```

**Meeting join** — join Zoom or Google Meet with computer audio:
```bash
npx tsx -e "import 'dotenv/config'; import { joinZoomTool } from './src/inline-tools.ts'; joinZoomTool.execute({}, null).then(r => console.log(JSON.stringify(r)))"
npx tsx -e "import 'dotenv/config'; import { joinGmeetTool } from './src/inline-tools.ts'; joinGmeetTool.execute({ meetingCode: 'abc-defg-hij' }, null).then(r => console.log(JSON.stringify(r)))"
npx tsx -e "import 'dotenv/config'; import { summonTool } from './src/inline-tools.ts'; summonTool.execute({}, null).then(r => console.log(JSON.stringify(r)))"
```
- `joinZoomTool` — Zoom desktop app + computer audio (no screen share)
- `joinGmeetTool` — Chrome browser + computer audio + camera off
- `summonTool` — Zoom + screen share + computer audio

**Conversational phone calls** — use the `/phone-conversation` skill:
- Outbound calls, meeting dial-in (Zoom/Google Meet), concurrent calls
- Auto-summary when calls/meetings end
- Look up contacts and calendar for numbers/PINs before calling
- The voice agent delegates "call X" and "join my meeting" requests to core via `work`

**Local skills** — check `~/.claude/skills/` for user-installed skills (video processing, etc.). Always prefer a local skill over raw commands when one exists for the task.

**App launcher** — open any macOS app:
```bash
open -a "Safari"                    # open by name
open -a "Slack"
open "https://github.com"           # open URL in default browser
```

**Context drop + shortcuts** — the Sutando menu bar app (`src/Sutando/`) provides global hotkeys. **Live config**: `~/.config/sutando/hotkeys.json` (per-user override) with defaults registered in `src/Sutando/main.swift:944` (`registerHotKey()` action list). When the user asks "what hotkeys do I have", read those sources — don't quote a static list from this file (it would drift behind the actual registration).

Launches automatically via `startup.sh`. Check `tasks/` for dropped context.
