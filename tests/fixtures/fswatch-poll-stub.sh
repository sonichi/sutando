#!/bin/bash
# A stand-in fswatch for hosts without one: prints the absolute path of every entry that appears
# under the watched directories, polling every 0.2 s; FSWATCH_STUB_REPORT_DIR=1 prints the directory.
dirs=()
while [ $# -gt 0 ]; do
  case "$1" in
    -l|--event|-m|--monitor) shift 2 ;;
    -*) shift ;;
    *) dirs+=("$(cd "$1" 2>/dev/null && pwd -P || printf '%s' "$1")"); shift ;;
  esac
done
# Like fswatch, only what appears AFTER the subscription is an event.
seen=""
for d in "${dirs[@]}"; do
  for f in "$d"/* "$d"/.[!.]*; do
    [ -e "$f" ] && seen="$seen|$f|"
  done
done
while true; do
  for d in "${dirs[@]}"; do
    for f in "$d"/* "$d"/.[!.]*; do
      [ -e "$f" ] || continue
      case "$seen" in *"|$f|"*) continue ;; esac
      seen="$seen|$f|"
      if [ "${FSWATCH_STUB_REPORT_DIR:-}" = 1 ]; then printf '%s\n' "$d" || exit 0
      else printf '%s\n' "$f" || exit 0; fi
    done
  done
  sleep 0.2
done
