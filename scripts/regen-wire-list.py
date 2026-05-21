#!/usr/bin/env python3
"""Regenerate the Sutando WIRE list in README.md from the YouTube playlist.

The README has a `<!-- wire-list:start -->` / `<!-- wire-list:end -->` marker
pair around the WIRE episode list. This script fetches the playlist via
YouTube Data API v3, ranks videos by recency + likes, dedups so a video
appears in at most one slot, and substitutes the rendered list back into
README atomically.

Slots produced (5 total):
- 2 newest episodes (by `publishedAt`, descending)
- 2 hero episodes (highest `likeCount`, excluding videos already in newest,
  and >= 30 days old to avoid rotation-lag double-listing)
- 1 "Earlier episodes →" playlist link

If fewer than 4 unique videos qualify (e.g. very early channel state),
the script falls back gracefully — emits whatever slots it can fill plus
the playlist link.

Env:
- YOUTUBE_API_KEY (required) — Google Cloud Console API key with YouTube
  Data API v3 enabled. No OAuth needed; public-data read scope only.
- PLAYLIST_ID (optional) — defaults to the canonical Sutando WIRE playlist.

Usage:
    python3 scripts/regen-wire-list.py            # rewrite README in place
    python3 scripts/regen-wire-list.py --dry-run  # print rendered block, no write
    python3 scripts/regen-wire-list.py --check    # exit non-zero if README differs

Exit codes:
    0 — README unchanged (or write succeeded)
    1 — would change (check mode) OR API/IO error
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"
PLAYLIST_ID = os.environ.get(
    "PLAYLIST_ID", "PLoEaHbP1bU5FDWAyeLDL9J9i7Iblp3_m_"
)
PLAYLIST_URL = f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"

START_MARKER = "<!-- wire-list:start"  # prefix match; full line may include note
END_MARKER = "<!-- wire-list:end -->"

NEWEST_SLOTS = 2
HERO_SLOTS = 2
HERO_MIN_AGE_DAYS = 30
API_BASE = "https://www.googleapis.com/youtube/v3"


def api_get(path: str, params: dict) -> dict:
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        raise RuntimeError("YOUTUBE_API_KEY not set")
    params = {**params, "key": api_key}
    url = f"{API_BASE}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        return json.load(resp)


def fetch_playlist_videos(playlist_id: str) -> list[dict]:
    """Return all playlist items as {videoId, title, publishedAt}."""
    items = []
    page_token = None
    while True:
        params = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token
        data = api_get("playlistItems", params)
        for item in data.get("items", []):
            video_id = item["contentDetails"]["videoId"]
            published = item["contentDetails"].get(
                "videoPublishedAt"
            ) or item["snippet"].get("publishedAt")
            items.append(
                {
                    "videoId": video_id,
                    "title": item["snippet"]["title"],
                    "publishedAt": published,
                }
            )
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return items


def fetch_video_stats(video_ids: list[str]) -> dict[str, dict]:
    """Return {video_id: {likeCount, viewCount}}. Batches by 50."""
    stats = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        data = api_get(
            "videos",
            {"part": "statistics", "id": ",".join(batch)},
        )
        for v in data.get("items", []):
            s = v.get("statistics", {})
            stats[v["id"]] = {
                "likeCount": int(s.get("likeCount", 0)),
                "viewCount": int(s.get("viewCount", 0)),
            }
    return stats


def age_days(published_at: str) -> int:
    dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - dt).days


def render_block(videos: list[dict]) -> str:
    """Build the markdown block — newest N, hero N, playlist link."""
    by_date = sorted(videos, key=lambda v: v["publishedAt"], reverse=True)
    newest = by_date[:NEWEST_SLOTS]
    newest_ids = {v["videoId"] for v in newest}

    hero_candidates = [
        v
        for v in videos
        if v["videoId"] not in newest_ids
        and age_days(v["publishedAt"]) >= HERO_MIN_AGE_DAYS
    ]
    hero_candidates.sort(key=lambda v: v.get("likeCount", 0), reverse=True)
    hero = hero_candidates[:HERO_SLOTS]

    lines = []
    for v in newest + hero:
        lines.append(
            f'- [{v["title"]}](https://youtu.be/{v["videoId"]})'
        )
    lines.append(f"- [Earlier episodes →]({PLAYLIST_URL})")
    return "\n".join(lines)


def splice(readme_text: str, new_block: str) -> str:
    """Replace content between markers atomically."""
    start_idx = readme_text.find(START_MARKER)
    if start_idx < 0:
        raise RuntimeError(f"START_MARKER not found: {START_MARKER}")
    start_line_end = readme_text.find("\n", start_idx)
    end_idx = readme_text.find(END_MARKER, start_line_end)
    if end_idx < 0:
        raise RuntimeError(f"END_MARKER not found: {END_MARKER}")
    return (
        readme_text[: start_line_end + 1]
        + new_block
        + "\n"
        + readme_text[end_idx:]
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print, don't write")
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if README would change",
    )
    args = ap.parse_args()

    items = fetch_playlist_videos(PLAYLIST_ID)
    if not items:
        print("ERR: playlist returned no items", file=sys.stderr)
        sys.exit(1)
    stats = fetch_video_stats([v["videoId"] for v in items])
    for v in items:
        v.update(stats.get(v["videoId"], {"likeCount": 0, "viewCount": 0}))

    new_block = render_block(items)

    if args.dry_run:
        print(new_block)
        return

    current = README.read_text()
    new_readme = splice(current, new_block)
    if new_readme == current:
        print("README unchanged")
        return
    if args.check:
        print("README would change; run without --check to apply")
        sys.exit(1)
    README.write_text(new_readme)
    print(f"README updated ({len(items)} videos considered)")


if __name__ == "__main__":
    main()
