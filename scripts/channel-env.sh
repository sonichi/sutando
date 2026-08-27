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

base="${CLAUDE_CONFIG_DIR:-${CLAUDE_HOME:-$HOME/.claude}}"
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

exec python3 "$repo/src/channel_env_resolve.py" "$base/channels" "$src"
