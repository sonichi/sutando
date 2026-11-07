#!/usr/bin/env bash
# The settle must happen while the chunk's REAL directories are on disk.
# The live-watcher does not follow symlinks, so a settle after every symlink is
# restored gives it nothing to read. A stub `sleep` inspects the filesystem at
# the moment of the settle and fails if it sees symlinks instead of directories.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SCRIPT="$REPO/skills/refresh-skill.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

BIN="$TMP/bin"
SKILLS="$TMP/skills"
TARGETS="$TMP/targets"
mkdir -p "$BIN" "$SKILLS" "$TARGETS"

# Stub sleep: record what each name is at the instant the settle begins.
cat > "$BIN/sleep" <<'EOF'
#!/usr/bin/env bash
out="${REFRESH_TEST_OBSERVE:?}"
for entry in "$SKILLS_DST"/*; do
    [ -e "$entry" ] || [ -L "$entry" ] || continue
    if [ -L "$entry" ]; then
        printf '%s symlink\n' "$(basename "$entry")"
    elif [ -d "$entry" ]; then
        printf '%s dir\n' "$(basename "$entry")"
    fi
done >> "$out"
exit 0
EOF
chmod +x "$BIN/sleep"

make_skill() {
    local name="$1"
    mkdir -p "$TARGETS/$name"
    printf '# %s\n' "$name" > "$TARGETS/$name/SKILL.md"
    ln -s "$TARGETS/$name" "$SKILLS/$name"
}

for name in alpha beta gamma; do make_skill "$name"; done

OBSERVE="$TMP/observed.txt"
: > "$OBSERVE"

env PATH="$BIN:$PATH" SKILLS_DST="$SKILLS" REFRESH_SKILL_SETTLE_S=1 \
    REFRESH_SKILL_JOBS=2 REFRESH_TEST_OBSERVE="$OBSERVE" \
    bash "$SCRIPT" --all >/dev/null 2>&1
rc=$?

fails=0
ok() { echo "  ok  $1"; }
fail() { echo "FAIL: $1"; fails=$((fails + 1)); }

[ "$rc" -eq 0 ] || fail "--all returned $rc"
[ -s "$OBSERVE" ] || fail "the settle never ran (stub sleep saw nothing)"

# At the settle, all three skills must be real directories, not symlinks.
for name in alpha beta gamma; do
    if grep -qx "$name dir" "$OBSERVE"; then
        ok "$name is a real directory during the settle"
    else
        fail "$name was not a real directory during the settle"
    fi
done

if grep -q " symlink$" "$OBSERVE"; then
    fail "a symlink was still in place during the settle"
else
    ok "no symlink was left in place during the settle"
fi

# And the script must still finish with clean symlinks.
for name in alpha beta gamma; do
    [ -L "$SKILLS/$name" ] || fail "$name was not restored to a symlink"
done

if [ "$fails" -ne 0 ]; then
    echo "refresh-skill-settle-visibility: $fails failure(s)"
    exit 1
fi
echo "refresh-skill-settle-visibility: all passed"
