# YouTube Live

Stream a source to **YouTube Live** over RTMP using `ffmpeg`. No camera or Google
API call is required for a basic broadcast — a persistent YouTube **stream key**
(plus auto-start enabled on the stream) is enough to go live.

**Usage**: `/youtube-live start [--source ...]` · `/youtube-live stop` · `/youtube-live status`

## On activation

```bash
# Go live with a test pattern + tone (no assets/camera — best for a first e2e test):
python3 skills/youtube-live/scripts/go_live.py start --source test

# Other sources:
python3 skills/youtube-live/scripts/go_live.py start --source file:/path/clip.mp4 --loop
python3 skills/youtube-live/scripts/go_live.py start --source image:/path/slate.png
python3 skills/youtube-live/scripts/go_live.py start --source screen

python3 skills/youtube-live/scripts/go_live.py stop
python3 skills/youtube-live/scripts/go_live.py status

# See the exact ffmpeg command without streaming (key redacted):
python3 skills/youtube-live/scripts/go_live.py start --source test --dry-run
```

## Enabling it (one-time setup)

1. On the YouTube channel you want to stream to, enable live streaming
   (studio.youtube.com → **Create → Go live**; first-time activation can take ~24h).
2. Create a stream and copy its **Stream key** (Studio → Go live → **Stream** tab →
   *Stream key*). Enable **auto-start / auto-stop** on that stream so pushing the
   RTMP feed goes live automatically.
3. Store the key in the vault (never on disk):
   ```
   vault set YOUTUBE_STREAM_KEY <your-stream-key>
   ```
   Send that via the Slack/Discord `vault set …` path — the bridge intercepts it
   into macOS Keychain before it touches disk.

That's all the skill needs to go live. The key is read as
`--stream-key` > `$YOUTUBE_STREAM_KEY` > vault `YOUTUBE_STREAM_KEY`, and is
**never printed** — the `--dry-run` command and the `ffmpeg_stderr` failure
diagnostics are redacted to `<STREAM_KEY>` before anything reaches stdout.
Known limitation: the raw ffmpeg log at `<workspace>/state/youtube-live.ffmpeg.log`
retains unredacted output at rest (the state dir is `0700`, owner-only); scrub or
delete it if you share diagnostics from that file directly.

## Sources

| `--source`      | What it streams                                              |
| --------------- | ----------------------------------------------------------- |
| `test`          | `lavfi` test pattern + 1 kHz tone. No assets — ideal for e2e |
| `file:<path>`   | A media file (`--loop` to repeat forever)                   |
| `image:<path>`  | A static image "slate" + silent audio                       |
| `screen`        | macOS `avfoundation` screen capture (+ default audio)       |

Encoding is fixed to YouTube-compatible H.264 + AAC; resolution/fps/bitrate come
from the manifest `config` block (override via env, e.g. `YOUTUBE_INGEST_BASE`).

## Notes / limitations (v0.1)

- **Stream-key MVP only.** This drives the RTMP push. Programmatic broadcast
  lifecycle (create/schedule/transition/end via the **YouTube Live Streaming API**,
  OAuth `youtube.force-ssl`) is a planned follow-up — it would let the skill
  create and end broadcasts without touching Studio. Filed as a TODO in the PR.
- macOS `screen` capture needs Screen Recording permission for the launching app.
- If the YouTube stream shows "offline" while ffmpeg is pushing, auto-start isn't
  enabled — either enable it on the stream or click **Go live** in Studio.
