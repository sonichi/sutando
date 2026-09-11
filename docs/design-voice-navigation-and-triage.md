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

## Prior art

Three existing things this builds on. None of them is being reimplemented.

**Clicky / HeyClicky** — <https://www.heyclicky.com/>, open-source v1 at
<https://github.com/farzaa/clicky>. Press a key, say what you are looking for, and
a marker flies across the screen to it while the app talks you through. Sutando's
`point_at` is that gesture, and [ADR-0001](adr/0001-pointer-teacher-brain.md)
records where it deliberately differs: Clicky resolves targets by vision, Sutando
queries the Accessibility tree first and falls back to vision only when the tree
is empty, and it asks the vision model for coordinates in the model's own native
normalized format rather than Clicky's raw-pixel prompt, which measured 23–69 px
off against 1–3 px.

**VoiceOS** — <https://www.voiceos.com/>, integration docs at
<https://docs.voiceos.com/integrations>. A voice assistant that lives in the Mac
notch: the user talks, the agent acts, and results come back as cards rendered in
the notch. That is the surface shape this proposal adopts, and the demo it was
drawn from is cited in `skills/notch/SKILL.md`.

**DynamicNotchKit** — <https://github.com/MrKai77/DynamicNotchKit>, by Kai Azim.
The Swift package that does the notch drawing: custom window, content insets,
safe areas, and a floating style for Macs without a notch. It is vendored under
`skills/notch/notch/Vendor/`, and the only change to it is one hook where the host
app supplies a placement, so upstream holds no policy of ours.

## What this proposes

One surface, and the parts that decide what goes on it.

### 1. The notch — the surface

A panel that shows external context on top of whatever the owner is doing. It is
not specific to any source: anything the current task needs and the current
window does not contain is rendered in the same place.

![The panel in the lower right, clear of the message it is
about.](design-voice-navigation-and-triage-placement.png)

**It chooses where to sit.** The owner never positions it. On every show, and
again whenever they switch app, it scores candidate slots against the live screen
and takes the emptiest one:

```
score(slot) = overlapFractionWithFocusedApp(slot) * 10000
            + meanTextDensity(slot)
```

Covering the app in use is categorically worse than covering idle text, so the
focus term outweighs the 0–255 text scale rather than adding to it. Text density
is the mean horizontal gradient of a downscaled grayscale screen grab: glyph
strokes make dense vertical edges, wallpaper almost none. A panel the owner has
dragged is never moved again.

**It stays until dismissed.** Nothing retracts it on a timer.

**It should appear when context needs bridging, not only when asked.** Today it
renders when something invokes it. The gaps in the Problem section are
identifiable in advance — a message asking for a verdict on a link the owner
cannot see is a bridgeable gap whether or not they think to ask. That inference
is the part not yet built.

#### What it shows

Three categories, one panel:

| Category | Examples | Answers |
|---|---|---|
| **A reference the current window names but does not contain** | a URL, a file, a directory | "what is this thing it is pointing at?" |
| **Context that already exists but is out of view** | earlier messages in this channel, a search across its history, the message under the pointer | "what was already said here?" |
| **A decision waiting on the owner** | a triage item: proposition, reason, freshness, five actions | "what should I do next?" |

The first two are the *within a task* half of the Problem section, the third is
*across tasks*. They are the same panel because they are the same gap.

![Mockup: a triage item rendered on the panel — proposition, source, a freshness
line, and Approve / Reject / Reply / Next /
Dismiss.](design-voice-navigation-and-triage-triage-on-notch.png)

A triage item is not a special case; it is the third row of that table. (This
figure is drawn, not captured — post verdicts are not routed into triage today.)

### 2. The voice panel — how the notch is reached

Already running: its own small floating web client on port 8081, beside the main
one on 8080, toggled with **Ctrl+F** at any time and closed the same way.

![The voice panel: "Can you open this highlighted URL in the notch?" answered by
"Okay, I've opened that URL in the notch."](design-voice-navigation-and-triage-voice.png)

Speaking is what makes the notch cheap enough to use mid-task: a request costs a
sentence rather than a detour.

### 3. Pointing — anchoring to a place on the screen

Some context is not a separate thing to fetch; it is *this part, here*. Marking
which part, on the owner's own screen, is its own capability and runs in both
directions.

![A drafted post with one sentence circled on screen and a callout attached to
it.](design-voice-navigation-and-triage-pointing.png)

**Outward.** `point_at` takes a plain-words query — "the commit button", "where
do I run the app", or a sentence in a draft — captures the display, locates the
target, and flies a marker to it with a label. The resolution is the Accessibility
tree first and a vision model only when the tree is empty or sparse
([ADR-0001](adr/0001-pointer-teacher-brain.md)). The command reaches the overlay
as `state/pointer-cmd.json` (`{nx, ny, label, say, ts}`); a monotonic `ts` decides
which of two overlapping commands wins. Above, one sentence in the draft is
circled and annotated with what makes it the conclusion — a remark about a span,
attached to that span, rather than a paragraph the owner has to map back onto the
text themselves.

**Inward.** The same anchoring in reverse: the owner highlights something and
says *"open this"* rather than reading a URL aloud. Resolving it runs a fallback
chain — a Discord selection read first, since the visible label and the real
`href` often differ; a vision read of the highlighted text if that finds nothing;
the clipboard only when the owner says they copied something.

Asking the owner to copy a link they have already highlighted would hand the work
back, which is the trip this exists to remove.

### 4. In-channel navigation — moving through long context

A channel accumulates more than fits on screen or in memory. The questions the
owner actually has about it are relational rather than keyword-shaped:

- *"What was the last message from X?"*
- *"Which message did X send that mentioned Y?"*
- *"What was already decided here, and what led to it?"*

Scrolling answers these badly. It is a linear scan of something that is not
ordered by the thing being looked for, and it requires already knowing roughly
where the answer is — which is the part the owner does not have. Understanding
why a draft says what it says, or what a reply is replying to, means
reconstructing a chain across messages that may be far apart.

An Accessibility watcher tracks the visible channel, the message under the
pointer, and the current selection, behind four tools:
`summarize_current_discord_channel`, `search_current_discord_channel`,
`inspect_hovered_discord_message`, `read_selected_discord_text`. The full history
is cached outside the model and only bounded matches are passed in, so a question
about a long channel does not cost the whole channel.

All four exist and all four answer by speech alone, which is why this part has no
figure. A summary of forty messages cannot be spoken usefully and is gone from
the voice agent's context in ten minutes; the same summary as a card is skimmable
and stays. Each tool gains an optional path that also writes a card.
