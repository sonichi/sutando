#!/usr/bin/env bash
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SCRIPT="$REPO/skills/refresh-skill.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

REAL_LN="$(command -v ln)"
BIN="$TMP/bin"
SKILLS="$TMP/skills"
TARGETS="$TMP/targets"
mkdir -p "$BIN" "$SKILLS" "$TARGETS"

cat > "$BIN/ln" <<'EOF'
#!/usr/bin/env bash
dest="${@: -1}"
if [ -n "${REFRESH_TEST_FAIL_LINK:-}" ] && [ "${dest##*/}" = "$REFRESH_TEST_FAIL_LINK" ]; then
    exit 1
fi
exec "$REFRESH_TEST_REAL_LN" "$@"
EOF
chmod +x "$BIN/ln"

make_skill() {
    local name="$1"
    rm -rf "${SKILLS:?}/$name" "${TARGETS:?}/$name"
    mkdir -p "$TARGETS/$name"
    printf '# %s\n' "$name" > "$TARGETS/$name/SKILL.md"
    "$REAL_LN" -s "$TARGETS/$name" "$SKILLS/$name"
}

run_refresh() {
    env \
        PATH="$BIN:$PATH" \
        SKILLS_DST="$SKILLS" \
        REFRESH_SKILL_SETTLE_S=0 \
        REFRESH_SKILL_JOBS=2 \
        REFRESH_TEST_REAL_LN="$REAL_LN" \
        REFRESH_TEST_FAIL_LINK="${REFRESH_TEST_FAIL_LINK:-}" \
        bash "$SCRIPT" "$@"
}

fails=0
ok() { echo "  ok  $1"; }
fail() { echo "FAIL: $1"; fails=$((fails + 1)); }

# A direct refresh must surface a restore failure through its exit status.
make_skill a-bad
REFRESH_TEST_FAIL_LINK=a-bad
run_refresh a-bad >/dev/null 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    ok "single-skill restore failure returns nonzero"
else
    fail "single-skill restore failure returned 0"
fi

# --all must retain a failed child status across a later successful chunk.
make_skill a-bad
make_skill b-good
make_skill c-good
REFRESH_TEST_FAIL_LINK=a-bad
run_refresh --all >/dev/null 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    ok "--all returns nonzero when one background refresh fails"
else
    fail "--all returned 0 after a background restore failure"
fi
if [ -L "$SKILLS/b-good" ] && [ -L "$SKILLS/c-good" ]; then
    ok "successful siblings still restore their symlinks"
else
    fail "a sibling refresh was stranded while collecting failures"
fi

# Clean control: every successful child still yields exit 0.
make_skill a-good
make_skill b-good
REFRESH_TEST_FAIL_LINK=""
run_refresh --all >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 0 ]; then
    ok "all-success batch returns 0"
else
    fail "all-success batch returned $rc"
fi

if [ "$fails" -ne 0 ]; then
    echo "refresh-skill-exit-status: $fails failure(s)"
    exit 1
fi

echo "refresh-skill-exit-status: all passed"
