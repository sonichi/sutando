#!/usr/bin/env bash
# test-x-search-bearer.sh — verify bug fix: x-post.py search/read work with
# only X_BEARER_TOKEN set (no OAuth1 credentials, no pip install).
#
# THE BUG (pre-fix): skills/x-twitter/x-post.py imported `requests` +
# `requests_oauthlib` at module load and auto-ran `pip3 install ...` on
# ImportError. On externally-managed Pythons (macOS Homebrew python3.14)
# pip refuses without --break-system-packages, so the import raises and
# every command exits before the argparser runs. On the Mac Studio node
# (only X_BEARER_TOKEN is configured), the advertised skill was unusable
# — even for read-only commands that don't technically need OAuth1.
#
# THE FIX (this PR): read-only commands (search, read) route through a
# stdlib-urllib bearer path when X_BEARER_TOKEN is set; `requests` + OAuth1
# are lazy-imported only when a write command (post, mentions, timeline)
# actually needs them.
#
# Before/after: reads the source at both commits via `git show` and
# asserts the bearer path is ABSENT at the buggy commit and PRESENT at
# the fix commit. Plus a live smoke test.
set -euo pipefail
cd "$(dirname "$0")/.."

BUGGY_COMMIT="${1:-d2d4458}"  # main before the fix (docs PR #393)
FIXED_COMMIT="${2:-$(git rev-parse HEAD 2>/dev/null || echo HEAD)}"  # current branch tip

check_bearer_path_in_source() {
  local commit="$1"; local label="$2"; local expect="$3"
  local src has_bearer_token has_bearer_get actual=fail
  src="$(git show ${commit}:skills/x-twitter/x-post.py 2>/dev/null)" \
    || { echo "  ✗ cannot read ${commit}:skills/x-twitter/x-post.py"; exit 1; }
  has_bearer_token=$(echo "$src" | grep -c 'X_BEARER_TOKEN' || true)
  has_bearer_get=$(echo "$src" | grep -c '_bearer_get' || true)
  [ "$has_bearer_token" -gt 0 ] && [ "$has_bearer_get" -gt 0 ] && actual=pass
  if [ "$actual" = "$expect" ]; then
    echo "  ✓ ${label} (${commit:0:7}): expected=${expect}, got=${actual}"
  else
    echo "  ✗ ${label} (${commit:0:7}): expected=${expect}, got=${actual}" >&2
    exit 1
  fi
}

echo "Phase 0: buggy commit should lack the bearer path (→ fail)"
check_bearer_path_in_source "$BUGGY_COMMIT" "buggy" "fail"

echo "Phase 1: fix commit should contain the bearer path (→ pass)"
check_bearer_path_in_source "$FIXED_COMMIT" "fixed" "pass"

# shellcheck disable=SC1091
source .env 2>/dev/null || true

if [ -z "${X_BEARER_TOKEN:-}" ]; then
  echo ""
  echo "SKIP Phase 2/3: X_BEARER_TOKEN not set — source-level checks already confirmed the fix is in place."
  echo "PASS"
  exit 0
fi

echo ""
echo "Phase 2: fix commit's search returns tweets via bearer (live smoke test)"
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
echo "Phase 3: no pip autoinstall triggered on the bearer path"
if echo "$OUT" | grep -qE "Installing|Collecting requests"; then
  echo "  ✗ bearer path triggered pip install — regression of dep-free path"
  exit 1
fi
echo "  ✓ stdlib urllib path active (no pip autoinstall observed)"

echo ""
echo "Phase 4: runtime before/after — extract both x-post.py versions and run"
# Extract buggy x-post.py to a tmp dir and run it with a scrubbed env
# (only X_BEARER_TOKEN passed through). This proves the buggy version fails
# for a bearer-only user even when requests is already installed on the
# system, because it unconditionally requires the OAuth1 quadruple.
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

git show "${BUGGY_COMMIT}":skills/x-twitter/x-post.py > "$TMPDIR/buggy.py"
git show "${FIXED_COMMIT}":skills/x-twitter/x-post.py > "$TMPDIR/fixed.py"

# Bearer-only env: strip OAuth1 keys, keep only X_BEARER_TOKEN.
run_bearer_only() {
  env -i \
    "HOME=$HOME" \
    "PATH=$PATH" \
    "X_BEARER_TOKEN=${X_BEARER_TOKEN}" \
    python3 "$1" search "moltbook" --limit 10 2>&1
}

echo "  → buggy (${BUGGY_COMMIT:0:7}) output:"
BUG_OUT=$(run_bearer_only "$TMPDIR/buggy.py" || true)
echo "$BUG_OUT" | sed 's/^/      /' | head -6
if echo "$BUG_OUT" | grep -qE "credentials not set|X_API_KEY|externally-managed"; then
  echo "  ✓ buggy fails with bearer-only env (as expected)"
else
  echo "  ✗ buggy unexpectedly succeeded — bug may already be gone upstream?"
  exit 1
fi

echo "  → fixed (${FIXED_COMMIT:0:7}) output:"
FIX_OUT=$(run_bearer_only "$TMPDIR/fixed.py" || true)
echo "$FIX_OUT" | sed 's/^/      /' | head -4
if echo "$FIX_OUT" | grep -q "https://x.com/i/status/"; then
  echo "  ✓ fixed succeeds with bearer-only env (returns tweets)"
else
  echo "  ✗ fixed did not return tweets with bearer-only env. Full output:"
  echo "$FIX_OUT"
  exit 1
fi

echo ""
echo "PASS: runtime before/after confirmed — buggy rejects, fixed accepts, bearer-only env sufficient."
