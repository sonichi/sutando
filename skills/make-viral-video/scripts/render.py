#!/usr/bin/env python3
"""Frame composition + ffmpeg encode for make-viral-video skill — phase 5.

Reads:
  - {workdir}/artifacts/final_script.md   (HOOK / SUPPORT / CLOSER sections)
  - {workdir}/artifacts/asset_manifest.json
  - {workdir}/fetched_assets/*.png

Produces:
  - {workdir}/frames/                     (PIL-rendered frames)
  - {workdir}/clips/                      (per-frame mp4s with Ken-Burns motion)
  - {workdir}/narration.mp3               (TTS, gemini default → openai fallback)
  - {workdir}/video.mp4                   (h264 + aac, 1280×720)

Visual rules per SKILL.md:
  - HOOK card: hero image as bg, dimmed; bold claim overlay; BREAKING badge
  - SUPPORT cards: hero image (cropped/zoomed differently per fact) + caption strip
  - CLOSER card: hero image dimmed + share-shape line overlay

Phase 1 v2 (2026-05-10, Chi A+C feedback): hero image is now used as visual
spine — previously, real images were tagged purpose=hook in the manifest but
the renderer ignored them and produced text-on-black for hook+closer, while
support frames rendered PIL data-cards instead of the real photo. Per Chi:
"bare PIL cards, no motion, no real imagery."
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

CANVAS_W, CANVAS_H = 1280, 720
FPS = 30

# Brand colors
BG = (10, 10, 14)        # near-black fallback
ACCENT = (220, 56, 76)   # red-ish for hook badge
TEXT = (240, 240, 240)
CAPTION_BG = (0, 0, 0, 180)
DARKEN_OVERLAY = (0, 0, 0, 110)  # for text-on-image readability

FONT_PATHS = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def get_font(size: int):
    from PIL import ImageFont
    for fp in FONT_PATHS:
        if Path(fp).exists():
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                pass
    return ImageFont.load_default()


def parse_script(script_md: str):
    """Parse final_script.md into HOOK/SUPPORT/CLOSER sections."""
    sections = {"HOOK": [], "SUPPORT": [], "CLOSER": []}
    current = None
    section_re = re.compile(r"^\s*(?:##|##|\*\*|\#)\s*(HOOK|SUPPORT|CLOSER)\b", re.IGNORECASE)
    for line in script_md.splitlines():
        m = section_re.match(line)
        if m:
            current = m.group(1).upper()
            continue
        if current and line.strip():
            sections[current].append(line.strip())
    if any(sections.values()):
        return {k: " ".join(v).strip() for k, v in sections.items() if v}
    text = script_md.strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) >= 3:
        return {
            "HOOK": sentences[0],
            "SUPPORT": " ".join(sentences[1:-1]),
            "CLOSER": sentences[-1],
        }
    return {"HOOK": text, "SUPPORT": "", "CLOSER": ""}


def find_hero_image(fetched: Path):
    """Return the real-photo hero image. Filters out data-card-*.jpg/png
    (PIL-generated text panels — not real imagery)."""
    candidates = [
        p for p in fetched.glob("*")
        if p.is_file() and not p.name.startswith("data-card-")
        and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
    ]
    return candidates[0] if candidates else None


def wrap_text(text: str, max_chars_per_line: int):
    words = text.split()
    lines, current = [], ""
    for w in words:
        if len(current) + len(w) + 1 <= max_chars_per_line:
            current = (current + " " + w).strip()
        else:
            lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def trim_black_borders(img, threshold: int = 25):
    """Auto-crop solid-black margin (e.g. press photo mats). Returns trimmed
    image (same as input if no borders detected). Uses ImageChops.difference
    against pure black + getbbox — pure PIL, no numpy dep."""
    from PIL import Image, ImageChops
    bg = Image.new(img.mode, img.size, (0, 0, 0))
    diff = ImageChops.difference(img, bg)
    # Boost the diff so threshold-level dark pixels get included
    diff = ImageChops.add(diff, diff, 2.0, -threshold)
    bbox = diff.getbbox()
    if bbox and (bbox[2] - bbox[0]) > 100 and (bbox[3] - bbox[1]) > 100:
        return img.crop(bbox)
    return img


def hero_bg(hero_path: Path, dim_alpha: int = 110):
    """Open hero image, trim black mat, fill 1280×720 cover-style, dim."""
    from PIL import Image
    img = Image.open(hero_path).convert("RGB")
    img = trim_black_borders(img)
    iw, ih = img.size
    canvas_ratio = CANVAS_W / CANVAS_H
    img_ratio = iw / ih
    if img_ratio > canvas_ratio:
        new_h = CANVAS_H
        new_w = int(iw * (CANVAS_H / ih))
    else:
        new_w = CANVAS_W
        new_h = int(ih * (CANVAS_W / iw))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - CANVAS_W) // 2
    top = (new_h - CANVAS_H) // 2
    img = img.crop((left, top, left + CANVAS_W, top + CANVAS_H))
    overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, dim_alpha))
    return Image.alpha_composite(img.convert("RGBA"), overlay)


def render_hook_frame(text: str, hero_path: Optional[Path], out_path: Path):
    """Hero image as bg (dimmed) + bold claim centered + BREAKING badge."""
    from PIL import Image, ImageDraw
    if hero_path:
        base = hero_bg(hero_path, dim_alpha=80).convert("RGB")
    else:
        base = Image.new("RGB", (CANVAS_W, CANVAS_H), BG)
    draw = ImageDraw.Draw(base)
    font = get_font(60)

    lines = wrap_text(text, 30)
    line_h = 72
    total_h = len(lines) * line_h
    y = (CANVAS_H - total_h) // 2 + 30

    # Localized darken behind text block for legibility
    if hero_path:
        from PIL import Image as _Im
        text_overlay = _Im.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        text_draw = ImageDraw.Draw(text_overlay)
        pad = 30
        text_draw.rectangle(
            [(0, y - pad), (CANVAS_W, y + total_h + pad)],
            fill=(0, 0, 0, 160),
        )
        base = _Im.alpha_composite(base.convert("RGBA"), text_overlay).convert("RGB")
        draw = ImageDraw.Draw(base)

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        x = (CANVAS_W - line_w) // 2
        for dx, dy in [(-2,0),(2,0),(0,-2),(0,2)]:
            draw.text((x+dx, y+dy), line, fill=(0,0,0), font=font)
        draw.text((x, y), line, fill=TEXT, font=font)
        y += line_h

    badge_font = get_font(28)
    bbox = draw.textbbox((0, 0), "BREAKING", font=badge_font)
    bw = bbox[2] - bbox[0]
    bh = bbox[3] - bbox[1]
    badge_pad_x, badge_pad_y = 16, 12
    badge_w = bw + badge_pad_x * 2
    badge_h = bh + badge_pad_y * 2 + 6
    draw.rectangle([(40, 40), (40 + badge_w, 40 + badge_h)], fill=ACCENT)
    draw.text((40 + badge_pad_x, 40 + badge_pad_y), "BREAKING", fill=TEXT, font=badge_font)

    base.save(out_path, "PNG")


def render_support_frame(hero_path: Path, caption: str, out_path: Path,
                          crop_seed: int = 0, badge: str = ""):
    """Hero image with crop variation + caption strip + optional name badge.

    If `badge` is non-empty, draws a styled name-chip in the top-left of the
    frame (e.g. "HARRISON SCHMITT"). Per Lucy's v1.7 design: when the bg image
    is a person's portrait, the badge anchors who's on screen for viewers
    who joined mid-scroll. Free-text — caller controls the wording.
    """
    from PIL import Image, ImageDraw
    bg = hero_bg(hero_path, dim_alpha=70).convert("RGB")
    overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    strip_h = 180
    draw.rectangle([(0, CANVAS_H - strip_h), (CANVAS_W, CANVAS_H)], fill=CAPTION_BG)

    cap_font = get_font(34)
    lines = wrap_text(caption.strip(), 60)[:3]

    y = CANVAS_H - strip_h + 22
    for line in lines:
        draw.text((40, y), line, fill=TEXT, font=cap_font)
        y += 48

    # Top-left name badge (e.g. "HARRISON SCHMITT") in brand-red chip
    if badge:
        badge_text = badge.upper()
        badge_font = get_font(28)
        bbb = draw.textbbox((0, 0), badge_text, font=badge_font)
        bw = bbb[2] - bbb[0]
        bh = bbb[3] - bbb[1]
        pad_x, pad_y = 18, 12
        chip_w = bw + pad_x * 2
        chip_h = bh + pad_y * 2 + 6
        draw.rectangle([(40, 40), (40 + chip_w, 40 + chip_h)], fill=ACCENT)
        draw.text((40 + pad_x, 40 + pad_y), badge_text, fill=TEXT, font=badge_font)

    out = Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")
    out.save(out_path, "PNG")


def draw_footer_strip(frame_path: Path, footer_text: str):
    """Open a rendered frame PNG, draw a thin lower-strip with footer_text,
    re-save in place. Persistent footer for event-ID anchoring per Lucy v1.7
    pattern. Skipped silently if footer_text is empty.

    Preserves the input mode (RGB for still frames; RGBA for video-overlay
    frames — so the overlay's transparent regions stay transparent and only
    the footer strip becomes opaque).
    """
    if not footer_text:
        return
    from PIL import Image, ImageDraw
    img = Image.open(frame_path)
    src_mode = img.mode
    img = img.convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    strip_h = 32
    draw.rectangle([(0, img.size[1] - strip_h), (img.size[0], img.size[1])],
                   fill=(0, 0, 0, 200))
    fnt = get_font(20)
    draw.text((20, img.size[1] - strip_h + 6), footer_text, fill=(220, 220, 220), font=fnt)
    out = Image.alpha_composite(img, overlay)
    # Preserve original mode — RGB for stills, RGBA for video-overlays
    if src_mode != "RGBA":
        out = out.convert(src_mode)
    out.save(frame_path, "PNG")


def render_video_overlay(caption: str, out_path: Path, badge: str = ""):
    """Transparent-bg PNG used as overlay on a video clip. Contains the bottom
    caption strip + optional top-left name-chip — same layout as the still
    support frame, but background pixels are 0-alpha so the source video shows
    through everywhere else.

    Used when a manifest support entry has `is_video: true` (Lucy v1.7
    integration: Pentagon UAP sensor clip plays as full-frame video instead of
    a Ken-Burns still).
    """
    from PIL import Image, ImageDraw
    overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    strip_h = 180
    draw.rectangle([(0, CANVAS_H - strip_h), (CANVAS_W, CANVAS_H)], fill=CAPTION_BG)

    cap_font = get_font(34)
    lines = wrap_text(caption.strip(), 60)[:3]
    y = CANVAS_H - strip_h + 22
    for line in lines:
        draw.text((40, y), line, fill=TEXT, font=cap_font)
        y += 48

    if badge:
        badge_text = badge.upper()
        badge_font = get_font(28)
        bbb = draw.textbbox((0, 0), badge_text, font=badge_font)
        bw, bh = bbb[2] - bbb[0], bbb[3] - bbb[1]
        pad_x, pad_y = 18, 12
        chip_w = bw + pad_x * 2
        chip_h = bh + pad_y * 2 + 6
        draw.rectangle([(40, 40), (40 + chip_w, 40 + chip_h)], fill=ACCENT)
        draw.text((40 + pad_x, 40 + pad_y), badge_text, fill=TEXT, font=badge_font)

    overlay.save(out_path, "PNG")


def video_clip(video_source: Path, overlay_png: Path, duration_s: float,
               clip_idx: int, out_path: Path):
    """Clip a source video to `duration_s` and composite the caption/badge
    overlay PNG over it. Output is silent h264 (audio overlaid on final concat).

    Source video is scaled to 1280×720 cover-style; overlay PNG is full canvas
    transparent except for caption strip + badge regions.
    """
    vf = (
        "[0:v]scale=1280:720:force_original_aspect_ratio=increase,"
        "crop=1280:720,setsar=1[v];"
        "[v][1:v]overlay=0:0[out]"
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video_source),
        "-i", str(overlay_png),
        "-filter_complex", vf,
        "-map", "[out]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-t", f"{duration_s:.3f}",
        "-an",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)


def render_closer_frame(text: str, hero_path: Optional[Path], out_path: Path):
    """Hero image (lightly dimmed) + share-shape closing line, centered, large."""
    from PIL import Image, ImageDraw
    if hero_path:
        base = hero_bg(hero_path, dim_alpha=90).convert("RGB")
    else:
        base = Image.new("RGB", (CANVAS_W, CANVAS_H), BG)
    draw = ImageDraw.Draw(base)
    font = get_font(56)

    lines = wrap_text(text, 35)
    line_h = 70
    total_h = len(lines) * line_h
    y = (CANVAS_H - total_h) // 2

    # Localized darken behind text block
    if hero_path:
        from PIL import Image as _Im
        text_overlay = _Im.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        text_draw = ImageDraw.Draw(text_overlay)
        pad = 28
        text_draw.rectangle(
            [(0, y - pad), (CANVAS_W, y + total_h + pad)],
            fill=(0, 0, 0, 170),
        )
        base = _Im.alpha_composite(base.convert("RGBA"), text_overlay).convert("RGB")
        draw = ImageDraw.Draw(base)

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        x = (CANVAS_W - line_w) // 2
        for dx, dy in [(-2,0),(2,0),(0,-2),(0,2)]:
            draw.text((x+dx, y+dy), line, fill=(0,0,0), font=font)
        draw.text((x, y), line, fill=TEXT, font=font)
        y += line_h

    base.save(out_path, "PNG")


def render_slate_frame(series_title: str, episode: str, date: str, out_path: Path,
                        byline: str = ""):
    """Series signature slate — 2s end card. Black bg, large brand-red wordmark,
    small episode + date subtitle, optional byline. Branding per Chi 2026-05-10.

    Layout (byline optional):
        [vertical center]
        SERIES TITLE         ← large, brand red, bold
        byline               ← smaller, white, optional (e.g. "by Echo Act IV · Sutando")
        ep. NNN · YYYY.MM.DD ← smaller, white
    """
    from PIL import Image, ImageDraw
    base = Image.new("RGB", (CANVAS_W, CANVAS_H), BG)
    draw = ImageDraw.Draw(base)

    title_font = get_font(120)
    byline_font = get_font(34)
    sub_font = get_font(36)

    title_text = series_title.upper()
    sub_text = f"ep. {episode} · {date}"

    tbb = draw.textbbox((0, 0), title_text, font=title_font)
    tw, th = tbb[2] - tbb[0], tbb[3] - tbb[1]
    sbb = draw.textbbox((0, 0), sub_text, font=sub_font)
    sw, sh = sbb[2] - sbb[0], sbb[3] - sbb[1]
    if byline:
        bbb = draw.textbbox((0, 0), byline, font=byline_font)
        bw, bh = bbb[2] - bbb[0], bbb[3] - bbb[1]
    else:
        bw = bh = 0

    gap_above_byline = 24
    gap_above_sub = 22 if byline else 30
    total_h = th + (gap_above_byline + bh if byline else 0) + gap_above_sub + sh
    y_title = (CANVAS_H - total_h) // 2

    # Title in brand red
    x_title = (CANVAS_W - tw) // 2
    draw.text((x_title, y_title), title_text, fill=ACCENT, font=title_font)

    y = y_title + th
    if byline:
        y += gap_above_byline
        x_byline = (CANVAS_W - bw) // 2
        draw.text((x_byline, y), byline, fill=TEXT, font=byline_font)
        y += bh

    y += gap_above_sub
    x_sub = (CANVAS_W - sw) // 2
    draw.text((x_sub, y), sub_text, fill=TEXT, font=sub_font)

    base.save(out_path, "PNG")


def synthesize_tts(text: str, out_path: Path, provider: str = "GEMINI",
                    gemini_voice: str = "Aoede", openai_voice: str = "sage"):
    """Render full narration to mp3. gemini-tts (free) → openai-tts fallback.

    Voice options:
      gemini_voice: Aoede (alto/neutral, default), Charon (baritone news-anchor —
        Susan/Lucy 2026-05-10 finding: matches Mini Wire's news-explainer shape
        better than Aoede), Kore (mid expressive), Puck (high conversational)
      openai_voice: sage (default), nova, alloy, etc.
    """
    repo_root = Path(__file__).resolve().parents[3]
    if provider == "GEMINI":
        gemini_script = repo_root / "skills" / "gemini-tts" / "scripts" / "synthesize.sh"
        if gemini_script.exists():
            try:
                subprocess.run(["bash", str(gemini_script), "--voice", gemini_voice,
                                 "--out", str(out_path), "--", text], check=True)
                return f"GEMINI:{gemini_voice}"
            except subprocess.CalledProcessError as e:
                print(f"  [render] gemini-tts failed (exit {e.returncode}); falling back to openai", file=sys.stderr)
    openai_script = repo_root / "skills" / "openai-tts" / "scripts" / "synthesize.sh"
    if openai_script.exists():
        subprocess.run(["bash", str(openai_script), "--voice", openai_voice,
                         "--out", str(out_path), "--", text], check=True)
        return f"OPENAI:{openai_voice}"
    raise RuntimeError("No TTS skill available")


def kenburns_clip(frame_path: Path, duration_s: float, clip_idx: int, clip_path: Path):
    """Pre-render a single still as a {duration_s} mp4 with subtle Ken-Burns zoom.

    Direction alternates per clip_idx for visual variety:
      - even idx: zoom in (1.00 → 1.08), slight drift down-right
      - odd idx:  zoom out (1.08 → 1.00), slight drift up-left

    Output is silent h264; audio is overlaid on the final concat.
    """
    total_frames = max(int(round(duration_s * FPS)), 30)
    even = clip_idx % 2 == 0

    if even:
        zoom_expr = f"min(zoom+0.0008,1.08)"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    else:
        # zoom-out: start zoomed at 1.08, end at 1.00
        zoom_expr = f"if(eq(on,0),1.08,max(zoom-0.0008,1.00))"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"

    vf = (
        f"scale=2560:1440:flags=lanczos,"
        f"zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}':"
        f"d={total_frames}:s={CANVAS_W}x{CANVAS_H}:fps={FPS}"
    )

    # CRITICAL: -t goes at the OUTPUT, not the input. Input -t with -loop 1 + zoompan
    # causes zoompan to fire per-input-frame, producing duration*fps output frames
    # PER input frame (so a 4s clip ended up 400s). With -t at output, zoompan
    # uses the d=total_frames as the motion span, and -t clips the encode to
    # exactly duration_s. Bug caught 2026-05-10 after re-run 5 sampled frames
    # showed only the HOOK across the whole 36s video.
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-loop", "1", "-i", str(frame_path),
        "-vf", vf,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-t", f"{duration_s:.3f}",
        "-an",
        str(clip_path),
    ]
    subprocess.run(cmd, check=True)


def concat_clips_with_audio(clip_paths: list, narration_path: Path, out_path: Path,
                             extra_silent_tail_s: float = 0.0):
    """Concat per-frame clips, overlay narration audio. Output duration =
    narration_dur + extra_silent_tail_s (slate gets a silent tail).

    Without extra_silent_tail_s the video clips at narration end (TTS finishes,
    clips truncate at -t). With extra_silent_tail_s we extend video by exactly
    that much past the narration, leaving the final frame (slate) visible
    in silence.
    """
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(narration_path)],
        capture_output=True, text=True, check=True,
    )
    narration_dur = float(json.loads(probe.stdout).get("format", {}).get("duration", 0) or 0)
    total_dur = narration_dur + extra_silent_tail_s

    concat_list = clip_paths[0].parent / "concat.txt"
    with open(concat_list, "w") as f:
        for cp in clip_paths:
            f.write(f"file '{cp.resolve()}'\n")

    # Pad audio with silence so AV streams stay synced through the slate.
    # apad=pad_dur=<extra> appends silence; -t clips total to total_dur.
    audio_filter = ["-af", f"apad=pad_dur={extra_silent_tail_s:.3f}"] if extra_silent_tail_s > 0 else []

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-i", str(narration_path),
        *audio_filter,
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-r", str(FPS),
        "-t", f"{total_dur:.3f}",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)


def main():
    p = argparse.ArgumentParser(description="Render make-viral-video output")
    p.add_argument("--workdir", required=True, help="state/viral-{ts}/ directory")
    p.add_argument("--tts-provider", default="GEMINI", choices=["GEMINI", "OPENAI"])
    p.add_argument("--gemini-voice", default="Aoede",
                   choices=["Aoede", "Charon", "Kore", "Puck"],
                   help="Gemini TTS voice (Charon is news-anchor baritone).")
    p.add_argument("--openai-voice", default="sage",
                   help="OpenAI TTS voice (used only if Gemini fallback path).")
    p.add_argument("--series-title", default="Mini Wire",
                   help="Branded series name shown on the end-card slate (set empty to skip slate).")
    p.add_argument("--episode", default="001", help="Episode number for slate (e.g. '001')")
    p.add_argument("--date", default=None, help="Date for slate (YYYY.MM.DD); defaults to today")
    p.add_argument("--byline", default="",
                   help="Optional byline shown between title and ep/date (e.g. 'by Echo Act IV · Sutando'). "
                        "Empty string omits the byline. Free-text — caller controls identity wording.")
    p.add_argument("--slate-duration", type=float, default=2.0, help="End-card slate duration (s)")
    p.add_argument("--footer", default="",
                   help="Optional persistent footer text drawn on every narrated frame (small lower-strip). "
                        "Free-text — e.g. 'pursue-release-01 · Apollo 17 / 1972 · wind farm / 2024'. "
                        "Anchors viewers in the event-ID across the whole video. Per Lucy v1.7 pattern. Empty = no footer.")
    args = p.parse_args()

    workdir = Path(args.workdir)
    artifacts = workdir / "artifacts"
    fetched = workdir / "fetched_assets"
    frames_dir = workdir / "frames"
    clips_dir = workdir / "clips"
    frames_dir.mkdir(parents=True, exist_ok=True)
    clips_dir.mkdir(parents=True, exist_ok=True)

    script_md = (artifacts / "final_script.md").read_text()
    sections = parse_script(script_md)
    print(f"[render] sections: {list(sections.keys())}", file=sys.stderr)

    # Per-frame asset selection from manifest (Mini Phase 3, 2026-05-10).
    # Manifest entries with purpose in {hook, support, closer} map to frames.
    # Multi-image support — fixes Lucy's "only one image across the whole video"
    # critique on re-run 5b. Fallback: hero (first real image) if no explicit
    # asset for a frame.
    manifest_path = artifacts / "asset_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    hero = find_hero_image(fetched)
    print(f"[render] hero (fallback): {hero}", file=sys.stderr)

    def asset_for(purpose: str, idx: int = 0):
        """Return (Path, manifest_entry) for the requested purpose+idx, or
        (hero, None) on fallback. Manifest entry exposes badge / alt / etc."""
        matches = [m for m in manifest if m.get("purpose") == purpose]
        entry = None
        if purpose == "support" and idx < len(matches):
            entry = matches[idx]
        elif purpose != "support" and matches:
            entry = matches[0]
        if entry:
            target = entry.get("local_file") or entry.get("url", "").split("/")[-1]
            p = fetched / target
            if p.is_file():
                return p, entry
        return hero, None

    # Per-frame durations are allocated PROPORTIONAL to that frame's narration
    # word count (Mini fix 2026-05-10 after Chi: "initial scene changed
    # prematurely before narration finished" — earlier code used fixed
    # HOOK=4 / SUPPORT=7 / CLOSER=4 then uniformly scaled to TTS length,
    # which under-allocates frames whose text is longer than average).
    durations = []  # placeholders, filled after TTS via word-share
    frame_words = []  # parallel: word count of each frame's narration
    video_source = []  # parallel: Path(mp4) if frame is video-sourced, else None
    frame_idx = 0

    if sections.get("HOOK"):
        hook_path, _ = asset_for("hook")
        render_hook_frame(sections["HOOK"], hook_path, frames_dir / f"frame_{frame_idx:03d}.png")
        durations.append(0.0)
        frame_words.append(len(sections["HOOK"].split()))
        video_source.append(None)
        frame_idx += 1

    support_text = sections.get("SUPPORT", "")
    support_facts = re.split(r"(?<=[.!?])\s+", support_text) if support_text else []
    support_facts = [f for f in support_facts if f.strip()]

    for i, fact in enumerate(support_facts):
        sup_asset, sup_entry = asset_for("support", i)
        is_video = bool(sup_entry and sup_entry.get("is_video"))
        if is_video and sup_asset:
            # Video-sourced support: render only the transparent caption/badge
            # overlay PNG; full clip is built from the source video at clip-gen time.
            badge = sup_entry.get("badge", "")
            render_video_overlay(fact, frames_dir / f"frame_{frame_idx:03d}.png", badge=badge)
            video_source.append(sup_asset)
        elif sup_asset:
            badge = (sup_entry.get("badge", "") if sup_entry else "")
            render_support_frame(sup_asset, fact, frames_dir / f"frame_{frame_idx:03d}.png",
                                  crop_seed=i, badge=badge)
            video_source.append(None)
        else:
            render_closer_frame(fact, None, frames_dir / f"frame_{frame_idx:03d}.png")
            video_source.append(None)
        durations.append(0.0)
        frame_words.append(len(fact.split()))
        frame_idx += 1

    if sections.get("CLOSER"):
        closer_path, _ = asset_for("closer")
        render_closer_frame(sections["CLOSER"], closer_path, frames_dir / f"frame_{frame_idx:03d}.png")
        durations.append(0.0)
        frame_words.append(len(sections["CLOSER"].split()))
        video_source.append(None)
        frame_idx += 1

    # Track narration-mapped frame count BEFORE adding the slate. The slate
    # is silent (extends video beyond TTS) and must NOT be scaled to fit
    # narration duration. Only durations[:narration_frame_count] get scaled.
    narration_frame_count = frame_idx

    # Apply persistent footer to all narrated frames (skipped on slate).
    # Per Lucy v1.7 pattern: small lower-strip with event-ID anchor on every
    # narrated frame. Skipped silently if --footer is empty.
    #
    # source_tag schema v1 (Mini ↔ Lucy 2026-05-10): if --footer is empty but
    # manifest entries carry `source_tag.footer_short` + `source_tag.event_id`,
    # auto-build the footer from those fields. Preserves wedge-1 (primary-source
    # citation density) without operator having to hand-craft the --footer string.
    footer_text = args.footer
    if not footer_text and manifest:
        event_ids = []
        shorts = []
        for entry in manifest:
            st = entry.get("source_tag") or {}
            if isinstance(st, dict):
                ev = st.get("event_id")
                fs = st.get("footer_short")
                if ev and ev not in event_ids:
                    event_ids.append(ev)
                if fs and fs not in shorts:
                    shorts.append(fs)
        if event_ids and shorts:
            footer_text = " · ".join(event_ids + shorts)
            print(f"[render] auto-footer from source_tag: {footer_text}", file=sys.stderr)

    # License gate (source_tag v1): warn on any manifest entry that claims
    # needs-license without supplying attribution. Non-fatal in v1 — operator
    # gets a stderr line, render continues. Can be made fatal in a later pass.
    if manifest:
        for entry in manifest:
            st = entry.get("source_tag") or {}
            if isinstance(st, dict) and st.get("license") == "needs-license" and not st.get("attribution"):
                print(f"[render] WARN license-gate: {entry.get('local_file', '?')} "
                      f"is `needs-license` with no attribution — fix before publishing",
                      file=sys.stderr)

    if footer_text:
        for i in range(narration_frame_count):
            draw_footer_strip(frames_dir / f"frame_{i:03d}.png", footer_text)
        print(f"[render] footer applied to {narration_frame_count} narrated frames", file=sys.stderr)

    # Series signature slate (Mini Wire branding per Chi 2026-05-10).
    if args.series_title:
        from datetime import datetime
        slate_date = args.date or datetime.now().strftime("%Y.%m.%d")
        render_slate_frame(args.series_title, args.episode, slate_date,
                           frames_dir / f"frame_{frame_idx:03d}.png",
                           byline=args.byline)
        durations.append(args.slate_duration)  # slate gets fixed duration, not narration-proportional
        frame_words.append(0)  # no narration on slate
        frame_idx += 1
        print(f"[render] added slate: {args.series_title} ep.{args.episode} {slate_date}", file=sys.stderr)

    full_narration = " ".join(filter(None, [sections.get("HOOK"), sections.get("SUPPORT"), sections.get("CLOSER")]))
    narration_path = workdir / "narration.mp3"
    provider_used = synthesize_tts(full_narration, narration_path,
                                   provider=args.tts_provider,
                                   gemini_voice=args.gemini_voice,
                                   openai_voice=args.openai_voice)
    print(f"[render] narration via {provider_used}", file=sys.stderr)

    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(narration_path)],
            capture_output=True, text=True, check=True,
        )
        narration_dur = float(json.loads(probe.stdout).get("format", {}).get("duration", 0))
    except Exception as e:
        print(f"[render] ffprobe on narration failed ({e}); fallback to 36s", file=sys.stderr)
        narration_dur = 36.0

    # Allocate narration_dur across narrated frames PROPORTIONAL TO WORD COUNT.
    # Each frame is on-screen for the time its text is being narrated. A frame
    # with 22 words gets ~22/total_words × narration_dur. Slate (0 words, set
    # earlier to args.slate_duration) is left unchanged.
    narrated_words_total = sum(frame_words[:narration_frame_count])
    if narrated_words_total > 0 and narration_dur > 0:
        for i in range(narration_frame_count):
            durations[i] = narration_dur * (frame_words[i] / narrated_words_total)
        print(f"[render] word-share durations: " +
              " ".join(f"{frame_words[i]}w→{durations[i]:.1f}s" for i in range(narration_frame_count)),
              file=sys.stderr)
    else:
        # Fallback if no narration: keep stub durations
        for i in range(narration_frame_count):
            durations[i] = narration_dur / max(narration_frame_count, 1)

    print(f"[render] total: {sum(durations):.1f}s ({narration_dur:.1f}s narration + {sum(durations[narration_frame_count:]):.1f}s slate)", file=sys.stderr)

    # Per-frame clip generation. Stills get Ken-Burns (zoompan); video-sourced
    # frames get the source video clipped + overlay PNG composited.
    clip_paths = []
    sorted_frames = sorted(frames_dir.glob("frame_*.png"))
    for i, (frame, dur) in enumerate(zip(sorted_frames, durations)):
        clip = clips_dir / f"clip_{i:03d}.mp4"
        if i < len(video_source) and video_source[i] is not None:
            video_clip(video_source[i], frame, dur, i, clip)
            print(f"[render] clip {i}: video-sourced ({video_source[i].name})", file=sys.stderr)
        else:
            kenburns_clip(frame, dur, i, clip)
        clip_paths.append(clip)
    print(f"[render] {len(clip_paths)} clips ({sum(1 for v in video_source if v)} video-sourced)", file=sys.stderr)

    out = workdir / "video.mp4"
    # Slate (if present) extends video duration past narration by slate_duration.
    extra_tail = args.slate_duration if args.series_title else 0.0
    concat_clips_with_audio(clip_paths, narration_path, out, extra_silent_tail_s=extra_tail)
    print(f"[render] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
