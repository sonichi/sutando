#!/usr/bin/env python3
"""Pre-publish ffprobe gate for make-viral-video skill — phase 6.

Per Lucy's `feedback_ffprobe_video_outputs_before_posting.md`:
the v11/v12 ship-broken pattern was "looks fine to the human eye but is
missing audio / wrong orientation / cropped pictures." Structural ffprobe
checks catch all three before the video reaches the user.

Validates:
  1. File exists + non-zero size
  2. Has h264 video stream
  3. Has aac audio stream (Lucy's v11 shipped silent — no audio stream)
  4. Dimensions are 1280×720 landscape (Lucy's v10/v11 were 1080×1920 portrait)
  5. Duration within ±5s of expected (caller passes expected_seconds)
  6. Audio duration matches video duration ±0.5s (no silent tail / dropped audio)

Exit 0 + JSON report on success. Exit 1 + structured failure reason on any check fail.

Usage:
  python3 verify_video.py /path/to/video.mp4 --expected-duration 45
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

EXPECTED_W, EXPECTED_H = 1280, 720
DURATION_TOLERANCE_S = 5.0
AV_SYNC_TOLERANCE_S = 0.5


def ffprobe(path: Path):
    """Return parsed JSON from ffprobe -show_streams + -show_format."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def verify(video_path: Path, expected_duration_s: float = None):
    if not video_path.exists():
        return {"valid": False, "reason": "file_not_found", "path": str(video_path)}
    if video_path.stat().st_size == 0:
        return {"valid": False, "reason": "zero_byte_file", "path": str(video_path)}

    try:
        probe = ffprobe(video_path)
    except subprocess.CalledProcessError as e:
        return {"valid": False, "reason": f"ffprobe_failed", "stderr": e.stderr}
    except Exception as e:
        return {"valid": False, "reason": f"ffprobe_exception: {type(e).__name__}: {e}"}

    streams = probe.get("streams", [])
    fmt = probe.get("format", {})
    report = {
        "path": str(video_path),
        "duration_s": float(fmt.get("duration", 0) or 0),
        "size_bytes": int(fmt.get("size", 0) or 0),
        "streams": [],
    }

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    for s in streams:
        report["streams"].append({
            "codec_type": s.get("codec_type"),
            "codec_name": s.get("codec_name"),
            "width": s.get("width"),
            "height": s.get("height"),
            "duration": s.get("duration"),
        })

    # Check 1: has video stream
    if not video_streams:
        return {**report, "valid": False, "reason": "no_video_stream"}

    # Check 2: video codec is h264
    vstream = video_streams[0]
    if vstream.get("codec_name") != "h264":
        return {**report, "valid": False, "reason": f"video_codec_not_h264 (got {vstream.get('codec_name')})"}

    # Check 3: dimensions are 1280×720
    w, h = vstream.get("width"), vstream.get("height")
    if w != EXPECTED_W or h != EXPECTED_H:
        return {**report, "valid": False, "reason": f"wrong_dimensions ({w}x{h}, expected {EXPECTED_W}x{EXPECTED_H})"}

    # Check 4: has audio stream (catches Lucy's v11 silent regression)
    if not audio_streams:
        return {**report, "valid": False, "reason": "no_audio_stream"}

    # Check 5: audio codec is aac
    astream = audio_streams[0]
    if astream.get("codec_name") != "aac":
        return {**report, "valid": False, "reason": f"audio_codec_not_aac (got {astream.get('codec_name')})"}

    # Check 6: duration within tolerance of expected
    actual_dur = report["duration_s"]
    if expected_duration_s is not None:
        if abs(actual_dur - expected_duration_s) > DURATION_TOLERANCE_S:
            return {**report, "valid": False,
                    "reason": f"duration_outside_tolerance (got {actual_dur:.1f}s, expected {expected_duration_s:.1f}s ± {DURATION_TOLERANCE_S}s)"}

    # Check 7: audio duration ≈ video duration
    vdur = float(vstream.get("duration", actual_dur) or actual_dur)
    adur = float(astream.get("duration", actual_dur) or actual_dur)
    if abs(vdur - adur) > AV_SYNC_TOLERANCE_S:
        return {**report, "valid": False,
                "reason": f"av_duration_mismatch (video={vdur:.2f}s, audio={adur:.2f}s, tolerance ±{AV_SYNC_TOLERANCE_S}s)"}

    return {**report, "valid": True}


def main():
    p = argparse.ArgumentParser(description="Pre-publish ffprobe gate for make-viral-video")
    p.add_argument("video", help="Path to mp4 file")
    p.add_argument("--expected-duration", type=float, default=None,
                   help="Expected duration in seconds (±5s tolerance)")
    args = p.parse_args()

    result = verify(Path(args.video), expected_duration_s=args.expected_duration)
    print(json.dumps(result, indent=2))
    return 0 if result.get("valid") else 1


if __name__ == "__main__":
    sys.exit(main())
