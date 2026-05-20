# Setup — auto-regen WIRE list

The README's Sutando WIRE list is regenerated nightly from the YouTube
playlist. Setup is one-time: enable the API + create a key + store it as a
GitHub Actions secret.

## Steps

1. **Enable YouTube Data API v3** in Google Cloud Console.

   - Go to https://console.cloud.google.com/apis/library/youtube.googleapis.com
   - Select (or create) a project for Sutando — e.g. `sutando-readme`
   - Click **Enable**

2. **Create an API key**.

   - Go to https://console.cloud.google.com/apis/credentials
   - Click **Create credentials → API key**
   - Restrict the key:
     - **Application restrictions**: None (the GH Action runs server-side)
     - **API restrictions**: limit to "YouTube Data API v3" only
   - Copy the key

3. **Store as GitHub Actions secret**.

   - Go to `https://github.com/<owner>/sutando/settings/secrets/actions`
   - Click **New repository secret**
   - Name: `YOUTUBE_API_KEY`
   - Value: paste the key from step 2

4. **Verify**.

   - Trigger the workflow manually:
     - GitHub → Actions → "Regen README WIRE list" → "Run workflow"
   - If a PR opens (or none is needed because nothing changed), setup is good.

## Quota

YouTube Data API v3 free tier: 10,000 units / day per project.

`scripts/regen-wire-list.py` makes ~3-5 calls per run (1 unit each). One
nightly run + occasional manual dispatch uses < 100 units / day. Well within
free tier; no billing card required.

## Local dry-run

To test locally without writing:

```bash
export YOUTUBE_API_KEY=<your-key>
python3 scripts/regen-wire-list.py --dry-run
```

Output is the rendered markdown block — paste-check before letting the cron
fire.

## What gets edited

Only the lines between `<!-- wire-list:start -->` and `<!-- wire-list:end -->`
in `README.md`. The headline above and the channel/playlist links below are
untouched.

If the markers go missing or the README is hand-edited inside them, the
script will refuse to substitute and the action surfaces the error.
