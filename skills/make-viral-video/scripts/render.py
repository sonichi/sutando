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


def render_support_frame(hero_path: Path, caption: str, out_path: Path, crop_seed: int = 0):
    """Hero image with crop variation + semi-transparent caption strip overlay."""
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

    out = Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")
    out.save(out_path, "PNG")


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


def render_slate_frame(series_title: str, episode: str, date: str, out_path: Path):
    """Series signature slate — 2s end card. Black bg, large brand-red wordmark,
    small episode + date subtitle. Mini Wire branding per Chi 2026-05-10.

    Layout:
        [vertical center]
        SERIES TITLE         ← large, brand red, bold
        ep. NNN · YYYY.MM.DD ← smaller, white
    """
    from PIL import Image, ImageDraw
    base = Image.new("RGB", (CANVAS_W, CANVAS_H), BG)
    draw = ImageDraw.Draw(base)

    title_font = get_font(120)
    sub_font = get_font(36)

    title_text = series_title.upper()
    sub_text = f"ep. {episode} · {date}"

    # Title bbox
    tbb = draw.textbbox((0, 0), title_text, font=title_font)
    tw = tbb[2] - tbb[0]
    th = tbb[3] - tbb[1]
    # Subtitle bbox
    sbb = draw.textbbox((0, 0), sub_text, font=sub_font)
    sw = sbb[2] - sbb[0]
    sh = sbb[3] - sbb[1]

    gap = 30
    total_h = th + gap + sh
    y_title = (CANVAS_H - total_h) // 2

    # Title in brand red
    x_title = (CANVAS_W - tw) // 2
    draw.text((x_title, y_title), title_text, fill=ACCENT, font=title_font)

    # Subtitle in white
    y_sub = y_title + th + gap
    x_sub = (CANVAS_W - sw) // 2
    draw.text((x_sub, y_sub), sub_text, fill=TEXT, font=sub_font)

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
    p.add_argument("--slate-duration", type=float, default=2.0, help="End-card slate duration (s)")
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
        matches = [m for m in manifest if m.get("purpose") == purpose]
        if purpose == "support" and idx < len(matches):
            target = matches[idx].get("local_file") or matches[idx].get("url", "").split("/")[-1]
            p = fetched / target
            if p.is_file():
                return p
        elif purpose != "support" and matches:
            target = matches[0].get("local_file") or matches[0].get("url", "").split("/")[-1]
            p = fetched / target
            if p.is_file():
                return p
        return hero

    # Per-frame durations are allocated PROPORTIONAL to that frame's narration
    # word count (Mini fix 2026-05-10 after Chi: "initial scene changed
    # prematurely before narration finished" — earlier code used fixed
    # HOOK=4 / SUPPORT=7 / CLOSER=4 then uniformly scaled to TTS length,
    # which under-allocates frames whose text is longer than average).
    durations = []  # placeholders, filled after TTS via word-share
    frame_words = []  # parallel: word count of each frame's narration
    frame_idx = 0

    if sections.get("HOOK"):
        render_hook_frame(sections["HOOK"], asset_for("hook"), frames_dir / f"frame_{frame_idx:03d}.png")
        durations.append(0.0)
        frame_words.append(len(sections["HOOK"].split()))
        frame_idx += 1

    support_text = sections.get("SUPPORT", "")
    support_facts = re.split(r"(?<=[.!?])\s+", support_text) if support_text else []
    support_facts = [f for f in support_facts if f.strip()]

    for i, fact in enumerate(support_facts):
        sup_asset = asset_for("support", i)
        if sup_asset:
            render_support_frame(sup_asset, fact, frames_dir / f"frame_{frame_idx:03d}.png", crop_seed=i)
        else:
            render_closer_frame(fact, None, frames_dir / f"frame_{frame_idx:03d}.png")
        durations.append(0.0)
        frame_words.append(len(fact.split()))
        frame_idx += 1

    if sections.get("CLOSER"):
        render_closer_frame(sections["CLOSER"], asset_for("closer"), frames_dir / f"frame_{frame_idx:03d}.png")
        durations.append(0.0)
        frame_words.append(len(sections["CLOSER"].split()))
        frame_idx += 1

    # Track narration-mapped frame count BEFORE adding the slate. The slate
    # is silent (extends video beyond TTS) and must NOT be scaled to fit
    # narration duration. Only durations[:narration_frame_count] get scaled.
    narration_frame_count = frame_idx

    # Series signature slate (Mini Wire branding per Chi 2026-05-10).
    if args.series_title:
        from datetime import datetime
        slate_date = args.date or datetime.now().strftime("%Y.%m.%d")
        render_slate_frame(args.series_title, args.episode, slate_date,
                           frames_dir / f"frame_{frame_idx:03d}.png")
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

    # Pre-render each frame as a Ken-Burns clip
    clip_paths = []
    for i, (frame, dur) in enumerate(zip(sorted(frames_dir.glob("frame_*.png")), durations)):
        clip = clips_dir / f"clip_{i:03d}.mp4"
        kenburns_clip(frame, dur, i, clip)
        clip_paths.append(clip)
    print(f"[render] {len(clip_paths)} Ken-Burns clips", file=sys.stderr)

    out = workdir / "video.mp4"
    # Slate (if present) extends video duration past narration by slate_duration.
    extra_tail = args.slate_duration if args.series_title else 0.0
    concat_clips_with_audio(clip_paths, narration_path, out, extra_silent_tail_s=extra_tail)
    print(f"[render] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
