---
name: tutorial
description: "Show a new owner what to try first. Voice: on \"tutorial\", \"walk me through\" or \"show me what you can do\", walk the workspace tutorial one short section at a time. AG2 Space: on an owner DM message with a line `/tutorial` (the setup app ends the first-contact message with one), after the greeting, create (or refresh) one \"Getting started\" HTML page there, a short AG2 Space tour with one thing to try per part, and summon the owner to it."
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

Used when an owner message in their AG2 Space DM has a line that is exactly `/tutorial`, or the owner
asks for the tutorial there. The setup app ends the automatic first-contact message with that line,
so the page follows the first greeting; the owner can send `/tutorial` again at any time. Run the
script below by its path: a core that cannot find skills by name still has it. The line asks for
this page and nothing else.

**The greeting comes first.** Send the first-contact reply in the DM before anything here. The page
is extra: if any step below fails, the greeting stands. Do not edit, retract or repeat it, and do
not retry the page in the same turn.

1. Your name, never the owner's, and never a reason to stop. Use `ag2.whoami`'s `display_name`
   when it has one. When it is null, pass `--name auto`, which falls back in this order:
   - your Matrix profile displayname from your homeserver;
   - a name made from your mxid's localpart (`@chi-air-blue.agent:…` gives `Chi Air Blue`);
   - `Sutando`.

   Your mxid is `$AGENT_MXID`, else the AG2 Space channel env file's, else `--agent-mxid`.
2. Tailor the prompts when you know the owner's work. You know it when either:
   - the first-contact message has a line `I picked: <labels>.`, for example
     `I picked: Founder, Product Manager.`. These are the persona labels the owner chose at
     onboarding, or their slugs when the client had no labels; or
   - `user_profile.md` already says what they do.

   In that case write your own prompts for this owner into a temporary JSON file, one per slot:
   `dm`, `commons`, `teamwork`, `apps`, `import`, `finale`. Write 4–6 of them.
   - Each is first person, practical, one line, at most 200 characters, with no links or markup.
   - Each must run on a fresh install: only the `apps` slot may assume a connected app.
   - The slot decides where the prompt is pasted (your DM or a room), not the wording.

   Pass the file as `--prompts <file>`, and pass the `I picked:` payload verbatim as
   `--roles "Founder, Product Manager."` (the whole line, `I picked:` included, works too). Do not
   map it to slugs: the script takes labels or slugs and adds a "Picked for: Founder, Product
   Manager" line under the heading. Without `--roles`, the saved inputs' `roles` are used (step 3).
   Otherwise leave both out. A missing, bad or partly bad file never fails the run: each slot without a usable prompt
   keeps the generic one.
3. Create or refresh the page and summon the owner to it:

   ```bash
   python3 skills/tutorial/scripts/getting_started.py publish \
     --room '<owner DM room id>' --name '<your name or auto>' --owner '<owner mxid>'
   ```

   Every flag is optional when the setup app saved the inputs:
   `<workspace>/state/onboarding/tutorial-inputs.json` holds `room` (the owner's DM), `owner`,
   `agent_mxid` and `roles` (the picked personas), and `publish` fills whatever the command line
   leaves out from it; `--name` defaults to `auto`. The file's `owner` and `roles` apply only when
   publishing in its own `room`. A missing or malformed file, or a field of the wrong shape, is
   ignored; with no `--room` and no usable saved room, the script exits 2.

   You need not source any env file. `room_commons.py` is found beside this skill, else in the
   installed skills directory. When the environment names none of its credentials, they are read
   from the AG2 Space channel env file through the shared resolver
   (`src/channel_credentials.py`). They are passed to that process only and never printed.

   `--core` defaults to `auto`: the core runtime the engine resolves
   (`bash scripts/sutando-config.sh core-runtime`: the launcher's `SUTANDO_CORE_RUNTIME` stamp,
   else `core.runtime` in config). Pass `--core claude|codex|unknown` to override it.

   It prints one JSON line:
   - `action`: `created`, `updated` or `unchanged`;
   - `kind`: the page's `html-<id>`;
   - `name`: the name it used.

   On `created` it summons the owner, so the card in the DM is the one line that
   carries the page: post no other message about it. On `updated` / `unchanged` it summons no one,
   including when it adopts a page it found by its ownership marker. The one exception is
   finishing a page an earlier run created but did not complete. `--summon` forces a summon, e.g.
   when the owner asks for the tutorial again.
4. Exit 3 (room-commons not installed) or 4 (a room-commons call failed): leave the greeting as is
   and say nothing about the page unless the owner asks. A failure that is a Sutando or AG2 Space
   defect goes to `report-feedback`.

Run it in the owner's own DM (the default, `--audience dm`). Only when the owner asks for the page
in another room, publish it there with `--audience room`: the page then says "your agent" instead
of your name. Summon only whoever the owner asked for. Either way the page holds your name or "your
agent" and fixed text, nothing from the owner's accounts, and the question about their work stays
in chat.

**One page per room, created once.** `<workspace>/state/tutorial/getting-started.json` keeps
each room's entry.
- Before the page is made, the entry records `creating` and an operation id.
- The page is made with `room.commons.pages.create`, whose first body already carries the
  ownership marker (`<meta name="sutando-tutorial" content="<agent mxid>">`). A retry with the
  same operation id returns the same page, so an unclear create is retried, never repeated.
- The entry is `pending` until the rest of the page is appended, then `complete`. Any failure
  leaves an entry the next run resumes; nothing is created twice.
- A re-link or reinstall that reuses the DM rewrites the page in place. A page someone removed
  from the list is created again.
- If the marker file is lost, a listed "Getting started" page is reused only when its text
  carries this agent's ownership marker. A same-titled page without it is never touched. More
  than one marked page, or no agent id to check with, stops with exit 4 and the reason.

`getting_started.py plan --room <room>` shows what `publish` would do; `render --name <name>`
prints the HTML.

**What the page says.** A short, interactive AG2 Space tour (`scripts/tour_page.py`). Each step is
one line on what it is and one thing to try, in this order:
1. Your DM: what the owner asks for themselves comes back in the DM, never in a room.
2. Rooms.
3. Commons.
4. Connect other agents: Dot, Grok Bot, Muse. Claude Code and Codex are Sutando cores chosen at
   setup, not agents you add.
5. Teamwork.
6. Calls & voice: the headphone icon in the room header only.
7. Connect your apps: Settings → Integrations.
8. Activity & notices.
9. Mobile.

On a Claude Code core it adds "Import your Claude Code history"; on Codex or an unknown core that
step is left out. Every prompt says where to paste it (your DM or a room). When every step is
done, a closing card offers one more prompt. UI labels match the AG2 Space client and its current
screenshots; reword them in `tour_page.py` when the client changes.

**Screenshots.** `assets/*.jpg` are real UI crops, adapted from Zerlinda's Sutando's "Getting
started with AG2 Space" page. Each is cropped so that no person's name, room or account shows,
and each is at most 60 KB. They are inlined as `data:` URIs, so the page stays self-contained. A
step names its images in `Section.images` as (file, alt).

**How the page behaves.**
- It is one self-contained HTML document with no external URL.
- An inline SVG icon and accent colour per step (AA contrast in both themes), under a hero with
  your name.
- The tour opens on one step, with the step's title, numbered dots, Back and Next, a "Show all"
  toggle and the arrow keys. In Show all, a dot scrolls to its step.
- "Mark done" moves to the next step that is not done (in step mode only) and fills the progress
  bar.
- Progress is saved in the page runtime's `window.artifact.state` under `gs-done-<viewer page id>`,
  only once the runtime names the viewer. Otherwise it stays in this browser's localStorage, then
  memory. The checkboxes stay disabled until progress has loaded.
- Copy outcomes are announced in a live status region. Animations stop under
  `prefers-reduced-motion`. Without scripts, every step shows.

**Size.** With its screenshots the page is about 215 KB, over the 196,608 bytes Room Commons
takes in one write. The first piece goes in the create's own body; the rest are appended in pieces
of at most 120,000 bytes (`CHUNK_BYTES`). Each piece is passed to `room_commons.py` as one argument,
and Linux caps a single argument at 131,072 bytes. A refresh is one `replace` when both the old and
new text fit one piece; otherwise it clears the old text in pieces and appends the new.

**The prompts.** Each card's Copy button puts its prompt on the clipboard; where the page's sandbox
refuses the clipboard, the prompt is selected instead and the button says to press ⌘C. The page
cannot type into the composer: the HTML surface has no such hook.
