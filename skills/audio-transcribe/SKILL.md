# audio-transcribe

Transcribes audio files (voice notes, clips) to text via Gemini 2.5-flash.
Used by the Slack, Discord, and Telegram bridges to surface voice-note content
in task bodies so the core agent can act on the words instead of a bare file path.

## How it works

```
skills/audio-transcribe/scripts/transcribe.py <audio_file_path>
```

Reads the file, sends it inline (base64) to the Gemini 2.5-flash generateContent
endpoint, and prints the transcript to stdout. Exits 0 on success, 1 on any
failure (unsupported format, missing API key, network error, API error).

**Fail-open design.** Every bridge wraps the call in a helper that returns `None`
on a non-zero exit. A failed transcription never blocks the task — the
`[File attached: /path]` line still goes through so the agent can at least see
a file was sent.

## Supported formats

`.m4a` (Slack voice clips), `.mp3`, `.ogg`, `.oga`, `.opus`, `.wav`, `.webm`,
`.aac`, `.flac`, `.mp4`

## Key resolution order

1. `GEMINI_API_KEY` or `GOOGLE_API_KEY` in the process environment
2. `$SUTANDO_WORKSPACE/.env` (default: `~/.sutando/workspace/.env`)
3. `$CLAUDE_CONFIG_DIR/channels/slack/.env`
4. `$CLAUDE_CONFIG_DIR/channels/discord/.env`
5. `$CLAUDE_CONFIG_DIR/channels/telegram/.env`

## Bridge integration

Each bridge calls `_transcribe_via_skill(local_path)` after downloading a file.
The helper locates the skill script relative to `src/` (or the app bundle), runs
it, and returns the transcript string or `None`. The bridge then appends either:
- `[Voice transcript: <text>]` — when transcription succeeds
- `[File attached: /path]` — when skill is absent or transcription fails

## Removing the skill

Delete `skills/audio-transcribe/` — all three bridges fall back to `[File attached:]`
automatically. Core services are unaffected.
