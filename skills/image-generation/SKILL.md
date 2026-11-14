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

## Send it to a different AG2 Space room or DM

A picture for the room or DM the request came from goes with `[file:]` (above). Only a picture for
another room ("send the boba tea picture to our AG2 Space DM") uses the upload script with `--post`,
because the bridge uploads `[file:]` only to the task's own room. A voice task carries every room's
id in its "known to the voice assistant" block: take the id from there, never `room_list`. The DM
with a person is the two-member room named after the two of you ("Maddy-susan"); only when the block
is missing, `room_list` then `room_inspect`. `posted: true` with an `event_id` means the chat message
exists; a read of that event is the check. The script lives in the ag2space-voice skill, so this
needs that skill installed.

```bash
U="<repo>/skills/ag2space-voice/scripts/upload-media.ts"
npx tsx "$U" --room '<room id>' --file "<path>" --name boba-tea.jpg --post
```

## Put it into a project

When the picture belongs in a LaTeX/Typst project ("add figure 3, a pineapple"), upload it from disk
with the upload script, without `--post` (it is filed on the Media page, not sent into the chat). **Never pass the image's base64 through a tool call
yourself** (`room.media.upload` by hand): writing tens of thousands of characters takes minutes
and corrupts the bytes.

```bash
U="<repo>/skills/ag2space-voice/scripts/upload-media.ts"
npx tsx "$U" --room '<room id>' --file "<path from generate.py>" --name pineapple.jpg --folder <project folder id>
```

It prints one JSON line: `{"ok": true, "name", "file_id", "mxc", "sha256", ...}`. The type comes from
the bytes, so the extension in `name` may change (a JPEG is filed as `.jpg`). With `--folder`, the
image is filed in that project folder, where Typst's `#image("<name>")` and LaTeX's
`\includegraphics{<name>}` find it. Then change the file with `room.commons.changes.apply` to
reference that `name`, and compile once (`room.commons.typst.compile` or `latex.compile`, passing
the folder's media) to check the build. `{"ok": false, "error": "needs_approval"}` means the room
requires an approval first: say so. `not_allowed` means the file is outside the attachment allowlist
(generated images in `results/media` are inside it); any other error carries `detail`, say it. An
error that still has `posted: true` means the picture is already in the chat: do not post it again.

## What to say when it fails (verbatim)

- `no_key`: "I can't generate images on this install yet: there is no Gemini key for image
  generation. Add one in Agent settings → Agent → Gemini API, or ask again once your plan
  includes the managed key."
- `refused`: "Gemini declined to generate that image: <message>. I can try a reworded prompt
  (no real people's faces, no copyrighted characters)."
- `no_image`: "Gemini answered with text instead of an image: <message>. Want me to try a more
  concrete description?"
- `quota`: "I can't generate images with the current Gemini key: Google says it has no image quota
  (a free-tier key has 0 image requests per day). Enable billing on that key's Google Cloud project,
  or switch to the managed key in Agent settings → Agent → Gemini API." Say which key it was when
  the message names it; never retry a quota error.
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
