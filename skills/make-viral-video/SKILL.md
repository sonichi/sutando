---
name: make-viral-video
description: "Build a short news-explainer video tuned for shareability. One striking moment per video; real fetched assets; self-heal validation; pluggable TTS (Gemini-free default, OpenAI fallback)."
user-invocable: true
---

# make-viral-video

Build a short (~30-60s, 1280×720 landscape, h264+aac) news-explainer video around a single source URL or news event. Designed for shareability via the **specificity-shape** framing (Chi's pick 2026-05-09): one striking number, image, or quote — not a comprehensive overview.

Distinct from Lucy's `recursive-meta-explorer` v8-v12 lineage (which optimized for completeness). This skill optimizes for *the one moment that makes someone share*.

## Usage

```bash
bash "$SKILL_DIR/scripts/build.sh" --topic "DoW UAP file release" --source "https://war.gov/UFO" --out results/
```

Produces:
- `results/viral-{ts}/video.mp4` — the rendered video
- `results/viral-{ts}/script.md` — the narration script
- `results/viral-{ts}/assets/` — fetched images with hash + validation log
- `results/viral-{ts}/build.log` — full build trace (codex iterations + validation results)
- `results/viral-{ts}/verify.sh` — script to independently re-validate the build

## Required tools

- `python3 ≥ 3.10` (PIL for frame composition)
- `ffmpeg` (encode + ffprobe gate)
- `gemini` API key (free tier, default) **OR** `openai` API key (fallback)
- Codex CLI (for script generation per pass)

## Architecture

### Three-phase build with self-heal

**Phase 1 — Source ingestion + script generation**
- Fetch the source URL (curl + Playwright fallback for JS-rendered pages)
- Codex generates a narration script + asset manifest with the **specificity-shape framing**:
  - One striking opening fact (the hook)
  - 3-5 supporting details (context)
  - One closing twist or open question (the share moment)
- Asset manifest is JSON: `[{"url": "...", "alt": "...", "purpose": "hook|support|closer"}]`

**Phase 2 — Asset validation (the self-heal loop)**

For each asset URL:
1. Fetch
2. Hash check: refuse duplicates (catches the v12 PR46-404-dupe pattern)
3. Content-type check: must be image/* or fall back
4. 404 detector: known-404-page hash list + OCR keywords ("404", "page not found", "Not Found")
5. Resolution check: ≥600×400 (sub-thumb assets are not viral-grade)
6. Re-fetch on failure with up to 2 retries
7. **If validation still fails** → script regen with that asset removed, NOT a silent skip

If 3+ assets fail validation, escalate to user (don't ship a video with placeholder content).

**Phase 3 — Render + pre-publish gate**

1. Compose frames via PIL (templates: hook-card / asset-with-caption / closer-card)
2. TTS narration via Gemini Flash (free) → fall back to OpenAI on rate-limit / error
3. ffmpeg encode
4. **ffprobe gate**: confirm h264 video stream + aac audio stream + 1280×720 dimensions + duration within ±5s of script estimate
5. If gate fails → report what's missing + don't post

## Visual constraints (specificity-shape)

Per Chi's "visual needs improvement too":

- **Hook card** (0-3s): single bold claim in large type (≥120pt), brand color background, no other text
- **Support cards** (3-X seconds before closer): real fetched image fills frame; caption overlay (semi-transparent strip, 36pt) with the *one* attributable fact about that image
- **Closer card** (last 3-5s): the share-moment text — not a recap. A pointed question, surprising number, or open-ended provocation.
- Aspect: 1280×720 (16:9). NOT vertical 9:16 (Lucy's v10/v11 cropped pictures because of this; landscape preserves news-photo composition).
- Typography: max 2 fonts (one display, one body). Body font must read at 36pt against any image (test: bottom-third of the frame must contain text or be black-strip overlay).

## TTS provider selection

Default: `GEMINI` (free tier, Aoede voice, 1500 req/day quota). Fallback: `OPENAI` (sage voice, paid, ~$0.02/min).

Override: `TTS_PROVIDER=OPENAI` env var.

## Smoke test

```bash
# Reproduce the UAP topic with this skill, compare against Lucy's v12 visually:
bash scripts/build.sh --topic "DoW UAP file release" --source "https://war.gov/UFO"
```

Validate the output passes:
- ffprobe shows h264 + aac, 1280×720, ~45s
- 0 duplicate-hash assets
- 0 404 page screenshots in assets/
- Hook card has one bold claim (not 3 bullet points)

## Why this isn't `recursive-meta-explorer-prompt` v13

Lucy's lineage optimized for *informational completeness* + per-pass codex /goal iteration. This skill optimizes for *one shareable moment* + a deterministic validation gate. They serve different goals; both can coexist. Not deleting Lucy's work.
