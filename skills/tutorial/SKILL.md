---
name: tutorial
description: "Show a new owner what to try first. Voice: on \"tutorial\", \"walk me through\" or \"show me what you can do\", walk the workspace tutorial one short section at a time. s.space: on an owner DM message with a line `/tutorial` (the setup app ends the first-contact message with one), after the greeting, create (or refresh) one \"Getting started\" HTML page there, the s.space overview and optional step-by-step checklist, and summon the owner to it."
user-invocable: true
---

# tutorial

Two modes. Pick by where the request came from.

## Voice mode

When the owner says "tutorial", "walk me through" or "show me what you can do" (voice or text):

1. Read `<workspace>/notes/first-time-tutorial.md`. The skill's launch hook
   (`scripts/seed_voice_tutorial.py`, the manifest's `on_launch`) copies the shipped walkthrough,
   `skills/tutorial/voice/first-time-tutorial.md`, there once and never replaces the owner's copy;
   if the workspace file is missing, read the shipped one.
2. Deliver the first section as a voice-friendly summary (1–2 sentences).
3. Wait for them to try it; when they come back, deliver the next section.
4. Continue until done or they say stop.

Keep each step conversational and brief: this is spoken, not read. Say what to try and skip setup
details unless asked. A section that needs an app they have not connected (email, phone, the
morning briefing) gets one sentence on connecting it, not the full step.

## s.space mode: the Getting started page

Used when an owner message in their s.space DM has a line that is exactly `/tutorial`, or the owner
asks for the tutorial there. The setup app ends the automatic first-contact message with that line,
so the page follows the first greeting; the owner can send `/tutorial` again at any time. Run the
script below by its path: a core that cannot find skills by name still has it. The line asks for
this page and nothing else.

**The greeting comes first.** Send the first-contact reply in the DM before anything here. The page
is extra: if any step below fails, the greeting stands. Do not edit, retract or repeat it, and do
not retry the page in the same turn.

1. Publish the generic editable-draft guide. Do not use the owner's profile, chosen roles,
   the agent display name, or personalized prompts in Room Commons. The current Commons write
   and read interfaces do not enforce an owner-only audience for this persistent HTML.
2. Create or refresh the page and summon the owner to it:

   ```bash
   python3 skills/tutorial/scripts/getting_started.py publish \
     --room '<owner DM room id>' --owner '<owner mxid>'
   ```

   The setup app's saved `tutorial-inputs.json` supplies routing defaults only. The agent mxid
   is required for the ownership marker (`--agent-mxid`, saved same-room routing, or the s.space
   connection); the publisher refuses to run without one. Legacy `--name`, `--prompts` and `--roles` options are ignored by `publish`;
   the offline renderer is for local previews. If an interrupted write predates the privacy
   guard, it is never replayed. When the current page matches a saved snapshot and its publisher
   can be verified, publish replaces the current page with the generic guide. It does not remove
   old room pages or versions.

   Before it writes the page, `publish` uploads the screenshots' full-resolution captures to the
   room's media (see "Screenshots are Commons asset references" in docs/tutorial-publisher.md) and reports it in the result's
   `upload`: `uploaded`, `skipped` (already in the room at the same hash, or mapped by `--media`),
   `fallback` (inlined instead) and `reason`. An encrypted room gets the inlined page; that is
   expected, not an error. `--no-upload` uploads nothing.

   You need not source any env file. `room_commons.py` is found beside this skill, else in the
   installed skills directory. When the environment names none of its credentials, they are read
   from the s.space channel env file through the shared resolver
   (`src/channel_credentials.py`). They are passed to that process only and never printed.

   `--core` defaults to `auto`: the core runtime the engine resolves
   (`bash scripts/sutando-config.sh core-runtime`: the launcher's `SUTANDO_CORE_RUNTIME` stamp,
   else `core.runtime` in config). Pass `--core claude|codex|unknown` to override it.

   It prints one JSON line:
   - `action`: `created`, `updated` or `unchanged`;
   - `kind`: the page's `html-<id>`;
   - `name`: the generic display label.

   On `created` it summons the owner, so the card in the DM is the one line that
   carries the page: post no other message about it. On `updated` / `unchanged` it summons no one,
   including when it adopts a page it found by its ownership marker. The one exception is
   finishing a page an earlier run created but did not complete. `--summon` forces a summon, e.g.
   when the owner asks for the tutorial again.
4. Exit 3 (room-commons not installed) or 4 (a room-commons call failed): leave the greeting as is
   and say nothing about the page unless the owner asks. A failure that is a Sutando or s.space
   defect goes to `report-feedback`.

Run it in the owner's own DM (the default, `--audience dm`). Only when the owner asks for the page
in another room, publish it there with `--audience room`. The room's entry records the audience, and a
later run without `--audience` keeps it. Summon only whoever the owner asked for. In either audience,
the published page says "your agent" and uses generic examples. The question about the owner's work
stays in chat, not in Commons.

**What the page says.** Every step is something the reader asks their agent to do: the step's
button sends a fixed request, and the click path is collapsed under "Where is it?".

- The compact DM guide opens on five explorable topics: Ecosystem, Multiplayer, Calls & Local Tools,
  Commons & Tasks, and Sutando Guides. Its 14-step checklist covers the DM, rooms, Commons, workers,
  other agents, teamwork, apps, scheduling, voice, memory, the morning check-in, activity, the
  mobile app and reporting a problem.
- Each step's button opens an editable draft. A compatible Commons host lists the reader's own agents
  and asks for confirmation before sending.
- The published guide is generic even if the saved record's room, owner and agent match. The
  synthetic personalized variant is a local renderer preview only, not an authorized publication
  route.

The room/reference tour is generated from `feature-list.json`. Its 15 steps carry the same asks:
- "Do it with your agent" sends a step's request through `artifact.sendToMyAgent`, with the fixed
  key `tutorial-<step id>`, when the host offers it. Copy stays available.
- Each step's screenshots sit under its "Where is it?".
- A step with a runtime restriction is shown only on the matching core.

Publisher internals are in [`docs/tutorial-publisher.md`](../../docs/tutorial-publisher.md):
- one page per room and the rebuild rule;
- the feature list format;
- screenshots as room media;
- size limits and how the page behaves.
