# Community use cases

Got a moment where your Sutando did something you'd want to show off?
Share it here. We promote the best entries to the public showcase at
**https://sutando.ai/** under "Use cases".

## Two ways to submit

### 1. GitHub issue (lowest friction)

Open a [use-case submission issue](https://github.com/sonichi/sutando/issues/new?template=use-case-submission.md).
Walk through the template fields. A maintainer triages and either
promotes it directly or asks for a small reframe.

### 2. Pull request (structured, gets you credit on merge)

Add a file to `community-use-cases/<slug>.md` matching the schema
below. CLA-Assistant gates the PR (one-time signature). On merge, a
sync job picks up the entry and stages a PR against the showcase site.

#### File schema

```markdown
---
slug: your-ai-does-laundry          # lowercased, dashed
title: "Your AI does your laundry while you're at work"
summary: "One sentence that reads on the tile + detail page."
videoUrl: /community-use-cases/videos/your-ai-does-laundry.mp4  # optional
youtubeId: ABCDEFG1234               # optional
xUrl: https://x.com/you/status/...   # optional
linkedinUrl: https://www.linkedin.com/...  # optional
thumbnail: /community-use-cases/thumbnails/your-ai-does-laundry.jpg
contact: "@yourhandle (or email)"    # optional; we credit you on the page
submitted_at: 2026-05-17T07:50:00Z
---

Long description (2–4 sentences). What happened, why it matters, who
else this is useful for. Outcome-first language ("books the table",
"fixes the bug") — not capability-list ("can make calls", "supports
voice").
```

Drop the thumbnail at `community-use-cases/thumbnails/<slug>.jpg`
(any image format works; we'll re-encode if needed). Drop a self-hosted
video (if you have one) at `community-use-cases/videos/<slug>.mp4`.
Skip the video and just link YouTube/X if you'd rather.

## Framing — what we look for

The showcase tells *user-outcome stories*, not *capability lists*.
Compare:

- ✅ "Your AI books your dinner — and talks to their AI"
- ❌ "Ask your AI to make a phone call"
- ✅ "Catch and fix your own bugs"
- ❌ "Sutando supports anomaly detection"

If the title starts with "Ask your AI to …", "Sutando can …", or
names a button or feature — please reframe as a user outcome. The
review will ask for that change anyway.

## Easiest path: use the skill

Sutando ships a `/submit-use-case` skill that does the issue + PR
in one shot, including the framing-check. If you've got Sutando
installed:

```bash
/submit-use-case --title "..." --summary "..." --image-url "..."
```

It validates, stages, and opens both an issue and a PR for you.
