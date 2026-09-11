# Design: Voice Navigation and Triage

Status: draft (RFC) · Owner-requested 2026-09-11 · Author: lucy-sutando

## Problem

`sonichi/sutando-life` decides what needs the owner. It does this well, and it
does it in a shape that is unusually demanding of the surface that presents it:

- **One proposal at a time, not a list.** `triage_proposals.py` recomputes
  `next_item` from the live reaction log on every call rather than serving a
  precomputed position, because *"the triage is one by one and the next item
  needs to be updated after I react to one"* (owner, 2026-09-07).
- **Every item is something the agent commits to doing**, phrased so that
  approve/reject carries information: *"you must propose and my approve/reject
  will give you feedback and you need to improve."* The record is
  `{kind: "proposal", id, proposition, why, source, ts}` — a proposition and its
  reason, never a raw question.
- **The premise is verified at scan time.** `triage_freshness.py` looks up the
  pull requests a question names and attaches their live state, because a
  question's body ages while the work it names moves on. Owner, 2026-09-09: an
  item met three of his four criteria and failed the fourth — it said "five pull
  requests" when one had merged six days earlier.

That is a decision surface, and it currently renders as a web page
(`static/triage.html`) the owner has to go and open. Going to look is a context
switch, and it is paid **per item** — the queue is one-at-a-time by design, so
the cost is multiplied by exactly the thing that makes the design good.

`sonichi/sutando` is growing a surface for exactly this queue — **#4003**
(`feat(web-client): one-at-a-time triage queue for pending questions`) adds
approve / reject / reply / next / dismiss over `src/pending_questions_triage.py`,
ordered by what a question blocks and how long it has waited, with a live
re-check before the card is shown. That is the same decision unit this RFC
assumes, and this RFC does not propose a second one.

What it proposes is a second *place*. #4003's surface is the web client: still a
page the owner navigates to, so the per-item context switch in the paragraph
above survives it. There is no notch in the upstream repo; the voice agent can speak a proposal but speech cannot be skimmed, cannot
show the freshness evidence, and is gone from the agent's context in about ten
minutes.

So the best-formed decision in the system is the one that costs the most to reach.

## What this proposes

Add a **notch** to `sonichi/sutando`: a small always-available surface that
places itself out of the owner's way on the screen they are already looking at.
It is a companion to triage, built for it, with three purposes the owner named:

1. **Make switching context cheaper.** The proposal comes to where you are
   instead of you going to a page.
2. **Make it easier to understand.** One proposition and its `why`, sized to be
   read at a glance, not a board to be scanned.
3. **Make it verifiable.** The freshness evidence travels with the proposal, so
   the premise can be checked without leaving it.

The same surface serves in-channel navigation, which is what makes it worth
having open in the first place.

## Motivating failures (observed 2026-09-11)

Seven, all from building and using the surface. The first five are the notch
itself; the last two are what the surface is for.

**1. The panel landed on the densest text on screen.** Placement scored
candidate slots by text density and picked the *worst* one. The scoring was
right — replayed offline against a screenshot of that exact moment it ranks
`bottom-right 2.51` best and `top-left 8.13` worst, and it chose top-left live.
The input was wrong: `CGWindowListCreateImage` without a Screen Recording grant
returns the wallpaper and the calling process's own windows, with **no error and
no empty result**. It was measuring the flatness of a photo of a lake.

**2. That grant reset on every rebuild.** The binary was ad-hoc signed, so its
identity is a hash of its own contents and every `swift build` minted a new one.
Symptom: the owner grants permission, the setting shows enabled, and the process
still reports `CGPreflightScreenCaptureAccess() == false`. Diagnosed only after
the grant had been re-issued three times.

**3. The panel was half the screen and captured mouse events across all of it.**
"Find an empty spot" is unanswerable for a box that size — every layout overlaps
something. Placement policy cannot be written until the surface is the size of
its content.

**4. The card dismissed itself while being read** — a five-second idle timer.
A surface the owner did not dismiss interrupted them twice: once to appear, once
to vanish. For a decision surface this is worse than cosmetic: a proposal that
disappears mid-read is a proposal that has to be re-fetched and re-read.

**5. Placement was decided once, at window-build time.** Switching apps left the
card over the newly focused window. Attention moves; a placement computed once is
stale by the first switch — and switching is precisely the moment a triage
proposal is most likely to be on screen.

**6. The decision surface costs a context switch per item.** One-at-a-time is the
right queue design and it is what makes the page expensive: N items is N trips to
a browser tab.

**7. A stale premise is invisible until someone checks.** The 2026-09-09 item
failed on a fact — "five pull requests", one already merged — that the acceptance
gate structurally could not catch, because `triage_gate` checks shape, not facts.
`triage_freshness` now supplies the facts. They have nowhere to be displayed.

The through-line for the surface: **it must keep asking where attention is, fail
loudly when its sensors are unavailable, and never retract something the owner
did not dismiss.** Failures 1 and 2 are the same shape — a silent fallback that
looks like a working answer.

## Model

### Layer 1 — Placement is a policy the host owns

The vendored `DynamicNotchKit` exposes one hook and holds no policy:

```swift
public var placementProvider: ((NSScreen, NSSize) -> NSPoint)?
```

Unset keeps upstream's notch-anchored behavior. The app supplies the policy in
`Sources/notch/Placement.swift`:

```
score(slot) = overlapFractionWithFocusedApp(slot) * 10000
            + meanTextDensity(slot)
```

The weight is not a tuning constant. Covering the app in use is categorically
worse than covering any amount of idle text, so the focus term is lifted clear of
the 0–255 gradient scale rather than summed into it. Text density is the mean
horizontal-gradient energy of a 1/8-scale grayscale grab: glyph strokes make
dense vertical edges, wallpaper and blank page area make almost none.

Placement recomputes on `NSWorkspace.didActivateApplicationNotification` (250 ms
after it fires, because the new app's windows are not on screen at that instant)
and the panel animates to the new slot. **A panel the owner has dragged is never
moved again for the life of that card.**

Two invariants fall out of failures 1 and 2:

- **Fail loudly when blind.** `CGPreflightScreenCaptureAccess()` is checked at
  startup and warns when absent, because the capture API reports nothing itself.
- **Sign with a stable identity.** `build.sh` reads `NOTCH_SIGN_IDENTITY` and
  `NOTCH_SIGN_KEYCHAIN`; set, the designated requirement becomes
  `identifier "com.sutando.notch" and certificate leaf = H"<stable>"` and
  survives rebuilds. Unset falls back to ad-hoc with the caveat documented rather
  than rediscovered.

### Layer 2 — The triage card

A `kind: "triage"` case in the notch's card renderer, fed from sutando-life's
existing one-at-a-time queue. The card carries exactly what the record carries,
and nothing invented:

| Card element | Source |
|---|---|
| the proposition | `proposal.proposition` |
| the reason, under it | `proposal.why` |
| where it came from | `proposal.source` |
| **freshness evidence** | `triage_freshness`'s structured `freshness` field |
| the response controls | the actions the reaction log already accepts |

**Freshness is the element that earns the surface.** On the page it is appended
to the item's detail. On a card it sits next to the proposition it qualifies, so
"this names five PRs, one merged six days ago" is read at the same moment as the
proposition — which is the difference between a verifiable proposal and a
plausible one. This is purpose 3, and it is the one that cannot be done by voice
at all.

**Transport is already built.** sutando-life's `server.py` serves the queue over
HTTP: `GET /api/triage` (the questions), `GET /api/triage/ranked`,
`GET /api/triage/freshness`, and `POST /api/triage/action` /
`POST /api/triage/answer` for the response. The node is a client of those
endpoints. Nothing new is designed here and no file-drop protocol is invented.

**Approving by voice is a first-class path** (owner, 2026-09-11). The card is the
place the proposition and its freshness evidence are *visible*; the answer does
not have to travel back the same way. Speaking "approve" posts the same
`/api/triage/action` the click posts. What the surface owes in exchange is that
the evidence was on screen when the owner spoke — which is the whole reason the
card exists and the reason speech alone was never sufficient.

**Reacting is one gesture and it is append-only.** A reaction is
`{kind: "reaction", id, action, ts}`; `triage_actions.py` is *append-only,
decision-free* and explicitly never scores or reorders. The card writes a
reaction and nothing else. `next_item` recomputes on the next call, so the card
does not advance itself — it asks again and renders whatever comes back. **The
surface must not cache a queue position**, or it reintroduces exactly the
precomputed ordering the queue was designed to avoid.

### Layer 3 — In-channel navigation, same surface

`skills/discord-voice-overlay/` already resolves where the owner is: visible
Discord channel, hovered message ID (from `chat-messages-<channel>-<message>`
Accessibility identifiers), and selection, behind four tools —
`summarize_current_discord_channel`, `search_current_discord_channel`,
`inspect_hovered_discord_message`, `read_selected_discord_text`. Every result
currently returns to speech and nowhere else.

Each gains an optional render path writing to the same `hud-card.json`. This is
not a second feature bolted on: it is what keeps the surface present and trusted
between triage items. A card that only ever appears to demand a decision is an
interruption; one that answers what you just asked, in the place you are already
looking, is a place you already look.

It also serves purpose 1 directly. A proposal about a PR discussion and the
discussion itself land on the same surface, so verifying one against the other is
not a context switch either.

### Layer 4 — The surface is per-owner; the work it describes is not

Deck §3 (slides 21–22) is the constraint this would otherwise miss. Sutando is
six lanes, not one machine: Chi's MacBook (2026-03-25), Susan's MacBook (03-28),
Sutando-Mini (04-11), Susan's Studio (04-22), Qingyun's Mac (05-05), others from
05-15. The notch is per-owner and per-display. Triage proposals are addressed to
an owner, but the work they name moves between all six.

Three consequences:

**Each node renders its own owner's queue.** `next_item` is owner-scoped already;
the node resolves which owner it is from `SUTANDO_STAND_NAME`. Nothing fans out.

**Freshness is the cross-owner channel, and it is read-only.** When a proposal's
named PRs have moved because someone else merged them, that fact reaches this
owner as evidence on their card — not as a notification, not as a push. The fleet
communicates through the state of the work, which is what `triage_freshness`
already reads.

**No node may put a card on another node's screen.** A surface one owner can make
appear on another's display is an interruption primitive, and nothing here needs
one. Shared *attention* during a call is a separate feature with a separate
consent story; this RFC does not propose it.

## What it reuses (not a rewrite)

| Existing | Used as-is | Change |
|---|---|---|
| #4003 / `src/pending_questions_triage.py` | the one-at-a-time queue model and its action set | none — the notch is a second surface, not a second model |
| `sutando-life` `triage_candidates` → `judge` → `gate` → `proposals` | the entire pipeline, ordering, and one-at-a-time contract | none |
| `sutando-life` `triage_freshness` | the structured `freshness` field | none — it is displayed, not recomputed |
| `sutando-life` `triage_actions` | append-only reaction log | none — the card appends, nothing else |
| `DynamicNotchKit` (vendored) | window lifecycle, animation, notch geometry | one `placementProvider` hook + a focus observer; no policy added |
| `skills/notch/` renderer | card schema, `hud-card.json` watch loop | placement policy, no self-dismiss, drag-movable, `kind: "triage"` case |
| `skills/discord-voice-overlay/` | Accessibility watcher, all four tools | optional render-to-card path per tool |

No new ranking model, no new queue, no new daemon. Triage already decides and
already verifies; navigation already resolves context; the card channel is
already file-watched. The proposal is a surface and the wiring to it.

**Not in scope, to avoid a known confusion:** `liususan091219/prtriage` is a
separate PR-ranking board with its own vocabulary and its own UI. It is not this
system and nothing here reads from it.

## Why now / value

The surface went from "shows a card" to "places itself against the live screen"
in one session, and the work surfaced that the hard part was never rendering — it
was knowing where attention is and not lying about it when the sensors are
unavailable. That machinery now exists and is verified end to end: the grant is
confirmed, the owner agreed with the chosen slot, and a live reposition on app
switch is in `notch.log`.

Meanwhile triage's quality keeps raising the cost of reaching it. One-at-a-time
with an authored proposition and a verified premise is a better decision unit
than a board — and each improvement adds another trip to a browser tab. The
surface is what stops the design from being taxed by its own strengths.

## Settled (owner, 2026-09-11)

- **Voice may approve directly.** No click gate on approve; reject and skip too.
- **Transport is HTTP to sutando-life's existing triage endpoints.** No new
  protocol, no file drop.

## Open questions (for owner)

1. **Does a triage card wait forever?** Cards no longer self-dismiss. A proposal
   that stays until answered is consistent with the queue's design, but it means
   the surface is occupied until the owner acts. Is that intended, or should an
   unanswered proposal yield to a navigation card and return later?
2. **Polling cadence.** Transport is settled — the node is an HTTP client of
   sutando-life's existing triage endpoints. How often should it ask, and should
   it stop asking while a proposal is already on screen unanswered?
3. **Multi-display.** Placement scores the screen the panel is on, defaulting to
   `NSScreen.screens[0]`. Should the notch follow the focused app to a second
   display?
4. **Window drags vs app switches.** Placement re-runs on app switch only; a drag
   within the same app does not trigger it. Covering that needs Accessibility
   window observers or a screen grab per tick. Worth the cost?
5. **Relationship to #4003.** That PR establishes the queue's action set and
   ordering in the web client. Should the notch card consume the same
   `pending_questions_triage` path, or sutando-life's `/api/triage` directly? One
   model, but currently two servers.
6. **Repo boundary.** The notch ships in `sonichi/sutando`; triage stays in
   `sonichi/sutando-life`, which is private. What does the notch do on a machine
   with no sutando-life — degrade to navigation only, or is the triage card
   behind a capability check?

## Next steps (on owner confirm)

1. Land Layer 1 as its own PR to `sonichi/sutando`: the notch plus its placement
   policy, no triage. Self-contained, implemented, verified.
2. Add the render-to-card path to one navigation tool
   (`summarize_current_discord_channel`) as the thin end of Layer 3, and evaluate
   before doing the other three.
3. Layer 2 last: the `kind: "triage"` card against the existing endpoints, read
   path first (proposition + why + freshness), then the reaction path with voice
   approve wired to `POST /api/triage/action`.
