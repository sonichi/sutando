#!/bin/bash
# launch-path.sh finds <engine>/runtime/node/bin (and the older runtime/bin), appends only
# present dirs not already on PATH, keeps the caller's order, and survives spaces in paths.
set -u
# The host desktop root would win over each fixture's own layout.
unset SUTANDO_APP_SUPPORT
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
check "appends overrides once, then node/bin and ~/.local/bin, after the caller's PATH" \
  "$out" "/usr/bin:/bin:$td/extra dir:$engine/runtime/node/bin:$td/home/.local/bin"

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

out="$(HOME="$td/home" PATH="/usr/bin" /bin/bash -c '
  . "$1/src/agent/launch-path.sh"
  sutando_append_launch_path "$2"
  sutando_append_launch_path "$2" "$3"
  printf %s "$PATH"' _ "$REPO" "$engine/sutando" "$td/extra dir")"
check "a second call puts its override ahead of the defaults the first call appended" \
  "$out" "/usr/bin:$td/extra dir:$engine/runtime/bin:$td/home/.local/bin"

# A worker started from a core keeps the core's CODEX_NODE_BIN / CODEX_INSTALL_BIN dirs,
# ahead of the defaults, though the core's call left its marker in the environment.
mkdir -p "$td/home/.npm-global/bin" "$td/node override" "$td/codex override"
out="$(HOME="$td/home" PATH="/usr/bin:/bin" /bin/bash -c '
  . "$1/src/agent/launch-path.sh"
  sutando_append_launch_path "$1" "$2" "$3"
  SUTANDO_INSTANCE_ID=w1 SUTANDO_TMUX_SESSION=w1 SUTANDO_TASKS_DIR="$4" SUTANDO_WORKSPACE_DIR="$4" \
  SUTANDO_RESULTS_DIR="$4" SUTANDO_INBOX_RESOLVER=x SUTANDO_POOL_DELIVERY_SCRIPT=x \
    /bin/bash "$1/skills/worker-pool/scripts/launch-codex-worker-session.sh" --print-env' \
  _ "$REPO" "$td/node override" "$td/codex override" "$td/ws" | sed -n 's/^PATH=//p')"
check "a worker under a core keeps both overrides ahead of the defaults" "$out" \
  "/usr/bin:/bin:$td/node override:$td/codex override:$td/home/.npm-global/bin:$td/home/.local/bin"

mkdir -p "$td/app node"
out="$(HOME="$td/home" PATH="/usr/bin" /bin/bash -c '
  . "$1/src/agent/launch-path.sh"
  sutando_prepend_default_dir "$3"
  sutando_append_launch_path "$2" "$4"
  printf %s "$PATH"' _ "$REPO" "$engine/sutando" "$td/app node" "$td/extra dir")"
check "a dir startup prepends as a default goes behind the overrides, not away" \
  "$out" "/usr/bin:$td/extra dir:$engine/runtime/bin:$td/home/.local/bin:$td/app node"

# A desktop install ships claude only in <app support>/cli/claude/bin: a launchd PATH must reach it.
mkdir -p "$td/App Support/cli/claude/bin" "$td/App Support/cli/codex/bin"
app="$(cd "$td/App Support" && pwd)"
out="$(HOME="$td/home" PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin" /bin/bash -c '
  . "$1/src/agent/launch-path.sh"
  sutando_append_launch_path "$2"
  printf %s "$PATH"' _ "$REPO" "$engine/sutando")"
check "the desktop app's cli/claude and cli/codex dirs are appended, ahead of ~/.local/bin" \
  "$out" "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$engine/runtime/bin:$app/cli/claude/bin:$app/cli/codex/bin:$td/home/.local/bin"

# A foreign checkout reaches the desktop CLIs through SUTANDO_APP_SUPPORT alone.
mkdir -p "$td/foreign/sutando"
out="$(HOME="$td/home" SUTANDO_APP_SUPPORT="$app" PATH="/usr/bin:/bin" /bin/bash -c '
  . "$1/src/agent/launch-path.sh"
  sutando_append_launch_path "$2"
  printf %s "$PATH"' _ "$REPO" "$td/foreign/sutando")"
check "a foreign checkout with SUTANDO_APP_SUPPORT gets that root's cli dirs" \
  "$out" "/usr/bin:/bin:$app/cli/claude/bin:$app/cli/codex/bin:$td/home/.local/bin"

exit "$fail"
