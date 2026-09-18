#!/bin/bash
# Generic, skill-agnostic: reads every installed skill's manifest.json "config"
# block (skills/MANIFEST.md's own convention, previously Node-only via
# inline-tools.ts) and prints "KEY=VALUE" for each key, one per line. Never
# names a skill; the caller decides what "already set" means and exports.

skill_manifest_config_pending() {
  local repo="$1" py="$2" m
  [ -n "$py" ] && [ -x "$py" ] || return 0
  for m in "$repo"/skills/*/manifest.json; do
    [ -f "$m" ] || continue
    "$py" -c '
import json, sys
try:
    cfg = json.load(open(sys.argv[1])).get("config") or {}
except Exception:
    cfg = {}
for k, v in cfg.items():
    if isinstance(k, str) and isinstance(v, str):
        print(f"{k}={v}")
' "$m"
  done
}
