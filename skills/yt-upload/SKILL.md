---
name: yt-upload
description: Upload videos to YouTube via the Data API v3 using cached OAuth credentials. Designed for WIRE episode auto-upload — supports cron / unattended runs after a one-time browser auth.
---

# YouTube Upload

## One-time setup

1. **GCP project**: reuse the existing `Default Gemini Project` (or create new). Enable **YouTube Data API v3** at `console.cloud.google.com/apis/library/youtube.googleapis.com`.
2. **OAuth consent screen**: APIs & Services → OAuth consent screen → External → fill app name, support email, dev contact email. Add scope `https://www.googleapis.com/auth/youtube.upload`.
3. **OAuth Client ID**: APIs & Services → Credentials → Create credentials → OAuth client ID → **Desktop app** → download JSON (default `~/Downloads/client_secret_*.json`).
4. **First-run auth**: opens browser for you to grant access, then caches the token:

```bash
python3 skills/yt-upload/scripts/auth.py ~/Downloads/client_secret_*.json
# → browser opens → click through OAuth (use "Advanced → unsafe" if "app not verified" warning)
# → token cached at ~/.config/sutando/youtube-token.json (chmod 600)
```

## Upload a video

```bash
python3 skills/yt-upload/scripts/upload.py path/to/video.mp4 \
    --title "Sutando WIRE ep013 — Setting up YouTube auto-upload" \
    --description "Demo of pointer-teacher guiding through GCP OAuth." \
    --privacy unlisted \
    --tags sutando,WIRE,ep013
# → stdout: https://www.youtube.com/watch?v=<video-id>
```

Defaults: `--privacy unlisted` (safer than public), `--category-id 22` (People & Blogs).

## Quota

Free tier: 10,000 units/day. An upload costs ~1,600 units → **~6 uploads/day max**. Fine for WIRE (one episode every few days).

## Cron use

After the one-time `auth.py` run, the token auto-refreshes silently. Wire into make-wire-episode or call directly from a cron:

```bash
python3 skills/yt-upload/scripts/upload.py "${RENDERED_MP4}" \
    --title "$(jq -r .title spec.json)" \
    --description "$(cat description.md)" \
    --privacy unlisted
```

## Dependencies

```bash
pip3 install google-auth-oauthlib google-api-python-client
```

(Both are pure-Python, no native deps.)

## Files

- `scripts/auth.py` — first-run OAuth flow + token cache
- `scripts/upload.py` — upload a video file, returns watch URL
- Token: `~/.config/sutando/youtube-token.json` (gitignored)

## Related

- `make-wire-episode` skill produces the rendered mp4 this uploads
- ep013 demo subject (pointer-teacher walking through the GCP setup flow)
