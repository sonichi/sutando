# Design: Voice Navigation and Triage

Status: draft (RFC) · Owner-requested 2026-09-11 · Author: lucy-sutando

## Problem

Finishing a task quickly requires understanding its context. Sutando slows the
owner down at both ends of a task, in the same way and for the same reason.

**Mid-task, the context is elsewhere.** What did this channel already decide?
What does this message refer to? The answer is in a scrollback, a page, another
tab — and going to get it abandons the context the task is in.

![A Discord message asking for an approve/reject verdict on a drafted post. The
draft's only evidence is a link to an outside article.](design-voice-navigation-and-triage-context-gap.png)

The screenshot is the gap in one frame. A decision is being asked for —
`approve — this is the post` / `reject — drop it, I take the next item` — and it
cannot be made on the page that asks for it. Ruling on the draft means reading
the article it is built from, which means leaving Discord for a browser. By the
time the answer is known, the message that asked is somewhere behind a window.
**The place a decision is requested is not a place a decision can be made.**

With a surface, the article comes to the message instead:

![The same message, with the linked article open in a notch panel beside it. The
voice transcript reads "Can you open this URL in the notch?" — "Okay, I've opened
that URL in the notch."](design-voice-navigation-and-triage-context-closed.png)

Same screen, same message, nothing abandoned. The owner asked by voice and the
evidence arrived where the question was — **no switching back and forth.**

**Between tasks, the next one is unknown.** "What should I do next" is itself
work — and the figure above was already an answer to it. That message is a triage
item, and it has the shape `sonichi/sutando-life` produces:

| In the figure | The record |
|---|---|
| the drafted post | `proposition` — what the agent commits to doing |
| "Proposed by the content loop, not yet written up" | `why` |
| "News published: 09-11 11:21 ET" | `source` |
| `approve` / `reject` | the actions the reaction log accepts |
| "Recommend: diagram. Say another, or no image, to override." | a default, so deciding is confirm-or-overrule rather than choose-from-blank |

That is the work triage removes: one proposal at a time, each an authored
commitment rather than a raw question, its premise re-verified at scan time by
`triage_freshness` against the pull requests it names. It is the strongest
decision unit in the system.

sutando-life gives it a page of its own — `static/triage.html`, whose own
description is the design in one sentence:

> Everything that needs your call, from every source, one at a time in rank
> order. Approve, reject, or reply, then move on. Your decisions are recorded for
> the agent to act on; nothing here reorders itself by what you pick.

One card at a time: a waiting label, the proposition, the detail, per-item
options, then approve / reject / reply and a list of what is already in progress.
No board, no backlog to scan. It refreshes on its own every fifteen minutes as a
standalone job, and a ↻ appears only when something new has actually arrived.

Which is a good page — and a page. To answer one item the owner leaves whatever
they were doing, and the queue is one-at-a-time by design, so **the trip is paid
per item.** Every property that makes the unit good widens the gap.

**The same unit is currently scattered across three destinations** — that page,
`#4003`'s web client, and Discord messages like the one above. One model, three
places, each of which you go to.

Both halves are the same gap, and it costs three things:

| | The gap |
|---|---|
| **Getting there** | The answer is somewhere else. Going to it abandons where you are. |
| **Understanding it** | What arrives is a list, a board, a scrollback. It must be scanned before it can be used. |
| **Trusting it** | It was written earlier. Nothing says which parts are still true. |

Sutando can already *speak* across this gap — the voice agent answers about the
current channel and about what needs deciding. Speech does not close it. Speech
cannot be skimmed, cannot put evidence beside a claim, and is gone from the
agent's context in about ten minutes.

In-channel navigation is the same gap from the other side:
`skills/discord-voice-overlay/` already resolves the visible channel, the hovered
message and the selection, and can only answer by speaking.

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
