#!/usr/bin/env bash
# Install shpotify (Spotify AppleScript wrapper) via Homebrew if missing.
# Idempotent: safe to re-run.
set -euo pipefail

if command -v spotify >/dev/null 2>&1; then
    echo "shpotify already installed: $(which spotify)"
    exit 0
fi

if ! command -v brew >/dev/null 2>&1; then
    echo "ERROR: Homebrew not installed. Install Homebrew first: https://brew.sh"
    exit 1
fi

echo "Installing shpotify via Homebrew..."
brew install shpotify

if command -v spotify >/dev/null 2>&1; then
    echo "shpotify installed: $(which spotify)"
    spotify --help | head -5
else
    echo "WARNING: brew install completed but 'spotify' command not on PATH. Check brew doctor."
    exit 1
fi
