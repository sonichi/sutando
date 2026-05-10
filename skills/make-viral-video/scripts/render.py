#!/usr/bin/env python3
"""Frame composition + ffmpeg encode for make-viral-video skill — phase 5.

Reads:
  - {workdir}/artifacts/final_script.md   (HOOK / SUPPORT / CLOSER sections)
  - {workdir}/artifacts/asset_manifest.json
  - {workdir}/fetched_assets/*.png

Produces:
  - {workdir}/frames/                     (PIL-rendered frames)
  - {workdir}/narration.mp3               (TTS, gemini default → openai fallback)
  - {workdir}/video.mp4                   (h264 + aac, 1280×720)

Visual rules per SKILL.md:
  - HOOK card (3s): single bold claim, ≥120pt, brand-color background
  - SUPPORT cards (per-fact, ~5-8s each): real fetched image fills frame +
    semi-transparent caption strip with one attributable fact
  - CLOSER card (3-5s): share-moment text on solid background

ffmpeg encoder concatenates frames at fixed FPS, overlays narration audio.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

CANVAS_W, CANVAS_H = 1280, 720
FPS = 30
HOOK_DURATION_S = 3.0
SUPPORT_DURATION_S = 6.0
CLOSER_DURATION_S = 4.0

# Brand colors (kept simple — black + accent)
BG = (10, 10, 14)        # near-black
ACCENT = (220, 56, 76)   # red-ish for hook badge
TEXT = (240, 240, 240)
CAPTION_BG = (0, 0, 0, 180)  # semi-transparent black for caption strips


def parse_script(script_md: str):
    """Parse final_script.md into HOOK/SUPPORT/CLOSER sections.

    Accepts two shapes:
      a) Section headers like "## HOOK" or "**Hook:**"
      b) Single paragraph (Lucy's v12 shape) — fall back to splitting on sentences
    """
    sections = {"HOOK": [], "SUPPORT": [], "CLOSER": []}

    # Shape (a): explicit section headers
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

    # Shape (b) fallback: split paragraph into hook=first-sentence, closer=last,
    # support=middle. Best-effort for back-compat with Lucy's v12 output.
    text = script_md.strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) >= 3:
        return {
            "HOOK": sentences[0],
            "SUPPORT": " ".join(sentences[1:-1]),
            "CLOSER": sentences[-1],
        }
    return {"HOOK": text, "SUPPORT": "", "CLOSER": ""}


def render_hook_frame(text: str, out_path: Path):
    """One frame: bold claim centered on dark bg with accent corner badge."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), BG)
    draw = ImageDraw.Draw(img)

    # Try to find a system font; fall back gracefully
    font_paths = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    font = None
    size = 64
    for fp in font_paths:
        if Path(fp).exists():
            try:
                font = ImageFont.truetype(fp, size)
                break
            except Exception:
                pass
    if font is None:
        font = ImageFont.load_default()

    # Word-wrap the hook text
    words = text.split()
    lines, current = [], ""
    max_chars_per_line = 30
    for w in words:
        if len(current) + len(w) + 1 <= max_chars_per_line:
            current = (current + " " + w).strip()
        else:
            lines.append(current)
            current = w
    if current:
        lines.append(current)

    line_h = size + 10
    total_h = len(lines) * line_h
    y = (CANVAS_H - total_h) // 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        x = (CANVAS_W - line_w) // 2
        draw.text((x, y), line, fill=TEXT, font=font)
        y += line_h

    # Accent badge top-left
    draw.rectangle([(40, 40), (160, 90)], fill=ACCENT)
    badge_font = ImageFont.truetype(font_paths[0], 24) if font_paths and Path(font_paths[0]).exists() else font
    draw.text((58, 52), "BREAKING", fill=TEXT, font=badge_font)

    img.save(out_path, "PNG")


def render_support_frame(image_path: Path, caption: str, out_path: Path):
    """Real fetched image fills frame; semi-transparent caption strip overlays bottom."""
    from PIL import Image, ImageDraw, ImageFont
    bg = Image.open(image_path).convert("RGB")
    bg = bg.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)

    # Caption strip
    overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    strip_h = 140
    draw.rectangle([(0, CANVAS_H - strip_h), (CANVAS_W, CANVAS_H)], fill=CAPTION_BG)

    font_path = "/System/Library/Fonts/Helvetica.ttc"
    try:
        cap_font = ImageFont.truetype(font_path, 36)
    except Exception:
        cap_font = ImageFont.load_default()

    # Wrap caption to ≤80 chars/line, ≤2 lines
    words = caption.split()
    lines, current = [], ""
    for w in words:
        if len(current) + len(w) + 1 <= 80:
            current = (current + " " + w).strip()
        else:
            lines.append(current)
            current = w
    if current:
        lines.append(current)
    lines = lines[:2]

    y = CANVAS_H - strip_h + 20
    for line in lines:
        draw.text((40, y), line, fill=TEXT, font=cap_font)
        y += 50

    out = Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")
    out.save(out_path, "PNG")


def render_closer_frame(text: str, out_path: Path):
    """Closer = share-moment text on solid bg, slightly larger than support captions."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), BG)
    draw = ImageDraw.Draw(img)
    font_path = "/System/Library/Fonts/Helvetica.ttc"
    try:
        font = ImageFont.truetype(font_path, 56)
    except Exception:
        font = ImageFont.load_default()

    words = text.split()
    lines, current = [], ""
    for w in words:
        if len(current) + len(w) + 1 <= 35:
            current = (current + " " + w).strip()
        else:
            lines.append(current)
            current = w
    if current:
        lines.append(current)

    line_h = 70
    total_h = len(lines) * line_h
    y = (CANVAS_H - total_h) // 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        x = (CANVAS_W - line_w) // 2
        draw.text((x, y), line, fill=TEXT, font=font)
        y += line_h

    img.save(out_path, "PNG")


def synthesize_tts(text: str, out_path: Path, provider: str = "GEMINI"):
    """Render full narration to mp3. Tries gemini-tts (free) → openai-tts (paid)."""
    repo_root = Path(__file__).resolve().parents[3]
    if provider == "GEMINI":
        gemini_script = repo_root / "skills" / "gemini-tts" / "scripts" / "synthesize.sh"
        if gemini_script.exists():
            try:
                subprocess.run(["bash", str(gemini_script), "--out", str(out_path), "--", text], check=True)
                return "GEMINI"
            except subprocess.CalledProcessError as e:
                print(f"  [render] gemini-tts failed (exit {e.returncode}); falling back to openai", file=sys.stderr)
    # OpenAI fallback
    openai_script = repo_root / "skills" / "openai-tts" / "scripts" / "synthesize.sh"
    if openai_script.exists():
        subprocess.run(["bash", str(openai_script), "--voice", "sage", "--out", str(out_path), "--", text], check=True)
        return "OPENAI"
    raise RuntimeError("No TTS skill available")


def encode_video(frames_dir: Path, narration_path: Path, durations_s: list, out_path: Path):
    """Concat frames at fixed durations, overlay narration audio. Uses ffmpeg concat demuxer."""
    # Build concat list file
    concat_list = frames_dir / "concat.txt"
    with open(concat_list, "w") as f:
        for frame, dur in zip(sorted(frames_dir.glob("frame_*.png")), durations_s):
            f.write(f"file '{frame.resolve()}'\n")
            f.write(f"duration {dur}\n")
        # ffmpeg concat demuxer requires the last file repeated without duration
        last = sorted(frames_dir.glob("frame_*.png"))[-1]
        f.write(f"file '{last.resolve()}'\n")

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-i", str(narration_path),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-r", str(FPS),
        "-shortest",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)


def main():
    p = argparse.ArgumentParser(description="Render make-viral-video output")
    p.add_argument("--workdir", required=True, help="state/viral-{ts}/ directory")
    p.add_argument("--tts-provider", default="GEMINI", choices=["GEMINI", "OPENAI"])
    args = p.parse_args()

    workdir = Path(args.workdir)
    artifacts = workdir / "artifacts"
    fetched = workdir / "fetched_assets"
    frames_dir = workdir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    script_md = (artifacts / "final_script.md").read_text()
    sections = parse_script(script_md)
    print(f"[render] sections: {list(sections.keys())}", file=sys.stderr)

    manifest_path = artifacts / "asset_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else []

    # Frame plan
    durations = []
    frame_idx = 0

    # HOOK
    if sections.get("HOOK"):
        render_hook_frame(sections["HOOK"], frames_dir / f"frame_{frame_idx:03d}.png")
        durations.append(HOOK_DURATION_S)
        frame_idx += 1

    # SUPPORT — one frame per asset in manifest, captioned with the support text
    support_text = sections.get("SUPPORT", "")
    support_facts = re.split(r"(?<=[.!?])\s+", support_text) if support_text else []
    support_assets = [m for m in manifest if m.get("purpose") == "support"]
    if not support_assets:
        # Fallback: use any non-hook/closer assets
        support_assets = [m for m in manifest if m.get("purpose") not in ("hook", "closer")]

    for i, fact in enumerate(support_facts[:len(support_assets) or 1]):
        if i < len(support_assets):
            asset_url = support_assets[i].get("url", "")
            # Map URL to local file by basename matching fetched_assets/
            local_assets = list(fetched.glob("*"))
            best = None
            for la in local_assets:
                if la.name in asset_url or asset_url.endswith(la.name):
                    best = la
                    break
            if best is None and local_assets:
                best = local_assets[i % len(local_assets)]
            if best:
                render_support_frame(best, fact, frames_dir / f"frame_{frame_idx:03d}.png")
                durations.append(SUPPORT_DURATION_S)
                frame_idx += 1

    # CLOSER
    if sections.get("CLOSER"):
        render_closer_frame(sections["CLOSER"], frames_dir / f"frame_{frame_idx:03d}.png")
        durations.append(CLOSER_DURATION_S)
        frame_idx += 1

    print(f"[render] {frame_idx} frames, total duration {sum(durations):.1f}s", file=sys.stderr)

    # TTS the full script (HOOK + SUPPORT + CLOSER concatenated)
    full_narration = " ".join(filter(None, [sections.get("HOOK"), sections.get("SUPPORT"), sections.get("CLOSER")]))
    narration_path = workdir / "narration.mp3"
    provider_used = synthesize_tts(full_narration, narration_path, provider=args.tts_provider)
    print(f"[render] narration via {provider_used}", file=sys.stderr)

    # Encode
    out = workdir / "video.mp4"
    encode_video(frames_dir, narration_path, durations, out)
    print(f"[render] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
