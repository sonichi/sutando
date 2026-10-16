---
name: tutorial
description: "Show a new owner what to try first. Voice: on \"tutorial\", \"walk me through\" or \"show me what you can do\", walk the workspace tutorial one short section at a time. AG2 Space: right after the first-contact greeting in the owner's DM, create (or refresh) one \"Getting started\" HTML page there, a short AG2 Space tour with one thing to try per part, and summon the owner to it."
user-invocable: true
---

# tutorial

Two modes. Pick by where the request came from.

## Voice mode

When the owner says "tutorial", "walk me through" or "show me what you can do" (voice or text):

1. Read `<workspace>/notes/first-time-tutorial.md`.
2. Deliver the first section as a voice-friendly summary (1–2 sentences).
3. Wait for them to try it; when they come back, deliver the next section.
4. Continue until done or they say stop.

Keep each step conversational and brief: this is spoken, not read. Say what to try and skip setup
details unless asked. A section that needs an app they have not connected (email, phone, the
morning briefing) gets one sentence on connecting it, not the full step.

## AG2 Space mode: the Getting started page

Used at first contact with the owner (the engine's "First contact with your owner" rules), and
whenever the owner asks for the tutorial in their AG2 Space DM.

**The greeting comes first.** Send the first-contact reply in the DM before anything here. The page
is extra: if any step below fails, the greeting stands. Do not edit, retract or repeat it, and do
not retry the page in the same turn.

1. Your name: `ag2.whoami` (the `ag2-space` tools). Use your own display name, not the owner's.
2. Create or refresh the page and summon the owner to it:

   ```bash
   python3 skills/tutorial/scripts/getting_started.py publish \
     --room '<owner DM room id>' --name '<your name>' --owner '<owner mxid>'
   ```

   `--core` defaults to `auto`: the core runtime the engine resolves
   (`bash scripts/sutando-config.sh core-runtime`: the launcher's `SUTANDO_CORE_RUNTIME` stamp,
   else `core.runtime` in config). Pass `--core claude|codex|unknown` to override it.

   It prints one JSON line: `action` is `created`, `updated` or `unchanged`; `kind` is the page's
   `html-<id>`. On `created` it summons the owner, so the card in the DM is the one line that
   carries the page: post no other message about it. On `updated` / `unchanged` it summons no one
   (`--summon` forces it, e.g. when the owner asks for the tutorial again).
3. Exit 3 (room-commons not installed) or 4 (a room-commons call failed): leave the greeting as is
   and say nothing about the page unless the owner asks. A failure that is a Sutando or AG2 Space
   defect goes to `report-feedback`.

Run it only in the owner's own DM. The page holds your name and fixed text, nothing from the
owner's accounts, and the question about their work stays in chat.

**One page per room.** `<workspace>/state/tutorial/getting-started.json` records the page's kind per
room. A re-link or reinstall that reuses the DM rewrites that page in place; a page someone removed
from the list is created again. `getting_started.py plan --room <room>` shows what `publish` would
do; `render --name <name>` prints the HTML.

**What the page says.** A short AG2 Space tour. Each part is one line on what it is and one
thing to try: Rooms, Commons, Teamwork, Calls & voice, Connect other agents (Dot, Grok Bot, Muse,
Other; Claude Code and Codex are Sutando cores chosen at setup, not agents you add), Connect your
apps (Settings → Integrations), Activity & notices, Mobile. On a Claude Code core it adds
"Import your Claude Code history"; on Codex or an unknown core that part is left out. UI labels
match the AG2 Space client; reword them here when the client renames one.

**The prompts.** Each card's Copy button puts its prompt on the clipboard; where the page's sandbox
refuses the clipboard, the prompt is selected instead and the button says to press ⌘C. The page
cannot type into the composer: the HTML surface has no such hook.
