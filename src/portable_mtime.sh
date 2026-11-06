#!/bin/bash
# Portable epoch mtime: GNU stat -f succeeds with prose, so validate the result.
# Return 2 when neither platform form yields a readable numeric timestamp.
portable_mtime() {
  local mt
  mt="$(stat -c %Y "$1" 2>/dev/null || true)"
  case "$mt" in '' | *[!0-9]*) mt="$(stat -f %m "$1" 2>/dev/null || true)" ;; esac
  case "$mt" in '' | *[!0-9]*) return 2 ;; esac
  printf '%s\n' "$mt"
}
