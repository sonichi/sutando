#!/bin/bash
# Thin Claude Code obs hook — forwards a raw hook payload (JSON on stdin) to the
# Sutando collector IFF an export endpoint is configured.
#
# Claude Code is just ONE source for the collector: this hook posts to the
# source-scoped route `/ingest/claude-code-hooks`, where the collector's
# Claude-Code normalizer maps it. Other sources (voice, filewatcher, bridges)
# post to their own `/ingest/<source>` on the SAME collector.
#
# start-cli.sh registers the hook only when an export endpoint is configured
# (no endpoint → no --settings → this script never runs). This stdin-drain +
# no-op guard stays as defense-in-depth: if the hook IS registered but the
# endpoint is unset at hook-time, it does nothing rather than erroring.
#
# Must stay THIN: it runs on every tool call, so it never blocks (curl is capped
# at 1s) and never fails the agent (errors swallowed, always exit 0). The
# collector receives the raw hook JSON and does the mapping.

# No endpoint configured → drain stdin and no-op (don't leave the pipe unread).
if [ -z "${SUTANDO_OBS_ENDPOINT:-}" ]; then
  cat > /dev/null 2>&1
  exit 0
fi

curl -sS -m 1 -X POST "${SUTANDO_OBS_ENDPOINT%/}/ingest/claude-code-hooks" \
  -H 'content-type: application/json' \
  --data-binary @- > /dev/null 2>&1 || true

exit 0
