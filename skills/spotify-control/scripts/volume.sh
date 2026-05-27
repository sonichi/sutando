#!/usr/bin/env bash
# Usage: bash volume.sh <0-100|up|down>
if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <0-100|up|down>" >&2
    exit 1
fi
spotify vol "$1"
