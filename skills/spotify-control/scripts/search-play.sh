#!/usr/bin/env bash
# Usage: bash search-play.sh "<artist/song/album query>"
if [ "$#" -lt 1 ]; then
    echo "Usage: $0 \"<query>\"" >&2
    exit 1
fi
spotify play "$1"
