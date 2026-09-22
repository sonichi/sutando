---
name: image-generation
description: "Generate an image from a prompt or edit an existing one (Gemini image model, standard library only, any Gemini key the resolver finds) and attach the file to the reply; generate a video with Veo when the google-genai SDK is installed. Use when the owner asks for a picture, logo, hero image, mockup, illustration, edited photo, or a short video clip."
---

# Image and video generation

`scripts/generate.py` makes an image (or a video) from a prompt and prints **one JSON line** on
stdout. Nothing to install for images: it calls the Gemini REST API with the standard library and
takes the key from the credential resolver (`gemini-image`: the managed key of a desktop install,
else `GEMINI_API_KEY` / `GEMINI_VOICE_API_KEY`). Never run `pip` for this skill; `Pillow` is
optional (input resizing, jpg/webp conversion) and `google-genai` is only needed for `--video`.

## Run it

```bash
G="<this skill's directory>/scripts/generate.py"
python3 "$G" --prompt "A flat-design mascot for a note-taking app, teal and cream"     # text-to-image
python3 "$G" --input photo.jpg --prompt "Replace the background with a sunset"          # edit
python3 "$G" --prompt "A cute robot" --output "<workspace>/results/media/robot.png"     # explicit path
python3 "$G" --video --prompt "A timelapse of a city at sunset" [--aspect 9:16]         # video (SDK)
```

Output lands at `<workspace>/results/media/generated-<ts>.png` (`.mp4` for video), which is inside
the attachment allowlist, so the file can be sent as-is. `--model` overrides `IMAGE_MODEL` /
`VIDEO_MODEL` (env, then this skill's `manifest.json` `config`).

## Read the result

stdout is exactly one JSON line; progress goes to stderr.

| line | exit | what to do |
|---|---|---|
| `{"ok": true, "path": "...", "model": "...", "note"?: "..."}` | 0 | deliver the file (below) |
| `{"ok": false, "error": "no_key", ...}` | 2 | say the no-key message, verbatim, and stop |
| `{"ok": false, "error": "refused", "message": ...}` | 1 | say it was declined; offer a reworded prompt |
| `{"ok": false, "error": "no_image", "message": ...}` | 1 | say what the model answered instead; offer a more concrete prompt |
| `{"ok": false, "error": "api_error", "message": ...}` | 1 | say it failed on Google's side; one retry at most, then stop |
| `{"ok": false, "error": "sdk_missing", ...}` (video) | 2 | say the SDK is missing; offer an image instead |
| `{"ok": false, "error": "bad_input", ...}` | 2 | fix the input path; ask for the image again |

Every failure line carries a `remedy`; say it. Never imply an image exists when `ok` is false.

## Deliver the image

The reply that carries the picture has the `[file: <path>]` marker **on its own line**, with the
`path` from the JSON, and one short line of text:

```
Here's the mascot.
[file: /Users/…/workspace/results/media/generated-1789000000000.png]
```

In a task result this goes in the result file; in a direct reply, in the message. A generated
image goes where it was asked (it is your own work, not private data): the room the request came
from, or the DM when that is where it was asked.

## What to say when it fails (verbatim)

- `no_key`: "I can't generate images on this install yet: there is no Gemini key for image
  generation. Add one in Agent settings → Agent → Gemini API, or ask again once your plan
  includes the managed key."
- `refused`: "Gemini declined to generate that image: <message>. I can try a reworded prompt
  (no real people's faces, no copyrighted characters)."
- `no_image`: "Gemini answered with text instead of an image: <message>. Want me to try a more
  concrete description?"
- `api_error`: "Image generation failed on Google's side: <message>. I can try once more in a moment."
- `sdk_missing` (video only): "I can't generate videos on this install: the google-genai package
  is not installed. Run `pip3 install google-genai` on this machine, or I can make an image instead."

State the blocker in one line, name what unblocks it, and stop; no silent retries.

## Notes

- Video generation takes 1-3 minutes (polled every 10 s); Google keeps generated videos for 2 days.
- For image editing, be explicit: "keep the subject unchanged, only modify the background".
- Output format follows the `--output` extension when Pillow can convert; otherwise the returned
  format is kept under its own extension, never mislabelled. Maximum input image size ~20 MB.
- Options: `--prompt` (required), `--input` (repeatable), `--output`, `--model`, `--video`,
  `--aspect` (16:9 default, 9:16), `--quality` (jpg/webp, default 90).
