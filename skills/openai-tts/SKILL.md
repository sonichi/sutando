---
name: openai-tts
description: "Synthesize speech from text via OpenAI's TTS-1-HD API. Use for video narrations, demo voiceovers, audio notes, and accessibility renderings. Returns mp3 path."
user-invocable: true
---

# OpenAI TTS

Render text to speech via OpenAI's `tts-1-hd` model. Reads `OPENAI_API_KEY` from `.env`. Saves an mp3 to a path you choose.

This is a *synthesis* skill — distinct from voice-agent's bidirectional Gemini Live audio. Use it for offline voiceovers, not real-time conversation.

**Usage**: `/openai-tts [text]`

ARGUMENTS: $ARGUMENTS

## When to Use

- Render narration for a video or screen recording
- Generate voiceover audio for a demo
- Produce an mp3 of a written passage (audio notes, accessibility)
- A/B test different voices or styles

## Voices

`tts-1-hd` accepts: `alloy`, `ash`, `coral`, `echo`, `fable`, `nova`, `onyx`, `sage`, `shimmer`.

Default: `coral` (cheerful, expressive — works well for upbeat narration).

## Common Commands

```bash
# Default voice + auto-pathed output (results/openai-tts-{ts}.mp3)
bash "$SKILL_DIR/scripts/synthesize.sh" -- "Hello, this is Sutando."

# Pick a voice and an output path
bash "$SKILL_DIR/scripts/synthesize.sh" --voice ash --out /tmp/intro.mp3 -- "Hi, I'm Sutando."

# From a file (multi-line / longer text)
bash "$SKILL_DIR/scripts/synthesize.sh" --voice coral --out /tmp/scene.mp3 --file path/to/script.txt
```

## If Invoked As A Slash Command

- If ARGUMENTS is empty, explain the voices and ask the user for the text.
- If ARGUMENTS is present, run with default voice + auto-pathed output:

```bash
bash "$SKILL_DIR/scripts/synthesize.sh" -- "$ARGUMENTS"
```

Then post the resulting mp3 path back to the user.
