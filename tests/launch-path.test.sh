#!/bin/bash
# launch-path.sh finds <engine>/runtime/node/bin (and the older runtime/bin), appends only
# present dirs not already on PATH, keeps the caller's order, and survives spaces in paths.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fail=0
td="$(mktemp -d)"
trap 'rm -rf "$td"' EXIT

engine="$td/App Support/engine"
mkdir -p "$engine/sutando" "$engine/runtime/node/bin" "$td/home/.local/bin" "$td/extra dir"
engine="$(cd "$engine" && pwd)"

check() { if [ "$2" = "$3" ]; then echo "  ✓ $1"; else echo "  ✗ $1: got [$2] want [$3]"; fail=1; fi; }

out="$(HOME="$td/home" PATH="/usr/bin:/bin" /bin/bash -c '
  . "$1/src/agent/launch-path.sh"
  sutando_append_launch_path "$2" "$3" "$3"
  printf %s "$PATH"' _ "$REPO" "$engine/sutando" "$td/extra dir")"
check "appends node/bin, ~/.local/bin and extras once, after the caller's PATH" \
  "$out" "/usr/bin:/bin:$engine/runtime/node/bin:$td/home/.local/bin:$td/extra dir"

out="$(HOME="$td/home" PATH="$engine/runtime/node/bin:/usr/bin" /bin/bash -c '
  . "$1/src/agent/launch-path.sh"
  sutando_append_launch_path "$2"
  printf %s "$PATH"' _ "$REPO" "$engine/sutando")"
check "a dir already on PATH keeps its place" \
  "$out" "$engine/runtime/node/bin:/usr/bin:$td/home/.local/bin"

rm -rf "$engine/runtime/node"; mkdir -p "$engine/runtime/bin"
out="$(HOME="$td/home" PATH="/usr/bin" /bin/bash -c '
  . "$1/src/agent/launch-path.sh"
  sutando_append_launch_path "$2"
  printf %s "$PATH"' _ "$REPO" "$engine/sutando")"
check "the older runtime/bin layout is still found" \
  "$out" "/usr/bin:$engine/runtime/bin:$td/home/.local/bin"

out="$(HOME="$td/home" bash "$REPO/src/agent/launch-path.sh" "$engine/sutando" | head -1)"
check "executed with a repo, prints the bundled node dir first" "$out" "$engine/sutando/../runtime/node/bin"

exit "$fail"
