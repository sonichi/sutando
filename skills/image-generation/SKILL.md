---
name: image-generation
description: "Generate and edit images using Gemini Flash Image, and generate videos using Veo. Supports text-to-image, image editing, text-to-video, and image-to-video."
---

# Media Generation

Generate images and videos using Gemini APIs.

## Image Generation (Gemini Flash Image)
- **Text-to-image**: Generate images from text descriptions
- **Image editing**: Modify existing images with natural language
- **Background replacement**: Change or enhance backgrounds
- **Hero/banner creation**: Create branded images with text overlays
- **Style transfer**: Apply artistic styles to photos

## Video Generation (Veo)
- **Text-to-video**: Generate video clips from text prompts
- **Image-to-video**: Animate a reference image with a prompt

## When to Use

- "Generate a hero image for my project"
- "Create a short video of a sunset timelapse"
- "Edit this photo to remove the background"
- "Make a video from this image"
- "Generate a logo with a dark theme"

## Usage

```bash
# Text-to-image
python3 "$SKILL_DIR/scripts/generate.py" --prompt "A futuristic city skyline at night"

# Edit an existing image
python3 "$SKILL_DIR/scripts/generate.py" --input photo.jpg --prompt "Add dramatic clouds"

# Specify output path
python3 "$SKILL_DIR/scripts/generate.py" --prompt "A cute robot mascot" --output mascot.png

# Text-to-video
python3 "$SKILL_DIR/scripts/generate.py" --video --prompt "A timelapse of a city at sunset"

# Video with portrait aspect ratio
python3 "$SKILL_DIR/scripts/generate.py" --video --prompt "Ocean waves" --aspect 9:16

# Image-to-video (animate a reference image)
python3 "$SKILL_DIR/scripts/generate.py" --video --input scene.jpg --prompt "Animate this scene with gentle wind"

# Specify output
python3 "$SKILL_DIR/scripts/generate.py" --video --prompt "Dancing robot" --output robot.mp4
```

## Options

| Flag | Description | Default |
|------|-------------|---------|
| `--prompt` | Text prompt describing what to generate | (required) |
| `--input` | Input image path(s) for editing/reference | None |
| `--output` | Output file path | `generated-{timestamp}.png` or `.mp4` |
| `--model` | Gemini model to use | `gemini-2.5-flash-image` / `veo-3.1-generate-preview` |
| `--video` | Generate video instead of image | false |
| `--aspect` | Video aspect ratio | `16:9` |
| `--quality` | JPEG quality (1-100, images only) | 90 |

## Requirements

- `google-genai` Python package (`pip3 install google-genai`)
- `GEMINI_API_KEY` in `.env` or environment
- Pillow (`pip3 install Pillow`) — for image generation/editing

## Notes

- Video generation takes 1-3 minutes (polling every 10s)
- Generated videos are stored on Google servers for 2 days
- Gemini may refuse some prompts (people's faces, copyrighted characters, etc.)
- For image editing, be explicit: "keep the subject unchanged, only modify the background"
- Image output format inferred from extension (.jpg, .png, .webp)
- Maximum input image size: ~20MB
