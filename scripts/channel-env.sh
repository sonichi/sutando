#!/usr/bin/env bash
# Print the path of the channel env file that actually defines the gateway
# credential, so callers source the right file on any onboarding layout.
#
# Layout resolution ($CLAUDE_CONFIG_DIR -> $CLAUDE_HOME -> ~/.claude) is edge
# mechanics and stays here; WHICH candidate is acceptable is policy and belongs
# to src/channel_env_resolve.py, which delegates containment and the non-empty
# token rule to their existing owners. The caller sources this path, so an
# unguarded pick would execute a credential file the sender contract refuses.
# Exits 1 (printing nothing on stdout) when no candidate qualifies.
set -u

src="${1:?usage: channel-env.sh <channel-source>}"
case "$src" in
  *[!a-z0-9._-]*|""|.|..) echo "channel-env: invalid source '$src'" >&2; exit 2 ;;
esac

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
base="$(bash "$repo/scripts/sutando-config.sh" claude-home-path)"

# A bare `python3` is the Xcode-CLT stub on a Mac without the tools, and it
# ignores the $SUTANDO_PY the desktop launcher sets.
. "$repo/scripts/python-binary.sh"
py="$(require_python "$repo" "resolve the channel env file")" || exit 1

exec "$py" "$repo/src/channel_env_resolve.py" "$base/channels" "$src"
