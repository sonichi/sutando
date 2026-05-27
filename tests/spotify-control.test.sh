#!/usr/bin/env bash
# Tests for skills/spotify-control — structure, execute bits, argument validation.
# All scripts are thin wrappers around `spotify` CLI (shpotify); tests don't
# call `spotify` directly — they verify structure and CLI argument guards.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SKILL="$REPO/skills/spotify-control"

pass=0
fail=0

ok()   { pass=$((pass + 1)); echo "  ✓ $1"; }
fail() { fail=$((fail + 1)); echo "  ✗ $1"; }

# -----------------------------------------------------------------------
# 1. SKILL.md exists and has required frontmatter
# -----------------------------------------------------------------------
if [ -f "$SKILL/SKILL.md" ]; then ok "SKILL.md exists"; else fail "SKILL.md exists"; fi
if grep -q "^name: spotify-control" "$SKILL/SKILL.md"; then ok "SKILL.md has name field"; else fail "SKILL.md has name field"; fi
if grep -q "^description:" "$SKILL/SKILL.md"; then ok "SKILL.md has description field"; else fail "SKILL.md has description field"; fi

# -----------------------------------------------------------------------
# 2. All expected scripts exist
# -----------------------------------------------------------------------
for script in play.sh pause.sh next.sh prev.sh now-playing.sh search-play.sh volume.sh; do
  if [ -f "$SKILL/scripts/$script" ]; then
    ok "scripts/$script exists"
  else
    fail "scripts/$script exists"
  fi
done

# -----------------------------------------------------------------------
# 3. All scripts are executable
# -----------------------------------------------------------------------
for script in play.sh pause.sh next.sh prev.sh now-playing.sh search-play.sh volume.sh; do
  if [ -x "$SKILL/scripts/$script" ]; then
    ok "scripts/$script is executable"
  else
    fail "scripts/$script is executable"
  fi
done
if [ -x "$SKILL/install.sh" ]; then ok "install.sh is executable"; else fail "install.sh is executable"; fi

# -----------------------------------------------------------------------
# 4. Shebang lines correct
# -----------------------------------------------------------------------
for script in play.sh pause.sh next.sh prev.sh now-playing.sh search-play.sh volume.sh; do
  head -1 "$SKILL/scripts/$script" | grep -q "bash" && ok "scripts/$script has bash shebang" || fail "scripts/$script has bash shebang"
done

# -----------------------------------------------------------------------
# 5. Wrapper scripts delegate to `spotify` CLI
# -----------------------------------------------------------------------
if grep -q "spotify play" "$SKILL/scripts/play.sh"; then ok "play.sh calls spotify play"; else fail "play.sh calls spotify play"; fi
if grep -q "spotify pause" "$SKILL/scripts/pause.sh"; then ok "pause.sh calls spotify pause"; else fail "pause.sh calls spotify pause"; fi
if grep -q "spotify next" "$SKILL/scripts/next.sh"; then ok "next.sh calls spotify next"; else fail "next.sh calls spotify next"; fi
if grep -q "spotify prev" "$SKILL/scripts/prev.sh"; then ok "prev.sh calls spotify prev"; else fail "prev.sh calls spotify prev"; fi
if grep -q "spotify status" "$SKILL/scripts/now-playing.sh"; then ok "now-playing.sh calls spotify status"; else fail "now-playing.sh calls spotify status"; fi

# -----------------------------------------------------------------------
# 6. search-play.sh validates argument — exits 1 with no args
# -----------------------------------------------------------------------
if PATH="/dev/null:$PATH" bash "$SKILL/scripts/search-play.sh" 2>/dev/null; then
  fail "search-play.sh exits non-zero with no args"
else
  ok "search-play.sh exits non-zero with no args"
fi

stderr_msg=$(PATH="/dev/null:$PATH" bash "$SKILL/scripts/search-play.sh" 2>&1 || true)
if echo "$stderr_msg" | grep -q "Usage:"; then ok "search-play.sh prints Usage on no args"; else fail "search-play.sh prints Usage on no args"; fi

# -----------------------------------------------------------------------
# 7. volume.sh validates argument — exits 1 with no args
# -----------------------------------------------------------------------
if PATH="/dev/null:$PATH" bash "$SKILL/scripts/volume.sh" 2>/dev/null; then
  fail "volume.sh exits non-zero with no args"
else
  ok "volume.sh exits non-zero with no args"
fi

stderr_vol=$(PATH="/dev/null:$PATH" bash "$SKILL/scripts/volume.sh" 2>&1 || true)
if echo "$stderr_vol" | grep -q "Usage:"; then ok "volume.sh prints Usage on no args"; else fail "volume.sh prints Usage on no args"; fi

# -----------------------------------------------------------------------
# 8. install.sh: shpotify already present → exits 0 (idempotent path)
# -----------------------------------------------------------------------
# Mock a fake `spotify` command on PATH and verify install.sh short-circuits.
MOCK_BIN="$(mktemp -d)"
printf '#!/usr/bin/env bash\necho "mock-spotify"\n' > "$MOCK_BIN/spotify"
chmod +x "$MOCK_BIN/spotify"
if PATH="$MOCK_BIN:$PATH" bash "$SKILL/install.sh" 2>/dev/null | grep -q "already installed"; then
  ok "install.sh short-circuits when spotify already on PATH"
else
  fail "install.sh short-circuits when spotify already on PATH"
fi
rm -rf "$MOCK_BIN"

# -----------------------------------------------------------------------
# 9. SKILL.md documents all 7 scripts in the table
# -----------------------------------------------------------------------
for cmd in play.sh pause.sh next.sh prev.sh now-playing.sh volume.sh search-play.sh; do
  if grep -q "$cmd" "$SKILL/SKILL.md"; then ok "SKILL.md documents $cmd"; else fail "SKILL.md documents $cmd"; fi
done

echo
if [ "$fail" -eq 0 ]; then
  echo "All $pass spotify-control tests passed."
else
  echo "$pass passed, $fail failed."
  exit 1
fi
