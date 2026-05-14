---
name: gemini-tts
description: "Render text to mp3 via Google Gemini Flash TTS. Free-tier eligible (1500 req/day). Use for video narration, demo voiceovers, audio notes. Parallels openai-tts; default for make-viral-video."
user-invocable: true
---

# Gemini TTS

Synthesize speech via Google's `gemini-2.5-flash-preview-tts` (or `-pro-tts` / `-lite-preview-tts` per env override). Reads `GEMINI_API_KEY` from `.env`.

This is offline synthesis — distinct from voice-agent's bidirectional Gemini Live audio. Same model family, different surface (POST text → get audio bytes back, no streaming).

**Usage**: `/gemini-tts [text]`

ARGUMENTS: $ARGUMENTS

## Voices

`Aoede` (default — alto, neutral), `Charon` (baritone, news-anchor), `Kore` (mid, expressive), `Puck` (high, conversational). Per Lucy's 2026-05-09 testing: Aoede is the closest match to OpenAI's `sage`.

## Audio tags for expression

Inline bracket tags like `[whispers]`, `[excitedly]`, `[slowly]` are interpreted as stylistic direction, not spoken literally. Empirically verified against `gemini-2.5-flash-preview-tts` (per PR #646 comment): `[whispers] hello` → 1.05s audio; `hello` alone → 1.01s. If the tag were spoken literally as 8 words, the clip would be ~5× longer.

```bash
bash "$SKILL_DIR/scripts/synthesize.sh" -- "[whispers] Pull request 691 has landed."
```

## Model selection

Default: `gemini-2.5-flash-preview-tts` (free tier, 1500 req/day, $0 within quota).

Override via `GEMINI_TTS_MODEL` env var:
- `gemini-2.5-pro-tts` — paid, higher fidelity
- `gemini-2.5-flash-lite-preview-tts` — preview, faster
- `gemini-3.1-flash-tts-preview` — preview

## Examples

```bash
bash "$SKILL_DIR/scripts/synthesize.sh" -- "Hello, this is Sutando."
bash "$SKILL_DIR/scripts/synthesize.sh" --voice Charon --out /tmp/intro.mp3 -- "Hi."
GEMINI_TTS_MODEL=gemini-2.5-pro-tts bash "$SKILL_DIR/scripts/synthesize.sh" -- "High-fidelity narration."
```

Default output path: `results/gemini-tts-{epoch}.mp3`.

## Cost

Free tier: $0 within 1500 req/day quota. For our cadence (a few demos a day), stays free indefinitely.
Paid (Flash): $0.50 / 1M input tokens + $10.00 / 1M output tokens.

Compared to OpenAI TTS (`gpt-4o-mini-tts`) at ~$0.02 per 60s: Gemini Flash is free-equivalent for typical demo workloads.

## When to fall back to openai-tts

The `make-viral-video` skill auto-falls-back to OpenAI TTS when:
- Gemini API returns 4xx/5xx
- Gemini quota hit (429)
- `GEMINI_API_KEY` missing
- `TTS_PROVIDER=OPENAI` env override set

## If Invoked As A Slash Command

If ARGUMENTS is empty, ask the user for the text. Otherwise:

```bash
bash "$SKILL_DIR/scripts/synthesize.sh" -- "$ARGUMENTS"
```
