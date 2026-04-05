---
name: x-twitter
description: "Post tweets, search, read mentions, and check engagement on X (Twitter) via API v2."
---

# X (Twitter)

Post, search, read, and monitor X from the command line.

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

1. Go to https://developer.x.com and sign in
2. Create a Project + App
3. Generate keys and add to `.env`:
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
