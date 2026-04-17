#!/usr/bin/env bash
# test-x-search-bearer.sh — POC for X_BEAR_TOKEN-only search/read in x-post.py
#
# Before this feature: x-post.py search/read required the full OAuth1 quadruple
# (X_API_KEY + X_API_SECRET + X_ACCESS_TOKEN + X_ACCESS_TOKEN_SECRET) and the
# `requests` + `requests_oauthlib` pip deps. A bearer-only environment (e.g.
# the Mac Studio Sutando-Studio node where only X_BEAR_TOKEN is configured)
# couldn't run the skill at all.
#
# After: search/read route through a stdlib-urllib bearer path when
# X_BEAR_TOKEN is set, with zero new dependencies. OAuth1 is still used
# (and lazy-imported) for write commands (post, mentions, timeline).
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source .env 2>/dev/null || true

if [ -z "${X_BEAR_TOKEN:-}" ]; then
  echo "SKIP: X_BEAR_TOKEN not set in .env — this POC requires it"
  exit 0
fi

echo "Phase 1: bearer-only search returns tweets"
# moltbook has steady organic traffic, good signal of "search works"
OUT=$(python3 skills/x-twitter/x-post.py search "moltbook" --limit 10 2>&1)
if echo "$OUT" | grep -q "https://x.com/i/status/"; then
  COUNT=$(echo "$OUT" | grep -c "https://x.com/i/status/" || echo 0)
  echo "  ✓ got $COUNT tweets via bearer auth"
else
  echo "  ✗ search returned no tweets. Output:"
  echo "$OUT"
  exit 1
fi

echo ""
echo "Phase 2: no --break-system-packages pip install triggered"
# The old code-path autoinstalled `requests` + `requests_oauthlib`. The
# bearer path must use stdlib urllib only. If the module autoran pip, the
# output would contain "Collecting requests" / "Installing" lines.
if echo "$OUT" | grep -qE "Installing|Collecting requests"; then
  echo "  ✗ bearer path triggered pip install — regression of dep-free path"
  exit 1
fi
echo "  ✓ stdlib urllib path active (no pip autoinstall observed)"

echo ""
echo "PASS: X_BEAR_TOKEN alone is sufficient for x-post.py search."
