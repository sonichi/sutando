# Tutorial delivery

The procedure lives in the `tutorial` skill: [`skills/tutorial/SKILL.md`](../skills/tutorial/SKILL.md).

- **Voice mode.** On "tutorial", "walk me through" or "show me what you can do", read
  `notes/first-time-tutorial.md` and deliver it one brief, voice-friendly section at a time,
  waiting for the user to try each step.
- **AG2 Space mode.** Right after the first-contact greeting in the owner's DM, the skill creates
  one "Getting started" HTML page (Room Commons) in that DM, a short AG2 Space tour with one thing
  to try per part (a Claude Code history import part only on a Claude Code core), and summons
  the owner to it. A marker under `<workspace>/state/tutorial/` keeps it to one page per room, so
  a re-link or reinstall refreshes the page instead of adding another.

## What the page is built from

- **The feature list.** The steps come from `skills/tutorial/feature-list.json` (one step per
  feature: title, two body parts, where to try it, a paste prompt, screenshots, the release that
  added it). `--list <file|https URL>` swaps the list; nothing is fetched by default, only https
  is fetched (redirects included), a list is at most 1 MiB and 24 steps, and a list that does not
  validate (markup, a placeholder other than `{name}`, a missing field) is reported on stderr and
  the skill's own file is used.
- **Revisions and `--if-newer`.** The list carries a revision (`tutorial.YYYY-MM-DD.N`), which the
  page records in its ownership tag. `publish --if-newer`, run after an engine update, rebuilds a
  page only when the list's revision is newer than the page's, with the prompts and roles the page
  was last built with; a current page is left alone even when a rebuild would differ.
- **Owner edits are kept.** Before any rewrite the page's text is compared with the hash recorded
  when the skill last wrote it. A page edited since is kept, with or without `--if-newer` and
  whatever the revision (`"action": "kept"`, one stderr line), and its entry is not touched, so a
  later revision does not overwrite the edit either.
- **A rewrite that is cut short is finished, and an edit made meanwhile is kept.** A rewrite clears
  the page and appends the new text in pieces, so before the first write the entry becomes an
  `updating` journal: the target page, the hash of the page before, the hash after each call, and
  the entry before. The next run settles the journal before rendering anything of its own. A page
  in a state the rewrite itself leaves is finished to the target byte for byte, checked against the
  recorded hash, and the run stops there (`"action": "resumed"`, reporting the journalled name and
  revision; its own request, `--summon` and new prompts or media included, waits for the next
  run). A page still as before means the rewrite never ran and the run proceeds as usual. Any other
  page is someone's edit: it is kept, and the entry from before the rewrite stands whole, so the
  next run keeps it too. A rewrite that fits one room-commons call (this host's argument budget:
  ARG_MAX less the environment, 131,072 bytes per argument on Linux) is a single `replace` whose
  needle is the whole page, so a page that changed since it was read is refused whole and nothing
  in between is written. Above the budget each call's needle is a whole piece and every call is
  read back; an edit landing between a read and its write is caught by that read-back with at most
  one piece already cleared, which the result reports and never records as written. A run that
  resumes continues from the call the journal recorded, never from the page's length.
- **Pages this script cannot vouch for.** The page's tag carries a digest of the page itself, so a
  page found after the marker is lost is adopted as this script's only when the digest matches; a
  page that does not match, or one whose creation never finished as journalled, is kept as found
  and never rewritten until a render equals it. A page from before tags carried a digest (our
  tag, a `data-revision`, no `data-digest`) is adopted once and rewritten with the digest when it
  equals the run's render less the digest, or when it is the room's only tutorial page and shaped
  exactly as one is written; otherwise it is kept. Creation runs through the same journal as a
  rewrite. A write taken back is undone the way it was made, in pieces that fit a call; a piece
  that cannot fit stops the undo, writes nothing more, and the result names the page as left.
- **Recovery comes first.** A run settles any journal in the room before it resolves its name,
  fetches or opens a list, or opens prompts or media; the identity is resolved and checked once and
  used for the name, the tag and the journal alike.
- **Identity.** `publish` needs the agent's mxid (`--agent-mxid`, `$AGENT_MXID` or the AG2 Space
  channel env file), resolved and checked once (`@localpart:server`, whitespace trimmed) before any
  Room Commons call, and refuses to run without one: the page's ownership tag is how a later run
  finds it. A revision-only bump of the list rewrites the page once (its tag records the revision)
  and the next `--if-newer` is a no-op.
- **Screenshots.** Each screenshot is a Room Commons asset reference: the page points at the
  room's media (`mxc://`, from `--media` or the room's recorded map) when the room has the asset,
  and inlines the bundled fallback file otherwise, so a room with no uploads gets the same page as
  before. Every page variant stays under 300 KB.

Last verified: 2026-10-24, against `tests/tutorial-feature-list.test.py` and
`tests/tutorial-getting-started.test.py` at the head that introduced the feature list. The live
Room Commons round trip of `publish --if-newer` was witnessed at the previous head; the
HTML-surface rendering of `mxc://` media was not.
