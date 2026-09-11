#!/bin/bash
# The ONE portable mtime probe. GNU `stat -f` means --file-system and SUCCEEDS
# with prose, so a BSD-first `||` chain never falls through on Linux; validate
# the RESULT. Prints epoch seconds; returns 2 when the mtime is unreadable.
portable_mtime() {
  local mt
  mt="$(stat -c %Y "$1" 2>/dev/null || true)"
  case "$mt" in '' | *[!0-9]*) mt="$(stat -f %m "$1" 2>/dev/null || true)" ;; esac
  case "$mt" in '' | *[!0-9]*) return 2 ;; esac
  printf '%s\n' "$mt"
}
