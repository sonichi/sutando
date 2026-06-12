#!/usr/bin/env python3
"""Upload a video to YouTube via the Data API v3.

Reads the cached token from ~/.config/sutando/youtube-token.json (written by
auth.py on first run). Automatically refreshes the access_token if expired
— refresh_token is long-lived, so cron-driven runs don't need user interaction.

Usage:
    python3 upload.py <video-path> --title "My title" [--description "..."]
                                   [--privacy unlisted|private|public]
                                   [--tags tag1,tag2]
                                   [--category-id 22]

Defaults: privacy=unlisted, category=22 (People & Blogs).

Returns the YouTube video URL on stdout on success, exits 0.
Exits 1 with diagnostic if upload fails.

Quota cost: ~1600 units per upload (free tier is 10,000/day → ~6 uploads/day).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

TOKEN_PATH = Path.home() / ".config" / "sutando" / "youtube-token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def load_credentials() -> Credentials:
    if not TOKEN_PATH.exists():
        print(
            f"ERROR: no token at {TOKEN_PATH}. Run auth.py first:\n"
            f"  python3 scripts/auth.py <client-secret.json>",
            file=sys.stderr,
        )
        sys.exit(1)
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    # Refresh if needed — Google's access tokens live ~1 hour, refresh tokens
    # are durable until revoked, so this is the silent-on-cron-rerun path.
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
    return creds


def upload(video_path: Path, title: str, description: str, privacy: str, tags: list[str], category_id: str) -> str:
    creds = load_credentials()
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/*")

    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"  uploading: {int(status.progress() * 100)}%", file=sys.stderr)
    return response["id"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video_path", type=Path)
    ap.add_argument("--title", required=True)
    ap.add_argument("--description", default="")
    ap.add_argument("--privacy", choices=("unlisted", "private", "public"), default="unlisted")
    ap.add_argument("--tags", default="", help="comma-separated")
    ap.add_argument("--category-id", default="22", help="default 22 = People & Blogs")
    args = ap.parse_args()

    if not args.video_path.exists():
        print(f"ERROR: video not found: {args.video_path}", file=sys.stderr)
        return 1

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]

    try:
        video_id = upload(args.video_path, args.title, args.description, args.privacy, tags, args.category_id)
    except HttpError as e:
        print(f"ERROR: YouTube API: {e}", file=sys.stderr)
        return 1

    print(f"https://www.youtube.com/watch?v={video_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
