# macos-tools

A Claude Code skill for AI agents.

## Install

```bash
# Clone and install
git clone https://github.com/liususan091219/sutando.git
cd sutando
bash skills/install.sh
```

Or manually:
```bash
ln -s /path/to/sutando/skills/macos-tools ~/.claude/skills/macos-tools
```

## What's included

5 scripts:
- `contacts.py` — contacts
- `screen-capture.sh` — screen capture
- `reminders.py` — reminders
- `calendar-reader.py` — calendar reader
- `email-sender.py` — email sender

## Usage

- **Screen**: "What's on my screen?", "help me with this", "describe what I'm looking at"
- **Calendar**: "What's on my schedule?", "do I have meetings today?"
- **Reminders**: "Add a reminder", "what's on my todo list?", "mark X as done"
- **Contacts**: "What's Bob's email?", "find contact for..."
- **Email**: "Send an email to...", "draft a message to..."
- **File search**: "Find my resume", "where's that PDF?"

## Requirements

- macOS (uses AppleScript for system integrations)
- Claude Code installed

## License

MIT

---

Built by [Sutando](https://github.com/liususan091219/sutando) — a personal AI agent platform.
