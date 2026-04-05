#!/usr/bin/env python3
"""Post to X (Twitter) using API v2 with OAuth 1.0a.

Usage:
  python3 src/x-post.py "Tweet text here"
  python3 src/x-post.py "Tweet with media" --media /path/to/image.png
  python3 src/x-post.py "Tweet with video" --media /path/to/video.mp4
  python3 src/x-post.py --reply-to 123456789 "Reply text"

Requires in .env:
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import requests
    from requests_oauthlib import OAuth1
except ImportError:
    print("Installing required packages...")
    os.system("pip3 install requests requests-oauthlib")
    import requests
    from requests_oauthlib import OAuth1

# Load .env
ENV_FILE = Path(__file__).parent.parent / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

API_KEY = os.environ.get("X_API_KEY", "")
API_SECRET = os.environ.get("X_API_SECRET", "")
ACCESS_TOKEN = os.environ.get("X_ACCESS_TOKEN", "")
ACCESS_TOKEN_SECRET = os.environ.get("X_ACCESS_TOKEN_SECRET", "")

UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"
TWEET_URL = "https://api.twitter.com/2/tweets"


def get_auth():
    if not all([API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        print("Error: X API credentials not set in .env")
        print("Need: X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET")
        sys.exit(1)
    return OAuth1(API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET)


def upload_media(filepath, auth):
    """Upload media using chunked upload (required for video)."""
    filepath = Path(filepath)
    if not filepath.exists():
        print(f"Error: File not found: {filepath}")
        sys.exit(1)

    file_size = filepath.stat().st_size
    mime = "video/mp4" if filepath.suffix.lower() in (".mp4", ".mov") else \
           "image/png" if filepath.suffix.lower() == ".png" else \
           "image/jpeg" if filepath.suffix.lower() in (".jpg", ".jpeg") else \
           "image/gif" if filepath.suffix.lower() == ".gif" else \
           "application/octet-stream"

    is_video = mime.startswith("video")
    media_category = "tweet_video" if is_video else "tweet_image"

    print(f"Uploading {filepath.name} ({file_size / 1024 / 1024:.1f}MB, {mime})...")

    # INIT
    resp = requests.post(UPLOAD_URL, auth=auth, data={
        "command": "INIT",
        "total_bytes": file_size,
        "media_type": mime,
        "media_category": media_category,
    })
    resp.raise_for_status()
    media_id = resp.json()["media_id_string"]
    print(f"  INIT: media_id={media_id}")

    # APPEND (chunked, 5MB chunks)
    CHUNK_SIZE = 5 * 1024 * 1024
    with open(filepath, "rb") as f:
        segment = 0
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            resp = requests.post(UPLOAD_URL, auth=auth,
                data={"command": "APPEND", "media_id": media_id, "segment_index": segment},
                files={"media": chunk})
            resp.raise_for_status()
            segment += 1
            print(f"  APPEND segment {segment} ({len(chunk) / 1024 / 1024:.1f}MB)")

    # FINALIZE
    resp = requests.post(UPLOAD_URL, auth=auth, data={
        "command": "FINALIZE",
        "media_id": media_id,
    })
    resp.raise_for_status()
    result = resp.json()
    print(f"  FINALIZE: {result.get('processing_info', 'done')}")

    # Poll for processing (videos need this)
    if "processing_info" in result:
        while True:
            info = result.get("processing_info", {})
            state = info.get("state", "")
            if state == "succeeded":
                print("  Processing complete!")
                break
            elif state == "failed":
                print(f"  Processing FAILED: {info.get('error', {}).get('message', 'unknown')}")
                sys.exit(1)
            wait = info.get("check_after_secs", 5)
            print(f"  Processing ({state})... waiting {wait}s")
            time.sleep(wait)
            resp = requests.get(UPLOAD_URL, auth=auth, params={
                "command": "STATUS",
                "media_id": media_id,
            })
            resp.raise_for_status()
            result = resp.json()

    return media_id


def post_tweet(text, media_id=None, reply_to=None, auth=None):
    """Post a tweet via API v2."""
    payload = {"text": text}
    if media_id:
        payload["media"] = {"media_ids": [media_id]}
    if reply_to:
        payload["reply"] = {"in_reply_to_tweet_id": reply_to}

    resp = requests.post(TWEET_URL, auth=auth, json=payload)
    if resp.status_code == 201:
        data = resp.json()["data"]
        tweet_id = data["id"]
        print(f"Posted! https://x.com/i/status/{tweet_id}")
        return tweet_id
    else:
        print(f"Error {resp.status_code}: {resp.text}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Post to X")
    parser.add_argument("text", help="Tweet text")
    parser.add_argument("--media", help="Path to media file (image or video)")
    parser.add_argument("--reply-to", help="Tweet ID to reply to")
    args = parser.parse_args()

    auth = get_auth()

    media_id = None
    if args.media:
        media_id = upload_media(args.media, auth)

    post_tweet(args.text, media_id=media_id, reply_to=args.reply_to, auth=auth)


if __name__ == "__main__":
    main()
