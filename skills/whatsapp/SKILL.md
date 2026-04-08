---
name: whatsapp
description: "Send and search WhatsApp messages via wacli (local sync, WhatsApp Web protocol). Use after the user has run wacli auth."
---

# WhatsApp (wacli)

Send messages, list chats, and search history using [wacli](https://github.com/steipete/wacli) — a local CLI backed by a synced store at `~/.wacli` (override with `--store DIR`).

## When to use

- The user asks to message someone on WhatsApp, list chats, or search their WhatsApp history.
- **Not** available until `wacli auth` has completed successfully on this Mac.

## Setup

1. Install: `brew install steipete/tap/wacli`
2. Authenticate (QR code): `wacli auth`
3. Optional: keep syncing in the background: `wacli sync --follow` (in a terminal or via your process manager)
4. Optional env (device label in WhatsApp): see `.env.example` — `WACLI_DEVICE_LABEL`, `WACLI_DEVICE_PLATFORM`

Diagnostics: `wacli doctor`

## Usage

```bash
# Send (use E.164 for phone numbers, e.g. +14155551234)
wacli send text --to "+14155551234" --message "Hello!"

# Send a file
wacli send file --to "+14155551234" --file /path/to/file.jpg --caption "Optional caption"

# List chats (JSON for scripting)
wacli chats list --limit 20
wacli --json chats list --limit 50

# Search messages
wacli messages search "keyword" --limit 10

# Groups
wacli groups list
```

Pass `--json` on supported commands for machine-readable output.

## Safety

- **Always confirm message content with the user before sending** (same as iMessage/SMS).
- wacli is a third-party tool; the user accepts WhatsApp ToS and linked-device risk.

## Notes

- Session and history live under `~/.wacli` by default — not in Sutando `.env`.
- Human-readable output by default; use `--json` when parsing in scripts.
