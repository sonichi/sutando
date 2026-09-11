#!/usr/bin/env bash
# Thin core-side entry: the capability lives in skills/model-switch/.
exec bash "$(cd "$(dirname "$0")/.." && pwd)/skills/model-switch/scripts/switch-model.sh" "$@"
