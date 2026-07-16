---
name: x-twitter
description: "Post to X via a signed-in browser session (live method — no API keys); API v2 path for search/read/engagement."
---

# X (Twitter)

Post, search, read, and monitor X from the command line.

## Posting — use the browser session (live method)

The OAuth1 API post path below is **NOT wired** on this fleet: posting needs 4 write
keys (X_API_KEY/SECRET + X_ACCESS_TOKEN/SECRET) that are not in the vault. Posting is
NOT credit-gated — it just needs those keys, which we don't have. **Do not conclude "X
is blocked."** The working path is a signed-in Chrome-for-Testing browser session:

```bash
# Is the profile signed in?  (headless, exit 0 = yes, 2 = no)
node skills/x-twitter/x-post-browser.mjs check

# Owner signs in once (headed GUI window; email/phone — Google/Apple OAuth stay blocked)
node skills/x-twitter/x-post-browser.mjs login

# Compose only, screenshot, DO NOT publish  (always run this first)
node skills/x-twitter/x-post-browser.mjs post "Your tweet text" --dry-run

# Publish  (only after owner OKs the dry-run)
node skills/x-twitter/x-post-browser.mjs post "Your tweet text"
```

- Profile: `~/.sutando/x-browser-profile` (override `$X_BROWSER_PROFILE`). Sign-in
  survives ONLY because `check`/`post` strip Playwright's `--use-mock-keychain` so
  cookies decrypt with the real login keychain — see
  `memory/reference_x_browser_signin_oauth_blocked_use_email_phone.md`.
- **Always `--dry-run` first and confirm with the owner before publishing.** Nothing
  posts without an explicit OK.

## API v2 usage (search / read / engagement — reads only, no post keys)

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
