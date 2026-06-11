#!/usr/bin/env python3
"""Transcribe an audio file via Gemini 2.5-flash.

Usage:
    python3 transcribe.py <audio_file_path>

Exits 0 and prints the transcript to stdout on success.
Exits 1 (no output) on any failure — missing key, unsupported format, API error.
Fail-open by design: callers must treat a non-zero exit as "no transcript available"
and fall back to passing the file path through unchanged.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

_AUDIO_MIME: dict[str, str] = {
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
}


def _claude_config() -> Path:
    """CCD-resolved config dir (PR #1525 pattern) — never hardcode ~/.claude."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))

def _api_key() -> str:
    """Resolve GEMINI_API_KEY from env, then workspace .env, then bridge .envs."""
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    # Walk candidate .env files: workspace root, then common bridge credential dirs.
    workspace = os.environ.get("SUTANDO_WORKSPACE", "")
    if not workspace:
        workspace = str(Path.home() / ".sutando" / "workspace")
    candidates = [
        Path(workspace) / ".env",
        _claude_config() / "channels" / "slack" / ".env",
        _claude_config() / "channels" / "discord" / ".env",
        _claude_config() / "channels" / "telegram" / ".env",
    ]
    for env_path in candidates:
        try:
            for line in env_path.read_text().splitlines():
                if line.startswith(("GEMINI_API_KEY=", "GOOGLE_API_KEY=")):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def transcribe(file_path: str) -> str | None:
    """Return transcript string or None on any failure."""
    ext = Path(file_path).suffix.lower()
    mime = _AUDIO_MIME.get(ext)
    if not mime:
        return None

    key = _api_key()
    if not key:
        print(f"[audio-transcribe] no GEMINI_API_KEY found; skipping {file_path}", file=sys.stderr)
        return None

    try:
        audio_b64 = base64.b64encode(Path(file_path).read_bytes()).decode()
    except OSError as e:
        print(f"[audio-transcribe] cannot read {file_path}: {e}", file=sys.stderr)
        return None

    body = {
        "contents": [{"parts": [
            {"text": "Transcribe this voice note verbatim. "
                     "Output only the transcription text, with no preamble or commentary."},
            {"inline_data": {"mime_type": mime, "data": audio_b64}},
        ]}]
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={key}"
    )
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        # 20s cap: a short voice note transcribes in ~2-5s; 20s is ample while
        # bounding worst-case latency. On timeout we return None so the caller
        # falls back to the plain file-path attachment.
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read())
        text = payload["candidates"][0]["content"]["parts"][0]["text"].strip()
        return text or None
    except Exception as e:
        print(f"[audio-transcribe] API error for {Path(file_path).name}: {e}", file=sys.stderr)
        return None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: transcribe.py <audio_file_path>", file=sys.stderr)
        sys.exit(1)
    result = transcribe(sys.argv[1])
    if result:
        print(result)
        sys.exit(0)
    sys.exit(1)
