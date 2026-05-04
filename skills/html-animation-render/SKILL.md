---
name: html-animation-render
description: Render an HTML/SVG animation page to an MP4 video, optionally muxing in a soundtrack. For investor demos, social posts, talk videos — anything you've designed in the browser and need as a shareable file.
---

# HTML Animation Render

Take any self-driving HTML/SVG animation (CSS animations + optional rAF JS) and convert it to an MP4. Playwright records the page in headless Chrome, then ffmpeg muxes in your audio track.

## When to use

- You've built an animation in HTML/SVG (because iterating in the browser is fast) and need an MP4 to attach to email, post on social, drop into a slide deck, or share on Discord.
- The animation is self-driving — runs from page load with no user interaction needed.
- You want exact framing — viewBox-based SVG renders pixel-perfect at any output resolution.

## When NOT to use

- Programmatic / pixel-perfect math-driven animation → use `~/.sutando-memory-sync/machine-Chis-Mac-mini/skills/personal-diagram-animation/` (Manim).
- Photorealistic / generative video → use Veo via `skills/image-generation`.
- Live screen recording of an interactive session → use `skills/screen-record`.

## Usage

```bash
node skills/html-animation-render/scripts/render.mjs \
  --html ~/.sutando-memory-sync/notes/sutando-fleet-growth.html \
  --out  ~/.sutando-memory-sync/notes/media/sutando-fleet-growth.mp4 \
  --audio ~/.sutando-memory-sync/notes/media/happy-light-30s.mp3 \
  --duration 33
```

Flags:
- `--html` (required) absolute or relative path to the HTML file.
- `--out` (required) output `.mp4` path.
- `--audio` optional MP3 to mux in.
- `--duration` seconds to record. Set to your animation length + ~1s buffer (default 33).
- `--width` output width in pixels (default 1920).
- `--height` output height (default `width / 2.25` to match a 22.5x10 viewBox).

Output goes to stdout (the OUT path). Status messages to stderr.

## Authoring tips for the source HTML

- Drive everything from page load — no user gesture needed for the recording (audio CAN stay autoplay-blocked in headless; ffmpeg supplies the audio track separately).
- Match the page's aspect to your output resolution. `viewBox="-11.25 -4.5 22.5 10"` (2.25:1) matches the default 1920x853.
- Verify the layout BEFORE you render — see `feedback_self_render_layout_fixes.md` (in memory). Run `node src/browser.mjs <url> "wait:<ms>" screenshot` first to confirm no overlaps at peak event windows.

## Dependencies

- `playwright` (project node_modules — Sutando has it).
- System Chrome (Playwright `channel: 'chrome'`).
- Playwright's bundled ffmpeg: `npx playwright install ffmpeg` (one-time, ~1MB).
- `ffmpeg` in PATH (Homebrew: `brew install ffmpeg`).

## Worked example: Sutando fleet growth animation

`scripts/sample-sutando-fleet.sh` runs the renderer with the live fleet-growth page and the canonical happy-light-30s soundtrack. Output: 1920x852, 33s, ~2.1MB MP4. Re-run after every HTML edit.
