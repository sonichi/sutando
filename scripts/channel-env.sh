#!/usr/bin/env bash
# Print the path of the channel env file that actually defines the gateway
# credential, so callers source the right file on any onboarding layout.
#
# Layouts differ per host: some write REMOTE_TASK_* into channels/<src>/.env,
# others into a sibling (e.g. relay-client.env) while .env holds Matrix creds.
# Resolving by CONTENT rather than filename is what makes one instruction
# correct on both. Exits 1 (printing nothing) when no candidate defines it.
set -u

src="${1:?usage: channel-env.sh <channel-source>}"
case "$src" in
  *[!a-z0-9._-]*|""|.|..) echo "channel-env: invalid source '$src'" >&2; exit 2 ;;
esac

base="${CLAUDE_CONFIG_DIR:-${CLAUDE_HOME:-$HOME/.claude}}"
dir="$base/channels/$src"
[ -d "$dir" ] || { echo "channel-env: no channel dir $dir" >&2; exit 1; }

# .env first so an existing correct layout keeps its current precedence; then
# any sibling *.env, sorted, so the pick is deterministic across hosts.
for f in "$dir/.env" "$dir"/*.env; do
  [ -f "$f" ] || continue
  if grep -qE '^[[:space:]]*(export[[:space:]]+)?(REMOTE_TASK_TOKEN|AG2_REMOTE_TOKEN)=' "$f" 2>/dev/null; then
    printf '%s\n' "$f"
    exit 0
  fi
done
echo "channel-env: no file under $dir defines REMOTE_TASK_TOKEN/AG2_REMOTE_TOKEN" >&2
exit 1
