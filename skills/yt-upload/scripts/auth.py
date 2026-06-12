#!/usr/bin/env python3
"""First-run OAuth flow for YouTube Data API v3.

Reads an OAuth client JSON (downloaded from GCP Console → APIs & Services →
Credentials → OAuth Client ID → Desktop App), opens a browser for the user
to grant `youtube.upload` scope, then writes a token file with the
refresh_token so subsequent runs are silent.

Usage:
    python3 auth.py <client-secret.json>
    # → opens browser → user authorizes → writes token to
    #   ~/.config/sutando/youtube-token.json

The token file holds: access_token, refresh_token, client_id, client_secret,
token_uri, scopes, expiry. upload.py reads it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_PATH = Path.home() / ".config" / "sutando" / "youtube-token.json"


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <client-secret.json>", file=sys.stderr)
        return 2

    client_secret_path = Path(sys.argv[1]).expanduser()
    if not client_secret_path.exists():
        print(f"ERROR: client secret JSON not found: {client_secret_path}", file=sys.stderr)
        return 1

    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
    # run_local_server opens the system browser, listens on localhost for the
    # OAuth redirect, and exchanges the code for tokens. Port 0 picks a random
    # free port — required because GCP's OAuth Client config doesn't pin one.
    creds = flow.run_local_server(port=0, open_browser=True)

    TOKEN_PATH.write_text(creds.to_json())
    # Tighten perms — the JSON contains a refresh_token that can act on the
    # user's YouTube channel until revoked from the GCP console.
    os.chmod(TOKEN_PATH, 0o600)
    print(f"OK: token cached at {TOKEN_PATH} (chmod 600)")
    print("Next: python3 scripts/upload.py <video.mp4> --title 'My title'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
