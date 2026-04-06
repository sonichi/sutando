#!/usr/bin/env python3
"""Add subtitles to a screen recording.

Uses live transcript (from phone call narration) when available — nearly instant.
Falls back to Whisper transcription if no transcript found — takes 30-60s.
"""

import subprocess
import sys
import os
import json
import tempfile
import re
import glob
from datetime import datetime


def find_live_transcript(video_path):
    """Find the live transcript that matches a recording's time window.

    Live transcripts are written to /tmp/sutando-live-transcript-{callSid}.txt
    during phone calls. We match by timestamp proximity to the recording.
    """
    video_mtime = os.path.getmtime(video_path)
    # Recording epoch is in the filename: sutando-recording-{epoch}.mov
    match = re.search(r'recording-(\d+)', video_path)
    rec_epoch = int(match.group(1)) if match else int(video_mtime)

    candidates = []

    for tf in glob.glob('/tmp/sutando-live-transcript-CA*.txt'):
        try:
            with open(tf) as f:
                content = f.read()
            # Skip empty/header-only transcripts
            lines = [l for l in content.split('\n') if l.startswith('[')]
            if len(lines) < 2:
                continue
            narration_lines = [l for l in content.split('\n') if re.match(r'\[\d{2}:\d{2}:\d{2}\] Sutando:', l)]
            # Use both creation and modification time — recording may happen
            # mid-call, so mtime (call end) can be much later than rec_epoch
            tf_mtime = os.path.getmtime(tf)
            tf_ctime = os.stat(tf).st_birthtime  # macOS creation time
            distance = min(abs(tf_mtime - rec_epoch), abs(tf_ctime - rec_epoch))
            if distance < 600:  # within 10 min (calls can be long)
                candidates.append((tf, content, distance, len(narration_lines)))
        except Exception:
            continue

    if not candidates:
        return None

    # Prefer: most narration lines first, then closest timestamp
    # The transcript with the most narration during a recording is usually the right one
    candidates.sort(key=lambda c: (-c[3], c[2]))
    best = candidates[0]

    return (best[0], best[1])


def transcript_to_srt(transcript_content, video_duration_s):
    """Convert live transcript lines to SRT format.

    Only includes Sutando's screen narration lines — descriptions of what's
    on screen. Filters out conversational responses, tool acknowledgments, etc.
    Timestamps are relative to the recording start time.
    """
    lines = []
    for line in transcript_content.split('\n'):
        m = re.match(r'\[(\d{2}:\d{2}:\d{2})\] Sutando: (.+)', line)
        if m:
            time_str, text = m.group(1), m.group(2)
            # Only include screen descriptions (narration)
            # These typically start with "The screen shows/displays" or describe visual content
            is_narration = any(phrase in text.lower() for phrase in [
                'the screen show', 'the screen display', 'the screen has',
                'the page show', 'the page display',
                'shows a', 'displays a', 'showing',
                'the heading', 'the title',
                'under the heading', 'with the heading',
                'two ai-generated', 'two images',
            ])
            if not is_narration:
                continue
            lines.append((time_str, text))

    if not lines:
        return None

    # Calculate relative timestamps from first narration line
    def time_to_seconds(t):
        h, m, s = map(int, t.split(':'))
        return h * 3600 + m * 60 + s

    # Use first narration line as base (recording may have started slightly before)
    base_time = time_to_seconds(lines[0][0])
    # If lines are too close together, space them out evenly across the video
    total_span = time_to_seconds(lines[-1][0]) - base_time if len(lines) > 1 else 0
    if total_span < 3 and len(lines) > 1:
        # Timestamps are compressed — space evenly across video duration
        interval = video_duration_s / len(lines)
        spaced_lines = []
        for i, (_, text) in enumerate(lines):
            fake_time = i * interval
            spaced_lines.append((fake_time, text))
        lines_with_times = spaced_lines
    else:
        lines_with_times = [(time_to_seconds(t) - base_time, text) for t, text in lines]
    srt_entries = []

    for i, (start_s, text) in enumerate(lines_with_times):
        # End time: next line's start or start + estimated duration
        if i + 1 < len(lines_with_times):
            end_s = lines_with_times[i + 1][0]
        else:
            # Last line: estimate 4 seconds or until video end
            end_s = min(start_s + 4, video_duration_s)

        # Clamp to video duration
        start_s = max(0, min(start_s, video_duration_s))
        end_s = max(start_s + 0.5, min(end_s, video_duration_s))

        def fmt(s):
            h = int(s // 3600)
            m = int((s % 3600) // 60)
            sec = int(s % 60)
            ms = int((s % 1) * 1000)
            return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"

        srt_entries.append(f"{i+1}\n{fmt(start_s)} --> {fmt(end_s)}\n{text}\n")

    return '\n'.join(srt_entries)


def get_video_duration(video_path):
    """Get video duration in seconds using ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=10
        )
        return float(result.stdout.strip())
    except Exception:
        return 30.0  # default


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

    # Vocabulary hint: primes Whisper with project-specific terms to improve accuracy.
    # Whisper uses initial_prompt as context — spelling these correctly reduces misrecognitions.
    VOCAB_HINT = (
        "Sutando, sonichi, MassGen, Cherry Blossoms, SVG, GitHub, "
        "Susan Liu, Claude Code, Gemini, Zoom, screen share, "
        "narration, recording, subtitle, describe screen"
    )

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", wav_path],
            capture_output=True, timeout=30
        )

        # Run whisper with word-level timestamps, output SRT
        srt_dir = tempfile.mkdtemp()
        subprocess.run(
            ["whisper", wav_path, "--model", "base", "--output_format", "srt", "--output_dir", srt_dir,
             "--language", "en", "--word_timestamps", "True",
             "--initial_prompt", VOCAB_HINT],
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

    srt_content = None
    srt_path = None

    # Try live transcript first (instant, perfect accuracy)
    transcript = find_live_transcript(video_path)
    if transcript:
        tf_path, tf_content = transcript
        duration = get_video_duration(video_path)
        srt_content = transcript_to_srt(tf_content, duration)
        if srt_content:
            srt_path = tempfile.mktemp(suffix=".srt")
            with open(srt_path, 'w') as f:
                f.write(srt_content)
            print(f"Using live transcript ({tf_path}) — skipping Whisper", file=sys.stderr)

    # Fall back to Whisper if no transcript available
    if not srt_content:
        print(f"Transcribing {video_path} with Whisper...", file=sys.stderr)
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
