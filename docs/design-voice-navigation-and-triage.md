# Design: Voice Navigation and Triage

Status: draft (RFC) · Owner-requested 2026-09-11 · Author: lucy-sutando

## Problem: the context gap

Finishing a task quickly requires understanding its context. Sutando slows the
owner down at both ends of a task, in the same way and for the same reason.

Sutando can already *speak* across this gap — the voice agent answers about the
current channel and about what needs deciding. Speech does not close it. Speech
cannot be skimmed, cannot put evidence beside a claim, and is gone from the
agent's context in about ten minutes.

### Within a task

Mid-task, the context is elsewhere. What did this channel already decide? What
does this message refer to? Many of the decisions Sutando asks for are
comparisons: an agent produces something and the owner is asked whether it is
right, which means holding the output against whatever it came from.

![A Discord message asking for an approve/reject verdict on a drafted post. Its
only evidence is a link to an outside article.](design-voice-navigation-and-triage-context-gap.png)

Above, an agent has drafted a social post from a news article and is asking the
owner to approve or reject it. Deciding means checking the draft's two claims
against the article. The article is not in the message; it is behind the link.

A screen shows one window at a time, so the owner opens the link, and the draft
they are judging disappears behind the browser. The comparison now has to be done
from memory: read the article, remember what the draft said, decide. Memory is
the weak half, so in practice this becomes several trips back and forth — and
each trip costs the place it came from, because coming back means finding the
message again.

The reading was never the expensive part. The expensive part is that the two
halves of a comparison cannot be on screen together.

![The same message, with the linked article open in a notch panel beside it. The
voice transcript reads "Can you open this URL in the notch?" — "Okay, I've opened
that URL in the notch."](design-voice-navigation-and-triage-context-closed.png)

Here the owner asked for the article by voice and it opened in a panel beside the
message. Draft and article are both visible, so the comparison is done by looking
rather than by remembering, and there is no trip to come back from.

### Across tasks

Between tasks, the next one is unknown. "What should I do next" is itself work,
and it is the work `sonichi/sutando-life` triage exists to remove. It does that
deciding. It collects the items that need the
owner from every source, ranks them, and shows one at a time. Each item is a
proposal the agent commits to carrying out, with a stated reason, so approving or
rejecting it answers something specific. Before an item is shown,
`triage_freshness` re-checks the pull requests it names, because an item's text
ages while the work it describes changes.

The queue renders at `static/triage.html`. On this host it holds forty items and
shows one card with five actions:

> Everything that needs your call, from every source, one at a time in rank
> order. Approve, reject, or reply, then move on. Your decisions are recorded for
> the agent to act on; nothing here reorders itself by what you pick.

To answer an item the owner opens that page and leaves the current work. Because
the queue shows one item at a time, that happens once per item rather than once
per sitting.

The verdict in the figures above is not in this queue. Pending questions go to
the sutando-life page, drafted-post verdicts go to Discord, and `#4003` is adding
a third destination in the web client. The three have separate producers and no
shared queue.

![Mockup: the same Discord channel, with a triage card for the post verdict
rendered as a notch panel — proposition, source, a freshness line, and Approve /
Reject / Reply / Next / Dismiss.](design-voice-navigation-and-triage-triage-on-notch.png)

This figure is drawn, not captured: post verdicts are not routed into triage, so
the item in it does not exist today. It places the triage page's own card markup
over the real Discord screenshot to show what would change — the item appears
where the work already is, carries the freshness line, and takes the same five
actions.

## What this proposes

A surface on the owner's screen that other parts of Sutando can render into, so
an answer or a decision arrives where the work already is. The surface sits at
the top of the display near the notch, is placed out of the way of whatever the
owner is using, and stays until dismissed.

Six components. Three of them already exist and are reused as they are.

### 1. The notch app — new

A Swift binary (`skills/notch/`) that watches one JSON file and renders whatever
card is in it. It owns two behaviours the figures depend on:

**Placement.** The card must not cover the thing it is about. On each show, and
again whenever the owner switches app, candidate positions are scored:

```
score(slot) = overlapFractionWithFocusedApp(slot) * 10000
            + meanTextDensity(slot)
```

Covering the app in use is categorically worse than covering idle text, so the
focus term is weighted past the 0–255 text scale rather than added to it. Text
density is the mean horizontal gradient of a downscaled grayscale screen grab:
glyph strokes produce dense vertical edges, wallpaper produces almost none. A
card the owner has dragged is never repositioned again.

![The panel sitting in the lower right, clear of the message it is about.](design-voice-navigation-and-triage-placement.png)

**Persistence.** The card stays until the owner dismisses it. Nothing retracts it
on a timer, because a decision surface that disappears mid-read has to be fetched
and re-read.

### 2. What a card can show — new

The watched file carries a typed envelope, so adding a kind is a new case in one
renderer rather than a new mechanism. Three tools write it today:

| Tool | Renders |
|---|---|
| `show_web` | a URL, loaded in place — figure 2 |
| `show_card` | structured rows: a title and labelled lines — the shape a triage item needs |
| `fold_notch` | dismisses the current card |

A triage card is `show_card` with the fields the queue already supplies:
proposition, reason, the freshness line, and the five actions.

### 3. Resolving what "this" means — exists, and is why figure 2 works

![The URL selected in the Discord message, blue highlight over the
link.](design-voice-navigation-and-triage-highlight.png)

The owner does not read a URL aloud. They highlight it and say *"open this"*.
Turning that into an absolute URL is its own problem, and the answer is a
fallback chain rather than one lookup:

1. If the selection is in Discord, `read_selected_discord_text` resolves it and
   returns the `href` — necessary because the visible label and the real target
   often differ.
2. If that resolver finds nothing, a vision query reads the highlighted text off
   the screen.
3. The clipboard is used only when the owner says they copied something.

The ordering is the design. Asking the owner to copy a link they have already
highlighted would hand the work back to them, which is the trip this is meant to
remove.

### 4. In-channel navigation — exists, gains an output

`skills/discord-voice-overlay/` already tracks the visible Discord channel, the
hovered message, and the current selection, behind four tools
(`summarize_current_discord_channel`, `search_current_discord_channel`,
`inspect_hovered_discord_message`, `read_selected_discord_text`). They answer by
speech and nowhere else. Each gains an optional path that also writes a card.

### 5. The triage client — new, thin

sutando-life already serves the queue over HTTP (`GET /api/triage`,
`/api/triage/freshness`, `POST /api/triage/action`). The notch is a client of
those endpoints: read the current item, render it as a triage card, post the
owner's answer back. No new ranking, no new queue, no new protocol — and the card
must not cache a queue position, because `next_item` recomputes from the live
reaction log on every call by design.

### 6. Voice — exists, unchanged

![The voice panel: "Can you open this highlighted URL in the notch?" answered by
"Okay, I've opened that URL in the notch."](design-voice-navigation-and-triage-voice.png)

Voice is already the input half; the surface is the output half that was missing.
The owner speaks the request and speaks the answer, and what the card adds is that
the evidence was visible when they spoke. That is what makes a spoken `approve`
verifiable rather than merely fast.
