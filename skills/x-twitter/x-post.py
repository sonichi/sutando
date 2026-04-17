#!/usr/bin/env python3
"""X (Twitter) CLI — post, search, read, and check engagement.

Usage:
  python3 src/x-post.py post "Tweet text here"
  python3 src/x-post.py post "Tweet with media" --media /path/to/video.mp4
  python3 src/x-post.py post --reply-to 123456789 "Reply text"
  python3 src/x-post.py search "sutando agent"
  python3 src/x-post.py read 123456789                    # read a tweet
  python3 src/x-post.py mentions                          # recent mentions
  python3 src/x-post.py timeline                          # your recent tweets
  python3 src/x-post.py engagement 123456789              # likes/retweets/views

Requires in .env:
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# `requests` + `requests_oauthlib` are only needed for write / user-context
# commands (post, mentions, timeline). Read-only commands (search, read) work
# with just X_BEARER_TOKEN over stdlib urllib. Keep the import lazy so a
# bearer-only environment (no OAuth1 creds, no `pip install` permission)
# can still run search/read.
requests = None
OAuth1 = None
def _require_requests():
    global requests, OAuth1
    if requests is not None and OAuth1 is not None:
        return
    try:
        import requests as _requests
        from requests_oauthlib import OAuth1 as _OAuth1
    except ImportError:
        print("Installing required packages...")
        os.system("pip3 install --break-system-packages requests requests-oauthlib")
        import requests as _requests
        from requests_oauthlib import OAuth1 as _OAuth1
    requests = _requests
    OAuth1 = _OAuth1

# Load .env
ENV_FILE = Path(__file__).parent.parent.parent / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

API_KEY = os.environ.get("X_API_KEY", "")
API_SECRET = os.environ.get("X_API_SECRET", "")
ACCESS_TOKEN = os.environ.get("X_ACCESS_TOKEN", "")
ACCESS_TOKEN_SECRET = os.environ.get("X_ACCESS_TOKEN_SECRET", "")
BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")

UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"
TWEET_URL = "https://api.twitter.com/2/tweets"


def get_auth():
    _require_requests()
    if not all([API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        print("Error: X API credentials not set in .env")
        print("Need: X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET")
        sys.exit(1)
    return OAuth1(API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET)


def _bearer_get(url):
    """GET an X API endpoint with bearer auth using stdlib only.

    Returns parsed JSON or exits on error. Used by search_tweets() and
    read_tweet() when X_BEARER_TOKEN is set, so a bearer-only environment
    doesn't need `requests` / `requests_oauthlib`.
    """
    import urllib.request, urllib.error
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {BEARER_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"Error {e.code}: {body[:400]}")
        sys.exit(1)


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


TWEET_FIELDS = "tweet.fields=created_at,public_metrics,author_id,text"
USER_FIELDS = "user.fields=username,name"


def search_tweets(query, auth, max_results=10):
    """Search recent tweets. Uses app-only bearer auth if X_BEARER_TOKEN is set
    (no dependency on `requests`); otherwise falls back to OAuth1 + requests."""
    import urllib.parse
    q = urllib.parse.quote(query)
    url = f"https://api.twitter.com/2/tweets/search/recent?query={q}&max_results={max_results}&{TWEET_FIELDS}"
    if BEARER_TOKEN:
        resp_json = _bearer_get(url)
    else:
        _require_requests()
        resp = requests.get(url, auth=auth)
        if resp.status_code != 200:
            print(f"Error {resp.status_code}: {resp.text}")
            return
        resp_json = resp.json()
    data = resp_json.get("data", [])
    if not data:
        print("No results found.")
        return
    for t in data:
        metrics = t.get("public_metrics", {})
        print(f"[{t['created_at'][:10]}] {t['text'][:120]}")
        print(f"  likes:{metrics.get('like_count',0)} rt:{metrics.get('retweet_count',0)} replies:{metrics.get('reply_count',0)} views:{metrics.get('impression_count',0)}")
        print(f"  https://x.com/i/status/{t['id']}")
        print()


def read_tweet(tweet_id, auth):
    """Read a single tweet with metrics. Uses bearer if available."""
    url = f"https://api.twitter.com/2/tweets/{tweet_id}?{TWEET_FIELDS}&expansions=author_id&{USER_FIELDS}"
    if BEARER_TOKEN:
        data = _bearer_get(url)
    else:
        _require_requests()
        resp = requests.get(url, auth=auth)
        if resp.status_code != 200:
            print(f"Error {resp.status_code}: {resp.text}")
            return
        data = resp.json()
    t = data.get("data", {})
    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
    author = users.get(t.get("author_id"), {})
    metrics = t.get("public_metrics", {})
    print(f"@{author.get('username', '?')} ({author.get('name', '?')}) — {t.get('created_at', '')}")
    print(t.get("text", ""))
    print(f"\nlikes:{metrics.get('like_count',0)} rt:{metrics.get('retweet_count',0)} replies:{metrics.get('reply_count',0)} quotes:{metrics.get('quote_count',0)} views:{metrics.get('impression_count',0)}")


def get_me(auth):
    """Get authenticated user ID."""
    resp = requests.get("https://api.twitter.com/2/users/me", auth=auth)
    resp.raise_for_status()
    return resp.json()["data"]["id"]


def get_mentions(auth, max_results=10):
    """Get recent mentions."""
    user_id = get_me(auth)
    resp = requests.get(
        f"https://api.twitter.com/2/users/{user_id}/mentions?max_results={max_results}&{TWEET_FIELDS}",
        auth=auth)
    if resp.status_code != 200:
        print(f"Error {resp.status_code}: {resp.text}")
        return
    data = resp.json().get("data", [])
    if not data:
        print("No recent mentions.")
        return
    for t in data:
        print(f"[{t['created_at'][:10]}] {t['text'][:140]}")
        print(f"  https://x.com/i/status/{t['id']}")
        print()


def get_timeline(auth, max_results=10):
    """Get your recent tweets."""
    user_id = get_me(auth)
    resp = requests.get(
        f"https://api.twitter.com/2/users/{user_id}/tweets?max_results={max_results}&{TWEET_FIELDS}",
        auth=auth)
    if resp.status_code != 200:
        print(f"Error {resp.status_code}: {resp.text}")
        return
    data = resp.json().get("data", [])
    if not data:
        print("No recent tweets.")
        return
    for t in data:
        metrics = t.get("public_metrics", {})
        print(f"[{t['created_at'][:10]}] {t['text'][:120]}")
        print(f"  likes:{metrics.get('like_count',0)} rt:{metrics.get('retweet_count',0)} views:{metrics.get('impression_count',0)}")
        print()


def main():
    parser = argparse.ArgumentParser(description="X (Twitter) CLI")
    sub = parser.add_subparsers(dest="command")

    p_post = sub.add_parser("post", help="Post a tweet")
    p_post.add_argument("text", help="Tweet text")
    p_post.add_argument("--media", help="Path to media file")
    p_post.add_argument("--reply-to", help="Tweet ID to reply to")

    p_search = sub.add_parser("search", help="Search recent tweets")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=10)

    p_read = sub.add_parser("read", help="Read a tweet")
    p_read.add_argument("tweet_id", help="Tweet ID")

    sub.add_parser("mentions", help="Recent mentions")
    sub.add_parser("timeline", help="Your recent tweets")

    p_eng = sub.add_parser("engagement", help="Check engagement on a tweet")
    p_eng.add_argument("tweet_id", help="Tweet ID")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Read-only commands that app-only bearer auth can handle. Skip OAuth1
    # setup (and its `requests`/`oauthlib` install) so X_BEARER_TOKEN-only
    # environments don't need pip.
    if args.command in ("search", "read") and BEARER_TOKEN:
        if args.command == "search":
            search_tweets(args.query, auth=None, max_results=args.limit)
        else:
            read_tweet(args.tweet_id, auth=None)
        return

    auth = get_auth()

    if args.command == "post":
        media_id = upload_media(args.media, auth) if args.media else None
        post_tweet(args.text, media_id=media_id, reply_to=args.reply_to, auth=auth)
    elif args.command == "search":
        search_tweets(args.query, auth, max_results=args.limit)
    elif args.command == "read":
        read_tweet(args.tweet_id, auth)
    elif args.command == "mentions":
        get_mentions(auth)
    elif args.command == "timeline":
        get_timeline(auth)
    elif args.command == "engagement":
        read_tweet(args.tweet_id, auth)  # same output, includes metrics


if __name__ == "__main__":
    main()
