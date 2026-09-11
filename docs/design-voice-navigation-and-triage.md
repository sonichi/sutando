# Design: Voice Navigation and Triage

Status: draft (RFC) · Owner-requested 2026-09-11 · Author: lucy-sutando

## Problem: the context gap

### Within a task

The thing you need is never where you are.

![A Discord message asking for an approve/reject verdict on a drafted post. Its
only evidence is a link to an outside article.](design-voice-navigation-and-triage-context-gap.png)

A decision is asked for here and cannot be made here. Ruling on the draft means
reading the article it is built from, which means leaving.

![The same message, with the linked article open in a notch panel beside it. The
voice transcript reads "Can you open this URL in the notch?" — "Okay, I've opened
that URL in the notch."](design-voice-navigation-and-triage-context-closed.png)

Asked by voice, the article arrived beside the message. Nothing abandoned.

### Task triage

"What should I do next" is itself work. `sonichi/sutando-life` removes it: one
proposal at a time, each an authored commitment with a `why`, its premise
re-verified at scan time by `triage_freshness` against the pull requests it names.

It renders on a page of its own, `static/triage.html` — one card out of forty on
this host, five actions, no board:

> Everything that needs your call, from every source, one at a time in rank
> order. Approve, reject, or reply, then move on. Your decisions are recorded for
> the agent to act on; nothing here reorders itself by what you pick.

A good page, and a page. One-at-a-time means **the trip is paid per item.**

And not one queue: pending questions reach that page, post verdicts reach
Discord, `#4003` adds the web client. The verdict above is in none of the others.

![Mockup: the same Discord channel, with a triage card for the post verdict
rendered as a notch panel — proposition, source, a freshness line, and Approve /
Reject / Reply / Next / Dismiss.](design-voice-navigation-and-triage-triage-on-notch.png)

Mockup, not a capture — that item is in no queue today. Same card, where the work
already is.

## What this proposes

Add a **notch** to `sonichi/sutando`: a small surface that places itself out of
the owner's way on the screen they are already on. It closes the gap by moving
the destination instead of the owner.

| Gap | What the surface does |
|---|---|
| Getting there | The answer appears where you are looking; nothing is abandoned. |
| Understanding | One thing at a time, sized to be read at a glance. |
| Trusting | Evidence sits beside the claim, checkable without leaving it. |

Its first two consumers are the two ends of a task: **navigation** while working,
**triage** between. Neither gets a new model — `#4003` defines the queue's action
set and ordering and this RFC assumes them. **A second place, not a second
model.**

## Model

### Placement

The vendored `DynamicNotchKit` gets one hook and no policy:

```swift
public var placementProvider: ((NSScreen, NSSize) -> NSPoint)?
```

The app supplies the policy in `Sources/notch/Placement.swift`:

```
score(slot) = overlapFractionWithFocusedApp(slot) * 10000
            + meanTextDensity(slot)
```

The weight is not a tuning constant: covering the app in use is categorically
worse than covering idle text, so the focus term is lifted clear of the 0–255
gradient scale rather than summed into it. Text density is the mean horizontal
gradient of a 1/8-scale grayscale grab — glyph strokes make dense vertical edges,
wallpaper makes almost none.

It recomputes on `NSWorkspace.didActivateApplicationNotification`, 250 ms late
because the new app's windows are not on screen at the instant it fires. **A
panel the owner has dragged is never moved again for that card**, and a card is
never retracted on a timer — only the owner dismisses it.

Two properties of the sensors, each learned the hard way:

- **Fail loudly when blind.** `CGWindowListCreateImage` without a Screen
  Recording grant returns the wallpaper and the caller's own windows — no error,
  no empty result — so the score is computed over a desktop photo and looks
  fine. `CGPreflightScreenCaptureAccess()` is checked at startup and warns.
- **Sign with a stable identity.** Ad-hoc signing makes the binary's identity a
  hash of its own contents, so every rebuild silently drops that grant.
  `build.sh` reads `NOTCH_SIGN_IDENTITY` / `NOTCH_SIGN_KEYCHAIN`; unset falls
  back to ad-hoc with the consequence documented rather than rediscovered.

### The triage card

A `kind: "triage"` case in the existing card renderer, carrying what the record
carries and nothing invented:

| Card | Source |
|---|---|
| the proposition | `proposal.proposition` |
| the reason | `proposal.why` |
| **freshness evidence** | `triage_freshness`'s structured `freshness` field |
| the response | the actions the reaction log already accepts |

Freshness is what earns the surface. On a page it is appended to the detail; on
a card it sits beside the proposition, so *"this names five PRs, one merged six
days ago"* is read at the same moment as the claim. That is the trust gap closed,
and it is the one speech cannot close at all.

**The card must not cache a queue position.** `next_item` recomputes from the
live reaction log on every call by design; a surface that advances itself
reintroduces the precomputed ordering that design avoids.

Transport already exists — sutando-life serves `GET /api/triage`,
`/api/triage/freshness`, and `POST /api/triage/action`. The node is a client.

### In-channel navigation

The four existing tools — `summarize_current_discord_channel`,
`search_current_discord_channel`, `inspect_hovered_discord_message`,
`read_selected_discord_text` — each gain an optional render path to the same
`hud-card.json`.

This is not a second feature. A surface that only ever appears to demand a
decision is an interruption; one that answers what you just asked, where you are
already looking, is a place you already look. A summary of forty messages cannot
be spoken usefully, and reading them is the trip the card removes.

### Per-owner, not per-fleet

Deck §3 (slides 21–22): six lanes, not one machine — Chi's MacBook (2026-03-25),
Susan's MacBook (03-28), Sutando-Mini (04-11), Susan's Studio (04-22), Qingyun's
Mac (05-05), others from 05-15. The surface is per-owner and per-display; the
work it describes moves between all six.

- Each node renders its own owner's queue, resolved from `SUTANDO_STAND_NAME`.
- The fleet reaches this owner through **evidence, not notification** — when
  someone else merges a named PR, that arrives as freshness on the card.
- **No node may put a card on another node's screen.** That is an interruption
  primitive and nothing here needs one.

## What it reuses (not a rewrite)

| Existing | Change |
|---|---|
| `#4003` / `src/pending_questions_triage.py` — queue model and action set | none; a second surface, not a second model |
| `sutando-life` triage pipeline, `triage_freshness`, `triage_actions` | none; displayed and appended to, never recomputed |
| `DynamicNotchKit` (vendored) | one `placementProvider` hook + a focus observer; no policy |
| `skills/notch/` renderer | placement policy, no self-dismiss, drag-movable, `kind: "triage"` |
| `skills/discord-voice-overlay/` | optional render-to-card path per tool |

No new ranking, queue, protocol, or daemon.

**Out of scope:** `liususan091219/prtriage` is a separate PR-ranking board with
its own UI. Nothing here reads from it.

## Settled (owner, 2026-09-11)

- **Voice may approve directly** — no click gate. The card's job is that the
  evidence was visible when the owner spoke, not that the answer travelled back
  the same way.
- **Transport is HTTP** to sutando-life's existing triage endpoints.

## Open questions (for owner)

1. **Does a triage card wait forever**, or yield to a navigation card and return?
2. **Polling cadence**, and whether to stop asking while one is unanswered.
3. **Multi-display** — follow the focused app, or stay on `screens[0]`?
4. **Window drags** do not trigger re-placement; covering them needs
   Accessibility observers or a grab per tick. Worth it?
5. **One model, two servers** — should the card read `pending_questions_triage`
   (as `#4003` establishes) or sutando-life's `/api/triage`?
6. **Repo boundary** — the notch ships in `sonichi/sutando`, triage stays in the
   private `sonichi/sutando-life`. What happens on a machine without it?

## Next steps (on owner confirm)

1. Land placement as its own PR — self-contained, implemented, verified.
2. Render-to-card for one navigation tool, evaluate, then the other three.
3. The triage card last: read path, then the reaction path.
