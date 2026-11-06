# image-generation

Generate or edit images with a Gemini image model, and videos with Veo, from a Sutando task.

## Install

```bash
git clone https://github.com/sonichi/sutando.git
cd sutando
bash skills/install.sh
```

Or manually:
```bash
ln -s /path/to/sutando/skills/image-generation "$CLAUDE_CONFIG_DIR/skills/image-generation"
```

## What's included

- `scripts/generate.py` — image generation and editing over the Gemini REST API (standard
  library only); video generation through the `google-genai` SDK when it is installed. Prints one
  JSON line (`{"ok": true, "path": ...}` or `{"ok": false, "error": ..., "message": ..., "remedy": ...}`).

## Usage

- "Generate an image of a sunset over mountains"
- "Edit this photo to replace the background"
- "Create a logo with a dark theme"
- "Make this image look like a watercolor painting"

The file is written under `<workspace>/results/media/` and attached to the reply.

## Requirements

- A Gemini key: the managed key of a desktop install, or `GEMINI_API_KEY` / `GEMINI_VOICE_API_KEY`
  in the environment or `.env`. Without one the script reports `no_key` (exit 2).
- Optional: `Pillow` (input resizing, jpg/webp conversion); `google-genai` for `--video` only
  (reported as `sdk_missing` when absent).

## License

MIT

---

Built by [Sutando](https://github.com/sonichi/sutando) — a personal AI agent platform.
