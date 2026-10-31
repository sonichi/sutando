---
name: tutorial
description: "Show a new owner what to try first. Voice: on \"tutorial\", \"walk me through\" or \"show me what you can do\", walk the workspace tutorial one short section at a time. s.space: on an owner DM message with a line `/tutorial` (the setup app ends the first-contact message with one), after the greeting, create (or refresh) one \"Getting started\" HTML page there, a short s.space tour with one thing to try per part, and summon the owner to it."
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

## s.space mode: the Getting started page

Used when an owner message in their s.space DM has a line that is exactly `/tutorial`, or the owner
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

   Your mxid is `--agent-mxid`, else `$AGENT_MXID`, else the s.space channel env file's (the
   launcher-named device file first). `publish` refuses to run without one: a page without the
   ownership tag could never be found again.
2. Tailor the prompts when you know the owner's work. You know it when either:
   - the first-contact message has a line `I picked: <labels>.`, for example
     `I picked: Founder, Product Manager.`. These are the persona labels the owner chose at
     onboarding, or their slugs when the client had no labels; or
   - `user_profile.md` already says what they do.

   In that case write your own prompts for this owner into a temporary JSON file, one per slot:
   `dm`, `commons`, `teamwork`, `apps`, `finale`. Write 4–5 of them.
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

   Before it writes the page, `publish` uploads the screenshots' full-resolution captures to the
   room's media (see "Screenshots are Commons asset references" below) and reports it in the result's
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
   - `name`: the name it used.

   On `created` it summons the owner, so the card in the DM is the one line that
   carries the page: post no other message about it. On `updated` / `unchanged` it summons no one,
   including when it adopts a page it found by its ownership marker. The one exception is
   finishing a page an earlier run created but did not complete. `--summon` forces a summon, e.g.
   when the owner asks for the tutorial again.
4. Exit 3 (room-commons not installed) or 4 (a room-commons call failed): leave the greeting as is
   and say nothing about the page unless the owner asks. A failure that is a Sutando or s.space
   defect goes to `report-feedback`.

Run it in the owner's own DM (the default, `--audience dm`). Only when the owner asks for the page
in another room, publish it there with `--audience room`: the page then says "your agent" instead
of your name. The room's entry records the audience, and a later run without `--audience` keeps
it. Summon only whoever the owner asked for. Either way the page holds your name or "your
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
- A re-link or reinstall that reuses the DM rewrites the page in place, as does `/tutorial` sent
  again. A page someone removed from the list is created again.
- The ownership tag also records the feature list's revision the page was built from
  (`data-revision="tutorial.2026-10-23.1"`). **Rebuild rule:** a page is rebuilt in place when the
  engine's list revision is newer than the page's. `publish --if-newer` applies exactly that rule
  and nothing else: a complete page whose revision is current is left alone even when a rebuild
  would differ (another name or core), a stale page is rewritten with the prompts and roles it was
  last built with (recorded in the entry), a page from before lists had a revision counts as stale,
  and a `pending` page is finished whatever its revision. A page someone edited since it was last
  written (its text no longer hashes to the entry) is kept, with or without `--if-newer`, and
  whatever the revision: the result says `"action": "kept"`, stderr names the page, and the entry
  stands (a gated `unchanged` or `kept` run records nothing; a page equal to the run's own render
  records its inputs, so a revision-only bump catches the entry up). A rewrite is journalled as
  `updating` before its first write: the target page, the page before it, the page after each
  call and the entry before it. The next run settles that journal before rendering anything: a page
  in a state the rewrite itself leaves is finished to the target byte for byte and the run stops
  there (`"action": "resumed"`, reporting the journalled name and revision; its own inputs,
  `--summon` included, wait for the next run); a page still as before means the rewrite never ran
  and the run proceeds as usual; any other page is someone's edit, kept with the entry from before
  the rewrite, whole. A rewrite that fits one room-commons call (both texts within `CALL_BYTES`,
  1 MiB each; arguments reach `room_commons.py` in a 0600 file, so no exec limit applies) is one
  `replace` whose needle is the whole page, so a page that changed since it was read is refused as
  a whole and nothing in between is written. When the old page fits a call but not beside the new
  one, the first call replaces the whole page by the new page's first piece and the rest is
  appended, each call read back. A page no call can hold is never
  rewritten in pieces: the run exits 4 and leaves it as is, since a page cut away piece by piece
  cannot be put back where an edit made meanwhile left it. The
  page's tag carries a digest of the page, so a page found after the marker is lost is adopted as
  this script's only when the digest matches; otherwise it is kept as found and never rewritten
  until a render equals it. A page from before tags carried a digest (our tag with a
  `data-revision` and no `data-digest`) is adopted as ours once, and rewritten with the digest,
  when it equals this run's render less the digest, or failing that when it is the room's only
  tutorial page and shaped exactly as one is written (our head, one tag, our tail); any other such
  page is kept as found. The shape rule cannot tell an edit inside the body of such a page from an
  older render, so a pre-digest page the owner edited inside is upgraded with the rest. A write that landed on a page someone edited meanwhile is taken back the
  way it was made, in place (the replaced page replaced back, an appended piece removed); when that
  call cannot fit, nothing more is written and the result names the page's hash as left. Creation goes through the same journal, and a run settles any journal
  before it reads its own name, list, prompts or media. It summons no one. `--all-rooms` refreshes every room with an entry, so an owner's page gains
  the steps a release added: one `--if-newer` run per room the marker records;
  a room whose record is already current makes no room call, a room that fails (an unexpected error
  included) leaves the others refreshed, and the result lists each room's outcome. Each page is rebuilt in the audience recorded
  for its room (`dm` or `room`). An entry written before audiences were recorded is rewritten only
  in the setup app's DM room; elsewhere it is reported `skipped` ("audience unknown") and left as is.
  **A refresh never creates a page**, only an explicit `publish` does: a room whose recorded page
  was deleted, even while its creation was still pending, is `skipped` ("page deleted"), and a
  creation cut short before its page existed waits for the next explicit `publish` ("creation
  pending"). By hand:

  ```bash
  python3 skills/tutorial/scripts/getting_started.py publish --all-rooms --if-newer
  python3 skills/tutorial/scripts/getting_started.py publish --room <room> --if-newer  # one room
  ```
- If the marker file is lost, a listed "Getting started" page is reused only when its text
  carries this agent's ownership marker. A same-titled page without it is never touched. More
  than one marked page, or no agent id to check with, stops with exit 4 and the reason.

`getting_started.py plan --room <room>` shows what `publish` would do; `render --name <name>`
prints the HTML. Both `render` and `publish` take `--list <path-or-url>` (below).

**What the page says.** A short, interactive s.space tour (`scripts/tour_page.py`) rendered
from the skill's feature list, `feature-list.json`. Each step is one line on what it is and one
thing to try, in the list's order:
1. Your DM: what the owner asks for themselves comes back in the DM, never in a room.
2. Rooms.
3. Commons.
4. Connect other agents: Dot, Grok Bot, Muse. Claude Code and Codex are Sutando cores chosen at
   setup, not agents you add.
5. Teamwork.
6. Calls & voice: the headphone icon in the room header only.
7. Connect your apps: Settings → Integrations.
8. Activity & notices.
9. Get the mobile app.
10. Mobile: pair the phone from the account menu.

Every step applies to every core (a step may name a `runtime` to show on one core only; none
does today). Every prompt says where to paste it (your DM or a room). When every step is done,
a closing card offers one more prompt.

**The feature list.** `feature-list.json` is the per-release list the tour is generated from, in
the shape s.space's backend serves for owner setup copy (ag2space-backend#2898's `owner` block),
so one validated format can serve both:

```json
{"schema_version": 1, "revision": "tutorial.2026-10-23.1",
 "steps": [{"id": "dm", "title": "Your DM", "body": ["what it is", "what to try"],
            "surface": "dm", "screenshot": null, "since_release": "2026-10-15",
            "paste": "What can you do for me today?"}]}
```

- `id`: a slug; it is the step's prompt slot (`--prompts`) and its CSS/JS anchor. A step whose id
  has no icon in `tour_page.py` renders with the default one; accents follow the step's position.
- `body`: exactly two parts, what the feature is and what to try, as trusted markup with `{name}`
  for your escaped name. A part may be `{"text": ...}`; a link (`href`) in a part is refused.
- `link` (optional): `{"href", "text"}`, one plain anchor under the step's text. `href` must be an
  app's App Store or Google Play page (`STORE_LINK`); the phone step links the mobile app with it.
- `surface`: `dm`, `room` or `mobile`; where the step is tried. With `paste` it must be `dm` or
  `room`, and it decides the "Paste in:" hint.
- `screenshot`: `null` or a list of asset references `{"asset", "hd_file", "fallback_file", "alt",
  "caption"}`; `caption` (optional, at most 32 characters) is a label under the image, like an app's
  name under its icon.
  `asset` is the name of the capture in the room's media (today the file's stem); `hd_file`
  (optional) is the full-resolution `.png` under `assets/` that `publish` uploads as that media;
  `fallback_file` is the small `.jpg` crop (or `.png`, for an image that needs transparency) under
  `assets/` inlined when the room has no such media; `alt` is required. A shot with no `hd_file`
  (the app icon, the QR code) is always inlined. The backend's
  single asset name is a list here because a step carries up to three crops.
- `since_release`: the release date (`YYYY-MM-DD`) that introduced the feature.
- `runtime` (optional): `claude` or `codex`; the step appears on that core only.
- `watches` (optional, release metadata): repo-relative paths (a trailing `/` means a directory) whose change can
  make the step out of date, at most 16. Point them at the engine code the step shows (a skill
  directory, say). Leave them off a step that shows only the desktop client, which lives in
  another repo. A step without watches never triggers the release warning below.
- `reviewed_at` (optional, top level, release metadata): the plain release tag (`vX.Y.Z`) at which the release
  curator checked the tour and found it unaffected.
- `title` and both `body` parts are plain text: no `<` or `>` (write entities such as `&rarr;`),
  and `{name}` is the only placeholder (a literal brace is `{{` or `}}`); all three render the
  same way. A list, file or URL, is at most 1 MiB.
- `revision`: `tutorial.YYYY-MM-DD.N`. Any change to a rendered field bumps it; two revisions
  order by date, then N. `watches` and `reviewed_at` are release metadata: the page never shows
  them, so changing them needs no bump and rebuilds no page. `content_key` hashes the rendered
  fields, and the tutorial tests pin it per revision in `RENDERED_AT`, so a rendered change
  without a bump fails.

Workflow when the client changes or a release adds a feature: edit `feature-list.json` (text,
`paste`, `screenshot` crops), bump `revision`, run the tutorial tests (they validate the list and
the size caps, and that every watched path exists). At release time `skills/release/scripts/docs_audit.py`
warns when a watched path changed since the last plain `vX.Y.Z` tag and neither `revision` nor
`reviewed_at` records it. Nobody rewords `tour_page.py`; it holds the renderer, icons and accents. A list
that does not validate fails the load with the step and field named, so a bad edit never renders.

**A served list.** `--list <path-or-url>` renders from another list instead of the file: a JSON
file, or an https URL (the backend endpoint from ag2space-backend#2898 with a `tutorial` scope,
once it exists). The file stays the fallback: a list that cannot be read or does not validate is
reported on stderr and the skill's own file is used, and the result's `list` says which was used.
Nothing is fetched unless `--list` names a URL.

**Screenshots are Commons asset references.** Images travel as room media, not as the page's own
bytes (decided for #5836): each `screenshot` entry names its asset, and the page points an
`<img src>` at the room's media for it, `mxc://<server>/<id>`. The desktop client's HTML surface
resolves such an address for the viewer (`RoomHtmlView.tsx` with `htmlAssets.ts`: the page asks the
host for each `mxc://` its source names, and the host answers with an authenticated media URL), so
an uploaded image never expires and needs no public link.

`publish` does the upload itself, in the room it publishes to, once it knows it will write the
page: a run that skips, keeps an edited page or stops on the page's size uploads nothing.
1. It checks the room is not end-to-end encrypted (room-commons' `room_media.py`
   `require_plain_room`, from `room.media.list`): an agent's upload there would sit in plaintext. An
   encrypted room, or one whose encryption cannot be read, gets no upload at all.
2. Each `hd_file` goes up with the `room.media.upload` Action (`data_b64`, `mime` `image/png`,
   `filename`, `post: false`; answered with `mxc`), with the operation id `tutorial-media-<sha256 of
   room, asset and the file's sha256>`, so a retry, or a run cut short and run again, replays the
   first upload instead of storing the bytes twice.
3. The room's entry records `{asset: {"mxc", "sha256"}}`. A later run uploads again only a file
   whose sha256 changed (a new release's capture); an unchanged one is `skipped`.
4. Whatever cannot go up (encrypted room, a room-commons without `room_media.py`, a failed call, an
   answer that is not a well-formed `mxc://`) falls back for that asset alone: the page inlines its
   `fallback_file` as a `data:` URI. The result's `upload` names each such asset and the reason.

`--media <json>` (`{asset name: "mxc://..."}`) maps assets by hand to `render` or `publish`; on
`publish` it wins over an upload and is recorded and reused there like an upload. `clean_media`
drops names the list does not use and anything that is not a well-formed `mxc://` URI, from either
source. `--no-upload` skips step 2: an asset the room has no recorded media for is inlined.

`assets/app-icon.png` is the desktop app's own icon (from its `icon.icns`, 256 px, shown at
96 px, captioned "S" in black); `assets/app-store-qr.png` is a QR code of the App Store link
(369 px, under 1 KB, shown at 132 px) for a desktop reader to scan with a phone. Neither has an
`hd_file`; both are always inlined. The other `assets/*.png` are the `hd_file`
captures, full-window 2x screenshots of the desktop app (0.8.2) taken on a test account, so no real
person's name, room or account shows; `assets/*.jpg` are their small fallback crops. Three caps: a
list has at most 24 steps (`STEPS_MAX`, refused by `parse_list`), a rendered page is at most 300 KB
(`PAGE_MAX_BYTES`; `render` and `publish` refuse a larger one, and asset names that share one
fallback file inline it once each), and the tests hold each fallback file to 60 KB (500 KB in all)
and each `hd_file` to a PNG under the 25 MiB `room.media.upload` takes, so publishing a page
publicly stays well under the 413 that an image-heavy page met (#5318). With every capture
uploaded the page is text plus the inlined app icon and QR code (about 55 KB; under 40 KB when every asset,
the icon too, is mapped). Room media does not load on a public copy of a page (`page-publish`), so
a copy meant for the public is rendered without `--media` and published with `--no-upload`.

**Visuals (follow-up).** Part 2 of #5836 adds captures to the room's media the same way, one
`hd_file` per asset. The visuals comment asks for:
1. screenshots for the imageless steps (Your DM, Connect other agents);
2. one "result" image per step showing the payoff (a plan in Commons, another agent's reply in a
   Teamwork thread, an answer that used the connected Calendar);
3. very short animations for the key actions: inviting an agent to a room, @-mentioning an agent,
   pressing Connect for an app;
4. an overview image at the top marking where each step lives in the client.

Each list entry names its own assets (crop, result image, optional animation) as further
`screenshot` references; with media references the animations' budget is the upload's, not the
page's, while every fallback file stays under the caps above.

**How the page behaves.**
- It is one self-contained HTML document that loads nothing external; its one outbound URL is the
  phone step's App Store anchor.
- An inline SVG icon per step, in ink on a brand gradient tile, and a top border in that
  gradient's outer shade; steps cycle orange, yellow, pastel pink. The hero carries a solid
  brand-orange oval behind the title.
- The tour opens on one step, with the step's title, numbered dots, Back and Next, a "Show all"
  toggle and the arrow keys. In Show all, a dot scrolls to its step.
- "Mark done" moves to the next step that is not done (in step mode only) and fills the progress
  bar.
- Progress is saved in the page runtime's `window.artifact.state` under `gs-done-<viewer page id>`,
  only once the runtime names the viewer. Otherwise it stays in this browser's localStorage, then
  memory. The checkboxes stay disabled until progress has loaded.
- Copy outcomes are announced in a live status region. Animations stop under
  `prefers-reduced-motion`. Without scripts, every step shows.

**Size.** With every screenshot inlined (no upload) the page is about 250 KB (capped at 300 KB), over the 196,608
bytes Room Commons takes in one write. The first piece goes in the create's own body; the rest are appended in pieces
of at most 120,000 bytes (`CHUNK_BYTES`). Arguments reach `room_commons.py` as JSON in a 0600 file,
never argv, so Linux's 131,072-byte cap on one argument does not apply. A refresh is one `replace` when the old and new
text fit one call; else one `replace` of the whole old page by the new page's first piece, then
appends; a page no call can hold is left as is (exit 4).

**The prompts.** Each card's Copy button puts its prompt on the clipboard; where the page's sandbox
refuses the clipboard, the prompt is selected instead and the button says to press ⌘C. The page
cannot type into the composer: the HTML surface has no such hook.
