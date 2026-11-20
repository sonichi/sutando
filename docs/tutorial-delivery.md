# Tutorial delivery

The procedure lives in the `tutorial` skill: [`skills/tutorial/SKILL.md`](../skills/tutorial/SKILL.md).

- **Voice mode.** On "tutorial", "walk me through" or "show me what you can do", read
  `notes/first-time-tutorial.md` and deliver it one brief, voice-friendly section at a time,
  waiting for the user to try each step.
- **s.space mode.** Right after the first-contact greeting in the owner's DM, the skill creates
  one "Getting started" HTML page (Room Commons) in that DM, a four-topic overview with an optional step-by-step checklist, and summons the owner to it. A marker under `<workspace>/state/tutorial/` keeps it to one page per room, so
  a re-link or reinstall refreshes the page instead of adding another.

## Product overview

The owner DM opens on four shared topics plus **Sutando Guides**. The general explanations stay stable;
each shared topic has one example to try. When the current room, owner and agent match the saved
setup record, the banner uses the agent's display name and supported selections such as Designer or
Product Manager pick vetted examples. Missing or unrecognized setup details keep the overview
generic. Nothing reads account connections, room history or private files to build the page.

The [standalone four-topic guide](../skills/tutorial/assets/getting-started.html) remains generic.
Topic selectors are demonstrations: they do not connect accounts, change access, start calls or
schedule work. Copy falls back to selecting the prompt when the clipboard is unavailable. Open
**Try it step by step** for the original personalized ten-step checklist.

## Using the ten-step owner tour

The labelled step navigator lets readers jump directly to a topic instead of guessing a number.
Use Back and Next for the guided path, Show all for a reference view, and Copy for an example
prompt. Completion is per viewer when the Commons runtime supplies a viewer identity; otherwise
it uses browser-local storage, falling back to memory when storage is unavailable.

The layout uses the s.space orange, yellow and pink gradient pairs with ink/offwhite surfaces.
Avantt SemiBold and Haas Recast are named in the font stack, with system-font fallbacks; no
licensed font binaries are bundled. Controls have 44-pixel minimum touch targets, the topic grid
wraps on narrow screens, and reduced-motion and dark-mode preferences are respected.

## What the page is built from

- **The feature list.** The steps come from `skills/tutorial/feature-list.json` (one step per
  feature: title, two body parts, where to try it, a paste prompt, screenshots, the release that
  added it, and optionally the repo paths it `watches`). A watched path that changes in a release
  without a revision bump or a matching `reviewed_at` makes the release audit warn
  (`docs/release-process.md` §4.2). `--list <file|https URL>` swaps the list; nothing is fetched by default, only https
  is fetched (redirects included), a list is at most 1 MiB and 24 steps, and a list that does not
  validate (markup, a placeholder other than `{name}`, a missing field) is reported on stderr and
  the skill's own file is used.
- **Revisions and `--if-newer`.** The list carries a revision (`tutorial.YYYY-MM-DD.N`), which the
  page records in its ownership tag. `publish --all-rooms --if-newer` runs once per room with an entry, in the
  audience recorded for it, never creates a page (a deleted one stays deleted), and rebuilds a
  page only when the list's revision is newer than the page's, with the prompts and roles the page
  was last built with; a current page is left alone even when a rebuild would differ.
- **Owner edits are kept.** Before any rewrite the page's text is compared with the hash recorded
  when the skill last wrote it. A page edited since is kept, with or without `--if-newer` and
  whatever the revision (`"action": "kept"`, one stderr line), and its entry is not touched, so a
  later revision does not overwrite the edit either.
- **A rewrite that is cut short is finished, and an edit made meanwhile is kept.** A rewrite may take
  more than one call, so before the first write the entry becomes an
  `updating` journal: the target page, the hash of the page before, the hash after each call, and
  the entry before. The next run settles the journal before rendering anything of its own. A page
  in a state the rewrite itself leaves is finished to the target byte for byte, checked against the
  recorded hash, and the run stops there (`"action": "resumed"`, reporting the journalled name and
  revision; its own request, `--summon` and new prompts or media included, waits for the next
  run). A page still as before means the rewrite never ran and the run proceeds as usual. Any other
  page is someone's edit: it is kept, and the entry from before the rewrite stands whole, so the
  next run keeps it too. A rewrite that fits one room-commons call (1 MiB a text; arguments travel
  in a 0600 file, never argv, so no exec limit applies) is a single `replace` whose
  needle is the whole page, so a page that changed since it was read is refused whole and nothing
  in between is written. When only the old page fits a call, the first call replaces the whole page
  by the new page's first piece and the rest is appended, every call read back. A page no call can
  hold is not rewritten: the run exits 4 and leaves it as is, because a
  page cleared in pieces cannot be put back where an edit made meanwhile left it. A run that
  resumes continues from the call the journal recorded, never from the page's length.
- **Pages this script cannot vouch for.** The page's tag carries a digest of the page itself, so a
  page found after the marker is lost is adopted as this script's only when the digest matches; a
  page that does not match, or one whose creation never finished as journalled, is kept as found
  and never rewritten until a render equals it. A page from before tags carried a digest (our
  tag, a `data-revision`, no `data-digest`) is adopted once and rewritten with the digest when it
  equals the run's render less the digest, or when it is the room's only tutorial page and shaped
  exactly as one is written; otherwise it is kept. Creation runs through the same journal as a
  rewrite. A write taken back is undone in place, the way it was made; an undo that cannot fit a
  call writes nothing, and the result names the page as left.
- **Recovery comes first.** A run settles any journal in the room before it resolves its name,
  fetches or opens a list, or opens prompts or media; the identity is resolved and checked once and
  used for the name, the tag and the journal alike.
- **Identity.** `publish` needs the agent's mxid (`--agent-mxid`, `$AGENT_MXID` or the s.space
  channel env file), resolved and checked once (`@localpart:server`, whitespace trimmed) before any
  Room Commons call, and refuses to run without one: the page's ownership tag is how a later run
  finds it. A revision-only bump of the list rewrites the page once (its tag records the revision)
  and the next `--if-newer` is a no-op.
- **Screenshots.** Each screenshot is a Room Commons asset reference: `publish` uploads its
  full-resolution `hd_file` to the room's media (`room.media.upload`, an operation id per room and
  file hash, never in an encrypted room), records `{asset: {mxc, sha256}}` and re-uploads only a
  changed file; the page points at that `mxc://` (or one from `--media`) and inlines the bundled
  fallback file for any asset that did not go up. It uploads only once it will write the page: a
  run that skips, keeps an edited page or stops on the page's size uploads nothing. Every page
  variant stays under 300 KB.

Last verified: navigation and responsive polish, 2026-11-19 in Chromium at 1280, 390 and 320 pixels,
including light/dark mode, Show all, step navigation, copying and completion after reload.
Existing screenshot assets were not recaptured in that pass.

Earlier publishing verification: 2026-10-31, against `tests/tutorial-feature-list.test.py` and
`tests/tutorial-getting-started.test.py`. The screenshot upload and the HTML-surface rendering of
`mxc://` media were verified live in a VM on 2026-10-30/31: a page published to a test account's
DM uploaded all 13 assets and rendered in the desktop app 0.8.2, in Safari on the web, in dark
mode and in a narrow panel. The live Room Commons round trip of `publish --if-newer` was witnessed
at an earlier head.
