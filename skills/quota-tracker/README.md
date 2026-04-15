# quota-tracker

A Claude Code skill for AI agents.

## Install

```bash
# Clone and install
git clone https://github.com/sonichi/sutando.git
cd sutando
bash skills/install.sh
```

Or manually:
```bash
ln -s /path/to/sutando/skills/quota-tracker ~/.claude/skills/quota-tracker
```

## What's included

2 scripts:
- `credential-proxy.ts` — credential proxy
- `read-quota.py` — read quota

## Usage

- "How much quota do I have left?"
- "Am I close to the rate limit?"
- "When does my quota reset?"
- Before starting expensive tasks

## Requirements

- macOS (uses AppleScript for system integrations)
- Claude Code installed

## License

MIT

---

Built by [Sutando](https://github.com/sonichi/sutando) — a personal AI agent platform.
