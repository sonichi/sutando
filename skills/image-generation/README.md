# image-generation

A Claude Code skill for AI agents.

## Install

```bash
# Clone and install
git clone https://github.com/liususan091219/sutando.git
cd sutando
bash skills/install.sh
```

Or manually:
```bash
ln -s /path/to/sutando/skills/image-generation ~/.claude/skills/image-generation
```

## What's included

1 scripts:
- `generate.py` — image generation and editing via Gemini API

## Usage

- "Generate an image of a sunset over mountains"
- "Edit this photo to replace the background"
- "Add text overlay to this image"
- "Create a logo with a dark theme"
- "Make this image look like a watercolor painting"

## Requirements

- `google-genai` Python package (`pip3 install google-genai`)
- `Pillow` Python package (`pip3 install Pillow`)
- `GEMINI_API_KEY` in `.env` or environment
- Claude Code installed

## License

MIT

---

Built by [Sutando](https://github.com/liususan091219/sutando) — a personal AI agent platform.
