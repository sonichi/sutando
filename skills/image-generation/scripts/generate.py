#!/usr/bin/env python3
"""
Media generation using Gemini APIs.

Supports:
- Text-to-image: generate from a text prompt (Gemini Flash Image)
- Image editing: modify an existing image with a text prompt
- Text-to-video: generate video from a text prompt (Veo)
- Image-to-video: generate video from reference image + prompt

Usage:
  python3 generate.py --prompt "A sunset over mountains"
  python3 generate.py --input photo.jpg --prompt "Replace the background"
  python3 generate.py --video --prompt "A timelapse of a city" --output city.mp4
  python3 generate.py --video --input ref.jpg --prompt "Animate this scene"
"""

import argparse
import os
import sys
import time
from pathlib import Path


def load_env():
    """Load GEMINI_API_KEY from .env files."""
    for env_path in [
        Path(__file__).resolve().parent.parent.parent.parent / ".env",
        Path.home() / ".env",
    ]:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    # .env wins over stale shell env — see PR #416.
                    os.environ[key.strip()] = val.strip()


def generate_image(client, args):
    """Generate or edit an image."""
    from google.genai import types
    from PIL import Image
    import io

    contents = []

    for img_path in args.input:
        img_path = os.path.expanduser(img_path)
        if not os.path.isfile(img_path):
            print(f"Error: Input image not found: {img_path}", file=sys.stderr)
            sys.exit(1)

        img = Image.open(img_path)
        max_dim = 4096
        if max(img.size) > max_dim:
            ratio = max_dim / max(img.size)
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            img = img.resize(new_size, Image.LANCZOS)
            print(f"  Resized {img_path} to {new_size[0]}x{new_size[1]}", file=sys.stderr)

        contents.append(img)
        print(f"  Input: {img_path} ({img.size[0]}x{img.size[1]})", file=sys.stderr)

    contents.append(args.prompt)

    if args.output:
        out_path = Path(args.output)
    else:
        ts = int(time.time() * 1000)
        out_path = Path(f"generated-{ts}.png")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    ext = out_path.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        out_format = "JPEG"
    elif ext == ".webp":
        out_format = "WEBP"
    else:
        out_format = "PNG"

    model = args.model or os.environ.get("IMAGE_MODEL", "gemini-3.1-flash-image-preview")
    print(f"  Model: {model}", file=sys.stderr)
    print(f"  Prompt: {args.prompt[:100]}{'...' if len(args.prompt) > 100 else ''}", file=sys.stderr)
    print(f"  Generating image...", file=sys.stderr)

    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
            ),
        )
    except Exception as e:
        print(f"Error: Gemini API call failed: {e}", file=sys.stderr)
        sys.exit(1)

    image_saved = False
    text_response = ""

    if response.candidates:
        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                img_data = part.inline_data.data
                img = Image.open(io.BytesIO(img_data))

                save_kwargs = {}
                if out_format == "JPEG":
                    img = img.convert("RGB")
                    save_kwargs["quality"] = args.quality
                elif out_format == "WEBP":
                    save_kwargs["quality"] = args.quality

                img.save(str(out_path), out_format, **save_kwargs)
                image_saved = True
                print(f"  Saved: {out_path} ({img.size[0]}x{img.size[1]}, {out_format})", file=sys.stderr)
            elif part.text:
                text_response += part.text

    if not image_saved:
        print(f"Error: No image in response.", file=sys.stderr)
        if text_response:
            print(f"  Model said: {text_response}", file=sys.stderr)
        sys.exit(1)

    if text_response:
        print(f"  Note: {text_response.strip()}", file=sys.stderr)

    print(str(out_path.resolve()))


def generate_video(client, args):
    """Generate a video using Veo."""
    from google.genai import types
    from PIL import Image
    import io

    if args.output:
        out_path = Path(args.output)
    else:
        ts = int(time.time() * 1000)
        out_path = Path(f"generated-{ts}.mp4")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    model = args.model or os.environ.get("VIDEO_MODEL", "veo-3.1-generate-preview")
    aspect = args.aspect or "16:9"

    print(f"  Model: {model}", file=sys.stderr)
    print(f"  Prompt: {args.prompt[:100]}{'...' if len(args.prompt) > 100 else ''}", file=sys.stderr)
    print(f"  Aspect: {aspect}", file=sys.stderr)

    # Build config
    config = types.GenerateVideosConfig(
        aspect_ratio=aspect,
    )

    # If input image provided, use as reference
    image = None
    if args.input:
        img_path = os.path.expanduser(args.input[0])
        if not os.path.isfile(img_path):
            print(f"Error: Input image not found: {img_path}", file=sys.stderr)
            sys.exit(1)
        img = Image.open(img_path)
        print(f"  Reference image: {img_path} ({img.size[0]}x{img.size[1]})", file=sys.stderr)
        # Convert to bytes for the API
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        image = types.Image(image_bytes=buf.getvalue(), mime_type="image/png")

    print(f"  Generating video (this may take 1-3 minutes)...", file=sys.stderr)

    try:
        if image:
            operation = client.models.generate_videos(
                model=model,
                prompt=args.prompt,
                image=image,
                config=config,
            )
        else:
            operation = client.models.generate_videos(
                model=model,
                prompt=args.prompt,
                config=config,
            )
    except Exception as e:
        print(f"Error: Veo API call failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Poll for completion
    elapsed = 0
    while not operation.done:
        time.sleep(10)
        elapsed += 10
        print(f"  Waiting... ({elapsed}s)", file=sys.stderr)
        try:
            operation = client.operations.get(operation)
        except Exception as e:
            print(f"Error polling: {e}", file=sys.stderr)
            sys.exit(1)

    # Download result
    try:
        generated_video = operation.response.generated_videos[0]
        client.files.download(file=generated_video.video)
        generated_video.video.save(str(out_path))
        print(f"  Saved: {out_path}", file=sys.stderr)
    except Exception as e:
        print(f"Error downloading video: {e}", file=sys.stderr)
        sys.exit(1)

    print(str(out_path.resolve()))


def main():
    parser = argparse.ArgumentParser(description="Generate images or videos using Gemini")
    parser.add_argument("--prompt", "-p", required=True, help="Text prompt")
    parser.add_argument("--input", "-i", action="append", default=[], help="Input image path(s)")
    parser.add_argument("--output", "-o", default=None, help="Output file path")
    parser.add_argument("--model", "-m", default=None,
                        help="Model (default: gemini-2.5-flash-image for images, veo-3.1-generate-preview for video)")
    parser.add_argument("--quality", "-q", type=int, default=90, help="JPEG quality 1-100 (default: 90)")
    parser.add_argument("--video", "-v", action="store_true", help="Generate video instead of image")
    parser.add_argument("--aspect", default=None, help="Video aspect ratio: 16:9 (default) or 9:16")

    args = parser.parse_args()

    load_env()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY not set. Add it to .env or export it.", file=sys.stderr)
        sys.exit(1)

    try:
        from google import genai
    except ImportError:
        print("Error: google-genai not installed. Run: pip3 install google-genai", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    if args.video:
        generate_video(client, args)
    else:
        try:
            from PIL import Image
        except ImportError:
            print("Error: Pillow not installed. Run: pip3 install Pillow", file=sys.stderr)
            sys.exit(1)
        generate_image(client, args)


if __name__ == "__main__":
    main()
