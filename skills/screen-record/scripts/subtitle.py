#!/usr/bin/env python3
"""Add subtitles to a screen recording by transcribing its audio with Whisper."""

import subprocess
import sys
import os
import json
import tempfile

def find_latest_recording():
    """Find the most recent sutando recording."""
    result = subprocess.run(
        ["bash", "-c", "ls -t /tmp/sutando-recording-*.mov 2>/dev/null | grep -v subtitled | head -1"],
        capture_output=True, text=True, timeout=3
    )
    path = result.stdout.strip()
    if path and os.path.exists(path):
        return path
    return None

def transcribe(video_path):
    """Transcribe audio from video using Whisper with word timestamps."""
    # Extract audio to temp wav
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", wav_path],
            capture_output=True, timeout=30
        )

        # Run whisper with word-level timestamps, output SRT
        srt_dir = tempfile.mkdtemp()
        subprocess.run(
            ["whisper", wav_path, "--model", "base", "--output_format", "srt", "--output_dir", srt_dir,
             "--language", "en", "--word_timestamps", "True"],
            capture_output=True, timeout=120
        )

        srt_path = os.path.join(srt_dir, os.path.basename(wav_path).replace(".wav", ".srt"))
        if os.path.exists(srt_path):
            with open(srt_path) as f:
                return f.read(), srt_path
        return None, None
    finally:
        if os.path.exists(wav_path):
            os.unlink(wav_path)

def burn_subtitles(video_path, srt_path):
    """Burn SRT subtitles into video with ffmpeg."""
    out_path = video_path.replace(".mov", "-subtitled.mov")
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", video_path,
            "-vf", f"subtitles={srt_path}:force_style='FontSize=24,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,MarginV=30'",
            "-c:v", "libx264", "-crf", "23", "-c:a", "aac", out_path
        ],
        capture_output=True, timeout=120
    )

    if os.path.exists(out_path):
        size_mb = round(os.path.getsize(out_path) / 1024 / 1024, 1)
        return out_path, size_mb
    return None, 0

def main():
    video_path = sys.argv[1] if len(sys.argv) > 1 else find_latest_recording()
    if not video_path:
        print(json.dumps({"error": "No recording found"}))
        return

    print(f"Transcribing {video_path}...", file=sys.stderr)
    srt_content, srt_path = transcribe(video_path)
    if not srt_content:
        print(json.dumps({"error": "Transcription failed — no speech detected or Whisper error"}))
        return

    print(f"Burning subtitles...", file=sys.stderr)
    out_path, size_mb = burn_subtitles(video_path, srt_path)
    if not out_path:
        print(json.dumps({"error": "ffmpeg subtitle burn failed"}))
        return

    print(json.dumps({"status": "done", "path": out_path, "size_mb": size_mb, "srt": srt_path}))

if __name__ == "__main__":
    main()
