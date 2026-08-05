# media-transcript

Pull a video's transcript (captions/subtitles) as clean text — the doc-ingest sibling for video. Any task that references a YouTube (or other yt-dlp-supported) URL can consume the spoken content as text instead of being blind to it.

**Usage**:
```bash
python3 skills/media-transcript/scripts/transcript.py <url> [--lang en] [--timestamps] [--json]
```

Prints the transcript to stdout. `--timestamps` prefixes blocks with `[mm:ss]` cue starts (use when the question asks *when/after what* something was said). `--json` wraps the result as `{"url", "ok", "kind", "text"|"error"}` for programmatic callers.

## When to use

- A task references a video URL whose *content* matters ("what did they say about X", "what number is mentioned after Y", summarizing a talk).
- The agent-eval harness hits video-based benchmark tasks (4 of 10 GAIA L3 fails on 2026-07-30 were video-content questions this skill unblocks).
- Anything needing spoken-word content where captions exist.

Not for:
- **Local audio/video files** — use `skills/audio-transcribe` (whisper on the audio track); the script points there and exits 3.
- **Videos with no captions at all** — v1 is captions-only (uploader subs preferred, auto-captions fallback). The script says so honestly (exit 1) rather than guessing; a whisper-on-downloaded-audio fallback is a possible v2, deliberately out of scope (media downloads are heavyweight and often unnecessary).

## Behavior

- Prefers uploader subtitles over auto-generated captions; `--lang` preference (default `en`) with graceful fallback to available English variants.
- Downloads ONLY the subtitle track — never the media stream — into a private temp dir, removed afterwards. No API keys.
- Auto-caption VTTs repeat rolling caption windows; the parser dedupes them, strips inline tags/entities, and drops cue numbers, producing readable prose.
- `yt-dlp` is probed at runtime (PATH binary, else the importable module) — a host without it gets one actionable error naming the install, never a crash (same design language as doc-ingest's extractor probing).

Exit codes: `0` transcript printed · `1` failure (no captions / tool error) · `2` bad invocation · `3` handled-elsewhere pointer (local media file).
