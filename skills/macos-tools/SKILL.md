---
name: macos-tools
description: "macOS native integrations: screen capture, calendar, reminders, contacts, email (Mail.app), Spotlight search. Use when the user asks about their screen, schedule, to-do list, contacts, or wants to send email on macOS."
---

# macOS Tools

Native macOS integrations via AppleScript. No API keys needed — works on any Mac.

**Calendar, Reminders and Contacts need the owner's consent.** Driving those apps raises a macOS
permission prompt on the owner's screen. The order: (1) the Superpower Station connector
(`composio_find` → `composio_exec`); (2) if it is not connected, the owner's own tools when they
are in the tool list (`mcp__claude_ai_Google_Calendar__*`); (3) otherwise ask the owner. Run the
scripts below with `--owner-asked` only when the owner asked for the local app in this
conversation — without it they refuse (exit 2). A macOS denial (exit 3) is final: say so, never
retry or re-prompt. The `native-pim-guard` hook denies raw `osascript`/`open -a` commands against
these apps for the same reason.

## When to Use

- **Screen**: "What's on my screen?", "help me with this", "describe what I'm looking at"
- **Calendar**: "What's on my schedule?", "do I have meetings today?"
- **Reminders**: "Add a reminder", "what's on my todo list?", "mark X as done"
- **Contacts**: "What's Bob's email?", "find contact for..."
- **Email**: "Send an email to...", "draft a message to..."
- **File search**: "Find my resume", "where's that PDF?"

## Tools

### Screen Capture
```bash
bash "$SKILL_DIR/scripts/screen-capture.sh"
```
Returns path to PNG screenshot. Use the Read tool on the path to view it.

### Calendar
Prefer the Superpower Station connector (`composio_find` / `composio_exec`); when the owner's calendar
app isn't connected, follow the `connect-apps` skill instead of falling back here. macOS Calendar is
only for an owner who asked for the local app, and an empty result from it is not an answer
(the owner's real calendar may live in Google): say you couldn't read their calendar.
```bash
python3 "$SKILL_DIR/scripts/calendar-reader.py" 7 --owner-asked          # next 7 days, JSON
python3 "$SKILL_DIR/scripts/calendar-reader.py" 1 text --owner-asked     # today, plain text
```

### Reminders
```bash
python3 "$SKILL_DIR/scripts/reminders.py" list --owner-asked              # all incomplete
python3 "$SKILL_DIR/scripts/reminders.py" add "Call Bob" --owner-asked     # add reminder
python3 "$SKILL_DIR/scripts/reminders.py" add "Fix bug" "2026-03-17" --owner-asked  # with due date
python3 "$SKILL_DIR/scripts/reminders.py" complete "Call Bob" --owner-asked # mark done
```

### Contacts
```bash
python3 "$SKILL_DIR/scripts/contacts.py" search "Bob" --owner-asked       # find by name
```
Returns name, emails, phones. Use before sending email to resolve names to addresses.

### Email (Apple Mail)
```bash
python3 "$SKILL_DIR/scripts/email-sender.py" "to@example.com" "Subject" "Body"
python3 "$SKILL_DIR/scripts/email-sender.py" "to@example.com" "Subject" "Body" --draft
```
Sends via Mail.app. Use `--draft` to create without sending. **Always confirm with user before sending.**

### Spotlight File Search
```bash
mdfind "quarterly report"                    # search by content or filename
mdfind -name "resume.pdf"                    # search by filename only
```

## Requirements

- macOS (uses AppleScript)
- Calendar, Reminders, Contacts, Mail apps (built into macOS)
- The owner grants the Automation permission when macOS asks them; once denied, do not ask again
