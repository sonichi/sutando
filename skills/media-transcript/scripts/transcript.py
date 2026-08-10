#!/usr/bin/env python3
"""media-transcript — pull a video's transcript (captions/subtitles) as clean text.

The doc-ingest sibling for video: any task that references a YouTube (or other
yt-dlp-supported) URL can consume the spoken content as text. Prefers uploader
subtitles, falls back to auto-generated captions; parses the VTT into deduped
plain text (auto-captions repeat rolling lines) with optional [mm:ss] markers.

Usage:
  python3 skills/media-transcript/scripts/transcript.py <url> [--lang en]
                                                        [--timestamps] [--json]

Exit codes: 0 transcript printed · 1 failure (no captions / tool error) ·
2 bad invocation · 3 handled-elsewhere pointer (local audio file → audio-transcribe).

Design notes (mirrors doc-ingest): the extractor tool (yt-dlp) is probed at
runtime — a host without it gets a clear error naming the install, never a
crash. Read-only on the network source; downloads ONLY the subtitle track
(never the media stream) into a private temp dir that is removed afterwards.
No API keys.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

_TS_LINE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2})[.,]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[.,]\d{3}")
_TAG = re.compile(r"<[^>]+>")


def _resolve_ytdlp() -> Optional[list[str]]:
    """Runtime probe for the extractor: PATH binary first, then the importable
    module. None → caller emits the actionable install error."""
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    try:
        import yt_dlp  # noqa: F401, PLC0415
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        return None


def parse_vtt(text: str, timestamps: bool = False) -> str:
    """WebVTT → clean transcript text.

    Auto-generated captions emit ROLLING windows — each cue repeats the tail of
    the previous one — so naive joining triples the text. Dedupe by dropping a
    line when it equals the previously kept line, and within a cue keep only
    lines not already seen in the previous cue. `timestamps=True` prefixes each
    kept block with its cue-start [mm:ss] (or [h:mm:ss] past an hour).
    """
    out: list[str] = []
    prev_lines: list[str] = []
    cur_ts = None
    cur_lines: list[str] = []

    def flush():
        nonlocal prev_lines
        fresh = [ln for ln in cur_lines if ln not in prev_lines]
        fresh = [ln for i, ln in enumerate(fresh) if i == 0 or ln != fresh[i - 1]]
        if fresh and (not out or fresh != [out[-1].split("] ", 1)[-1]]):
            for i, ln in enumerate(fresh):
                if out and ln == out[-1].split("] ", 1)[-1]:
                    continue
                if timestamps and i == 0 and cur_ts:
                    out.append(f"[{cur_ts}] {ln}")
                else:
                    out.append(ln)
        prev_lines = cur_lines.copy()

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        m = _TS_LINE.match(line)
        if m:
            if cur_lines:
                flush()
            cur_lines = []
            h, mnt, s = int(m.group(1)), m.group(2), m.group(3)
            cur_ts = f"{h}:{mnt}:{s}" if h else f"{mnt}:{s}"
            continue
        if re.fullmatch(r"\d+", line):  # cue sequence number
            continue
        cleaned = _TAG.sub("", line).replace("&nbsp;", " ").replace("&amp;", "&").strip()
        if cleaned:
            cur_lines.append(cleaned)
    if cur_lines:
        flush()
    return "\n".join(out)


def fetch_vtt(url: str, lang: str) -> tuple[str, str]:
    """Download ONLY the subtitle track via yt-dlp into a temp dir; return
    (vtt_text, subtitle_kind). Prefers uploader subs over auto-captions; the
    requested language over others. Raises RuntimeError with an actionable
    message on every failure path."""
    cmd_base = _resolve_ytdlp()
    if cmd_base is None:
        raise RuntimeError(
            "yt-dlp not found (binary or python module). Install: `brew install yt-dlp` "
            "or `pip install yt-dlp`.")
    with tempfile.TemporaryDirectory(prefix="media-transcript-") as td:
        # Exact language codes only — a `.*` glob here matches YouTube's dozens
        # of auto-TRANSLATED variants (en-de, en-es, …), so yt-dlp requests them
        # all and the subtitle endpoint 429s. Found in live testing (2026-07-30);
        # the hermetic stub can't see rate limits. `-orig` is the untranslated
        # auto-caption track name.
        langs = [lang, f"{lang}-orig"]
        if lang != "en":
            langs += ["en", "en-orig"]
        cmd = cmd_base + [
            "--skip-download",
            "--write-subs", "--write-auto-subs",
            "--sub-langs", ",".join(langs),
            "--sub-format", "vtt",
            "-o", str(Path(td) / "sub.%(ext)s"),
            "--no-playlist",
            # `--` terminates option parsing: even if a hostile target slips
            # past main()'s URL gate, yt-dlp can never read it as an option
            # (qingyun CR: `--exec=...` must not cross the option boundary).
            "--",
            url,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["unknown error"]
            raise RuntimeError(f"yt-dlp failed: {tail[0]}")
        vtts = sorted(Path(td).glob("*.vtt"))
        if not vtts:
            raise RuntimeError(
                "no captions available for this video (neither uploader subtitles nor "
                "auto-generated captions in the requested language)")
        # Uploader subs land as sub.<lang>.vtt; the `-orig` suffix marks the
        # untranslated AUTO-caption track. Rank the exact manual-language file
        # first — a substring test accepts sub.en-orig.vtt and lexicographic
        # sort puts it BEFORE sub.en.vtt ('-' < '.'), which is how auto-captions
        # were beating uploader subtitles when both exist (qingyun CR).
        rank = {f"sub.{lang}.vtt": 0, f"sub.{lang}-orig.vtt": 1,
                "sub.en.vtt": 2, "sub.en-orig.vtt": 3}
        preferred = sorted(vtts, key=lambda p: (rank.get(p.name, 4), p.name))[0]
        kind = "captions"
        return preferred.read_text(encoding="utf-8", errors="replace"), kind


def main(argv: list[str]) -> int:
    args = list(argv)
    as_json = "--json" in args
    if as_json:
        args.remove("--json")
    with_ts = "--timestamps" in args
    if with_ts:
        args.remove("--timestamps")
    lang = "en"
    if "--lang" in args:
        i = args.index("--lang")
        try:
            lang = args[i + 1]
        except IndexError:
            print("media-transcript: --lang needs a value", file=sys.stderr)
            return 2
        del args[i:i + 2]
    if len(args) != 1:
        print("usage: transcript.py <url> [--lang L] [--timestamps] [--json]", file=sys.stderr)
        return 2
    target = args[0]

    # Local media files are audio-transcribe's territory (whisper on the audio
    # track) — same pointer convention as doc-ingest's image/audio exits.
    p = Path(target)
    if p.exists() and p.suffix.lower() in {
            ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".mp4", ".mov", ".mkv", ".webm"}:
        msg = ("local media file — use skills/audio-transcribe on the audio track "
               "(this skill pulls CAPTIONS from video URLs)")
        if as_json:
            print(json.dumps({"url": target, "ok": False, "error": msg}))
        else:
            print(f"media-transcript: {msg}", file=sys.stderr)
        return 3

    # Untrusted task text can reach this argv (qingyun CR): only http(s) URLs
    # are valid targets, so a dash-leading string like `--exec=...` is rejected
    # here — and fetch_vtt's `--` terminator is the second wall if anything
    # option-shaped ever gets through.
    if not re.match(r"^https?://", target, re.IGNORECASE):
        msg = ("target must be an http(s) URL — refusing a non-URL argument "
               "(untrusted text must never become a yt-dlp option)")
        if as_json:
            print(json.dumps({"url": target, "ok": False, "error": msg}))
        else:
            print(f"media-transcript: {msg}", file=sys.stderr)
        return 2

    try:
        vtt, kind = fetch_vtt(target, lang)
        text = parse_vtt(vtt, timestamps=with_ts)
        if not text.strip():
            raise RuntimeError("caption track was empty after parsing")
    except Exception as exc:  # noqa: BLE001 — single reporting point, never a traceback at the CLI
        if as_json:
            print(json.dumps({"url": target, "ok": False, "error": str(exc)}))
        else:
            print(f"media-transcript: {target}: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps({"url": target, "ok": True, "kind": kind, "text": text},
                         ensure_ascii=False))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
