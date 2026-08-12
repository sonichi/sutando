---
name: x-twitter
description: "Post tweets, search, read mentions, and check engagement on X (Twitter) via API v2."
---

# X (Twitter)

Post, search, read, and monitor X from the command line.

## When to use
The user wants to **post to / read from X (Twitter)** — publish a tweet or thread,
reply, search recent tweets, check mentions/timeline, or pull engagement on a
known tweet id. Not for other social platforms.

## Failure modes
- **403 / "not permitted"** on post → the X API tier doesn't allow writes, or the
  app lacks Read+Write permission (regenerate the access token AFTER setting RW).
- **429 rate-limited** → API v2 free tier has low write/read caps; back off and retry later, don't loop.
- **401** → a key in `.env` is missing or stale. The skill uses five: `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`, and `X_BEARER_TOKEN`. **Reads authenticate with the bearer token**, so a 401 on search/mentions/timeline is most often a stale `X_BEARER_TOKEN`, not the OAuth pair.
- **Done =** the command prints the new tweet id / the result rows; a post with no id back did not publish.

## Usage

```bash
# Post
python3 skills/x-twitter/x-post.py post "Your tweet text"
python3 skills/x-twitter/x-post.py post "With video" --media /path/to/video.mp4
python3 skills/x-twitter/x-post.py post --reply-to 123456789 "Reply text"

# Search
python3 skills/x-twitter/x-post.py search "sutando agent"
python3 skills/x-twitter/x-post.py search "from:Chi_Wang_" --limit 5

# Read a tweet
python3 skills/x-twitter/x-post.py read 2040817066199195818

# Mentions & timeline
python3 skills/x-twitter/x-post.py mentions
python3 skills/x-twitter/x-post.py timeline

# Engagement (likes, retweets, views)
python3 skills/x-twitter/x-post.py engagement 2040817066199195818
```

## Setup

1. Install Python dependencies (one-time):
   ```
   pip3 install requests requests-oauthlib
   ```
2. Go to https://developer.x.com and sign in
3. Create a Project + App
4. Generate keys and add to `.env`:
   ```
   X_API_KEY=...
   X_API_SECRET=...
   X_ACCESS_TOKEN=...
   X_ACCESS_TOKEN_SECRET=...
   ```

## Notes

- Free tier: 500 posts/month, search recent tweets (7 days)
- Video upload uses chunked upload (supports 4K)
- Always confirm post content with user before publishing
