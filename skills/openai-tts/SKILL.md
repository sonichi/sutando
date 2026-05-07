---
name: openai-tts
description: "Render text to mp3 via OpenAI's tts-1-hd. Use for video narration, demo voiceovers, audio notes."
user-invocable: true
---

# OpenAI TTS

Synthesize speech via OpenAI `tts-1-hd`. Reads `OPENAI_API_KEY` from `.env`.

This is offline synthesis — distinct from voice-agent's bidirectional Gemini Live audio.

**Usage**: `/openai-tts [text]`

ARGUMENTS: $ARGUMENTS

## Voices

`alloy`, `ash`, `coral` (default), `echo`, `fable`, `nova`, `onyx`, `sage`, `shimmer`.

## Examples

```bash
bash "$SKILL_DIR/scripts/synthesize.sh" -- "Hello, this is Sutando."
bash "$SKILL_DIR/scripts/synthesize.sh" --voice ash --out /tmp/intro.mp3 -- "Hi."
```

Default output path: `results/openai-tts-{epoch}.mp3`. Cost: ~$0.02 per 60s of narration.

## If Invoked As A Slash Command

If ARGUMENTS is empty, ask the user for the text. Otherwise:

```bash
bash "$SKILL_DIR/scripts/synthesize.sh" -- "$ARGUMENTS"
```
