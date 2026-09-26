---
name: wizard
description: Give the owner a short tour of the important features — text plus, on the desktop, a local "tour" card whose buttons open the key pages. Triggered by the /wizard composer command.
---

# wizard

**Usage**: `/wizard` — the owner types it in a room; the message arrives as the text `/wizard`.

## What to do

1. **Present the tour card first** (desktop only; the script is a no-op elsewhere):
   ```bash
   python3 skills/wizard/scripts/present-tour.py --room <room_id>
   ```
   It finds the app's local-card presenter beside the engine checkout and presents the shipped
   `tour` card — six stops with buttons that open the page: who you are and who can reach you,
   channels, runtime, the owner's notifications, all their agents, explore rooms. Without the
   desktop presenter it prints `text-only` and exits 0; give the tour in text alone.
2. **Then the text**, five or six sentences a newcomer needs in the first minute, in the owner's
   language:
   - You live on their Mac, do real work, and remember what you learn.
   - Rooms are where they, other people and other agents meet; you are in the rooms they invite you to.
   - @-mentioning you is how they reach you; the Attention panel in a room header collects what
     needs them.
   - Agent Settings holds your identity, channels and runtime; Room Settings › Agent-native sets
     what you may read and how you respond in that room.
   - Say the card is there: "tap any stop on the card to open it".
3. **Stop there.** No feature list, no settings dump. If they ask about one stop, answer that one —
   and present the matching card (see the local-cards guidance in the app's agent memory).

Voice narration is a later addition to this skill; do not attempt it now.

## Notes

- The card directory and presenter belong to the desktop app (`engine/local-cards/`,
  `engine/local-card.py` beside this checkout); this skill only calls them. A missing presenter is
  the normal case on a non-desktop install, not an error.
- The composer command is discoverability only: the client sends the bare text `/wizard`; nothing
  is intercepted client-side.
