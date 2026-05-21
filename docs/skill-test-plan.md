# Skill test plan — post upstream sync

Concrete checklist for live-testing Sutando after the `sonichi/sutando`
upstream sync (checkpoints CP-0–CP-12). Automated checks are green; this
covers what automation can't — feature correctness on real hardware.

For each item: the trigger, what success looks like, and any setup needed.
Mark `[x]` as you confirm it. Report failures with the skill/feature name
and what you observed.

---

## Core surfaces — start here

- [ ] **Rebuild the `.app`** (`bash app/build-app.sh`) and confirm it boots clean.
- [ ] **Voice** — start a voice session; confirm the orb/menu-bar avatar
  reflects state and Sutando responds.
- [ ] **Conversation screen** — open `http://localhost:8080`, confirm it loads
  and the conversation/tasks render.
- [ ] **Settings → Channels** — the Slack card accepts a bot + app token.

## Bridges

- [ ] **Telegram** — DM the bot; first message should auto-onboard you (TOFU)
  and you should get a reply.
- [ ] **Discord** — DM the bot; confirm it receives and replies.
- [ ] **Slack** — after saving tokens in Settings, DM/@mention the bot; confirm
  round-trip. The bridge should auto-start once the token file is written.
- [ ] Send a file/attachment through any bridge — confirm it round-trips.

## New skills (CP-8)

- [ ] **deal-finder** — "run the deal finder" / "check for mac mini deals" —
  scan runs, returns matches or "none". (SMS/Telegram delivery needs those
  channels configured.)
- [ ] **whatsapp** — "list my recent WhatsApp chats" then a test send. Needs
  `wacli auth` first; without it, expect a clear "not paired" error.
- [ ] **context-drop** — select text in any app, fire the context-drop hotkey,
  ask "what context did I just drop". Needs macOS Accessibility permission.
- [ ] **screen-companion** — in a voice session, "start screen companion" /
  "read this paper with me" — loads a config and starts watching.
- [ ] **zoom** — "join my zoom" / "summon" / "dismiss" — regression check that
  the relocated tools still behave exactly as before.
- [ ] **discord-voice** — start the server, join the channel, talk, try "share
  screen". Needs `npm install` (4 deps), a Discord bot token + invite, and a
  separate chrome-devtools-mcp Chrome for screen-share. Heaviest to set up —
  partial verification is fine.

## Diagnostics & memory (CP-9)

- [ ] **self-diagnose** — "self diagnose the last 24 hours" / "what have you
  been doing" — returns a concise narrative.
- [ ] **cross-node-sync / sync-memory** — only relevant with a second machine
  + SSH. Single-host: expect a clean "SUTANDO_SYNC_PEER not set, skipping".

## Tool fixes (CP-10) — quick voice spot-checks

- [ ] **type_text** — type something with an emoji or em-dash → no mojibake;
  "append" mode doesn't replace a selection.
- [ ] **press_key** — "press the down arrow" scrolls (doesn't type "downarrow").
- [ ] **scroll** — "scroll down a little" vs "a lot" move different distances.
- [ ] **open_url** — same-site URL reuses the tab instead of spawning a new one.
- [ ] **cancel_task** — start a task, say "nevermind" — confirm it's cancelled
  when core reaches the cancel instruction.

## App / startup (CP-11)

- [ ] **Dashboard** (`:7844`) — no longer shows "brain offline" with a healthy
  core agent.
- [ ] `python3 src/health-check.py` — summary is not "failed".
- [ ] `bash src/startup.sh` boots all services; service logs land in
  `~/.sutando/workspace/logs/`.

## Vision & conversation-store

- [ ] **Vision** — in a voice session, `curl http://127.0.0.1:7848/vision/state`
  responds; ask Sutando to "watch my screen".
- [ ] **Artifact cache** — "load this PDF" then follow-up questions; the 2nd+
  answers in seconds, not a task round-trip.
- [ ] **conversation-store** — after a session, `data/conversation.sqlite`
  exists with rows. Needs Node ≥ 22.13 to populate; degrades silently below
  that (text `conversation.log` is unaffected either way).

---

## Notes for testers

- Skills are **optional** — an unconfigured skill reads as "needs setup", not
  "broken". Record how far you got.
- discord-voice and cross-node-sync need real external setup — partial
  verification is expected.
- On failure: grab the error text + the skill/feature name. Skill scripts live
  under `skills/<name>/`.
