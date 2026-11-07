---
name: x-twitter
description: "Post to X via a signed-in Chrome-for-Testing session (live method — no API keys); read and engage (like, reply) with no API key by driving your own logged-in Chrome; API v2 path for search/read/engagement."
---

# X (Twitter)

Post, search, read, and monitor X from the command line.

Three backends. Two of them drive a browser, and they drive **different** browsers —
pick by which one the action should run in:
- **Posting** (`x-post-browser.mjs`) — publishes via a dedicated **Chrome-for-Testing**
  profile under `<workspace>/data/x-browser-profile`. Not your everyday browser. No API keys.
- **Engaging** (`x-browser.py`) — read, `like`, `reply` in **your own logged-in Chrome**, via
  AppleScript, so it sees X exactly as you do. No API keys. macOS only.
- **API mode** (`x-post.py`) — search/read/engagement over X API v2, and posting where the
  OAuth1 write keys exist. Needs API keys.

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
- Neither browser lane has API keys to go stale. The posting lane fails on a signed-out
  Chrome-for-Testing profile; the engage lane fails on a signed-out everyday Chrome or a
  missing Apple Events toggle — see **Setup — engaging in your own Chrome**.

## Usage

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

- Profile: `<workspace>/data/x-browser-profile`, resolved through `scripts/sutando-config.sh workspace`
  (override with `$X_BROWSER_PROFILE`, declared in this skill's `manifest.json`). Per-host, holds live
  session cookies, and never synced — `data/` is in the vault `exclude` list. A profile still sitting at
  the pre-#2133 location is used with a one-line notice until you move it, so upgrading does not cost you
  a fresh sign-in. `$X_LOGIN_DONE_SENTINEL` and `$X_LOGIN_TIMEOUT_ITERS` are declared alongside it but are
  test/CI controls — there is no reason to set them by hand. Sign-in
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

# Mentions & your own timeline (OAuth1 — resolves users/me)
python3 skills/x-twitter/x-post.py mentions
python3 skills/x-twitter/x-post.py timeline

# ANOTHER account's timeline (bearer only — no OAuth1 needed)
python3 skills/x-twitter/x-post.py user-timeline Chi_Wang_
python3 skills/x-twitter/x-post.py user-timeline Chi_Wang_ --limit 100 --exclude retweets,replies

# Engagement (likes, retweets, views)
python3 skills/x-twitter/x-post.py engagement 2040817066199195818
```

## Engaging in your own Chrome (`x-browser.py`, no API key)

Drives your real, logged-in Google Chrome via AppleScript, so it reads **and
engages with** X exactly as you see it — no developer account, no keys. Reads
need no permissions beyond the Apple Events toggle; `reply` additionally needs
Accessibility permission (the submit is a real OS keystroke).

```bash
# The logged-in account (name + @handle)
python3 skills/x-twitter/x-browser.py whoami

# Visible tweets on your home timeline
python3 skills/x-twitter/x-browser.py home --limit 10

# A single tweet (id or full URL)
python3 skills/x-twitter/x-browser.py read 2040817066199195818

# Latest results for a search
python3 skills/x-twitter/x-browser.py search "sutando agent" --limit 10

# Engagement (opt-in writes — post publicly under your handle)
python3 skills/x-twitter/x-browser.py like 2040817066199195818
python3 skills/x-twitter/x-browser.py reply 2040817066199195818 "Nice thread"
```

Engagement notes:
- **`like`** is pure DOM (a synthetic click is honored by X) — reliable.
- **`reply`** is a hybrid: JS fills the composer, but the final **submit** needs
  a real OS keystroke (System Events Cmd+Return) because X ignores synthetic
  submit events. So `reply` additionally needs **Accessibility permission**, and
  it briefly brings Chrome to the foreground + activates the x.com tab to land
  the keystroke — don't run it while typing elsewhere. For bulk/headless writes,
  use API mode (`x-post.py`) or the posting lane (`x-post-browser.mjs`).

## Which auth each command needs

`search`, `read` and `user-timeline` run on **`X_BEARER_TOKEN` alone** — app-only auth, stdlib
urllib, no `requests`/`requests_oauthlib` install. `post`, `mentions` and `timeline` need the
OAuth1 four (`X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`).

**`timeline` vs `user-timeline` is an auth distinction, not a scope one.** `timeline` resolves
`users/me`, which is OAuth1-only; `user-timeline` takes a handle and reads the same endpoint
(`users/{id}/tweets`) over bearer. On a bearer-only host `timeline` cannot run and
`user-timeline` can.

**`--limit` is 5..100 on `user-timeline`** — this endpoint's own bound. `search` rejects below
10; different endpoints, different bounds. Outside the range the command refuses locally (rc=2)
rather than letting X return a 400 that reads like an account problem.

**`--exclude replies` keeps self-replies.** X drops replies to *others* and retains an author's
own thread continuations, so `--exclude retweets,replies` can still return a tweet whose
`in_reply_to_user_id` is the author. Measured, not assumed.

## Setup — API mode

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

## Setup — engaging in your own Chrome

macOS + Google Chrome only. No keys needed.

1. Be logged into x.com in Chrome.
2. Enable Chrome > View > Developer > **"Allow JavaScript from Apple Events"**
   (one-time toggle; without it Chrome refuses `execute javascript`).

## Notes

- Free tier: 500 posts/month, search recent tweets (7 days)
- Video upload uses chunked upload (supports 4K)
- Always confirm post content with user before publishing
- `x-browser.py` reads need no permissions beyond the Apple Events toggle.
  `like` works via DOM; `reply` works but needs Accessibility permission (the
  submit is a real OS keystroke) and foregrounds Chrome. Prefer API mode for
  bulk or headless writes.
